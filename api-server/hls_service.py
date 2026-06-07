import asyncio
import hashlib
import hmac
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import aiofiles
import httpx

from config import (
    HLS_DIR,
    MEDIAMTX_HLS_TIMEOUT,
    MEDIAMTX_HLS_URL,
    SEGMENT_SECRET,
    SEGMENT_TTL,
    SEGMENT_WAIT_TIMEOUT,
)


@dataclass(frozen=True)
class PlaylistRead:
    content: str
    rel_path: str
    source: str


class HlsService:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._waiters: dict[str, list[asyncio.Event]] = defaultdict(list)
        self._waiters_lock = asyncio.Lock()
        self._playlist_queues: dict[str, list[asyncio.Queue[str]]] = defaultdict(list)
        self._playlist_queues_lock = asyncio.Lock()

    async def start(self) -> None:
        HLS_DIR.mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(timeout=MEDIAMTX_HLS_TIMEOUT)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def add_playlist_queue(
        self,
        rel_paths: tuple[str, ...],
        queue: asyncio.Queue[str],
    ) -> None:
        async with self._playlist_queues_lock:
            for rel_path in rel_paths:
                self._playlist_queues[rel_path].append(queue)

    async def remove_playlist_queue(
        self,
        rel_paths: tuple[str, ...],
        queue: asyncio.Queue[str],
    ) -> None:
        async with self._playlist_queues_lock:
            for rel_path in rel_paths:
                try:
                    self._playlist_queues[rel_path].remove(queue)
                except ValueError:
                    pass

    async def notify_waiters(self, rel_path: str) -> None:
        aliases = self.rel_path_candidates(rel_path)
        async with self._waiters_lock:
            for alias in aliases:
                for ev in self._waiters.pop(alias, []):
                    ev.set()

        async with self._playlist_queues_lock:
            seen: set[int] = set()
            for alias in aliases:
                for queue in self._playlist_queues.get(alias, []):
                    queue_id = id(queue)
                    if queue_id in seen:
                        continue
                    seen.add(queue_id)
                    try:
                        queue.put_nowait(rel_path)
                    except asyncio.QueueFull:
                        pass

    async def poll_changes(self) -> None:
        mtimes: dict[str, int] = {}
        while True:
            try:
                for fp in HLS_DIR.glob("**/*.m3u8"):
                    if not fp.is_file():
                        continue
                    rel_path = str(fp.relative_to(HLS_DIR))
                    try:
                        mtime = fp.stat().st_mtime_ns
                    except OSError:
                        continue
                    if mtimes.get(rel_path) != mtime:
                        mtimes[rel_path] = mtime
                        await self.notify_waiters(rel_path)
            except Exception:
                pass
            await asyncio.sleep(0.1)

    async def watch_files(self) -> None:
        try:
            from watchfiles import Change, awatch

            async for changes in awatch(HLS_DIR):
                for change_type, change_path in changes:
                    if change_type in (Change.modified, Change.added):
                        rel_path = str(Path(change_path).relative_to(HLS_DIR))
                        await self.notify_waiters(rel_path)
        except ImportError:
            pass
        except Exception:
            pass

    async def fetch_mediamtx_hls(
        self,
        rel_path: str,
        params: dict[str, int] | None = None,
    ) -> bytes | None:
        for candidate in self.rel_path_candidates(rel_path):
            data = await self._fetch_mediamtx_candidate(candidate, params=params)
            if data is not None:
                return data
        return None

    async def _fetch_mediamtx_candidate(
        self,
        rel_path: str,
        params: dict[str, int] | None = None,
    ) -> bytes | None:
        client = self._client
        close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=MEDIAMTX_HLS_TIMEOUT)
            close_client = True
        try:
            resp = await client.get(self._mediamtx_hls_url(rel_path), params=params)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.content
        except Exception:
            return None
        finally:
            if close_client:
                await client.aclose()

    async def read_playlist_content(
        self,
        rel_path: str,
        params: dict[str, int] | None = None,
    ) -> str | None:
        result = await self.read_playlist(rel_path, params=params)
        return result.content if result is not None else None

    async def read_media_playlist(
        self,
        rel_path: str,
        params: dict[str, int] | None = None,
    ) -> PlaylistRead | None:
        return await self.read_playlist(rel_path, params=params, media_only=True)

    async def read_playlist(
        self,
        rel_path: str,
        params: dict[str, int] | None = None,
        media_only: bool = False,
    ) -> PlaylistRead | None:
        candidates = self.playlist_rel_path_candidates(rel_path)
        if params is None:
            for candidate in candidates:
                file_path = self.find_existing_file(candidate, exact=True)
                if file_path is None:
                    continue
                try:
                    async with aiofiles.open(file_path, encoding="utf-8") as f:
                        content = await f.read()
                    if not media_only or self._is_media_playlist(content):
                        return PlaylistRead(content, candidate, "disk")
                except OSError:
                    pass

        for candidate in candidates:
            data = await self._fetch_mediamtx_candidate(candidate, params=params)
            if data is None:
                continue
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if not media_only or self._is_media_playlist(content):
                return PlaylistRead(content, candidate, "mediamtx")
        return None

    async def block_until_ready(
        self,
        rel_path: str,
        file_path: Path,
        msn: int,
        part: int | None,
        timeout: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if file_path.exists():
                try:
                    async with aiofiles.open(file_path, encoding="utf-8") as f:
                        content = await f.read()
                    if self.playlist_satisfies(content, msn, part):
                        return
                except OSError:
                    pass

            remaining = deadline - loop.time()
            if remaining <= 0:
                return

            ev = asyncio.Event()
            async with self._waiters_lock:
                self._waiters[rel_path].append(ev)
            try:
                await asyncio.wait_for(ev.wait(), timeout=min(remaining, 0.15))
            except asyncio.TimeoutError:
                async with self._waiters_lock:
                    try:
                        self._waiters[rel_path].remove(ev)
                    except ValueError:
                        pass

    async def wait_for_file(
        self,
        file_path: Path,
        timeout: float = SEGMENT_WAIT_TIMEOUT,
    ) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if file_path.exists():
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(remaining, 0.05))

    def find_existing_file(self, rel_path: str, exact: bool = False) -> Path | None:
        candidates = [rel_path] if exact else self.rel_path_candidates(rel_path)
        for candidate in candidates:
            file_path = HLS_DIR / candidate
            try:
                file_path.resolve().relative_to(HLS_DIR.resolve())
            except ValueError:
                continue
            if file_path.exists():
                return file_path
        return None

    async def wait_for_existing_file(
        self,
        rel_path: str,
        timeout: float = SEGMENT_WAIT_TIMEOUT,
    ) -> Path | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            file_path = self.find_existing_file(rel_path)
            if file_path is not None:
                return file_path
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(remaining, 0.05))

    def playlist_satisfies(self, content: str, msn: int, part: int | None) -> bool:
        match = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", content)
        if not match:
            return False
        base = int(match.group(1))
        segs = content.count("#EXTINF:")
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

    async def playlist_state(self, key: str) -> dict[str, dict]:
        variants = {
            "high": f"live/{key}/stream.m3u8",
            "low": f"live/{key}_transcode/stream.m3u8",
        }
        state: dict[str, dict] = {}
        now = time.time()
        for name, rel_path in variants.items():
            path = self.find_existing_file(rel_path)
            if path is not None:
                st = path.stat()
                state[name] = {
                    "ready": True,
                    "source": "disk",
                    "path": str(path.relative_to(HLS_DIR)),
                    "age_sec": max(0.0, round(now - st.st_mtime, 3)),
                    "bytes": st.st_size,
                }
            else:
                playlist = await self.read_media_playlist(rel_path)
                content = playlist.content if playlist is not None else None
                state[name] = {
                    "ready": content is not None,
                    "source": playlist.source if playlist is not None else "missing",
                    "path": playlist.rel_path if playlist is not None else "",
                    "bytes": len(content.encode("utf-8")) if content is not None else 0,
                }
        return state

    def patch_playlist(self, content: str) -> str:
        if not content.strip() or "#EXTM3U" not in content:
            return content
        has_pdt = "EXT-X-PROGRAM-DATE-TIME" in content
        part_target = self._playlist_float_tag(
            content,
            r"#EXT-X-PART-INF:[^\n]*\bPART-TARGET=([\d.]+)",
        )
        target_duration = self._playlist_float_tag(
            content,
            r"#EXT-X-TARGETDURATION:([\d.]+)",
        )
        out: list[str] = []
        pdt_inserted = False
        for line in content.splitlines(keepends=True):
            tag = line.rstrip("\r\n")
            if tag.startswith("#EXT-X-SERVER-CONTROL:"):
                line = self._shrink_hold_back(tag, part_target, target_duration) + "\n"
            if not has_pdt and not pdt_inserted and tag.startswith("#EXTINF:"):
                pdt = self._estimate_first_segment_pdt(content)
                if pdt:
                    out.append(f"#EXT-X-PROGRAM-DATE-TIME:{pdt}\n")
                pdt_inserted = True
            out.append(line)
        return "".join(out)

    def inject_sid(self, content: str, sid: str) -> str:
        content = re.sub(r'(URI="[^"?]+)"', rf'\1?sid={sid}"', content)
        out: list[str] = []
        for line in content.splitlines(keepends=True):
            tag = line.rstrip("\r\n")
            if tag and not tag.startswith("#") and "?" not in tag:
                out.append(f"{tag}?sid={sid}\n")
            else:
                out.append(line)
        return "".join(out)

    def sign_segment_url(self, rel_path: str) -> str:
        exp = int(time.time()) + SEGMENT_TTL
        mac = hmac.new(SEGMENT_SECRET, f"{rel_path}:{exp}".encode(), hashlib.sha256)
        sig = mac.hexdigest()[:16]
        return f"/seg/{rel_path}?exp={exp}&sig={sig}"

    def verify_segment(self, rel_path: str, exp: str, sig: str) -> bool:
        try:
            if int(exp) < time.time():
                return False
            mac = hmac.new(SEGMENT_SECRET, f"{rel_path}:{exp}".encode(), hashlib.sha256)
            return hmac.compare_digest(mac.hexdigest()[:16], sig)
        except Exception:
            return False

    def sign_playlist_segments(self, content: str, base_rel_dir: str) -> str:
        """Rewrite playlist segment references to short-lived signed /seg URLs."""
        base_rel_dir = base_rel_dir.strip("/")

        def sign_uri(match: re.Match) -> str:
            uri = match.group(1)
            rel_path = self.segment_rel_path(base_rel_dir, uri)
            if rel_path is None:
                return match.group(0)
            return f'URI="{self.sign_segment_url(rel_path)}"'

        out: list[str] = []
        for line in content.splitlines(keepends=True):
            tag = line.rstrip("\r\n")
            if tag.startswith(("#EXT-X-MAP:", "#EXT-X-PART:", "#EXT-X-PRELOAD-HINT:")):
                out.append(re.sub(r'URI="([^"]+)"', sign_uri, line))
                continue
            if tag and not tag.startswith("#") and not tag.startswith(("/", "http")):
                rel_path = self.segment_rel_path(base_rel_dir, tag)
                out.append((self.sign_segment_url(rel_path) if rel_path else tag) + "\n")
            else:
                out.append(line)
        return "".join(out)

    def build_ws_master_content(self, key: str) -> str:
        return (
            "#EXTM3U\n"
            "#EXT-X-VERSION:6\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,NAME=high\n"
            f"/hls/live/{key}/stream.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,NAME=low\n"
            f"/hls/live/{key}_transcode/stream.m3u8\n"
        )

    def build_http_master_content(self, key: str) -> str:
        return (
            "#EXTM3U\n"
            "#EXT-X-VERSION:6\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,NAME=high\n"
            "stream.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,NAME=low\n"
            f"../{key}_transcode/stream.m3u8\n"
        )

    def rel_path_candidates(self, rel_path: str) -> list[str]:
        rel_path = self._normalize_rel_path(rel_path)
        if not rel_path:
            return []

        candidates = [rel_path]
        if rel_path.startswith("live/"):
            candidates.append(rel_path.removeprefix("live/"))
        else:
            candidates.append(f"live/{rel_path}")
        return self._unique_safe_rel_paths(candidates)

    def playlist_rel_path_candidates(self, rel_path: str) -> list[str]:
        rel_path = self._normalize_rel_path(rel_path)
        names = [rel_path]
        if rel_path.endswith("/stream.m3u8"):
            names.append(rel_path.removesuffix("stream.m3u8") + "index.m3u8")
        elif rel_path.endswith("/index.m3u8"):
            names.append(rel_path.removesuffix("index.m3u8") + "stream.m3u8")

        candidates: list[str] = []
        for name in names:
            candidates.extend(self.rel_path_candidates(name))
        return self._unique_safe_rel_paths(candidates)

    def segment_rel_path(self, base_rel_dir: str, uri: str) -> str | None:
        uri_path = uri.split("?", 1)[0].split("#", 1)[0].strip()
        if not uri_path or uri_path.startswith(("http://", "https://", "/", "../")):
            return None
        while uri_path.startswith("./"):
            uri_path = uri_path[2:]
        return self._normalize_rel_path(f"{base_rel_dir}/{uri_path}")

    def _mediamtx_hls_url(self, rel_path: str) -> str:
        safe_path = quote(rel_path.lstrip("/"), safe="/-_.~")
        return f"{MEDIAMTX_HLS_URL}/{safe_path}"

    def _normalize_rel_path(self, rel_path: str) -> str:
        return rel_path.replace("\\", "/").lstrip("/")

    def _unique_safe_rel_paths(self, rel_paths: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for rel_path in rel_paths:
            rel_path = self._normalize_rel_path(rel_path)
            if not rel_path or any(part == ".." for part in rel_path.split("/")):
                continue
            if rel_path not in seen:
                seen.add(rel_path)
                unique.append(rel_path)
        return unique

    def _is_media_playlist(self, content: str) -> bool:
        return "#EXT-X-MEDIA-SEQUENCE:" in content or "#EXTINF:" in content

    def _playlist_float_tag(self, content: str, pattern: str) -> float | None:
        match = re.search(pattern, content)
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    def _shrink_hold_back(
        self,
        line: str,
        part_target: float | None,
        target_duration: float | None,
    ) -> str:
        def part_repl(match: re.Match) -> str:
            current = float(match.group(1))
            lower = (part_target or 0.1) * 3
            return f"PART-HOLD-BACK={max(lower, current * 0.6):.3f}"

        def hold_repl(match: re.Match) -> str:
            current = float(match.group(1))
            lower = (target_duration or 1.0) * 3
            return f"HOLD-BACK={max(lower, current * 0.6):.3f}"

        line = re.sub(r"\bPART-HOLD-BACK=([\d.]+)", part_repl, line)
        return re.sub(r"(?<!PART-)\bHOLD-BACK=([\d.]+)", hold_repl, line)

    def _estimate_first_segment_pdt(self, content: str) -> str | None:
        durations = [float(m.group(1)) for m in re.finditer(r"#EXTINF:([\d.]+)", content)]
        if not durations:
            return None
        first = datetime.now(timezone.utc) - timedelta(seconds=sum(durations))
        return first.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
