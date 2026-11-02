from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain

try:
    from astrbot.core.utils.quoted_message import extract_quoted_message_images
except ImportError:  # Compatibility with AstrBot versions before this helper existed.
    extract_quoted_message_images = None

from .client import SearchParameters, SoutubotError, SoutubotRateLimitError
from .image_utils import ImagePreparationError, prepare_image_async
from .jm_resolver import JmResolution, format_jm_resolution
from .models import SearchResponse, format_match_header, format_search_footer, format_search_item, select_search_items


from .workflow_common import _SearchResultTimeoutError

class SearchWorkflow:
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


    async def _search_bytes(
        self, image_bytes: bytes, filename: str, *, strict: bool, trace_id: str
    ) -> SearchResponse:
        logger.info(
            "[搜图:%s] 开始图片预处理: filename=%s, input_bytes=%d, strict=%s",
            trace_id,
            filename,
            len(image_bytes),
            strict,
        )
        prepared = await prepare_image_async(image_bytes, filename)
        params = SearchParameters(
            factor=1.4 if strict else 1.2,
            metadata_mode="display",
            top_k=self._top_k,
        )
        logger.info(
            "[搜图:%s] 发起搜图请求: upload=%s, upload_bytes=%d, content_type=%s, "
            "changed=%s, factor=%.1f, metadata_mode=%s, top_k=%d",
            trace_id,
            prepared.filename,
            len(prepared.data),
            prepared.content_type,
            prepared.changed,
            params.factor,
            params.metadata_mode,
            params.top_k,
        )
        async with self._semaphore:
            return await self._client.search(prepared, params)


    def _build_result_chain(
        self,
        response: SearchResponse,
        jm_resolution: JmResolution | None = None,
    ) -> MessageChain:
        selected, qualified_count = select_search_items(
            response,
            min_similarity=self._min_similarity,
            max_results=self._max_results,
        )
        components: list[Any] = [
            Comp.Plain(
                format_match_header(
                    qualified_count,
                    len(selected),
                    min_similarity=self._min_similarity,
                )
            )
        ]
        for index, item in enumerate(selected, start=1):
            if self._show_preview_image and item.thumbnail_url:
                components.append(Comp.Image.fromURL(item.thumbnail_url))
            components.append(
                Comp.Plain(
                    "\n"
                    + format_search_item(
                        item,
                        index,
                        show_match_page_url=self._show_match_page_url,
                        show_detail_page_url=self._show_detail_page_url,
                    )
                )
            )
        jm_text = self._format_jm_resolution_for_user(jm_resolution)
        if jm_text:
            components.append(Comp.Plain("\n\n" + jm_text))
        components.append(Comp.Plain("\n\n" + format_search_footer(response)))
        return MessageChain(components)


    @staticmethod
    def _user_error(exc: Exception) -> str:
        if isinstance(exc, asyncio.TimeoutError):
            return "搜图Bot酱请求超时，请稍后重试。"
        if isinstance(exc, SoutubotRateLimitError) and exc.retry_after:
            return f"{exc}，建议 {exc.retry_after} 秒后重试。"
        if isinstance(exc, (SoutubotError, ImagePreparationError)):
            return str(exc)
        return "搜图失败，请稍后重试；管理员可查看插件日志了解原因。"


    @staticmethod
    async def _await_search_result_deadline(
        awaitable: Awaitable[Any], deadline: float
    ) -> Any:
        task = asyncio.ensure_future(awaitable)
        try:
            remaining = deadline - time.monotonic()
            done, _ = await asyncio.wait({task}, timeout=max(0.0, remaining))
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise _SearchResultTimeoutError
            return task.result()
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise


    async def _send_search_timeout_notice(self, umo: str, trace_id: str) -> None:
        notice = MessageChain(
            [
                Comp.Plain(
                    f"搜图查询超过 {self._search_result_timeout} 秒仍未返回，已中断本次操作，请稍后重试。"
                )
            ]
        )
        try:
            await asyncio.wait_for(
                self._send_chain(umo, notice), timeout=min(10, self._search_result_timeout)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[搜图:%s] 超时提示发送结果: 失败（%s）", trace_id, type(exc).__name__
            )


    async def _background_search(self, *, umo, image_bytes, filename, strict, sender_id=""):
        from .routing import target_response, choose_download_route
        trace_id = uuid.uuid4().hex[:8]
        deadline = time.monotonic() + self._search_result_timeout
        try:
            response = await self._await_search_result_deadline(
                self._search_bytes(image_bytes, filename, strict=strict, trace_id=trace_id), deadline)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._send_chain(umo, MessageChain([Comp.Plain(self._user_error(exc))]))
            await self._background_multi_search(umo, image_bytes, filename)
            return

        # Output decisions depend solely on qualified soutubot results, never on
        # whether JM/NH downloads are enabled or succeed later.
        target = target_response(response, self._min_similarity)
        try:
            await self._await_search_result_deadline(
                self._send_chain(umo, self._build_result_chain(response)), deadline)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[搜图:%s] 原结果发送未确认，停止后续流程", trace_id)
            return
        if not target.items:
            await self._background_multi_search(umo, image_bytes, filename)
            return

        route = choose_download_route(target,
            nh_enabled=getattr(self, "_nh_download_enabled", False),
            jm_enabled=self._jm_download_enabled)
        if route.action == "none":
            return
        if route.action in {"jm", "nh"}:
            await self._send_download_confirmation(umo, sender_id, route.action, route.album_id)
            return
        await asyncio.sleep(self._jm_result_message_settle_seconds)
        try:
            resolution = await self._resolve_jm_for_response(target, trace_id=trace_id, umo=umo)
            message = format_jm_resolution(resolution)
            if message: await self._send_chain(umo, MessageChain([Comp.Plain(message)]))
            if resolution.status == "matched" and resolution.selected:
                await self._send_download_confirmation(umo, sender_id, "jm", resolution.selected.album_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[搜图:%s] JM 阶段失败: %s", trace_id, type(exc).__name__)
            await self._send_chain(umo, MessageChain([Comp.Plain("JM 查询或下载启动失败，原搜图结果已保留。")]))

    async def _background_multi_search(self, umo, image_bytes, filename):
        """Run direct image search or the find-book fallback without downloads."""
        service = getattr(self, "_multi_search", None)
        if service is None or not service.enabled:
            return
        try:
            image = await prepare_image_async(image_bytes, filename)
            text = await service.search(image)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Multi-search failed: %s", type(exc).__name__)
            text = "多引擎搜图失败，请稍后重试。"
        if text:
            await self._send_chain(umo, MessageChain([Comp.Plain(text)]))
