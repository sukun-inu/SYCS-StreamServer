"""
SYCS Stream Server — ABR fmp4 LL-HLS 配信ポータル

出力: /hls/live/<key>/master.m3u8 (ABR マスター)
  high/ … 元解像度 VBR、LL-HLS 対応プレイヤーはパーツ単位取得
  low/  … 720p  VBR、LL-HLS 非対応プレイヤー(Android)はセグメント単位フォールバック

playlist サーブ時の自動パッチ:
  - EXT-X-PROGRAM-DATE-TIME 注入 (遅延蓄積防止)
  - HOLD-BACK 縮小             (ライブエッジ追従促進)
"""

import asyncio
import os
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiofiles
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response

# ─── 設定 ────────────────────────────────────────────────────────────────────

HLS_DIR        = Path(os.environ.get("HLS_DIR", "/hls"))
SITE_BASE_URL  = os.environ.get("SITE_BASE_URL", "")
NGROK_RTMP_API = os.environ.get("NGROK_RTMP_API", "http://localhost:4040")
_NGROK_TTL     = float(os.environ.get("NGROK_CACHE_TTL", "30"))

MEDIA_TYPES: dict[str, str] = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m4s":  "video/iso.segment",
    ".mp4":  "video/mp4",
    ".ts":   "video/MP2T",
}

NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma":        "no-cache",
    "Expires":       "0",
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Headers": "*",
}

# ─── ngrok URL キャッシュ ─────────────────────────────────────────────────────

_ngrok_cache:    dict[str, str] = {}
_ngrok_cache_ts: float          = 0.0


async def get_ngrok_urls() -> dict[str, str]:
    global _ngrok_cache, _ngrok_cache_ts
    now = asyncio.get_running_loop().time()
    if now - _ngrok_cache_ts < _NGROK_TTL and _ngrok_cache:
        return _ngrok_cache

    async def _fetch(api_url: str) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{api_url}/api/tunnels")
                return r.json().get("tunnels", [])
        except Exception:
            return []

    result: dict[str, str] = {"site": SITE_BASE_URL, "rtmp": ""}
    for t in await _fetch(NGROK_RTMP_API):
        pub = t.get("public_url", "")
        if pub.startswith("tcp://"):
            result["rtmp"] = "rtmp://" + pub[6:] + "/live"
            break

    _ngrok_cache    = result
    _ngrok_cache_ts = now
    return result


# ─── LL-HLS ブロッキング ──────────────────────────────────────────────────────

_waiters:      dict[str, list[asyncio.Event]] = defaultdict(list)
_waiters_lock: asyncio.Lock                   = asyncio.Lock()


async def _notify_waiters(rel_path: str) -> None:
    async with _waiters_lock:
        for ev in _waiters.pop(rel_path, []):
            ev.set()


async def _watch_hls() -> None:
    try:
        from watchfiles import awatch, Change
        async for changes in awatch(HLS_DIR):
            for change_type, change_path in changes:
                if change_type in (Change.modified, Change.added):
                    rel = str(Path(change_path).relative_to(HLS_DIR))
                    await _notify_waiters(rel)
    except ImportError:
        pass
    except Exception:
        pass


async def _block_until_ready(
    rel_path:  str,
    file_path: Path,
    msn:       int,
    part:      int | None,
    timeout:   float,
) -> None:
    loop     = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if file_path.exists():
            try:
                async with aiofiles.open(file_path, encoding="utf-8") as f:
                    content = await f.read()
                if _playlist_satisfies(content, msn, part):
                    return
            except OSError:
                pass

        remaining = deadline - loop.time()
        if remaining <= 0:
            return

        ev = asyncio.Event()
        async with _waiters_lock:
            _waiters[rel_path].append(ev)
        try:
            await asyncio.wait_for(ev.wait(), timeout=min(remaining, 0.5))
        except asyncio.TimeoutError:
            async with _waiters_lock:
                try:
                    _waiters[rel_path].remove(ev)
                except ValueError:
                    pass


def _playlist_satisfies(content: str, msn: int, part: int | None) -> bool:
    m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", content)
    if not m:
        return False
    base    = int(m.group(1))
    max_msn = base + content.count("#EXTINF:") - 1
    if msn > max_msn:
        return False
    if part is None:
        return True
    part_uris = re.findall(r'#EXT-X-PART:[^,\n]*,URI="([^"]+)"', content)
    return len([u for u in part_uris if re.search(rf"seg{msn:05d}\.", u)]) > part


# ─── Playlist パッチ処理 ──────────────────────────────────────────────────────

def _patch_playlist(content: str) -> str:
    """EXT-X-PROGRAM-DATE-TIME 注入と HOLD-BACK 縮小を適用する。"""
    if not content.strip() or "#EXTM3U" not in content:
        return content

    has_pdt     = "EXT-X-PROGRAM-DATE-TIME" in content
    out:        list[str] = []
    pdt_inserted = False

    for line in content.splitlines(keepends=True):
        tag = line.rstrip("\r\n")

        if tag.startswith("#EXT-X-SERVER-CONTROL:"):
            line = _shrink_hold_back(tag) + "\n"

        if not has_pdt and not pdt_inserted and tag.startswith("#EXTINF:"):
            pdt = _estimate_first_segment_pdt(content)
            if pdt:
                out.append(f"#EXT-X-PROGRAM-DATE-TIME:{pdt}\n")
            pdt_inserted = True

        out.append(line)

    return "".join(out)


def _shrink_hold_back(line: str) -> str:
    def _shrink(m: re.Match) -> str:
        return f"HOLD-BACK={max(0.75, float(m.group(1)) * 0.6):.3f}"
    return re.sub(r"\bHOLD-BACK=([\d.]+)", _shrink, line)


def _estimate_first_segment_pdt(content: str) -> str | None:
    durations = [float(m.group(1)) for m in re.finditer(r"#EXTINF:([\d.]+)", content)]
    if not durations:
        return None
    first = datetime.now(timezone.utc) - timedelta(seconds=sum(durations))
    return first.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ─── アプリ ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI):
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
    """ABR fmp4 LL-HLS ストリーム一覧。PC / Android 共通の master.m3u8 URL を返す。"""
    streams = []
    live_dir = HLS_DIR / "live"
    if live_dir.exists():
        for sd in sorted(live_dir.iterdir()):
            if not sd.is_dir():
                continue
            streams.append({
                "key":     sd.name,
                "active":  (sd / "high" / "index.m3u8").exists(),
                "hls_url": f"/hls/live/{sd.name}/master.m3u8",
            })
    return {"streams": streams}


@app.get("/api/ngrok")
async def ngrok_info():
    """ngrok RTMP URL と Cloudflare サイト URL を返す。ngrok 未起動時は rtmp が空文字。"""
    return await get_ngrok_urls()


@app.get("/hls/{path:path}")
async def serve_hls(
    path:      str,
    _HLS_msn:  int | None = Query(default=None),
    _HLS_part: int | None = Query(default=None),
):
    """HLS ファイル配信。LL-HLS ブロッキングリクエストと playlist パッチ処理に対応。"""
    file_path = HLS_DIR / path

    if path.endswith(".m3u8") and _HLS_msn is not None:
        await _block_until_ready(path, file_path, _HLS_msn, _HLS_part, timeout=5.0)

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Not found")

    if path.endswith(".m3u8"):
        try:
            async with aiofiles.open(file_path, encoding="utf-8") as f:
                content = await f.read()
        except OSError:
            raise HTTPException(status_code=404, detail="Not found")
        return Response(
            content=_patch_playlist(content).encode("utf-8"),
            media_type="application/vnd.apple.mpegurl",
            headers=NO_CACHE_HEADERS,
        )

    media_type = MEDIA_TYPES.get(file_path.suffix, "application/octet-stream")
    return FileResponse(file_path, media_type=media_type, headers=NO_CACHE_HEADERS)


# ─── ポータル HTML ────────────────────────────────────────────────────────────

_PORTAL_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SYCS Stream Server</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}
header{background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;align-items:center;gap:12px}
header h1{font-size:1.15rem;font-weight:700;color:#58a6ff}
.badge{background:#1f6feb;color:#fff;font-size:.68rem;padding:2px 8px;border-radius:10px;font-weight:600;letter-spacing:.04em}
main{max-width:1120px;margin:0 auto;padding:16px;display:grid;gap:14px}
@media(min-width:800px){main{grid-template-columns:1fr 340px}}

/* Player */
#player-wrap{background:#000;border-radius:8px;overflow:hidden;aspect-ratio:16/9;position:relative;grid-row:1}
#player-wrap video{width:100%;height:100%;display:block}
.placeholder{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;color:#484f58;font-size:.9rem}
.placeholder svg{width:52px;opacity:.25}

/* Card */
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;grid-row:span 1}
.card h2{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:#8b949e;margin-bottom:12px;font-weight:600}
@media(min-width:800px){.card:nth-of-type(1){grid-column:2;grid-row:1/3}}

/* Stream list */
.stream-item{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid #21262d}
.stream-item:last-child{border-bottom:none}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot.live{background:#3fb950;box-shadow:0 0 5px #3fb950}
.dot.off{background:#484f58}
.stream-key{flex:1;font-size:.9rem;font-weight:600;color:#e6edf3;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.platform-badges{display:flex;gap:4px;flex-shrink:0}
.pb{font-size:.65rem;padding:2px 6px;border-radius:4px;font-weight:600}
.pb.live{background:#1a4731;color:#56d364}
.pb.off{background:#21262d;color:#6e7681}
.play-btn{background:#1f6feb;border:none;color:#fff;border-radius:6px;padding:4px 10px;font-size:.75rem;cursor:pointer;white-space:nowrap;flex-shrink:0}
.play-btn:hover{background:#388bfd}

/* URL rows */
.url-section{display:flex;flex-direction:column;gap:7px;margin-top:4px}
.url-row{display:flex;align-items:center;gap:8px}
.url-label{font-size:.7rem;color:#8b949e;width:86px;flex-shrink:0;line-height:1.2}
.url-input-wrap{flex:1;display:flex;gap:5px;min-width:0}
.url-input-wrap input{flex:1;min-width:0;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:5px 9px;color:#58a6ff;font-family:monospace;font-size:.75rem;cursor:text}
.url-input-wrap input.empty{color:#484f58}
.copy-btn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:4px 9px;font-size:.72rem;cursor:pointer;white-space:nowrap;flex-shrink:0}
.copy-btn:hover{background:#30363d}
.copy-btn.ok{color:#3fb950;border-color:#3fb950}

/* Alert */
.alert{background:#1c2128;border:1px solid #9e6a03;border-radius:6px;padding:9px 12px;font-size:.75rem;color:#d29922;margin-bottom:10px;display:none}
.alert.show{display:block}

/* Info card */
#info-card{grid-column:1}
@media(min-width:800px){#info-card{grid-column:1}}
</style>
</head>
<body>
<header>
  <h1>SYCS Stream Server</h1>
  <span class="badge">LL-HLS</span>
</header>

<main>
  <!-- ストリーム一覧 (右カラム) -->
  <div class="card" id="stream-card">
    <h2>配信一覧</h2>
    <div id="stream-list"><p style="color:#484f58;font-size:.85rem">配信なし</p></div>
  </div>

  <!-- プレイヤー (左上) -->
  <div id="player-wrap">
    <video id="video" controls playsinline></video>
    <div class="placeholder" id="placeholder">
      <svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
      <span>配信待機中</span>
    </div>
  </div>

  <!-- 接続情報 (左下) -->
  <div class="card" id="info-card">
    <h2>接続情報</h2>
    <div class="alert" id="ngrok-alert">
      ⚠ ngrok 無料プランでは RTMP URL が再起動ごとに変わります。OBS の配信先 URL は ngrok-rtmp 再起動後にポータルで確認してください。
    </div>
    <div class="url-section" id="url-section">
      <div class="url-row">
        <span class="url-label">RTMP<br>(OBS 配信先)</span>
        <div class="url-input-wrap">
          <input id="u-rtmp" readonly value="読み込み中...">
          <button class="copy-btn" onclick="cp('u-rtmp',this)">コピー</button>
        </div>
      </div>
      <div class="url-row">
        <span class="url-label">VRChat URL<br>(PC / Android)</span>
        <div class="url-input-wrap">
          <input id="u-vrc" readonly class="empty" value="配信待機中">
          <button class="copy-btn" onclick="cp('u-vrc',this)">コピー</button>
        </div>
      </div>
      <div class="url-row">
        <span class="url-label">ブラウザ再生</span>
        <div class="url-input-wrap">
          <input id="u-browser" readonly class="empty" value="配信待機中">
          <button class="copy-btn" onclick="cp('u-browser',this)">コピー</button>
        </div>
      </div>
    </div>
  </div>
</main>

<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<script>
let hlsObj = null;
let playingKey = null;

function cp(id, btn) {
  const val = document.getElementById(id)?.value;
  if (!val || val.startsWith('配信') || val.startsWith('読み込み')) return;
  navigator.clipboard.writeText(val).then(() => {
    btn.textContent = '✓ コピー済';
    btn.classList.add('ok');
    setTimeout(() => { btn.textContent = 'コピー'; btn.classList.remove('ok'); }, 2000);
  });
}

function setInput(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  el.value = val;
  el.classList.toggle('empty', !val);
}

function playStream(key, url) {
  const video = document.getElementById('video');
  const ph    = document.getElementById('placeholder');
  ph.style.display = 'none';
  if (hlsObj) { hlsObj.destroy(); hlsObj = null; }
  playingKey = key;

  if (Hls.isSupported()) {
    hlsObj = new Hls({
      lowLatencyMode: true,
      backBufferLength: 2,
      maxBufferLength: 4,
      liveSyncDurationCount: 2,
      liveMaxLatencyDurationCount: 5,
      liveDurationInfinity: true,
    });
    hlsObj.loadSource(url);
    hlsObj.attachMedia(video);
    hlsObj.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
    hlsObj.on(Hls.Events.ERROR, (_, d) => {
      if (d.fatal) { ph.style.display = 'flex'; playingKey = null; }
    });
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = url;
    video.play().catch(() => {});
  }
}

async function refresh() {
  const [sRes, nRes] = await Promise.allSettled([
    fetch('/api/streams').then(r => r.json()),
    fetch('/api/ngrok').then(r => r.json()),
  ]);

  const localBase = location.origin;
  let extBase = localBase;
  let rtmpUrl = 'rtmp://[サーバーIP]:1935/live  ← IPに置換してください';

  if (nRes.status === 'fulfilled') {
    const n = nRes.value;
    if (n.site) extBase = n.site;
    if (n.rtmp) {
      rtmpUrl = n.rtmp;
      document.getElementById('ngrok-alert').classList.add('show');
    }
  }
  setInput('u-rtmp', rtmpUrl);

  if (sRes.status !== 'fulfilled') return;
  const { streams } = sRes.value;

  const listEl = document.getElementById('stream-list');
  if (!streams.length) {
    listEl.innerHTML = '<p style="color:#484f58;font-size:.85rem">配信なし</p>';
    setInput('u-vrc', '');
    setInput('u-browser', '');
    return;
  }

  listEl.innerHTML = streams.map(s => `
    <div class="stream-item">
      <div class="dot ${s.active ? 'live' : 'off'}"></div>
      <span class="stream-key">${s.key}</span>
      <div class="platform-badges">
        <span class="pb ${s.active ? 'live' : 'off'}">${s.active ? 'LIVE' : 'OFF'}</span>
      </div>
      ${s.active ? `<button class="play-btn" onclick="playStream('${s.key}','${localBase}${s.hls_url}')">▶</button>` : ''}
    </div>`).join('');

  const active = streams.find(s => s.active);
  if (!active) {
    setInput('u-vrc', '');
    setInput('u-browser', '');
    return;
  }

  const hlsUrl = `${extBase}${active.hls_url}`;
  setInput('u-vrc',     hlsUrl);
  setInput('u-browser', hlsUrl);

  if (!playingKey) playStream(active.key, `${localBase}${active.hls_url}`);
}

refresh();
setInterval(refresh, 4000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def portal():
    return _PORTAL_HTML


# ─── 起動 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )
