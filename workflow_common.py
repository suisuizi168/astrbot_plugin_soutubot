from __future__ import annotations
import time
from typing import Any, NamedTuple
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


class _SearchResultTimeoutError(TimeoutError):
    """The end-to-end deadline expired before the original search result was sent."""


class PendingDownload(NamedTuple):
    album_id: str
    expires_at: float
    source: str


def confirmation_duration(seconds: int) -> str:
    return f"{seconds // 60} 分钟" if seconds % 60 == 0 else f"{seconds} 秒"


class JmDownloadReplyFilter(filter.CustomFilter):
    """Only wake for an unexpired confirmation from the original searcher."""

    pending: dict[tuple[str, str], PendingDownload] = {}

    @classmethod
    def remember(
        cls,
        umo: str,
        sender_id: str,
        album_id: str,
        *,
        source: str = "jm",
        timeout_seconds: int = 600,
    ) -> PendingDownload:
        now = time.monotonic()
        for key, entry in list(cls.pending.items()):
            if now >= entry.expires_at:
                cls.pending.pop(key, None)
        entry = PendingDownload(album_id, now + timeout_seconds, source)
        cls.pending[(umo, sender_id)] = entry
        return entry

    @classmethod
    def peek(cls, event: AstrMessageEvent) -> str | None:
        entry = cls.peek_request(event)
        return entry.album_id if entry else None

    @classmethod
    def peek_request(cls, event: AstrMessageEvent) -> PendingDownload | None:
        if str(event.message_str or "").strip() != "下载":
            return None
        key = (event.unified_msg_origin, str(event.get_sender_id()))
        entry = cls.pending.get(key)
        if entry is None:
            return None
        if time.monotonic() >= entry[1]:
            cls.pending.pop(key, None)
            return None
        return entry

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return self.peek(event) is not None
