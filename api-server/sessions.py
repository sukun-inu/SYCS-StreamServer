import asyncio
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from fastapi import HTTPException

from config import MAX_BPS, MAX_SESSIONS, QUEUE_TIMEOUT, SESSION_TIMEOUT, TOKEN_TTL


class TokenStore:
    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, float]] = {}

    def create(self, key: str) -> str:
        token = uuid.uuid4().hex
        self._tokens[token] = (key, time.time() + TOKEN_TTL)
        return token

    def consume(self, token: str, key: str) -> bool:
        entry = self._tokens.pop(token, None)
        if not entry:
            return False
        stored_key, exp = entry
        return stored_key == key and exp >= time.time()

    async def cleanup(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            expired = [token for token, (_, exp) in list(self._tokens.items()) if exp < now]
            for token in expired:
                self._tokens.pop(token, None)


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, float] = {}
        self._bw: dict[str, deque] = defaultdict(deque)
        self._cap_cond = asyncio.Condition()

    @property
    def active(self) -> int:
        return len(self._sessions)

    @property
    def max_sessions(self) -> int:
        return MAX_SESSIONS

    async def acquire(self, sid: str | None) -> str:
        loop = asyncio.get_running_loop()
        async with self._cap_cond:
            if sid and sid in self._sessions:
                self._sessions[sid] = loop.time()
                return sid

            deadline = loop.time() + QUEUE_TIMEOUT
            while True:
                if len(self._sessions) < MAX_SESSIONS:
                    new_sid = uuid.uuid4().hex[:12]
                    self._sessions[new_sid] = loop.time()
                    return new_sid

                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise HTTPException(503, "満員です。他のお客様の退出をお待ちください。")
                try:
                    await asyncio.wait_for(self._cap_cond.wait(), timeout=min(remaining, 1.0))
                except asyncio.TimeoutError:
                    pass

    async def renew(self, sid: str) -> bool:
        loop = asyncio.get_running_loop()
        async with self._cap_cond:
            if sid in self._sessions:
                self._sessions[sid] = loop.time()
                return True
            return False

    def check_and_record_bw(self, sid: str, nbytes: int) -> bool:
        now = asyncio.get_running_loop().time()
        dq = self._bw[sid]
        dq.append((now, nbytes))
        while dq and now - dq[0][0] > 1.0:
            dq.popleft()
        return sum(size for _, size in dq) <= MAX_BPS

    async def revoke(self, sid: str) -> None:
        async with self._cap_cond:
            self._sessions.pop(sid, None)
            self._bw.pop(sid, None)
            self._cap_cond.notify_all()

    async def cleanup(self, on_freed: Callable[[], Awaitable[None]] | None = None) -> None:
        while True:
            await asyncio.sleep(1.0)
            loop = asyncio.get_running_loop()
            now = loop.time()
            freed = 0
            async with self._cap_cond:
                expired = [sid for sid, ts in self._sessions.items() if now - ts > SESSION_TIMEOUT]
                for sid in expired:
                    self._sessions.pop(sid, None)
                    self._bw.pop(sid, None)
                freed = len(expired)
                if freed:
                    self._cap_cond.notify_all()

            if freed and on_freed is not None:
                await on_freed()
