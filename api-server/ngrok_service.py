import asyncio
import socket

import httpx

from config import NGROK_CACHE_TTL, NGROK_RTMP_API, SITE_BASE_URL


class NgrokService:
    def __init__(self) -> None:
        self._cache: dict[str, str] = {}
        self._cache_ts = 0.0
        self._local_ip = ""

    def _get_local_ip(self) -> str:
        if not self._local_ip:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect(("8.8.8.8", 80))
                    self._local_ip = s.getsockname()[0]
            except Exception:
                self._local_ip = "サーバーIP"
        return self._local_ip

    async def urls(self) -> dict[str, str]:
        now = asyncio.get_running_loop().time()
        if now - self._cache_ts < NGROK_CACHE_TTL and self._cache:
            return self._cache

        result: dict[str, str] = {
            "site": SITE_BASE_URL,
            "rtmp": "",
            "rtmp_base": "",
            "rtmp_local": f"rtmp://{self._get_local_ip()}:1935/live",
            "rtmp_local_base": f"rtmp://{self._get_local_ip()}:1935",
        }
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{NGROK_RTMP_API}/api/tunnels")
                for tunnel in resp.json().get("tunnels", []):
                    public_url = tunnel.get("public_url", "")
                    if public_url.startswith("tcp://"):
                        result["rtmp_base"] = "rtmp://" + public_url[6:]
                        result["rtmp"] = result["rtmp_base"] + "/live"
                        break
        except Exception:
            pass

        self._cache = result
        self._cache_ts = now
        return result
