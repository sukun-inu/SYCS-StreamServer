"""
SYCS Stream Server - ABR fmp4 LL-HLS 配信ポータル

ポータル (/)         : ストリーマー向け管理画面
視聴ページ (/watch)  : WebSocket 経由でプレイリストを受信 (m3u8 URL 非公開)
HLS 配信 (/hls)      : VRChat 向け通常 HTTP 配信 (sid セッション管理)
セグメント (/seg)    : HMAC 署名付き短命 URL でのセグメント配信
WebSocket (/ws)      : RTMP URL・セッション数プッシュ
WebSocket (/ws/hls)  : LL-HLS プレイリストをリアルタイムプッシュ
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response

from config import (
    HLS_DIR,
    KEY_RE,
    MEDIA_TYPES,
    NO_CACHE_HEADERS,
    SEGMENT_TTL,
    TEMPLATE_DIR,
    TOKEN_TTL,
)
from hls_service import HlsService
from ngrok_service import NgrokService
from sessions import SessionManager, TokenStore


ngrok = NgrokService()
hls = HlsService()
sessions = SessionManager()
tokens = TokenStore()
portal_clients: set[WebSocket] = set()


async def _broadcast(msg: dict) -> None:
    if not portal_clients:
        return
    text = json.dumps(msg, ensure_ascii=False)
    dead: set[WebSocket] = set()
    for ws in list(portal_clients):
        try:
            await ws.send_text(text)
        except Exception:
            dead.add(ws)
    portal_clients.difference_update(dead)


async def _current_status() -> dict:
    urls = await ngrok.urls()
    return {
        "rtmp": urls.get("rtmp", ""),
        "rtmp_base": urls.get("rtmp_base", ""),
        "rtmp_local": urls.get("rtmp_local", ""),
        "rtmp_local_base": urls.get("rtmp_local_base", ""),
        "site": urls.get("site", ""),
        "sessions": {"active": sessions.active, "max": sessions.max_sessions},
    }


async def _broadcast_current_status() -> None:
    await _broadcast(await _current_status())


async def _ws_broadcaster() -> None:
    prev: dict = {}
    while True:
        await asyncio.sleep(5.0)
        if not portal_clients:
            continue
        state = await _current_status()
        if state != prev:
            prev = state.copy()
            await _broadcast(state)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await hls.start()
    tasks = [
        asyncio.create_task(hls.watch_files()),
        asyncio.create_task(hls.poll_changes()),
        asyncio.create_task(sessions.cleanup(_broadcast_current_status)),
        asyncio.create_task(tokens.cleanup()),
        asyncio.create_task(_ws_broadcaster()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await hls.close()


app = FastAPI(title="SYCS Stream Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/stream/{key}")
async def stream_info(key: str):
    if not KEY_RE.match(key):
        raise HTTPException(400, "無効なストリームキーです。")
    variants = await hls.playlist_state(key)
    return {
        "key": key,
        "active": variants["high"]["ready"],
        "hls_url": f"/hls/live/{key}/master.m3u8",
        "variants": variants,
    }


@app.get("/api/debug/stream/{key}")
async def stream_debug(key: str):
    if not KEY_RE.match(key):
        raise HTTPException(400, "無効なストリームキーです。")
    return await hls.debug_stream(key)


@app.get("/api/ngrok")
async def ngrok_info():
    return await ngrok.urls()


@app.get("/api/status")
async def server_status():
    return {
        "active": sessions.active,
        "max": sessions.max_sessions,
        "available": max(0, sessions.max_sessions - sessions.active),
    }


@app.get("/api/token/{key}")
async def create_stream_token(key: str):
    if not KEY_RE.match(key):
        raise HTTPException(400, "無効なストリームキーです。")
    return {"token": tokens.create(key), "ttl": TOKEN_TTL}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    portal_clients.add(ws)
    try:
        await ws.send_text(json.dumps(await _current_status(), ensure_ascii=False))
        while True:
            await asyncio.sleep(30)
            await ws.send_text(json.dumps({"ping": True}))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        portal_clients.discard(ws)


@app.websocket("/ws/hls/{key}")
async def ws_hls(ws: WebSocket, key: str, token: str = Query(...)):
    if not KEY_RE.match(key):
        await ws.close(1008, "Invalid key")
        return
    if not tokens.consume(token, key):
        await ws.close(1008, "Invalid or expired token")
        return

    try:
        sid = await sessions.acquire(None)
    except HTTPException:
        await ws.close(1013, "満員です")
        return

    await ws.accept()

    high_rel = f"{key}/stream.m3u8"
    low_rel = f"live/{key}_transcode/stream.m3u8"
    rel_paths = (high_rel, low_rel)
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=30)
    await hls.add_playlist_queue(rel_paths, queue)

    last_raw: dict[str, str] = {}

    async def push_variant(variant: str, rel_path: str) -> bool:
        playlist = await hls.read_media_playlist(rel_path)
        raw = playlist.content if playlist is not None else None
        if raw is None or last_raw.get(variant) == raw:
            return False
        last_raw[variant] = raw
        content = hls.patch_playlist(raw)
        base_rel_dir = playlist.rel_path.rsplit("/", 1)[0]
        content = hls.sign_playlist_segments(content, base_rel_dir)
        await ws.send_json({"type": "level", "variant": variant, "content": content})
        return True

    try:
        await ws.send_json({"type": "master", "content": hls.build_ws_master_content(key)})
        await push_variant("high", high_rel)
        await push_variant("low", low_rel)

        loop = asyncio.get_running_loop()
        last_ping = loop.time()
        last_renew = loop.time()
        while True:
            try:
                changed_rel = await asyncio.wait_for(queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                changed_rel = None

            now = loop.time()
            if now - last_renew >= 1.0:
                last_renew = now
                if not await sessions.renew(sid):
                    break

            if changed_rel == high_rel:
                await push_variant("high", high_rel)
            elif changed_rel == low_rel:
                await push_variant("low", low_rel)
            else:
                await push_variant("high", high_rel)
                await push_variant("low", low_rel)

            if now - last_ping >= 25.0:
                last_ping = now
                await ws.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        await hls.remove_playlist_queue(rel_paths, queue)
        await sessions.revoke(sid)


@app.get("/seg/{path:path}")
async def serve_segment(
    path: str,
    exp: str = Query(...),
    sig: str = Query(...),
):
    if not hls.verify_segment(path, exp, sig):
        raise HTTPException(403, "URL の有効期限が切れています。再生を再開してください。")

    file_path = HLS_DIR / path
    _ensure_hls_path(file_path)

    media_type = MEDIA_TYPES.get(file_path.suffix, "application/octet-stream")
    headers = {
        "Cache-Control": f"private, max-age={SEGMENT_TTL}",
        "Access-Control-Allow-Origin": "*",
    }

    disk_path = await hls.wait_for_existing_file(path)
    if disk_path is None:
        data = await hls.fetch_mediamtx_hls(path)
        if data is None:
            raise HTTPException(404, "Not found")
        return Response(content=data, media_type=media_type, headers=headers)

    return FileResponse(disk_path, media_type=media_type, headers=headers)


@app.get("/hls/{path:path}")
async def serve_hls(
    path: str,
    sid: str | None = Query(default=None),
    _HLS_msn: int | None = Query(default=None),
    _HLS_part: int | None = Query(default=None),
):
    file_path = HLS_DIR / path
    _ensure_hls_path(file_path)

    is_master = path.endswith("master.m3u8")
    is_playlist = path.endswith(("index.m3u8", "stream.m3u8"))

    master_key = _dynamic_master_key(path)
    if master_key is not None:
        sid = await sessions.acquire(sid)
        content = hls.build_http_master_content(master_key)
        return _playlist_response(hls.inject_sid(content, sid))

    if is_master:
        sid = await sessions.acquire(sid)
    elif is_playlist and sid and not await sessions.renew(sid):
        raise HTTPException(403, "セッション期限切れ。再度アクセスしてください。")

    if path.endswith(".m3u8"):
        params: dict[str, int] = {}
        if _HLS_msn is not None:
            params["_HLS_msn"] = _HLS_msn
        if _HLS_part is not None:
            params["_HLS_part"] = _HLS_part

        playlist_disk_path = hls.find_existing_file(path)
        if is_playlist and playlist_disk_path is not None and _HLS_msn is not None:
            await hls.block_until_ready(path, playlist_disk_path, _HLS_msn, _HLS_part, timeout=5.0)

        playlist = await hls.read_media_playlist(path, params=params or None)
        if playlist is None:
            raise HTTPException(404, "Not found")
        content = playlist.content
        content = hls.patch_playlist(content)
        if sid:
            content = hls.inject_sid(content, sid)
        return _playlist_response(content)

    disk_path = await hls.wait_for_existing_file(path)
    if disk_path is None:
        data = await hls.fetch_mediamtx_hls(path)
        if data is None:
            raise HTTPException(404, "Not found")
        if sid and not sessions.check_and_record_bw(sid, len(data)):
            await sessions.revoke(sid)
            raise HTTPException(429, "帯域超過によりセッションを終了しました。")
        media_type = MEDIA_TYPES.get(file_path.suffix, "application/octet-stream")
        return Response(content=data, media_type=media_type, headers=NO_CACHE_HEADERS)

    if sid:
        try:
            nbytes = disk_path.stat().st_size
        except OSError:
            raise HTTPException(404, "Not found")
        if not sessions.check_and_record_bw(sid, nbytes):
            await sessions.revoke(sid)
            raise HTTPException(429, "帯域超過によりセッションを終了しました。")

    media_type = MEDIA_TYPES.get(disk_path.suffix, "application/octet-stream")
    return FileResponse(disk_path, media_type=media_type, headers=NO_CACHE_HEADERS)


def _ensure_hls_path(file_path) -> None:
    try:
        file_path.resolve().relative_to(HLS_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid path")


def _dynamic_master_key(path: str) -> str | None:
    path_parts = path.split("/")
    if (
        len(path_parts) == 3
        and path_parts[0] == "live"
        and path_parts[2] == "master.m3u8"
        and not path_parts[1].endswith("_transcode")
        and KEY_RE.match(path_parts[1])
    ):
        return path_parts[1]
    return None


def _playlist_response(content: str) -> Response:
    return Response(
        content=content.encode("utf-8"),
        media_type="application/vnd.apple.mpegurl",
        headers=NO_CACHE_HEADERS,
    )


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
    if not KEY_RE.match(key):
        raise HTTPException(400, "無効なストリームキーです。")
    content = _read_template("watch.html").replace("__KEY_JSON__", json.dumps(key))
    return HTMLResponse(content)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )
