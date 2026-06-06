"""
SYCS Stream Server — LL-HLS HTTP配信サーバー

LL-HLS (Low Latency HLS) の仕様 (Apple HLS Authoring Spec) に従い、
playlist ブロッキングリクエスト (_HLS_msn / _HLS_part) をサポートする。
これにより視聴側の遅延を最小化できる。
"""

import asyncio
import os
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
import uvicorn

# ─── 設定 ────────────────────────────────────────────────────────────────────

HLS_DIR = Path(os.environ.get("HLS_DIR", "/hls"))

MEDIA_TYPES: dict[str, str] = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m4s":  "video/iso.segment",
    ".mp4":  "video/mp4",
    ".ts":   "video/MP2T",
}

NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
}

# ─── LL-HLS ブロッキング管理 ─────────────────────────────────────────────────
#
# クライアントが "?_HLS_msn=N&_HLS_part=P" 付きで playlist を要求してきた場合、
# そのセグメント/パーツが playlist に現れるまで応答を保留する。
# watchfiles でファイル変更を監視し、変更検知時に待機中の asyncio.Event を解放する。

_waiters: dict[str, list[asyncio.Event]] = defaultdict(list)
_waiters_lock = asyncio.Lock()


async def _notify_waiters(rel_path: str) -> None:
    async with _waiters_lock:
        for event in _waiters.pop(rel_path, []):
            event.set()


async def _watch_hls() -> None:
    try:
        from watchfiles import awatch, Change
        async for changes in awatch(HLS_DIR):
            for change_type, change_path in changes:
                if change_type in (Change.modified, Change.added):
                    rel = str(Path(change_path).relative_to(HLS_DIR))
                    await _notify_waiters(rel)
    except ImportError:
        pass  # watchfiles 未インストール時はブロッキング無効
    except Exception:
        pass


# ─── アプリ起動/停止 ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(_watch_hls())
    yield
    task.cancel()


app = FastAPI(title="SYCS Stream Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# ─── ルート ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/streams")
async def list_streams():
    """配信中ストリームの一覧を返す。ステータスページから定期ポーリングされる。"""
    streams = []
    live_dir = HLS_DIR / "live"
    if live_dir.exists():
        for sd in sorted(live_dir.iterdir()):
            if sd.is_dir():
                active = (sd / "index.m3u8").exists()
                streams.append({
                    "key": sd.name,
                    "active": active,
                    "hls_url": f"/hls/live/{sd.name}/master.m3u8",
                })
    return {"streams": streams}


@app.get("/", response_class=HTMLResponse)
async def status_page():
    return _STATUS_HTML


@app.get("/hls/{path:path}")
async def serve_hls(
    path: str,
    request: Request,
    _HLS_msn: Optional[int] = Query(default=None),
    _HLS_part: Optional[int] = Query(default=None),
):
    """
    HLS ファイル配信エンドポイント。
    playlist リクエストに _HLS_msn / _HLS_part が付いている場合は
    LL-HLS ブロッキングリクエストとして処理する。
    """
    file_path = HLS_DIR / path

    if path.endswith(".m3u8") and _HLS_msn is not None:
        await _block_until_ready(path, file_path, _HLS_msn, _HLS_part, timeout=5.0)

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Not found")

    # playlist はパッチ処理してから返す (タイムスタンプ注入・HOLD-BACK 調整)
    if path.endswith(".m3u8"):
        try:
            async with aiofiles.open(str(file_path), encoding="utf-8") as f:
                content = await f.read()
        except OSError:
            raise HTTPException(status_code=404, detail="Not found")
        return Response(
            content=_patch_playlist(content).encode("utf-8"),
            media_type="application/vnd.apple.mpegurl",
            headers=NO_CACHE_HEADERS,
        )

    media_type = MEDIA_TYPES.get(file_path.suffix, "application/octet-stream")
    return FileResponse(str(file_path), media_type=media_type, headers=NO_CACHE_HEADERS)


# ─── LL-HLS ブロッキング実装 ─────────────────────────────────────────────────

