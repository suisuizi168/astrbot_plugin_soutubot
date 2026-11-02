"""JM PDF delivery with bounded diagnostics and safe private fallback."""
from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp

try:
    from aiocqhttp.exceptions import ActionFailed
except ImportError:  # Other adapters and isolated test environments.
    ActionFailed = None

SendFn = Callable[[str, MessageChain], Awaitable[bool | None]]


class MessageNotSentError(RuntimeError):
    """AstrBot could not find a platform and explicitly returned False."""


def _redact(text: str) -> str:
    text = re.sub(r"(?i)\b(?:https?|file)://[^\s<>\"']+", "[URL redacted]", text)
    text = re.sub(
        r'''(?ix)(["']?(?:access_token|token|password|passwd|secret|api_key|authorization|cookie)["']?\s*[:=]\s*)
        (?:"[^"]*"|'[^']*'|[^,;\s}]+(?:\s+[^,;\s}]+)? )''',
        r"\1[redacted]", text,
    )
    return " ".join(text.split())[:600]


def ob_error_detail(exc: BaseException) -> str:
    """Keep diagnostic wording, never dump arbitrary response data or headers."""
    name = type(exc).__name__
    result = getattr(exc, "result", None)
    if isinstance(result, dict) and result:
        wording = result.get("wording") or result.get("msg") or result.get("message") or result.get("status") or ""
        return _redact(f"{name}(retcode={result.get('retcode')}, wording={wording})")
    return _redact(f"{name}: {exc}")


def inspect_pdf(pdf: str | os.PathLike[str]) -> str:
    """Best-effort diagnostics; AstrBot remains responsible for URI conversion."""
    try:
        raw = os.fsdecode(pdf)
        if raw.startswith("file://"):
            return "file URI（由 AstrBot 解析）"
        path = os.path.abspath(raw)
        if not os.path.isfile(path):
            return "路径预检提示：文件不存在或不是普通文件，仍交由适配器处理"
        size = os.path.getsize(path)
        return f"本地 PDF {size / 1024 / 1024:.2f} MB"
    except (OSError, ValueError, TypeError) as exc:
        return f"路径预检不可用（{type(exc).__name__}），仍交由适配器处理"


def private_session_of(umo: str, sender_id: str) -> str:
    parts = umo.split(":", 2)
    if len(parts) != 3 or parts[1] != "GroupMessage" or not parts[0] or not parts[2]:
        return ""
    if not sender_id or not sender_id.isascii() or not sender_id.isdecimal():
        return ""
    return f"{parts[0]}:FriendMessage:{sender_id}"


def safe_sender_id(event: Any) -> str:
    try:
        getter = getattr(event, "get_sender_id", None)
        return str(getter() or "").strip() if callable(getter) else ""
    except Exception:
        return ""


def _explicit_rejection(exc: Exception) -> bool:
    # Timeouts / disconnects do not tell us whether the remote accepted the PDF.
    # OneBot retcode=1 means asynchronous acceptance, not a safe retry signal.
    rejected = bool(
        ActionFailed is not None and isinstance(exc, ActionFailed)
        and isinstance(exc.result, dict)
        and exc.result.get("status") == "failed"
        and exc.result.get("retcode") not in (None, 0, 1, "0", "1")
    )
    if not rejected:
        return False
    wording = " ".join(str(exc.result.get(key, "")) for key in ("wording", "msg", "message"))
    return not re.search(r"timeout|timed?\s*out|超时|断[开线]|disconnect|connection\s*(?:reset|lost)|网络", wording, re.I)


async def send_pdf(
    *, send: SendFn, umo: str, pdf: str | os.PathLike[str] | None,
    jm_id: str, task_id: str = "", cache: bool = False, sender_id: str = "",
    filename: str | None = None, notice: str = "", private_fallback: bool = True, source: str = "JM",
) -> tuple[bool, str]:
    """Send once; privately fall back only when the PDF was not ambiguously sent.

    A failed text notice can safely fall back before attempting any PDF. After
    attempting a PDF, only an explicit OneBot rejection permits private delivery.
    Cancellation propagates. Success means the adapter returned without error.
    """
    tag = f"[{source}下载:{task_id or 'internal'}]"
    try:
        raw = os.fsdecode(pdf) if pdf is not None else ""
    except (TypeError, ValueError):
        return False, "PDF 路径类型非法"
    if not raw.strip():
        return False, "PDF 路径为空"
    logger.info(f"{tag} 文件发送开始 | {source}{jm_id} | cache={cache} | {inspect_pdf(raw)}")
    private_session = private_session_of(umo, sender_id) if private_fallback else ""

    async def checked_send(session: str, chain: MessageChain) -> None:
        if await send(session, chain) is False:
            raise MessageNotSentError("未找到可用的发送平台")

    async def send_notice(session: str) -> None:
        if notice:
            await checked_send(session, MessageChain([Comp.Plain(notice)]))

    async def send_file(session: str) -> None:
        await checked_send(session, MessageChain([Comp.File(file=raw, name=filename or f"{source}{jm_id}.pdf")]))

    file_attempted = False
    try:
        await send_notice(umo)
        file_attempted = True
        await send_file(umo)
        return True, ""
    except Exception as exc:
        detail = ob_error_detail(exc)
        logger.warning(f"{tag} 发送失败 | {source}{jm_id} | error={detail}")
        if (
            not private_session or isinstance(exc, MessageNotSentError)
            or (file_attempted and not _explicit_rejection(exc))
        ):
            return False, detail

    logger.warning(f"{tag} 尝试私聊发送给请求者 | {source}{jm_id}")
    try:
        # Include the actual password/metadata in the new destination as well.
        await send_notice(private_session)
        await send_file(private_session)
    except Exception as exc:
        fallback_detail = ob_error_detail(exc)
        logger.warning(f"{tag} 私聊发送失败 | {source}{jm_id} | error={fallback_detail}")
        return False, f"原会话：{detail}；私聊：{fallback_detail}"

    logger.info(f"{tag} 私聊发送成功 | {source}{jm_id}")
    try:
        await checked_send(umo, MessageChain([Comp.Plain(f"{source}{jm_id} 已通过私聊发送给请求者，请查看私聊消息。")]))
    except Exception as exc:
        # The PDF is already sent; a failed follow-up must never resend it.
        logger.warning(f"{tag} 私聊成功后的群提示失败 | error={ob_error_detail(exc)}")
    return True, ""

# Backward-compatible name for integrations using the JM helper.
send_jm_pdf = send_pdf
