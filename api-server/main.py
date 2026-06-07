"""
SYCS Stream Server — ABR fmp4 LL-HLS 配信ポータル

ポータル (/) : ストリーマー向け管理画面
  - RTMP URL をリアルタイム表示 (WebSocket)
  - ストリームキーを入力すると視聴 URL を生成
視聴ページ (/watch/{key}) : キーを知る視聴者向けプレイヤー
HLS 配信 (/hls/{path}) : ABR fmp4 LL-HLS + セッション管理
WebSocket (/ws) : ngrok RTMP URL・セッション数をブラウザにプッシュ
"""

import asyncio
import json
import os
import re
import socket
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiofiles
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response

# ─── 設定 ────────────────────────────────────────────────────────────────────

HLS_DIR        = Path(os.environ.get("HLS_DIR", "/hls"))
SITE_BASE_URL  = os.environ.get("SITE_BASE_URL", "")
NGROK_RTMP_API = os.environ.get("NGROK_RTMP_API", "http://localhost:4040")
_NGROK_TTL     = float(os.environ.get("NGROK_CACHE_TTL", "30"))

MAX_SESSIONS    = int(os.environ.get("MAX_SESSIONS",    "100"))
# SESSION_TIMEOUT はブロッキングタイムアウト (5s) + 余裕を考慮して 15s 以上を推奨。
# 8s だとブロッキング中にクリーンアップが走りセッションが消える可能性がある。
SESSION_TIMEOUT = float(os.environ.get("SESSION_TIMEOUT", "15.0"))
QUEUE_TIMEOUT   = float(os.environ.get("QUEUE_TIMEOUT",  "20.0"))
MAX_BPS         = int(os.environ.get("MAX_BPS", str(3_000_000)))

_KEY_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')

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
_local_ip:       str            = ""


def _get_local_ip() -> str:
    global _local_ip
    if not _local_ip:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                _local_ip = s.getsockname()[0]
        except Exception:
            _local_ip = "サーバーIP"
    return _local_ip


async def get_ngrok_urls() -> dict[str, str]:
    global _ngrok_cache, _ngrok_cache_ts
    now = asyncio.get_running_loop().time()
    if now - _ngrok_cache_ts < _NGROK_TTL and _ngrok_cache:
        return _ngrok_cache

    result: dict[str, str] = {
        "site":       SITE_BASE_URL,
        "rtmp":       "",
        "rtmp_local": f"rtmp://{_get_local_ip()}:1935/live",
    }
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{NGROK_RTMP_API}/api/tunnels")
            for t in r.json().get("tunnels", []):
                pub = t.get("public_url", "")
                if pub.startswith("tcp://"):
                    result["rtmp"] = "rtmp://" + pub[6:] + "/live"
                    break
    except Exception:
        pass

    _ngrok_cache    = result
    _ngrok_cache_ts = now
    return result


# ─── WebSocket ブロードキャスト ───────────────────────────────────────────────

_ws_clients: set[WebSocket] = set()


async def _broadcast(msg: dict) -> None:
    """全接続クライアントに JSON をプッシュ。切断済みクライアントは除去。"""
    if not _ws_clients:
        return
    text = json.dumps(msg, ensure_ascii=False)
    dead: set[WebSocket] = set()
    for ws in list(_ws_clients):
        try:
            await ws.send_text(text)
        except Exception:
            dead.add(ws)
    _ws_clients -= dead


async def _current_status() -> dict:
    urls = await get_ngrok_urls()
    return {
        "rtmp":       urls.get("rtmp", ""),
        "rtmp_local": urls.get("rtmp_local", ""),
        "site":       urls.get("site", ""),
        "sessions":   {"active": len(_sessions), "max": MAX_SESSIONS},
    }


async def _ws_broadcaster() -> None:
    """ngrok URL とセッション数を 5 秒ごとに変化があればブロードキャスト。"""
    prev: dict = {}
    while True:
        await asyncio.sleep(5.0)
        if not _ws_clients:
            continue
        state = await _current_status()
        if state != prev:
            prev = state.copy()
            await _broadcast(state)


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
            await asyncio.wait_for(ev.wait(), timeout=min(remaining, 0.15))
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
    if not content.strip() or "#EXTM3U" not in content:
        return content
    has_pdt = "EXT-X-PROGRAM-DATE-TIME" in content
    out: list[str] = []
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
    def _s(m: re.Match) -> str:
        return f"HOLD-BACK={max(0.75, float(m.group(1)) * 0.6):.3f}"
    return re.sub(r"\bHOLD-BACK=([\d.]+)", _s, line)


def _estimate_first_segment_pdt(content: str) -> str | None:
    durations = [float(m.group(1)) for m in re.finditer(r"#EXTINF:([\d.]+)", content)]
    if not durations:
        return None
    first = datetime.now(timezone.utc) - timedelta(seconds=sum(durations))
    return first.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _inject_sid(content: str, sid: str) -> str:
    content = re.sub(r'(URI="[^"?]+)"', rf'\1?sid={sid}"', content)
    out = []
    for line in content.splitlines(keepends=True):
        tag = line.rstrip("\r\n")
        if tag and not tag.startswith("#") and "?" not in tag:
            out.append(f"{tag}?sid={sid}\n")
        else:
            out.append(line)
    return "".join(out)


# ─── セッション管理 ───────────────────────────────────────────────────────────

_sessions: dict[str, float]  = {}
_bw:       dict[str, deque]  = defaultdict(deque)
_cap_cond: asyncio.Condition = asyncio.Condition()


async def _acquire_session(sid: str | None) -> str:
    loop = asyncio.get_running_loop()
    async with _cap_cond:
        if sid and sid in _sessions:
            _sessions[sid] = loop.time()
            return sid
        deadline = loop.time() + QUEUE_TIMEOUT
        while True:
            if len(_sessions) < MAX_SESSIONS:
                new_sid = uuid.uuid4().hex[:12]
                _sessions[new_sid] = loop.time()
                return new_sid
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise HTTPException(503, "満員です。他のお客様の退出をお待ちください。")
            try:
                await asyncio.wait_for(_cap_cond.wait(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                pass


async def _renew_session(sid: str) -> bool:
    loop = asyncio.get_running_loop()
    async with _cap_cond:
        if sid in _sessions:
            _sessions[sid] = loop.time()
            return True
        return False


def _check_and_record_bw(sid: str, nbytes: int) -> bool:
    now = asyncio.get_running_loop().time()
    dq  = _bw[sid]
    dq.append((now, nbytes))
    while dq and now - dq[0][0] > 1.0:
        dq.popleft()
    return sum(b for _, b in dq) <= MAX_BPS


async def _revoke_session(sid: str) -> None:
    async with _cap_cond:
        _sessions.pop(sid, None)
        _bw.pop(sid, None)
        _cap_cond.notify_all()


async def _cleanup_sessions() -> None:
    while True:
        await asyncio.sleep(1.0)
        loop = asyncio.get_running_loop()
        now  = loop.time()
        freed = 0
        async with _cap_cond:
            expired = [s for s, ts in _sessions.items() if now - ts > SESSION_TIMEOUT]
            for s in expired:
                _sessions.pop(s, None)
                _bw.pop(s, None)
            freed = len(expired)
            if freed:
                _cap_cond.notify_all()
        if freed:
            asyncio.create_task(_broadcast(await _current_status()))


# ─── アプリ ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI):
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    tasks = [
        asyncio.create_task(_watch_hls()),
        asyncio.create_task(_cleanup_sessions()),
        asyncio.create_task(_ws_broadcaster()),
    ]
    yield
    for t in tasks:
        t.cancel()


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


@app.get("/api/stream/{key}")
async def stream_info(key: str):
    """指定キーのストリーム状態を返す。配信一覧は公開しない。"""
    if not _KEY_RE.match(key):
        raise HTTPException(400, "無効なストリームキーです。")
    stream_dir = HLS_DIR / "live" / key
    active = (stream_dir / "high" / "index.m3u8").exists()
    return {
        "key":     key,
        "active":  active,
        "hls_url": f"/hls/live/{key}/master.m3u8",
    }


@app.get("/api/ngrok")
async def ngrok_info():
    """ngrok RTMP URL を返す REST エンドポイント (WebSocket フォールバック用)。"""
    return await get_ngrok_urls()


@app.get("/api/status")
async def server_status():
    return {
        "active":    len(_sessions),
        "max":       MAX_SESSIONS,
        "available": max(0, MAX_SESSIONS - len(_sessions)),
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """ngrok RTMP URL とセッション数をリアルタイムでプッシュする。"""
    await ws.accept()
    _ws_clients.add(ws)
    try:
        await ws.send_text(json.dumps(await _current_status(), ensure_ascii=False))
        while True:
            # Cloudflare Tunnel の idle タイムアウト対策に定期 ping
            await asyncio.sleep(30)
            await ws.send_text(json.dumps({"ping": True}))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _ws_clients.discard(ws)


@app.get("/hls/{path:path}")
async def serve_hls(
    path:      str,
    sid:       str | None = Query(default=None),
    _HLS_msn:  int | None = Query(default=None),
    _HLS_part: int | None = Query(default=None),
):
    file_path   = HLS_DIR / path
    is_master   = path.endswith("master.m3u8")
    is_playlist = path.endswith("index.m3u8")

    # パストラバーサル防止: resolve() で ".." を展開してから比較する。
    # relative_to() のみでは "/hls/../../etc/passwd" が ValueError を投げないため不十分。
    try:
        file_path.resolve().relative_to(HLS_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid path")

    if is_master:
        sid = await _acquire_session(sid)
    elif is_playlist:
        if sid and not await _renew_session(sid):
            raise HTTPException(403, "セッション期限切れ。再度アクセスしてください。")

    if is_playlist and _HLS_msn is not None:
        await _block_until_ready(path, file_path, _HLS_msn, _HLS_part, timeout=5.0)

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Not found")

    if path.endswith(".m3u8"):
        try:
            async with aiofiles.open(file_path, encoding="utf-8") as f:
                content = await f.read()
        except OSError:
            raise HTTPException(status_code=404, detail="Not found")
        content = _patch_playlist(content)
        if sid:
            content = _inject_sid(content, sid)
        return Response(
            content=content.encode("utf-8"),
            media_type="application/vnd.apple.mpegurl",
            headers=NO_CACHE_HEADERS,
        )

    if sid:
        try:
            nbytes = file_path.stat().st_size
        except OSError:
            raise HTTPException(status_code=404, detail="Not found")
        if not _check_and_record_bw(sid, nbytes):
            await _revoke_session(sid)
            raise HTTPException(429, "帯域超過によりセッションを終了しました。")

    media_type = MEDIA_TYPES.get(file_path.suffix, "application/octet-stream")
    return FileResponse(file_path, media_type=media_type, headers=NO_CACHE_HEADERS)


# ─── HTML ─────────────────────────────────────────────────────────────────────

_PORTAL_HTML = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SYCS Stream Server</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}
header{background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;align-items:center;gap:10px}
header h1{font-size:1.1rem;font-weight:700;color:#58a6ff}
.chip{background:#1f6feb;color:#fff;font-size:.65rem;padding:2px 7px;border-radius:10px;font-weight:600}
#cap{margin-left:auto;font-size:.75rem;color:#56d364}
main{max-width:640px;margin:32px auto;padding:0 16px;display:flex;flex-direction:column;gap:16px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px 20px}
.card h2{font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;color:#8b949e;margin-bottom:14px;font-weight:600}
.row{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.row:last-child{margin-bottom:0}
label{font-size:.75rem;color:#8b949e;width:96px;flex-shrink:0}
input[type=text]{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:6px 10px;color:#e6edf3;font-family:monospace;font-size:.8rem;min-width:0}
input[readonly]{color:#58a6ff}
input.empty{color:#484f58}
.btn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:5px 12px;font-size:.75rem;cursor:pointer;white-space:nowrap;flex-shrink:0}
.btn:hover{background:#30363d}
.btn.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}
.btn.primary:hover{background:#388bfd}
.btn.ok{color:#3fb950;border-color:#3fb950}
.alert{background:#1c2128;border:1px solid #9e6a03;border-radius:6px;padding:8px 12px;font-size:.75rem;color:#d29922;margin-bottom:12px;display:none}
.alert.show{display:block}
#stream-result{display:none;margin-top:12px;padding-top:12px;border-top:1px solid #21262d}
.status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
.live{background:#3fb950;box-shadow:0 0 5px #3fb950}.off{background:#484f58}
</style>
</head>
<body>
<header>
  <h1>SYCS Stream Server</h1>
  <span class="chip">LL-HLS</span>
  <span id="cap" title="接続中 / 最大">● 0 / 0</span>
</header>
<main>

  <div class="card">
    <h2>OBS 配信設定</h2>
    <div class="alert" id="ngrok-alert">⚠ ngrok 無料プランでは再起動ごとに RTMP URL が変わります。</div>
    <div class="alert" id="lan-alert" style="border-color:#1f6feb;color:#79c0ff;display:none">ℹ ngrok 未起動。LAN から直接接続する場合は下記 URL を使用してください。</div>
    <div class="row">
      <label>RTMP URL</label>
      <input id="u-rtmp" type="text" readonly class="empty" value="取得中...">
      <button class="btn" onclick="cp('u-rtmp',this)">コピー</button>
    </div>
    <div class="row">
      <label>ストリームキー</label>
      <span style="font-size:.8rem;color:#8b949e">OBS で任意の値を設定してください</span>
    </div>
  </div>

  <div class="card">
    <h2>視聴 URL 生成</h2>
    <div class="row">
      <label>ストリームキー</label>
      <input id="key-input" type="text" placeholder="例: stream">
      <button class="btn primary" onclick="checkStream()">確認</button>
    </div>
    <div id="stream-result">
      <div class="row" style="margin-bottom:8px">
        <span class="status-dot" id="s-dot"></span>
        <span id="s-label" style="font-size:.85rem;font-weight:600"></span>
      </div>
      <div class="row">
        <label>VRChat URL</label>
        <input id="u-vrc" type="text" readonly class="empty" value="">
        <button class="btn" onclick="cp('u-vrc',this)">コピー</button>
      </div>
      <div class="row">
        <label>視聴ページ</label>
        <input id="u-watch" type="text" readonly class="empty" value="">
        <button class="btn" onclick="openWatch()">開く</button>
        <button class="btn" onclick="cp('u-watch',this)">コピー</button>
      </div>
    </div>
  </div>

</main>
<script>
// ─── 状態 ───────────────────────────────────────────────────────────────────
let siteBase = '';
let ws = null;
let reconnectTimer = null;
let pollTimer = null;

// ─── UI ヘルパー ─────────────────────────────────────────────────────────────
function setVal(id, val, empty) {
  const el = document.getElementById(id);
  if (!el) return;
  el.value = val;
  el.classList.toggle('empty', empty == null ? !val : empty);
}

function applyNgrokData(d) {
  if (typeof d.site === 'string' && d.site) siteBase = d.site;
  const hasNgrok = typeof d.rtmp === 'string' && d.rtmp !== '';
  const displayUrl = hasNgrok ? d.rtmp : (d.rtmp_local || '');
  if (displayUrl) setVal('u-rtmp', displayUrl, !hasNgrok);
  document.getElementById('ngrok-alert').classList.toggle('show', hasNgrok);
  const lanEl = document.getElementById('lan-alert');
  lanEl.style.display = !hasNgrok && displayUrl ? 'block' : 'none';
}

function applyCapacity(active, max) {
  const pct = max > 0 ? active / max : 0;
  const el = document.getElementById('cap');
  el.textContent = `● ${active} / ${max}`;
  el.style.color = pct >= 1 ? '#f85149' : pct >= 0.8 ? '#d29922' : '#56d364';
}

// ─── REST フォールバック ─────────────────────────────────────────────────────
async function fetchFromRest() {
  try {
    const r = await fetch('/api/ngrok');
    if (r.ok) applyNgrokData(await r.json());
  } catch {}
  try {
    const r = await fetch('/api/status');
    if (r.ok) { const s = await r.json(); applyCapacity(s.active, s.max); }
  } catch {}
}

// ─── WebSocket ───────────────────────────────────────────────────────────────
function connectWS() {
  clearTimeout(reconnectTimer);
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen = () => {
    clearInterval(pollTimer);
    pollTimer = null;
  };

  ws.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.ping) return;
    if ('rtmp' in d || 'site' in d) applyNgrokData(d);
    if (d.sessions) applyCapacity(d.sessions.active, d.sessions.max);
  };

  ws.onclose = () => {
    ws = null;
    if (!pollTimer) pollTimer = setInterval(fetchFromRest, 10000);
    reconnectTimer = setTimeout(connectWS, 5000);
  };

  ws.onerror = () => ws && ws.close();
}

// ─── 初期化 ─────────────────────────────────────────────────────────────────
// ページ読み込み直後に REST で即座取得し、WebSocket も並行接続
fetchFromRest();
connectWS();

// ─── コピー / 開く ───────────────────────────────────────────────────────────
function cp(id, btn) {
  const el = document.getElementById(id);
  if (!el || el.classList.contains('empty')) return;
  navigator.clipboard.writeText(el.value).then(() => {
    const orig = btn.textContent;
    btn.textContent = '✓'; btn.classList.add('ok');
    setTimeout(() => { btn.textContent = orig; btn.classList.remove('ok'); }, 2000);
  });
}

function openWatch() {
  const el = document.getElementById('u-watch');
  if (el && !el.classList.contains('empty')) window.open(el.value, '_blank');
}

// ─── ストリーム確認 ──────────────────────────────────────────────────────────
async function checkStream() {
  const key = document.getElementById('key-input').value.trim();
  if (!key) return;
  document.getElementById('stream-result').style.display = 'block';

  try {
    const r = await fetch(`/api/stream/${encodeURIComponent(key)}`);
    if (!r.ok) { showStatus(false, '無効なキーです'); return; }
    const d = await r.json();
    const base = (siteBase || location.origin).replace(/\\/$/, '');
    showStatus(d.active, d.active ? 'LIVE' : '待機中');
    setVal('u-vrc',   `${base}${d.hls_url}`, false);
    setVal('u-watch', `${base}/watch/${encodeURIComponent(key)}`, false);
  } catch {
    showStatus(false, '取得失敗');
  }
}

function showStatus(live, label) {
  document.getElementById('s-dot').className = 'status-dot ' + (live ? 'live' : 'off');
  document.getElementById('s-label').textContent = label;
}

document.getElementById('key-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') checkStream();
});
</script>
</body>
</html>
"""

_WATCH_HTML = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>視聴中 — SYCS</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:100%;height:100%;background:#000;overflow:hidden}
#wrap{position:relative;width:100%;height:100%;display:flex;align-items:center;justify-content:center}
video{width:100%;height:100%;object-fit:contain}
#msg{position:absolute;color:#484f58;font-family:'Segoe UI',system-ui,sans-serif;font-size:.9rem;pointer-events:none}
</style>
</head>
<body>
<div id="wrap">
  <video id="v" controls autoplay playsinline></video>
  <p id="msg">配信待機中...</p>
</div>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<script>
const KEY = __KEY_JSON__;
const HLS_URL = `/hls/live/${KEY}/master.m3u8`;
let hls = null;
let polling = null;

function startPlayer() {
  const video = document.getElementById('v');
  document.getElementById('msg').style.display = 'none';
  if (hls) { hls.destroy(); hls = null; }
  if (Hls.isSupported()) {
    hls = new Hls({
      lowLatencyMode: true,
      backBufferLength: 2,
      maxBufferLength: 4,
      liveSyncDurationCount: 2,
      liveMaxLatencyDurationCount: 5,
      liveDurationInfinity: true,
    });
    hls.loadSource(HLS_URL);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
    hls.on(Hls.Events.ERROR, (_, d) => {
      if (d.fatal) { hls.destroy(); hls = null; startPolling(); }
    });
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = HLS_URL;
    video.play().catch(() => {});
  }
}

async function checkActive() {
  try {
    const r = await fetch(`/api/stream/${encodeURIComponent(KEY)}`);
    if (!r.ok) return false;
    return (await r.json()).active;
  } catch { return false; }
}

function startPolling() {
  document.getElementById('msg').style.display = '';
  if (polling) return;
  polling = setInterval(async () => {
    if (await checkActive()) {
      clearInterval(polling);
      polling = null;
      startPlayer();
    }
  }, 3000);
}

(async () => {
  if (await checkActive()) {
    startPlayer();
  } else {
    startPolling();
  }
})();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def portal():
    return _PORTAL_HTML


@app.get("/watch/{key}", response_class=HTMLResponse)
async def watch_page(key: str):
    """ストリームキーを知る視聴者向けプレイヤーページ。"""
    if not _KEY_RE.match(key):
        raise HTTPException(400, "無効なストリームキーです。")
    return _WATCH_HTML.replace("__KEY_JSON__", json.dumps(key))


# ─── 起動 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )
