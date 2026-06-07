import os
import re
import uuid
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_DIR / "templates"

HLS_DIR = Path(os.environ.get("HLS_DIR", "/hls"))
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "")

NGROK_RTMP_API = os.environ.get("NGROK_RTMP_API", "http://localhost:4040")
NGROK_CACHE_TTL = float(os.environ.get("NGROK_CACHE_TTL", "30"))

MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "100"))
SESSION_TIMEOUT = float(os.environ.get("SESSION_TIMEOUT", "15.0"))
QUEUE_TIMEOUT = float(os.environ.get("QUEUE_TIMEOUT", "20.0"))
MAX_BPS = int(os.environ.get("MAX_BPS", str(3_000_000)))

MEDIAMTX_HLS_URL = os.environ.get("MEDIAMTX_HLS_URL", "http://127.0.0.1:8888").rstrip("/")
MEDIAMTX_HLS_TIMEOUT = float(os.environ.get("MEDIAMTX_HLS_TIMEOUT", "1.0"))
SEGMENT_WAIT_TIMEOUT = float(os.environ.get("SEGMENT_WAIT_TIMEOUT", "1.5"))

SEGMENT_SECRET = (os.environ.get("SEGMENT_SECRET", "") or uuid.uuid4().hex).encode()
SEGMENT_TTL = int(os.environ.get("SEGMENT_TTL", "120"))
TOKEN_TTL = int(os.environ.get("TOKEN_TTL", "60"))

KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

MEDIA_TYPES: dict[str, str] = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".ts": "video/MP2T",
}

NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
}
