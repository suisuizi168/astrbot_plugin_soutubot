from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Iterable


def normalize_whitelist(values: Iterable[object] | None) -> frozenset[str]:
    """Normalize WebUI list values without logging identifiers."""

    if not values:
        return frozenset()
    return frozenset(
        text
        for value in values
        if value is not None and (text := str(value).strip())
    )


class AccessPolicy:
    """Allow every conversation unless a conversation whitelist is configured."""

    def __init__(self, whitelist: Iterable[object] | None = None) -> None:
        self.whitelist = normalize_whitelist(whitelist)

    def is_allowed(self, *, group_id: object = "", sender_id: object = "") -> bool:
        if not self.whitelist:
            return True
        conversation_id = str(group_id or sender_id or "").strip()
        return bool(conversation_id and conversation_id in self.whitelist)


class CooldownManager:
    """Concurrency-safe, monotonic per-user cooldown tracker."""

    def __init__(
        self,
        cooldown_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._clock = clock
        self._last_use: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def claim(self, key: str) -> int:
        """Claim a slot, returning zero or the whole seconds still remaining."""

        if self.cooldown_seconds <= 0:
            return 0
        now = self._clock()
        async with self._lock:
            previous = self._last_use.get(key)
            if previous is not None:
                remaining = self.cooldown_seconds - (now - previous)
                if remaining > 0:
                    return max(1, math.ceil(remaining))
            self._last_use[key] = now
        return 0

    async def release(self, key: str) -> None:
        """Release a just-claimed slot when no request was actually scheduled."""

        async with self._lock:
            self._last_use.pop(key, None)
