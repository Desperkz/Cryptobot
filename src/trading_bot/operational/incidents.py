from __future__ import annotations

import time
from collections.abc import Awaitable, Callable


class IncidentAlerter:
    def __init__(self, cooldown_sec: float = 300.0) -> None:
        self.cooldown_sec = cooldown_sec
        self._last_sent: dict[str, float] = {}

    def should_send(self, key: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        last = self._last_sent.get(key)
        if last is not None and current - last < self.cooldown_sec:
            return False
        self._last_sent[key] = current
        return True

    async def send(
        self,
        key: str,
        message: str,
        callback: Callable[[str], Awaitable[None]],
        now: float | None = None,
    ) -> bool:
        if not self.should_send(key, now):
            return False
        await callback(message)
        return True
