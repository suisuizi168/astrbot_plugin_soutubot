from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

try:
    from astrbot.core.utils.quoted_message import extract_quoted_message_images
except ImportError:  # Compatibility with AstrBot versions before this helper existed.
    extract_quoted_message_images = None

from .client import (
    SearchParameters,
    SoutubotClient,
    SoutubotError,
    SoutubotRateLimitError,
)
from .image_utils import ImagePreparationError, prepare_image_async
from .models import (
    MIN_SIMILARITY,
    SearchResponse,
    format_match_header,
    format_search_footer,
    format_search_item,
    select_search_items,
)
from .policy import AccessPolicy, CooldownManager


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)


class Main(Star):
    """通过搜图Bot酱查询当前消息或引用消息中的图片。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._policy = AccessPolicy(self.config.get("whitelist", []))
        self._cooldown = CooldownManager(
            _bounded_int(self.config.get("cooldown_seconds"), 30, 0, 3600)
        )
        self._timeout = _bounded_int(
            self.config.get("timeout_seconds"), 300, 10, 600
        )
        self._top_k = _bounded_int(self.config.get("top_k"), 25, 1, 50)
        self._max_results = _bounded_int(
            self.config.get("max_results"), 3, 1, 10
        )
        self._max_pending_tasks = _bounded_int(
            self.config.get("max_pending_tasks"), 20, 1, 100
        )
        self._strict_default = bool(self.config.get("strict_mode", False))
        max_concurrency = _bounded_int(
            self.config.get("max_concurrency"), 2, 1, 8
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._client = SoutubotClient(timeout_seconds=self._timeout)
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False

    @staticmethod
    def _cooldown_key(event: AstrMessageEvent) -> str:
        platform = event.get_platform_name() or "unknown"
        sender = event.get_sender_id() or "unknown"
        return f"{platform}:{sender}"

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        return self._policy.is_allowed(
            group_id=event.get_group_id(), sender_id=event.get_sender_id()
        )

    @staticmethod
    async def _extract_image(event: AstrMessageEvent) -> Comp.Image | None:
        chain = list(event.get_messages() or [])
        for segment in chain:
            if isinstance(segment, Comp.Image):
                return segment
        reply = None
        for segment in chain:
            if isinstance(segment, Comp.Reply):
                reply = segment
                break
        if reply is not None and extract_quoted_message_images is not None:
            try:
                image_refs = await extract_quoted_message_images(event, reply)
                if image_refs:
                    return Comp.Image(file=image_refs[0])
            except Exception as exc:
                logger.warning("解析引用图片失败: %s", type(exc).__name__)
        if reply is not None:
            for quoted in list(reply.chain or []):
                if isinstance(quoted, Comp.Image):
                    return quoted
        return None

    @staticmethod
    async def _materialize_image(image: Comp.Image) -> tuple[bytes, str]:
        path = await image.convert_to_file_path()
        if not path:
            raise ImagePreparationError("无法取得图片文件")
        image_path = Path(path)
        data = await asyncio.to_thread(image_path.read_bytes)
        filename = (
            getattr(image, "filename", None)
            or image_path.name
            or "image"
        )
        return data, str(filename)

    async def _claim_request(self, event: AstrMessageEvent) -> tuple[bool, str]:
        if not self._is_allowed(event):
            return False, "当前会话不在搜本子插件白名单内。"
        remaining = await self._cooldown.claim(self._cooldown_key(event))
        if remaining:
            return False, f"请求冷却中，请等待 {remaining} 秒后再试。"
        return True, ""

    async def _search_bytes(
        self, image_bytes: bytes, filename: str, *, strict: bool
    ) -> SearchResponse:
        prepared = await prepare_image_async(image_bytes, filename)
        params = SearchParameters(
            factor=1.4 if strict else 1.2,
            metadata_mode="display",
            top_k=self._top_k,
        )
        async with self._semaphore:
            return await self._client.search(prepared, params)

    def _build_result_chain(self, response: SearchResponse) -> MessageChain:
        selected, qualified_count = select_search_items(
            response,
            min_similarity=MIN_SIMILARITY,
            max_results=self._max_results,
        )
        components: list[Any] = [
            Comp.Plain(
                format_match_header(
                    qualified_count,
                    len(selected),
                    min_similarity=MIN_SIMILARITY,
                )
            )
        ]
        for index, item in enumerate(selected, start=1):
            if item.thumbnail_url:
                components.append(Comp.Image.fromURL(item.thumbnail_url))
            components.append(Comp.Plain("\n" + format_search_item(item, index)))
        components.append(Comp.Plain("\n\n" + format_search_footer(response)))
        return MessageChain(components)

    @staticmethod
    def _user_error(exc: Exception) -> str:
        if isinstance(exc, asyncio.TimeoutError):
            return "获取消息图片超时，请稍后重试。"
        if isinstance(exc, SoutubotRateLimitError) and exc.retry_after:
            return f"{exc}，建议 {exc.retry_after} 秒后重试。"
        if isinstance(exc, (SoutubotError, ImagePreparationError)):
            return str(exc)
        return "搜图失败，请稍后重试；管理员可查看插件日志了解原因。"

    async def _send_chain(self, umo: str, chain: MessageChain) -> None:
        await self.context.send_message(umo, chain)

    async def _background_search(
        self,
        *,
        umo: str,
        image_bytes: bytes,
        filename: str,
        strict: bool,
    ) -> None:
        try:
            response = await self._search_bytes(image_bytes, filename, strict=strict)
            chain = self._build_result_chain(response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # The user receives a sanitized error.
            logger.warning("搜本子后台任务失败: %s", type(exc).__name__)
            chain = MessageChain([Comp.Plain(self._user_error(exc))])
        try:
            await self._send_chain(umo, chain)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("搜本子结果发送失败: %s", type(exc).__name__)

    def _track_task(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)

        def done(completed: asyncio.Task[None]) -> None:
            self._tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                completed.result()
            except Exception as exc:
                logger.error("搜本子后台任务异常结束: %s", type(exc).__name__)

        task.add_done_callback(done)

    @filter.command("找本")
    async def search_doujin_command(self, event: AstrMessageEvent):
        """上传当前消息或引用消息中的图片并查询相似作品。"""

        event.stop_event()
        allowed, message = await self._claim_request(event)
        if not allowed:
            yield event.plain_result(message)
            return
        image = await self._extract_image(event)
        if image is None:
            await self._cooldown.release(self._cooldown_key(event))
            yield event.plain_result(
                "请在同一条消息中附带图片，或引用一条图片消息后发送 /找本。"
            )
            return
        if self._closing or len(self._tasks) >= self._max_pending_tasks:
            await self._cooldown.release(self._cooldown_key(event))
            yield event.plain_result("当前待处理任务较多，请稍后再试。")
            return
        try:
            image_bytes, filename = await asyncio.wait_for(
                self._materialize_image(image), timeout=60
            )
        except Exception as exc:
            await self._cooldown.release(self._cooldown_key(event))
            yield event.plain_result(self._user_error(exc))
            return

        task = asyncio.create_task(
            self._background_search(
                umo=event.unified_msg_origin,
                image_bytes=image_bytes,
                filename=filename,
                strict=self._strict_default,
            ),
            name="astrbot_plugin_soutubot.search",
        )
        self._track_task(task)
        yield event.plain_result("正在查询图片来源，完成后会在当前会话发送结果。")

    async def terminate(self) -> None:
        self._closing = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        await self._client.close()
