"""Exact nickname commands, independent of the global conversational wake word."""

from __future__ import annotations

import re

import astrbot.api.message_components as Comp
from astrbot.api.event import filter


def parse_nickname_command(text: str, nickname: str) -> str | None:
    nickname = nickname.strip()
    if not nickname:
        return None
    match = re.fullmatch(re.escape(nickname) + r"\s*(找鸡|找本|搜图)", text.strip())
    return match.group(1) if match else None


def current_command_text(event) -> str | None:
    """Read only current plain text, tolerating bot and QQ reply auto-mentions."""
    chain = list(event.get_messages() or [])
    at_type = getattr(Comp, "At", ())
    bot_id = str(event.get_self_id() or "")
    reply_sender = ""
    if (
        event.get_platform_name() == "aiocqhttp"
        and chain
        and isinstance(chain[0], Comp.Reply)
    ):
        reply_sender = str(getattr(chain[0], "sender_id", "") or "")
    text = []
    for index, component in enumerate(chain):
        if isinstance(component, Comp.Plain):
            text.append(component.text)
        elif isinstance(component, (Comp.Image, Comp.Reply)):
            continue
        elif isinstance(component, at_type):
            recipient = str(component.qq)
            if recipient == bot_id and bot_id:
                continue
            if index == 1 and reply_sender and recipient == reply_sender:
                continue
            return None
        else:
            return None
    return "".join(text)


class NicknameCommandFilter(filter.CustomFilter):
    """Keep only the live plugin's nickname, never global bot configuration."""

    _nickname = ""
    _owner = None

    @classmethod
    def bind(cls, nickname: str, owner: object) -> None:
        cls._nickname = nickname.strip()
        cls._owner = owner

    @classmethod
    def unbind(cls, owner: object) -> None:
        if cls._owner is owner:
            cls._nickname = ""
            cls._owner = None

    def filter(self, event, cfg) -> bool:
        if not self._nickname:
            return False
        text = current_command_text(event)
        command = (
            parse_nickname_command(text, self._nickname) if text is not None else None
        )
        if command is None:
            return False
        event._soutubot_nickname_command = command
        return True
