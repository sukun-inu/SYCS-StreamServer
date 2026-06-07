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
        return f"HOLD-BACK={max(0.5, float(m.group(1)) * 0.6):.3f}"
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
        if uri.startswith(("http://", "https://", "/seg/", "/")):
            return m.group(0)
        rel = f"live/{base_key}/{uri}"
        return f'URI="{_sign_segment_url(rel)}"'

    content = re.sub(r'URI="([^"]+)"', _sign_uri, content)

    out = []
    for line in content.splitlines(keepends=True):
        tag = line.rstrip("\r\n")
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
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    tasks = [
        asyncio.create_task(_watch_hls()),
        asyncio.create_task(_cleanup_sessions()),
        asyncio.create_task(_cleanup_tokens()),
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
    if not _KEY_RE.match(key):
        raise HTTPException(400, "無効なストリームキーです。")
    stream_dir = HLS_DIR / "live" / key
    active = (stream_dir / "index.m3u8").exists()
    return {
        "key":     key,
        "active":  active,
        "hls_url": f"/hls/live/{key}/master.m3u8",
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

    async def _push_variant(variant: str, rel: str, vkey: str) -> None:
        fp = HLS_DIR / rel
        if not fp.exists():
            return
        try:
            async with aiofiles.open(fp, encoding="utf-8") as f:
                content = await f.read()
            content = _patch_playlist(content)
            content = _sign_playlist_segments(content, vkey)
            await ws.send_json({"type": "level", "variant": variant, "content": content})
        except OSError:
            pass

    try:
        await ws.send_json({"type": "master", "content": _build_master_content(key)})
        await _push_variant("high", high_rel, key)
        await _push_variant("low",  low_rel,  f"{key}_transcode")

        while True:
            try:
                changed_rel = await asyncio.wait_for(q.get(), timeout=25.0)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
                if not await _renew_session(sid):
                    break
                continue

            await _renew_session(sid)
            if changed_rel == high_rel:
                await _push_variant("high", high_rel, key)
            else:
                await _push_variant("low", low_rel, f"{key}_transcode")

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

    if not file_path.exists():
        raise HTTPException(404, "Not found")

    media_type = MEDIA_TYPES.get(file_path.suffix, "application/octet-stream")
    seg_headers = {
        "Cache-Control": f"private, max-age={_SEGMENT_TTL}",
        "Access-Control-Allow-Origin": "*",
    }
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
let siteBase = '';
let ws = null;
let reconnectTimer = null;
let pollTimer = null;

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

function connectWS() {
  clearTimeout(reconnectTimer);
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => { clearInterval(pollTimer); pollTimer = null; };
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

fetchFromRest();
connectWS();

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
#msg{position:absolute;color:#484f58;font-family:'Segoe UI',system-ui,sans-serif;font-size:.9rem;pointer-events:none;text-align:center;padding:8px}
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

// ─── WebSocket HLS 共有状態 ───────────────────────────────────────────────────
const _ws = {
  conn:         null,
  master:       null,         // master content string | null
  masterWaiters: [],          // [resolve fn]
  levels:       {},           // 'high'|'low' → content string
  levelWaiters: {},           // 'high'|'low' → [{msn,part,resolve,timer}]
};

// ─── LL-HLS ユーティリティ ────────────────────────────────────────────────────
function _parseMsn(url) {
  try {
    const u = new URL(url, location.href);
    const msn  = u.searchParams.get('_HLS_msn');
    const part = u.searchParams.get('_HLS_part');
    return {
      msn:  msn  !== null ? parseInt(msn,  10) : null,
      part: part !== null ? parseInt(part, 10) : null,
    };
  } catch { return { msn: null, part: null }; }
}

function _hlsSatisfies(content, msn, part) {
  if (msn === null) return true;
  const seqM = content.match(/#EXT-X-MEDIA-SEQUENCE:(\d+)/);
  if (!seqM) return false;
  const base  = parseInt(seqM[1], 10);
  const segs  = (content.match(/#EXTINF:/g) || []).length;
  const maxMsn = base + segs - 1;
  if (msn < maxMsn) return true;   // 完了済みセグメント
  if (msn > maxMsn + 1) return false;
  if (part === null) return msn <= maxMsn;
  // 末尾の partial セグメントのパーツ数を数える
  const lastInfPos = content.lastIndexOf('#EXTINF:');
  let tail = content;
  if (lastInfPos >= 0) {
    const nl1 = content.indexOf('\\n', lastInfPos);
    const nl2 = content.indexOf('\\n', nl1 + 1);
    tail = content.slice(nl2 + 1);
  }
  return (tail.match(/#EXT-X-PART:/g) || []).length > part;
}

function _deliverLevel(variant, content) {
  _ws.levels[variant] = content;
  const pending = (_ws.levelWaiters[variant] || []).splice(0);
  const remaining = [];
  for (const req of pending) {
    if (_hlsSatisfies(content, req.msn, req.part)) {
      clearTimeout(req.timer);
      req.resolve(content);
    } else {
      remaining.push(req);
    }
  }
  _ws.levelWaiters[variant] = remaining;
}

// ─── カスタム pLoader (プレイリストを WebSocket から受け取る) ─────────────────
class WsPlaylistLoader {
  constructor(_config) {
    this._aborted = false;
    this._pending = null;
  }

  load(ctx, config, callbacks) {
    this._aborted = false;

    const deliver = (content) => {
      if (this._aborted) return;
      const t = performance.now();
      callbacks.onSuccess({ data: content, url: ctx.url },
        { trequest: t, tfirst: t, tload: t }, ctx);
    };

    if (ctx.type === 'manifest') {
      if (_ws.master !== null) { setTimeout(() => deliver(_ws.master), 0); return; }
      _ws.masterWaiters.push(deliver);
      return;
    }

    if (ctx.type === 'level') {
      const variant = ctx.url.includes('_transcode') ? 'low' : 'high';
      const { msn, part } = _parseMsn(ctx.url);
      const latest = _ws.levels[variant];
      if (latest !== undefined && _hlsSatisfies(latest, msn, part)) {
        setTimeout(() => deliver(latest), 0);
        return;
      }
      const timer = setTimeout(() => {
        this._removePending();
        callbacks.onTimeout({}, { trequest: performance.now(), tfirst: 0, tload: 0 }, ctx);
      }, config.timeout || 10000);
      this._pending = { variant, msn, part, resolve: deliver, timer };
      if (!_ws.levelWaiters[variant]) _ws.levelWaiters[variant] = [];
      _ws.levelWaiters[variant].push(this._pending);
      return;
    }
  }

  _removePending() {
    if (!this._pending) return;
    const list = _ws.levelWaiters[this._pending.variant] || [];
    const idx = list.indexOf(this._pending);
    if (idx >= 0) list.splice(idx, 1);
    this._pending = null;
  }

  abort() {
    this._aborted = true;
    if (this._pending) { clearTimeout(this._pending.timer); this._removePending(); }
  }

  destroy() { this.abort(); }
}

// ─── プレイヤー制御 ───────────────────────────────────────────────────────────
let _hls     = null;
let _hlsUp   = false;
let _retryTm = null;
let _pollTm  = null;

function _msg(text) { document.getElementById('msg').style.display = text ? '' : 'none';
                      document.getElementById('msg').textContent   = text || ''; }

function _startHls() {
  if (_hlsUp) return;
  _hlsUp = true;
  clearInterval(_pollTm); _pollTm = null;
  _msg(null);

  const video = document.getElementById('v');
  if (_hls) _hls.destroy();
  _hls = new Hls({
    lowLatencyMode:             true,
    backBufferLength:           2,
    maxBufferLength:            4,
    liveSyncDurationCount:      1,
    liveMaxLatencyDurationCount: 3,
    liveDurationInfinity:       true,
    pLoader:                    WsPlaylistLoader,
  });
  _hls.loadSource(`/hls/live/${KEY}/master.m3u8`);
  _hls.attachMedia(video);
  _hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
  _hls.on(Hls.Events.ERROR, (_, d) => {
    if (d.fatal) { _hls.destroy(); _hls = null; _hlsUp = false; _reconnect(); }
  });
}

function _resetWsState() {
  _ws.master = null;
  _ws.masterWaiters.length = 0;
  _ws.levels = {};
  for (const list of Object.values(_ws.levelWaiters)) {
    for (const r of list.splice(0)) clearTimeout(r.timer);
  }
}

function _reconnect() {
  if (_hls) { _hls.destroy(); _hls = null; _hlsUp = false; }
  _resetWsState();
  clearTimeout(_retryTm);
  _retryTm = setTimeout(_initWs, 3000);
}

// ─── WebSocket 接続 ───────────────────────────────────────────────────────────
async function _initWs() {
  let token;
  try {
    const r = await fetch(`/api/token/${encodeURIComponent(KEY)}`);
    if (!r.ok) throw new Error();
    token = (await r.json()).token;
  } catch { _msg('接続中...'); _reconnect(); return; }

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(
    `${proto}//${location.host}/ws/hls/${encodeURIComponent(KEY)}?token=${encodeURIComponent(token)}`
  );
  _ws.conn = ws;

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'ping') return;
    if (msg.type === 'master') {
      _ws.master = msg.content;
      for (const fn of _ws.masterWaiters.splice(0)) fn(msg.content);
      if (!_hlsUp) _startHls();
    }
    if (msg.type === 'level') _deliverLevel(msg.variant, msg.content);
  };

  ws.onclose = () => { _ws.conn = null; _reconnect(); };
  ws.onerror = () => ws.close();
}

// ─── 起動 ────────────────────────────────────────────────────────────────────
(async () => {
  async function isActive() {
    try {
      const r = await fetch(`/api/stream/${encodeURIComponent(KEY)}`);
      return r.ok && (await r.json()).active;
    } catch { return false; }
  }

  if (await isActive()) {
    _initWs();
  } else {
    _msg('配信待機中...');
    _pollTm = setInterval(async () => {
      if (await isActive()) { clearInterval(_pollTm); _pollTm = null; _initWs(); }
    }, 3000);
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
