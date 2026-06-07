"""
SYCS Stream Server — ABR fmp4 LL-HLS 配信ポータル

ポータル (/)         : ストリーマー向け管理画面
視聴ページ (/watch)  : WebSocket 経由でプレイリストを受信 (m3u8 URL 非公開)
HLS 配信 (/hls)      : VRChat 向け通常 HTTP 配信 (sid セッション管理)
セグメント (/seg)    : HMAC 署名付き短命 URL でのセグメント配信
WebSocket (/ws)      : ngrok RTMP URL・セッション数プッシュ
WebSocket (/ws/hls)  : LL-HLS プレイリストをリアルタイムプッシュ
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import socket
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

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
SESSION_TIMEOUT = float(os.environ.get("SESSION_TIMEOUT", "15.0"))
QUEUE_TIMEOUT   = float(os.environ.get("QUEUE_TIMEOUT",  "20.0"))
MAX_BPS         = int(os.environ.get("MAX_BPS", str(3_000_000)))
SEGMENT_WAIT_TIMEOUT = float(os.environ.get("SEGMENT_WAIT_TIMEOUT", "1.5"))
MEDIAMTX_HLS_URL = os.environ.get("MEDIAMTX_HLS_URL", "http://127.0.0.1:8888").rstrip("/")
MEDIAMTX_HLS_TIMEOUT = float(os.environ.get("MEDIAMTX_HLS_TIMEOUT", "1.0"))

# セグメント URL 署名用シークレット (env 未設定時は起動ごとにランダム生成)
_SEGMENT_SECRET: bytes = (os.environ.get("SEGMENT_SECRET", "") or uuid.uuid4().hex).encode()
_SEGMENT_TTL = int(os.environ.get("SEGMENT_TTL", "120"))   # 秒
_TOKEN_TTL   = int(os.environ.get("TOKEN_TTL",   "60"))    # 秒

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
_hls_client: httpx.AsyncClient | None = None


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


# ─── WebSocket ブロードキャスト (ポータル用) ─────────────────────────────────

_ws_clients: set[WebSocket] = set()


async def _broadcast(msg: dict) -> None:
    if not _ws_clients:
        return
    text = json.dumps(msg, ensure_ascii=False)
    dead: set[WebSocket] = set()
    for ws in list(_ws_clients):
        try:
            await ws.send_text(text)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


async def _current_status() -> dict:
    urls = await get_ngrok_urls()
    return {
        "rtmp":       urls.get("rtmp", ""),
        "rtmp_local": urls.get("rtmp_local", ""),
        "site":       urls.get("site", ""),
        "sessions":   {"active": len(_sessions), "max": MAX_SESSIONS},
    }


async def _ws_broadcaster() -> None:
    prev: dict = {}
    while True:
        await asyncio.sleep(5.0)
        if not _ws_clients:
            continue
        state = await _current_status()
        if state != prev:
            prev = state.copy()
            await _broadcast(state)


# ─── LL-HLS ファイル変更監視 ─────────────────────────────────────────────────

_waiters:      dict[str, list[asyncio.Event]] = defaultdict(list)
_waiters_lock: asyncio.Lock                   = asyncio.Lock()

# WebSocket HLS プッシュ用キュー: rel_path → [Queue]
_ws_hls_queues: dict[str, list[asyncio.Queue]] = defaultdict(list)
_ws_hls_lock:   asyncio.Lock                   = asyncio.Lock()


async def _notify_waiters(rel_path: str) -> None:
    # HTTP LL-HLS ロングポール待機者
    async with _waiters_lock:
        for ev in _waiters.pop(rel_path, []):
            ev.set()
    # WebSocket プッシュキュー
    async with _ws_hls_lock:
        for q in _ws_hls_queues.get(rel_path, []):
            try:
                q.put_nowait(rel_path)
            except asyncio.QueueFull:
                pass


async def _poll_hls_changes() -> None:
    mtimes: dict[str, int] = {}
    live_dir = HLS_DIR / "live"
    while True:
        try:
            for fp in live_dir.glob("*/index.m3u8"):
                rel = str(fp.relative_to(HLS_DIR))
                try:
                    mtime = fp.stat().st_mtime_ns
                except OSError:
                    continue
                if mtimes.get(rel) != mtime:
                    mtimes[rel] = mtime
                    await _notify_waiters(rel)
        except Exception:
            pass
        await asyncio.sleep(0.1)


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


def _mediamtx_hls_url(rel_path: str) -> str:
    safe_path = quote(rel_path.lstrip("/"), safe="/-_.~")
    return f"{MEDIAMTX_HLS_URL}/{safe_path}"


async def _fetch_mediamtx_hls(
    rel_path: str,
    params: dict[str, int] | None = None,
) -> bytes | None:
    client = _hls_client
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=MEDIAMTX_HLS_TIMEOUT)
        close_client = True
    try:
        r = await client.get(_mediamtx_hls_url(rel_path), params=params)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content
    except Exception:
        return None
    finally:
        if close_client:
            await client.aclose()


async def _read_playlist_content(
    rel_path: str,
    params: dict[str, int] | None = None,
) -> str | None:
    file_path = HLS_DIR / rel_path
    if file_path.exists() and params is None:
        try:
            async with aiofiles.open(file_path, encoding="utf-8") as f:
                return await f.read()
        except OSError:
            pass

    data = await _fetch_mediamtx_hls(rel_path, params=params)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


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


async def _wait_for_file(file_path: Path, timeout: float = SEGMENT_WAIT_TIMEOUT) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if file_path.exists():
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(remaining, 0.05))


def _playlist_satisfies(content: str, msn: int, part: int | None) -> bool:
    m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", content)
    if not m:
        return False
    base    = int(m.group(1))
    segs    = content.count("#EXTINF:")
    max_msn = base + segs - 1
    if msn <= max_msn:
        return True
    if msn > max_msn + 1:
        return False
    if part is None:
        return False

    tail = content
    last_inf = content.rfind("#EXTINF:")
    if last_inf >= 0:
        first_nl = content.find("\n", last_inf)
        second_nl = content.find("\n", first_nl + 1) if first_nl >= 0 else -1
        if second_nl >= 0:
            tail = content[second_nl + 1:]
    return tail.count("#EXT-X-PART:") > part


async def _playlist_state(key: str) -> dict:
    variants = {
        "high": ("live/{}/index.m3u8".format(key), HLS_DIR / "live" / key / "index.m3u8"),
        "low":  ("live/{}_transcode/index.m3u8".format(key), HLS_DIR / "live" / f"{key}_transcode" / "index.m3u8"),
    }
    state: dict[str, dict] = {}
    now = time.time()
    for name, (rel_path, path) in variants.items():
        try:
            st = path.stat()
            state[name] = {
                "ready": True,
                "source": "disk",
                "age_sec": max(0.0, round(now - st.st_mtime, 3)),
                "bytes": st.st_size,
            }
        except OSError:
            content = await _read_playlist_content(rel_path)
            state[name] = {
                "ready": content is not None,
                "source": "mediamtx" if content is not None else "missing",
                "bytes": len(content.encode("utf-8")) if content is not None else 0,
            }
    return state


# ─── Playlist パッチ処理 ──────────────────────────────────────────────────────

def _patch_playlist(content: str) -> str:
    if not content.strip() or "#EXTM3U" not in content:
        return content
    has_pdt = "EXT-X-PROGRAM-DATE-TIME" in content
    part_target = _playlist_float_tag(content, r"#EXT-X-PART-INF:[^\n]*\bPART-TARGET=([\d.]+)")
    target_duration = _playlist_float_tag(content, r"#EXT-X-TARGETDURATION:([\d.]+)")
    out: list[str] = []
    pdt_inserted = False
    for line in content.splitlines(keepends=True):
        tag = line.rstrip("\r\n")
        if tag.startswith("#EXT-X-SERVER-CONTROL:"):
            line = _shrink_hold_back(tag, part_target, target_duration) + "\n"
        if not has_pdt and not pdt_inserted and tag.startswith("#EXTINF:"):
            pdt = _estimate_first_segment_pdt(content)
            if pdt:
                out.append(f"#EXT-X-PROGRAM-DATE-TIME:{pdt}\n")
            pdt_inserted = True
        out.append(line)
    return "".join(out)


def _playlist_float_tag(content: str, pattern: str) -> float | None:
    m = re.search(pattern, content)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _shrink_hold_back(
    line: str,
    part_target: float | None,
    target_duration: float | None,
) -> str:
    def _part(m: re.Match) -> str:
        current = float(m.group(1))
        lower = (part_target or 0.1) * 3
        return f"PART-HOLD-BACK={max(lower, current * 0.6):.3f}"

    def _hold(m: re.Match) -> str:
        current = float(m.group(1))
        lower = (target_duration or 1.0) * 3
        return f"HOLD-BACK={max(lower, current * 0.6):.3f}"

    line = re.sub(r"\bPART-HOLD-BACK=([\d.]+)", _part, line)
    return re.sub(r"(?<!PART-)\bHOLD-BACK=([\d.]+)", _hold, line)


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


# ─── セグメント URL 署名 ──────────────────────────────────────────────────────

def _sign_segment_url(rel_path: str) -> str:
    exp = int(time.time()) + _SEGMENT_TTL
    mac = hmac.new(_SEGMENT_SECRET, f"{rel_path}:{exp}".encode(), hashlib.sha256)
    sig = mac.hexdigest()[:16]
    return f"/seg/{rel_path}?exp={exp}&sig={sig}"


def _verify_segment(rel_path: str, exp: str, sig: str) -> bool:
    try:
        if int(exp) < time.time():
            return False
        mac = hmac.new(_SEGMENT_SECRET, f"{rel_path}:{exp}".encode(), hashlib.sha256)
        return hmac.compare_digest(mac.hexdigest()[:16], sig)
    except Exception:
        return False


def _sign_playlist_segments(content: str, base_key: str) -> str:
    """m3u8 内の URI= タグと素のセグメント行を /seg/ 署名付き絶対 URL に書き換える。"""
    def _sign_uri(m: re.Match) -> str:
        uri = m.group(1)
        if uri.startswith(("http://", "https://", "/seg/", "/", "../")):
            return m.group(0)
        rel = f"live/{base_key}/{uri}"
        return f'URI="{_sign_segment_url(rel)}"'

    out = []
    for line in content.splitlines(keepends=True):
        tag = line.rstrip("\r\n")
        if tag.startswith(("#EXT-X-MAP:", "#EXT-X-PART:", "#EXT-X-PRELOAD-HINT:")):
            out.append(re.sub(r'URI="([^"]+)"', _sign_uri, line))
            continue
        if tag and not tag.startswith("#") and not tag.startswith("/") and not tag.startswith("http"):
            rel = f"live/{base_key}/{tag}"
            out.append(_sign_segment_url(rel) + "\n")
        else:
            out.append(line)
    return "".join(out)


def _build_master_content(key: str) -> str:
    return (
        "#EXTM3U\n"
        "#EXT-X-VERSION:6\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,NAME=high\n"
        f"/hls/live/{key}/index.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,NAME=low\n"
        f"/hls/live/{key}_transcode/index.m3u8\n"
    )


# ─── ワンタイムトークン ───────────────────────────────────────────────────────

_tokens: dict[str, tuple[str, float]] = {}   # token → (key, expiry)


def _create_token(key: str) -> str:
    token = uuid.uuid4().hex
    _tokens[token] = (key, time.time() + _TOKEN_TTL)
    return token


def _consume_token(token: str, key: str) -> bool:
    entry = _tokens.pop(token, None)
    if not entry:
        return False
    stored_key, exp = entry
    return stored_key == key and exp >= time.time()


async def _cleanup_tokens() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        expired = [t for t, (_, exp) in list(_tokens.items()) if exp < now]
        for t in expired:
            _tokens.pop(t, None)


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
    global _hls_client
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    _hls_client = httpx.AsyncClient(timeout=MEDIAMTX_HLS_TIMEOUT)
    tasks = [
        asyncio.create_task(_watch_hls()),
        asyncio.create_task(_poll_hls_changes()),
        asyncio.create_task(_cleanup_sessions()),
        asyncio.create_task(_cleanup_tokens()),
        asyncio.create_task(_ws_broadcaster()),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await _hls_client.aclose()
        _hls_client = None


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
    if not _KEY_RE.match(key):
        raise HTTPException(400, "無効なストリームキーです。")
    variants = await _playlist_state(key)
    active = variants["high"]["ready"]
    return {
        "key":     key,
        "active":  active,
        "hls_url": f"/hls/live/{key}/master.m3u8",
        "variants": variants,
    }


@app.get("/api/ngrok")
async def ngrok_info():
    return await get_ngrok_urls()


@app.get("/api/status")
async def server_status():
    return {
        "active":    len(_sessions),
        "max":       MAX_SESSIONS,
        "available": max(0, MAX_SESSIONS - len(_sessions)),
    }


@app.get("/api/token/{key}")
async def create_stream_token(key: str):
    """視聴ページが WebSocket 接続前に取得するワンタイムトークン。"""
    if not _KEY_RE.match(key):
        raise HTTPException(400, "無効なストリームキーです。")
    return {"token": _create_token(key), "ttl": _TOKEN_TTL}


# ─── WebSocket: ポータル用 ────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        await ws.send_text(json.dumps(await _current_status(), ensure_ascii=False))
        while True:
            await asyncio.sleep(30)
            await ws.send_text(json.dumps({"ping": True}))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _ws_clients.discard(ws)


# ─── WebSocket: LL-HLS プレイリストプッシュ ───────────────────────────────────

@app.websocket("/ws/hls/{key}")
async def ws_hls(ws: WebSocket, key: str, token: str = Query(...)):
    """
    認証済み WebSocket でプレイリストをリアルタイムプッシュする。
    - 接続時に master + 現在の variant を即時送信
    - mediamtx が index.m3u8 を更新するたびにセグメント署名済みプレイリストをプッシュ
    - 切断でセッションを即解放
    """
    if not _KEY_RE.match(key):
        await ws.close(1008, "Invalid key")
        return
    if not _consume_token(token, key):
        await ws.close(1008, "Invalid or expired token")
        return

    try:
        sid = await _acquire_session(None)
    except HTTPException:
        await ws.close(1013, "満員です")
        return

    await ws.accept()

    high_rel = f"live/{key}/index.m3u8"
    low_rel  = f"live/{key}_transcode/index.m3u8"
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=30)

    async with _ws_hls_lock:
        _ws_hls_queues[high_rel].append(q)
        _ws_hls_queues[low_rel].append(q)

    last_raw: dict[str, str] = {}

    async def _push_variant(variant: str, rel: str, vkey: str) -> bool:
        raw = await _read_playlist_content(rel)
        if raw is None or last_raw.get(variant) == raw:
            return False
        last_raw[variant] = raw
        content = _patch_playlist(raw)
        content = _sign_playlist_segments(content, vkey)
        await ws.send_json({"type": "level", "variant": variant, "content": content})
        return True

    try:
        await ws.send_json({"type": "master", "content": _build_master_content(key)})
        await _push_variant("high", high_rel, key)
        await _push_variant("low",  low_rel,  f"{key}_transcode")

        loop = asyncio.get_running_loop()
        last_ping = loop.time()
        last_renew = loop.time()
        while True:
            try:
                changed_rel = await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                changed_rel = None

            now = loop.time()
            if now - last_renew >= 1.0:
                last_renew = now
                if not await _renew_session(sid):
                    break

            if changed_rel == high_rel:
                await _push_variant("high", high_rel, key)
            elif changed_rel == low_rel:
                await _push_variant("low", low_rel, f"{key}_transcode")
            else:
                await _push_variant("high", high_rel, key)
                await _push_variant("low", low_rel, f"{key}_transcode")

            if now - last_ping >= 25.0:
                last_ping = now
                await ws.send_json({"type": "ping"})

    except (WebSocketDisconnect, Exception):
        pass
    finally:
        async with _ws_hls_lock:
            for rel in (high_rel, low_rel):
                try:
                    _ws_hls_queues[rel].remove(q)
                except ValueError:
                    pass
        await _revoke_session(sid)


# ─── セグメント配信 (署名付き短命 URL) ───────────────────────────────────────

@app.get("/seg/{path:path}")
async def serve_segment(
    path: str,
    exp:  str = Query(...),
    sig:  str = Query(...),
):
    """HMAC 署名を検証してセグメントを配信する。署名は _SEGMENT_TTL 秒で失効。"""
    if not _verify_segment(path, exp, sig):
        raise HTTPException(403, "URL の有効期限が切れています。再生を再開してください。")

    file_path = HLS_DIR / path
    try:
        file_path.resolve().relative_to(HLS_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid path")

    media_type = MEDIA_TYPES.get(file_path.suffix, "application/octet-stream")
    seg_headers = {
        "Cache-Control": f"private, max-age={_SEGMENT_TTL}",
        "Access-Control-Allow-Origin": "*",
    }

    if not file_path.exists() and not await _wait_for_file(file_path):
        data = await _fetch_mediamtx_hls(path)
        if data is None:
            raise HTTPException(404, "Not found")
        return Response(content=data, media_type=media_type, headers=seg_headers)

    return FileResponse(file_path, media_type=media_type, headers=seg_headers)


# ─── HLS 配信 (VRChat 向け HTTP 経由) ────────────────────────────────────────

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

    try:
        file_path.resolve().relative_to(HLS_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid path")

    # live/{key}/master.m3u8 は動的生成 (mediamtx は書き出さない)
    path_parts = path.split("/")
    if (is_master
            and len(path_parts) == 3
            and path_parts[0] == "live"
            and path_parts[2] == "master.m3u8"
            and not path_parts[1].endswith("_transcode")):
        key = path_parts[1]
        if not _KEY_RE.match(key):
            raise HTTPException(400, "無効なストリームキーです。")
        sid = await _acquire_session(sid)
        master_content = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:6\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,NAME=high\n"
            "index.m3u8\n"
            f"#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,NAME=low\n"
            f"../{key}_transcode/index.m3u8\n"
        )
        if sid:
            master_content = _inject_sid(master_content, sid)
        return Response(
            content=master_content.encode("utf-8"),
            media_type="application/vnd.apple.mpegurl",
            headers=NO_CACHE_HEADERS,
        )

    if is_master:
        sid = await _acquire_session(sid)
    elif is_playlist:
        if sid and not await _renew_session(sid):
            raise HTTPException(403, "セッション期限切れ。再度アクセスしてください。")

    if path.endswith(".m3u8"):
        params: dict[str, int] = {}
        if _HLS_msn is not None:
            params["_HLS_msn"] = _HLS_msn
        if _HLS_part is not None:
            params["_HLS_part"] = _HLS_part

        if is_playlist and file_path.exists() and _HLS_msn is not None:
            await _block_until_ready(path, file_path, _HLS_msn, _HLS_part, timeout=5.0)

        content = await _read_playlist_content(path, params=params or None)
        if content is None:
            raise HTTPException(status_code=404, detail="Not found")
        content = _patch_playlist(content)
        if sid:
            content = _inject_sid(content, sid)
        return Response(
            content=content.encode("utf-8"),
            media_type="application/vnd.apple.mpegurl",
            headers=NO_CACHE_HEADERS,
        )

    if not file_path.exists():
        if not await _wait_for_file(file_path):
            data = await _fetch_mediamtx_hls(path)
            if data is None:
                raise HTTPException(status_code=404, detail="Not found")
            if sid and not _check_and_record_bw(sid, len(data)):
                await _revoke_session(sid)
                raise HTTPException(429, "帯域超過によりセッションを終了しました。")
            media_type = MEDIA_TYPES.get(file_path.suffix, "application/octet-stream")
            return Response(content=data, media_type=media_type, headers=NO_CACHE_HEADERS)

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

APP_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_DIR / "templates"


def _read_template(name: str) -> str:
    try:
        return (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(500, "テンプレートが見つかりません。")


@app.get("/", response_class=HTMLResponse)
async def portal():
    return HTMLResponse(_read_template("portal.html"))


@app.get("/watch/{key}", response_class=HTMLResponse)
async def watch_page(key: str):
    if not _KEY_RE.match(key):
        raise HTTPException(400, "無効なストリームキーです。")
    content = _read_template("watch.html").replace("__KEY_JSON__", json.dumps(key))
    return HTMLResponse(content)

# ─── 起動 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )
