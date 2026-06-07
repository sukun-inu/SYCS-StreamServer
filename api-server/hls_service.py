import asyncio
import hashlib
import hmac
import posixpath
import re
import time
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import aiofiles
import httpx

from config import (
    HLS_CHANGE_POLL_INTERVAL,
    HLS_DIR,
    HLS_MEDIA_CACHE_MAX_BYTES,
    HLS_MEDIA_CACHE_TTL,
    HLS_MISSING_CACHE_TTL,
    HLS_PART_HOLD_BACK_PARTS,
    MEDIAMTX_API_URLS,
    MEDIAMTX_HLS_TIMEOUT,
    MEDIAMTX_HLS_URLS,
    SEGMENT_SECRET,
    SEGMENT_TTL,
    SEGMENT_WAIT_TIMEOUT,
)


@dataclass(frozen=True)
class PlaylistRead:
    content: str
    rel_path: str
    source: str
    url: str = ""


@dataclass(frozen=True)
class MediaFetch:
    content: bytes
    rel_path: str
    base_url: str


@dataclass(frozen=True)
class MediaCacheEntry:
    content: bytes
    expires_at: float


class HlsService:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._waiters: dict[str, list[asyncio.Event]] = defaultdict(list)
        self._waiters_lock = asyncio.Lock()
        self._playlist_queues: dict[str, list[asyncio.Queue[str]]] = defaultdict(list)
        self._playlist_queues_lock = asyncio.Lock()
        self._playlist_resolutions: dict[str, str] = {}
        self._missing_until: dict[tuple[str, bool], float] = {}
        self._media_cache: OrderedDict[str, MediaCacheEntry] = OrderedDict()
        self._media_cache_bytes = 0

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
            await asyncio.sleep(HLS_CHANGE_POLL_INTERVAL)

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
        cache_key = self._normalize_rel_path(rel_path)
        if params is None and self._media_cacheable(cache_key):
            cached = self._media_cache_get(cache_key)
            if cached is not None:
                return cached

        fetched = await self._fetch_first_mediamtx(self.rel_path_candidates(rel_path), params=params)
        if (
            fetched is not None
            and params is None
            and self._media_cacheable(cache_key)
        ):
            self._media_cache_put(cache_key, fetched.content)
        return fetched.content if fetched is not None else None

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
        normalized_rel_path = self._normalize_rel_path(rel_path)
        cache_key = (normalized_rel_path, media_only)
        if params is None and self._missing_until.get(cache_key, 0.0) > time.monotonic():
            return None

        candidates = self._ordered_playlist_candidates(normalized_rel_path)

        if params is not None and normalized_rel_path in self._playlist_resolutions:
            candidates = [self._playlist_resolutions[normalized_rel_path]]

        fetched = await self._fetch_playlist_mediamtx(
            candidates,
            params=params,
            media_only=media_only,
        )
        if fetched is not None:
            try:
                content = fetched.content.decode("utf-8")
            except UnicodeDecodeError:
                content = ""
            if content and (not media_only or self._is_media_playlist(content)):
                self._playlist_resolutions[normalized_rel_path] = fetched.rel_path
                self._missing_until.pop(cache_key, None)
                return PlaylistRead(
                    content,
                    fetched.rel_path,
                    "mediamtx",
                    self._mediamtx_hls_url(fetched.base_url, fetched.rel_path),
                )

        if params is None:
            for candidate in candidates:
                file_path = self.find_existing_file(candidate, exact=True)
                if file_path is None:
                    continue
                try:
                    async with aiofiles.open(file_path, encoding="utf-8") as f:
                        content = await f.read()
                    if not media_only or self._is_media_playlist(content):
                        self._playlist_resolutions[normalized_rel_path] = candidate
                        self._missing_until.pop(cache_key, None)
                        return PlaylistRead(content, candidate, "disk", str(file_path))
                except OSError:
                    pass

        if params is None:
            self._missing_until[cache_key] = time.monotonic() + HLS_MISSING_CACHE_TTL
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
            "high": f"{key}/index.m3u8",
            "low": f"live/{key}_transcode/index.m3u8",
        }

        async def variant_state(name: str, rel_path: str) -> tuple[str, dict]:
            path = self.find_existing_file(rel_path)
            if path is not None:
                st = path.stat()
                return name, {
                    "ready": True,
                    "source": "disk",
                    "path": str(path.relative_to(HLS_DIR)),
                    "age_sec": max(0.0, round(now - st.st_mtime, 3)),
                    "bytes": st.st_size,
                }

            playlist = await self.read_media_playlist(rel_path)
            content = playlist.content if playlist is not None else None
            return name, {
                "ready": content is not None,
                "source": playlist.source if playlist is not None else "missing",
                "path": playlist.rel_path if playlist is not None else "",
                "checked": self.playlist_rel_path_candidates(rel_path) if playlist is None else [],
                "bytes": len(content.encode("utf-8")) if content is not None else 0,
            }

        now = time.time()
        pairs = await asyncio.gather(*(variant_state(name, rel_path) for name, rel_path in variants.items()))
        return dict(pairs)

    async def debug_stream(self, key: str) -> dict:
        high_rel = f"{key}/index.m3u8"
        low_rel = f"live/{key}_transcode/index.m3u8"
        high_candidates = self.playlist_rel_path_candidates(high_rel)
        low_candidates = self.playlist_rel_path_candidates(low_rel)

        high_probe, low_probe, api_probe, high_resolved, low_resolved = await asyncio.gather(
            self._probe_playlist_candidates(high_candidates),
            self._probe_playlist_candidates(low_candidates),
            self._probe_mediamtx_api_paths(),
            self.read_media_playlist(high_rel),
            self.read_media_playlist(low_rel),
        )

        return {
            "key": key,
            "hls_dir": self._debug_hls_dir(high_candidates + low_candidates),
            "candidates": {
                "high": high_candidates,
                "low": low_candidates,
            },
            "mediamtx_hls": {
                "base_urls": MEDIAMTX_HLS_URLS,
                "resolved": {
                    "high": self._debug_playlist_read(high_resolved),
                    "low": self._debug_playlist_read(low_resolved),
                },
                "high": high_probe,
                "low": low_probe,
            },
            "mediamtx_api": {
                "base_urls": MEDIAMTX_API_URLS,
                "paths": api_probe,
            },
        }

    def _debug_playlist_read(self, playlist: PlaylistRead | None) -> dict:
        if playlist is None:
            return {"ready": False}
        return {
            "ready": True,
            "source": playlist.source,
            "rel_path": playlist.rel_path,
            "url": playlist.url,
            "bytes": len(playlist.content.encode("utf-8")),
            "media_sequence": self._playlist_int_tag(playlist.content, r"#EXT-X-MEDIA-SEQUENCE:(\d+)"),
        }

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
                line = self._stabilize_server_control(tag, part_target, target_duration) + "\n"
            if not has_pdt and not pdt_inserted and tag.startswith("#EXTINF:"):
                pdt = self._estimate_first_segment_pdt(content)
                if pdt:
                    out.append(f"#EXT-X-PROGRAM-DATE-TIME:{pdt}\n")
                pdt_inserted = True
            out.append(line)
        return "".join(out)

    def inject_sid(self, content: str, sid: str) -> str:
        def with_sid(uri: str) -> str:
            if uri.startswith(("http://", "https://")) or re.search(r"([?&])sid=", uri):
                return uri
            sep = "&" if "?" in uri else "?"
            return f"{uri}{sep}sid={sid}"

        content = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{with_sid(m.group(1))}"', content)
        out: list[str] = []
        for line in content.splitlines(keepends=True):
            tag = line.rstrip("\r\n")
            if tag and not tag.startswith("#"):
                out.append(with_sid(tag) + "\n")
            else:
                out.append(line)
        return "".join(out)

    def sign_segment_url(self, rel_path: str, source_query: str = "") -> str:
        exp = int(time.time()) + SEGMENT_TTL
        source_query = source_query.lstrip("?")
        mac = hmac.new(
            SEGMENT_SECRET,
            self._segment_sig_payload(rel_path, exp, source_query).encode(),
            hashlib.sha256,
        )
        sig = mac.hexdigest()[:16]
        url = f"/seg/{rel_path}?exp={exp}&sig={sig}"
        if source_query:
            url += f"&src={quote(source_query, safe='-_.~')}"
        return url

    def verify_segment(self, rel_path: str, exp: str, sig: str, source_query: str = "") -> bool:
        try:
            exp_int = int(exp)
            if exp_int < time.time():
                return False
            source_query = source_query.lstrip("?")
            mac = hmac.new(
                SEGMENT_SECRET,
                self._segment_sig_payload(rel_path, exp_int, source_query).encode(),
                hashlib.sha256,
            )
            return hmac.compare_digest(mac.hexdigest()[:16], sig)
        except Exception:
            return False

    def sign_playlist_segments(self, content: str, base_rel_dir: str) -> str:
        """Rewrite playlist segment references to short-lived signed /seg URLs."""
        base_rel_dir = base_rel_dir.strip("/")

        def sign_uri(match: re.Match) -> str:
            uri = match.group(1)
            segment = self.segment_rel_url(base_rel_dir, uri)
            if segment is None:
                return match.group(0)
            rel_path, source_query = segment
            return f'URI="{self.sign_segment_url(rel_path, source_query)}"'

        out: list[str] = []
        for line in content.splitlines(keepends=True):
            tag = line.rstrip("\r\n")
            if tag.startswith(("#EXT-X-MAP:", "#EXT-X-PART:", "#EXT-X-PRELOAD-HINT:")):
                out.append(re.sub(r'URI="([^"]+)"', sign_uri, line))
                continue
            if tag and not tag.startswith("#") and not tag.startswith(("/", "http")):
                segment = self.segment_rel_url(base_rel_dir, tag)
                if segment is None:
                    out.append(tag + "\n")
                else:
                    rel_path, source_query = segment
                    out.append(self.sign_segment_url(rel_path, source_query) + "\n")
            else:
                out.append(line)
        return "".join(out)

    def build_ws_master_content(self, key: str, include_high: bool = True, include_low: bool = True) -> str:
        return self._build_master_content(
            high_uri=f"/hls/live/{key}/index.m3u8",
            low_uri=f"/hls/live/{key}_transcode/index.m3u8",
            include_high=include_high,
            include_low=include_low,
        )

    def build_http_master_content(self, key: str, include_high: bool = True, include_low: bool = True) -> str:
        return self._build_master_content(
            high_uri="index.m3u8",
            low_uri=f"../{key}_transcode/index.m3u8",
            include_high=include_high,
            include_low=include_low,
        )

    def _build_master_content(
        self,
        high_uri: str,
        low_uri: str,
        include_high: bool,
        include_low: bool,
    ) -> str:
        if not include_high and not include_low:
            include_high = True
        lines = ["#EXTM3U", "#EXT-X-VERSION:6"]
        if include_low:
            lines.extend(
                [
                    "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,NAME=low",
                    low_uri,
                ]
            )
        if include_high:
            lines.extend(
                [
                    "#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,NAME=high",
                    high_uri,
                ]
            )
        return "\n".join(lines) + "\n"

    def rel_path_candidates(self, rel_path: str) -> list[str]:
        rel_path = self._normalize_rel_path(rel_path)
        if not rel_path:
            return []

        candidates = [rel_path]
        if rel_path.startswith("live/"):
            stripped = rel_path.removeprefix("live/")
            path_name = stripped.split("/", 1)[0]
            if path_name.endswith("_transcode"):
                candidates.append(stripped)
            else:
                candidates = [stripped, rel_path]
        else:
            candidates.append(f"live/{rel_path}")
        return self._unique_safe_rel_paths(candidates)

    def playlist_rel_path_candidates(self, rel_path: str) -> list[str]:
        rel_path = self._normalize_rel_path(rel_path)
        if rel_path.endswith("/stream.m3u8"):
            names = [rel_path.removesuffix("stream.m3u8") + "index.m3u8", rel_path]
        elif rel_path.endswith("/index.m3u8"):
            names = [rel_path, rel_path.removesuffix("index.m3u8") + "stream.m3u8"]
        else:
            names = [rel_path]

        candidates: list[str] = []
        for name in names:
            candidates.extend(self.rel_path_candidates(name))
        return self._unique_safe_rel_paths(candidates)

    def _ordered_playlist_candidates(self, rel_path: str) -> list[str]:
        candidates = self.playlist_rel_path_candidates(rel_path)
        preferred = self._playlist_resolutions.get(rel_path)
        if not preferred:
            return candidates
        return self._unique_safe_rel_paths([preferred, *candidates])

    def segment_rel_path(self, base_rel_dir: str, uri: str) -> str | None:
        segment = self.segment_rel_url(base_rel_dir, uri)
        return segment[0] if segment is not None else None

    def segment_rel_url(self, base_rel_dir: str, uri: str) -> tuple[str, str] | None:
        uri = uri.split("#", 1)[0].strip()
        if not uri or uri.startswith(("http://", "https://", "/", "../")):
            return None
        while uri.startswith("./"):
            uri = uri[2:]
        uri_path, source_query = self._split_rel_url(uri)
        rel_path = self._normalize_rel_path(posixpath.normpath(f"{base_rel_dir}/{uri_path}"))
        if not rel_path or rel_path.startswith("../") or "/../" in rel_path:
            return None
        return rel_path, source_query

    async def _fetch_first_mediamtx(
        self,
        rel_paths: list[str],
        params: dict[str, int] | None = None,
        content_filter: Callable[[bytes], bool] | None = None,
    ) -> MediaFetch | None:
        rel_paths = self._unique_safe_rel_paths(rel_paths)
        if not rel_paths:
            return None

        client = self._client
        close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=MEDIAMTX_HLS_TIMEOUT)
            close_client = True

        tasks = [
            asyncio.create_task(self._fetch_mediamtx_url(client, base_url, rel_path, params=params))
            for rel_path in rel_paths
            for base_url in MEDIAMTX_HLS_URLS
        ]
        try:
            for task in asyncio.as_completed(tasks):
                fetched = await task
                if fetched is not None and (content_filter is None or content_filter(fetched.content)):
                    for pending in tasks:
                        if not pending.done():
                            pending.cancel()
                    return fetched
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if close_client:
                await client.aclose()
        return None

    async def _fetch_playlist_mediamtx(
        self,
        rel_paths: list[str],
        params: dict[str, int] | None = None,
        media_only: bool = False,
    ) -> MediaFetch | None:
        pending = self._unique_safe_rel_paths(rel_paths)
        seen: set[str] = set()

        for _depth in range(3):
            batch = [rel_path for rel_path in pending if rel_path not in seen]
            if not batch:
                return None
            seen.update(batch)

            child_candidates: list[str] = []
            for rel_path in batch:
                fetched = await self._fetch_first_mediamtx(
                    [rel_path],
                    params=params,
                    content_filter=lambda data: self._playlist_bytes_match(data, media_only=False),
                )
                if fetched is None:
                    continue
                try:
                    content = fetched.content.decode("utf-8")
                except UnicodeDecodeError:
                    continue

                if not media_only or self._is_media_playlist(content):
                    return fetched
                child_candidates.extend(self._playlist_child_candidates(fetched.rel_path, content))

            pending = self._unique_safe_rel_paths(child_candidates)
        return None

    async def _fetch_mediamtx_url(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        rel_path: str,
        params: dict[str, int] | None = None,
    ) -> MediaFetch | None:
        try:
            resp = await client.get(
                self._mediamtx_hls_url(base_url, rel_path),
                params=params,
                follow_redirects=True,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return MediaFetch(resp.content, rel_path, base_url)
        except Exception:
            return None

    async def _probe_playlist_candidates(self, rel_paths: list[str]) -> list[dict]:
        client = self._client
        close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=MEDIAMTX_HLS_TIMEOUT)
            close_client = True

        tasks = [
            asyncio.create_task(self._probe_mediamtx_url(client, base_url, rel_path))
            for rel_path in self._unique_safe_rel_paths(rel_paths)
            for base_url in MEDIAMTX_HLS_URLS
        ]
        try:
            return await asyncio.gather(*tasks)
        finally:
            if close_client:
                await client.aclose()

    async def _probe_mediamtx_url(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        rel_path: str,
    ) -> dict:
        url = self._mediamtx_hls_url(base_url, rel_path)
        result = {"base_url": base_url, "rel_path": rel_path, "url": url}
        try:
            resp = await client.get(url, follow_redirects=True)
            text = resp.text[:240] if resp.headers.get("content-type", "").startswith(("application", "text")) else ""
            result.update(
                {
                    "status": resp.status_code,
                    "bytes": len(resp.content),
                    "media_playlist": self._playlist_bytes_match(resp.content, media_only=True),
                    "content_type": resp.headers.get("content-type", ""),
                    "head": text,
                }
            )
        except Exception as exc:
            result.update({"status": 0, "error": type(exc).__name__})
        return result

    async def _probe_mediamtx_api_paths(self) -> list[dict]:
        results: list[dict] = []
        async with httpx.AsyncClient(timeout=MEDIAMTX_HLS_TIMEOUT) as client:
            for base_url in MEDIAMTX_API_URLS:
                url = f"{base_url.rstrip('/')}/v3/paths/list"
                result = {"base_url": base_url, "url": url}
                try:
                    resp = await client.get(url)
                    result["status"] = resp.status_code
                    if resp.headers.get("content-type", "").startswith("application/json"):
                        data = resp.json()
                        result["names"] = self._extract_names(data)[:50]
                        result["raw_keys"] = list(data.keys()) if isinstance(data, dict) else []
                    else:
                        result["head"] = resp.text[:240]
                except Exception as exc:
                    result.update({"status": 0, "error": type(exc).__name__})
                results.append(result)
        return results

    def _debug_hls_dir(self, candidates: list[str]) -> dict:
        root = HLS_DIR
        result: dict = {
            "path": str(root),
            "exists": root.exists(),
            "is_dir": root.is_dir(),
        }
        try:
            st = root.stat()
            result.update(
                {
                    "mode": oct(st.st_mode & 0o777),
                    "uid": getattr(st, "st_uid", None),
                    "gid": getattr(st, "st_gid", None),
                }
            )
        except OSError as exc:
            result["stat_error"] = type(exc).__name__

        try:
            result["readable"] = root.is_dir() and any(True for _ in root.iterdir()) or root.is_dir()
        except OSError as exc:
            result["readable"] = False
            result["read_error"] = type(exc).__name__

        result["candidate_files"] = [self._debug_file(candidate) for candidate in self._unique_safe_rel_paths(candidates)]
        entries: list[dict] = []
        try:
            for idx, entry in enumerate(root.glob("**/*")):
                if idx >= 80:
                    break
                try:
                    rel = str(entry.relative_to(root))
                    st = entry.stat()
                    entries.append(
                        {
                            "path": rel,
                            "type": "dir" if entry.is_dir() else "file",
                            "bytes": st.st_size,
                            "mode": oct(st.st_mode & 0o777),
                        }
                    )
                except OSError:
                    pass
        except OSError as exc:
            result["entries_error"] = type(exc).__name__
        result["entries"] = entries
        return result

    def _debug_file(self, rel_path: str) -> dict:
        file_path = HLS_DIR / rel_path
        result = {"rel_path": rel_path, "path": str(file_path)}
        try:
            file_path.resolve().relative_to(HLS_DIR.resolve())
        except ValueError:
            result["safe"] = False
            return result
        result["safe"] = True
        result["exists"] = file_path.exists()
        result["is_file"] = file_path.is_file()
        result["is_dir"] = file_path.is_dir()
        if result["exists"]:
            try:
                st = file_path.stat()
                result.update(
                    {
                        "bytes": st.st_size,
                        "mode": oct(st.st_mode & 0o777),
                        "mtime": st.st_mtime,
                    }
                )
            except OSError as exc:
                result["stat_error"] = type(exc).__name__
        return result

    def _extract_names(self, data) -> list[str]:
        names: list[str] = []
        if isinstance(data, dict):
            value = data.get("name")
            if isinstance(value, str):
                names.append(value)
            for child in data.values():
                names.extend(self._extract_names(child))
        elif isinstance(data, list):
            for item in data:
                names.extend(self._extract_names(item))
        return names

    def _mediamtx_hls_url(self, base_url: str, rel_path: str) -> str:
        path, query = self._split_rel_url(rel_path)
        safe_path = quote(path.lstrip("/"), safe="/-_.~")
        url = f"{base_url.rstrip('/')}/{safe_path}"
        return f"{url}?{query}" if query else url

    def _normalize_rel_path(self, rel_path: str) -> str:
        return rel_path.replace("\\", "/").lstrip("/")

    def _unique_safe_rel_paths(self, rel_paths: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for rel_path in rel_paths:
            rel_path = self._normalize_rel_path(rel_path)
            path, _query = self._split_rel_url(rel_path)
            if not path or any(part == ".." for part in path.split("/")):
                continue
            if rel_path not in seen:
                seen.add(rel_path)
                unique.append(rel_path)
        return unique

    def _split_rel_url(self, rel_url: str) -> tuple[str, str]:
        rel_url = self._normalize_rel_path(rel_url).split("#", 1)[0]
        path, sep, query = rel_url.partition("?")
        return path, query if sep else ""

    def _segment_sig_payload(self, rel_path: str, exp: int, source_query: str = "") -> str:
        return f"{rel_path}:{source_query}:{exp}" if source_query else f"{rel_path}:{exp}"

    def _media_cacheable(self, rel_path: str) -> bool:
        path, _query = self._split_rel_url(rel_path)
        return not path.endswith(".m3u8") and HLS_MEDIA_CACHE_TTL > 0 and HLS_MEDIA_CACHE_MAX_BYTES > 0

    def _media_cache_get(self, key: str) -> bytes | None:
        entry = self._media_cache.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._media_cache_bytes -= len(entry.content)
            self._media_cache.pop(key, None)
            return None
        self._media_cache.move_to_end(key)
        return entry.content

    def _media_cache_put(self, key: str, content: bytes) -> None:
        size = len(content)
        if size > HLS_MEDIA_CACHE_MAX_BYTES:
            return
        old = self._media_cache.pop(key, None)
        if old is not None:
            self._media_cache_bytes -= len(old.content)
        self._media_cache[key] = MediaCacheEntry(content, time.monotonic() + HLS_MEDIA_CACHE_TTL)
        self._media_cache_bytes += size
        while self._media_cache_bytes > HLS_MEDIA_CACHE_MAX_BYTES and self._media_cache:
            _old_key, old_entry = self._media_cache.popitem(last=False)
            self._media_cache_bytes -= len(old_entry.content)

    def _is_media_playlist(self, content: str) -> bool:
        return "#EXT-X-MEDIA-SEQUENCE:" in content or "#EXTINF:" in content

    def _playlist_bytes_match(self, data: bytes, media_only: bool) -> bool:
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return not media_only or self._is_media_playlist(content)

    def _playlist_child_candidates(self, parent_rel_path: str, content: str) -> list[str]:
        parent_dir = posixpath.dirname(self._normalize_rel_path(parent_rel_path))
        children: list[str] = []

        for line in content.splitlines():
            tag = line.strip()
            if not tag or tag.startswith("#") or ".m3u8" not in tag:
                continue
            child = self._resolve_playlist_uri(parent_dir, tag)
            if child:
                children.append(child)

        for match in re.finditer(r'URI="([^"]+\.m3u8(?:\?[^"]*)?)"', content):
            child = self._resolve_playlist_uri(parent_dir, match.group(1))
            if child:
                children.append(child)

        return self._unique_safe_rel_paths(children)

    def _resolve_playlist_uri(self, parent_dir: str, uri: str) -> str | None:
        uri = uri.split("#", 1)[0].strip()
        if not uri or uri.startswith(("http://", "https://", "/")):
            return None
        uri_path, query = self._split_rel_url(uri)
        if not uri_path:
            return None
        normalized = posixpath.normpath(posixpath.join(parent_dir, uri_path))
        if normalized == "." or normalized.startswith("../") or "/../" in normalized:
            return None
        rel_path = self._normalize_rel_path(normalized)
        return f"{rel_path}?{query}" if query else rel_path

    def _playlist_float_tag(self, content: str, pattern: str) -> float | None:
        match = re.search(pattern, content)
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    def _playlist_int_tag(self, content: str, pattern: str) -> int | None:
        match = re.search(pattern, content)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    def _stabilize_server_control(
        self,
        line: str,
        part_target: float | None,
        target_duration: float | None,
    ) -> str:
        def part_repl(match: re.Match) -> str:
            current = float(match.group(1))
            lower = (part_target or 0.1) * HLS_PART_HOLD_BACK_PARTS
            return f"PART-HOLD-BACK={max(lower, current):.3f}"

        def hold_repl(match: re.Match) -> str:
            current = float(match.group(1))
            return f"HOLD-BACK={current:.3f}"

        line = re.sub(r"\bPART-HOLD-BACK=([\d.]+)", part_repl, line)
        return re.sub(r"(?<!PART-)\bHOLD-BACK=([\d.]+)", hold_repl, line)

    def _estimate_first_segment_pdt(self, content: str) -> str | None:
        durations = [float(m.group(1)) for m in re.finditer(r"#EXTINF:([\d.]+)", content)]
        if not durations:
            return None
        first = datetime.now(timezone.utc) - timedelta(seconds=sum(durations))
        return first.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