async def _block_until_ready(
    rel_path: str,
    file_path: Path,
    msn: int,
    part: Optional[int],
    timeout: float,
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout

    while True:
        if file_path.exists():
            try:
                async with aiofiles.open(file_path, encoding="utf-8") as f:
                    content = await f.read()
                if _playlist_satisfies(content, msn, part):
                    return
            except OSError:
                pass

        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return  # タイムアウト: あるものを返す

        event = asyncio.Event()
        async with _waiters_lock:
            _waiters[rel_path].append(event)

        try:
            await asyncio.wait_for(event.wait(), timeout=min(remaining, 0.5))
        except asyncio.TimeoutError:
            async with _waiters_lock:
                try:
                    _waiters[rel_path].remove(event)
                except ValueError:
                    pass


def _playlist_satisfies(content: str, msn: int, part: Optional[int]) -> bool:
    """playlist が要求された MSN (およびパート) を含んでいるか判定する。"""
    m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", content)
    if not m:
        return False
    base_seq = int(m.group(1))
    seg_count = content.count("#EXTINF:")
    max_msn = base_seq + seg_count - 1

    if msn > max_msn:
        return False
    if part is None:
        return True

    # 最終セグメントの特定パーツが存在するか
    # FFmpeg の命名規則: seg{N:05d}.{part}.m4s
    segment_idx = msn - base_seq
    part_pattern = rf"seg\d{{5}}\.{part}\.m4s"
    part_uris = re.findall(r'#EXT-X-PART:[^,\n]*,URI="([^"]+)"', content)
    target_seg_parts = [
        u for u in part_uris
        if re.search(rf"seg{msn:05d}\.", u)
    ]
    return len(target_seg_parts) > part


# ─── Playlist パッチ処理 ────────────────────────────────────────────────────────
#
# FFmpeg が -hls_flags program_date_time を付けて生成した場合は EXT-X-PROGRAM-DATE-TIME
# が既に含まれるのでそのまま通す。含まれない場合はここで推定値を注入する。
# また EXT-X-SERVER-CONTROL の HOLD-BACK を縮小し、プレイヤーのライブエッジ追従を促す。

def _patch_playlist(content: str) -> str:
    """
    media playlist (index.m3u8) を受け取り、以下を行う:
    1. EXT-X-PROGRAM-DATE-TIME が無ければ現在時刻から推定して注入
       → プレイヤーが「自分がライブエッジから何秒遅れているか」を把握できる
    2. EXT-X-SERVER-CONTROL の HOLD-BACK を削減
       → プレイヤーがライブエッジにより近い位置で再生するよう誘導する
    master.m3u8 には #EXTINF が無いため自動的に何もしない。
    """
    if not content.strip() or "#EXTM3U" not in content:
        return content

    has_pdt = "EXT-X-PROGRAM-DATE-TIME" in content
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    pdt_inserted = False

    for line in lines:
        tag = line.rstrip("\r\n")

        # (2) HOLD-BACK を削減してライブエッジ追従を改善
        if tag.startswith("#EXT-X-SERVER-CONTROL:"):
            line = _shrink_hold_back(tag) + "\n"

        # (1) 最初の #EXTINF 直前に EXT-X-PROGRAM-DATE-TIME を注入
        if not has_pdt and not pdt_inserted and tag.startswith("#EXTINF:"):
            pdt = _estimate_first_segment_pdt(content)
            if pdt:
                out.append(f"#EXT-X-PROGRAM-DATE-TIME:{pdt}\n")
            pdt_inserted = True

        out.append(line)

    return "".join(out)


def _shrink_hold_back(server_control_line: str) -> str:
    """
    HOLD-BACK の値を現在値の 60% (最低 0.75s) に削減する。
    HOLD-BACK はプレイヤーがライブエッジから何秒遅れて再生するかの目安。
    小さくするほど低遅延だが、ネットワーク揺らぎに対する耐性が下がる。
    """
    def shrink(m: re.Match) -> str:
        val = float(m.group(1))
        return f"HOLD-BACK={max(0.75, val * 0.6):.3f}"

    return re.sub(r"\bHOLD-BACK=([\d.]+)", shrink, server_control_line)


def _estimate_first_segment_pdt(content: str) -> str | None:
    """
    playlist 内の #EXTINF 時間を合計し、
    「現在時刻 - 合計時間」を先頭セグメントの開始時刻として返す。
    FFmpeg が -use_wallclock_as_timestamps 1 を使っている場合、
    この推定はかなり正確になる。
    """
    durations = [float(m.group(1)) for m in re.finditer(r"#EXTINF:([\d.]+)", content)]
    if not durations:
        return None
    total = sum(durations)
    first = datetime.now(timezone.utc) - timedelta(seconds=total)
    return first.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ─── ステータスページ HTML ────────────────────────────────────────────────────

_STATUS_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SYCS Stream Server</title>
<style>
  body{font-family:monospace;background:#111;color:#ddd;padding:20px;margin:0}
  h1{color:#4fc3f7;margin-bottom:4px}
  small{color:#888}
  .card{background:#1e1e1e;border:1px solid #333;border-radius:6px;padding:16px;margin:12px 0}
  .live{color:#66bb6a;font-weight:bold}
  .offline{color:#ef5350}
  .url{color:#81c784;word-break:break-all;font-size:0.9em}
  .label{color:#888;font-size:0.8em}
  #obs-hint{background:#1a2733;border:1px solid #1e4a6e;border-radius:6px;padding:14px;margin:12px 0}
  #obs-hint code{color:#4fc3f7}
</style>
</head>
<body>
<h1>SYCS Stream Server</h1>
<small>Ultra Low Latency LL-HLS</small>

<div id="obs-hint">
  <b>OBS 設定</b><br>
  設定 → 配信 → カスタム ... → サーバー: <code>rtmp://&lt;このサーバーIP&gt;:1935/live</code><br>
  ストリームキー: <code>任意の文字列 (例: stream)</code>
</div>

<div id="streams"><p style="color:#888">配信待機中...</p></div>

<script>
const base = location.origin;
async function refresh() {
  try {
    const r = await fetch('/api/streams');
    const { streams } = await r.json();
    const el = document.getElementById('streams');
    if (!streams.length) {
      el.innerHTML = '<p style="color:#888">配信ストリームがありません。OBS から配信を開始してください。</p>';
      return;
    }
    el.innerHTML = streams.map(s => `
      <div class="card">
        <span class="label">STREAM KEY</span> <b>${s.key}</b>
        &nbsp;—&nbsp;
        <span class="${s.active ? 'live' : 'offline'}">${s.active ? '● LIVE' : '○ OFFLINE'}</span>
        <p class="label" style="margin:8px 0 2px">VRChat / ブラウザ URL:</p>
        <div class="url">${base}${s.hls_url}</div>
      </div>
    `).join('');
  } catch(e) {}
}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""


# ─── 起動 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )
