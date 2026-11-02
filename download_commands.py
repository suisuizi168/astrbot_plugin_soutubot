"""Recognize standalone source/ID requests before conversational LLM routing."""
from __future__ import annotations

import re

from astrbot.api.event import filter


_EXPLICIT_DOWNLOAD = re.compile(r"/?\s*(jm|nh)\s*([0-9]{1,12})", re.IGNORECASE)


def parse_download_command(text: str) -> tuple[str, str] | None:
    """Only a complete source + numeric ID is a deterministic download request."""
    match = _EXPLICIT_DOWNLOAD.fullmatch(str(text or "").strip())
    if match is None:
        return None
    return match.group(1).lower(), match.group(2)


class ExplicitDownloadFilter(filter.CustomFilter):
    def filter(self, event, cfg) -> bool:
        # Custom filters also match without an @mention or wake prefix. This is
        # deliberate for a standalone explicit ID, never a number inside prose.
        return parse_download_command(event.message_str) is not None
