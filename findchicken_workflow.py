"""Isolated author-search entry, image selection and background delivery."""

from __future__ import annotations

import asyncio
import re
import time
import uuid

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain, filter

from .findchicken_client import FindChickenError
from .findchicken_judge import (
    best_source_fallback,
    build_candidates,
    enrich_identities,
    judge_author,
)
from .findchicken_models import author_matches, format_authors
from .google_lens_client import GoogleLensError
from .image_utils import prepare_image_async
from .user_messages import FINDCHICKEN_EMPTY, FINDCHICKEN_ERROR


URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。；！？、）】》]+", re.I)
USAGE = (
    "请发送「找鸡」时附带或引用一张图片，也可以发送：找鸡 https://example.com/image.jpg"
)


class FindChickenReplyFilter(filter.CustomFilter):
    def filter(self, event, cfg):
        """Recognize the QQ reply/auto-mention shape without matching prose."""
        if event.get_platform_name() != "aiocqhttp":
            return False
        chain = list(event.get_messages() or [])
        at_type = getattr(Comp, "At", ())
        if (
            len(chain) not in (3, 4)
            or not isinstance(chain[0], Comp.Reply)
            or not isinstance(chain[1], at_type)
        ):
            return False
        sender = str(getattr(chain[0], "sender_id", "") or "")
        if not sender or str(chain[1].qq) != sender:
            return False
        rest = chain[2:]
        if len(rest) == 2:
            if not isinstance(rest[0], at_type) or str(rest[0].qq) != str(
                event.get_self_id()
            ):
                return False
            rest = rest[1:]
        return (
            len(rest) == 1
            and isinstance(rest[0], Comp.Plain)
            and re.fullmatch(r"/?找鸡(?:\s+.+)?", rest[0].text.strip()) is not None
        )


class FindChickenWorkflow:
    async def _start_findchicken(self, event):
        """Admit one independent job and let it acknowledge before any lookup."""
        if getattr(event, "_soutubot_findchicken_started", False):
            return
        event._soutubot_findchicken_started = True
        event.stop_event()
        if not self._is_allowed(event):
            yield event.plain_result("当前会话不在插件白名单内。")
            return
        if not self._findchicken_enabled:
            yield event.plain_result("找鸡功能当前已关闭。")
            return
        if self._closing or len(self._findchicken_tasks) >= self._max_pending_tasks:
            yield event.plain_result("找鸡待处理任务较多，请稍后再试。")
            return
        key = self._cooldown_key(event)
        remaining = await self._findchicken_cooldown.claim(key)
        if remaining:
            yield event.plain_result(f"别急，鸡要慢慢找～请 {remaining} 秒后再试。")
            return
        task = asyncio.create_task(
            self._background_findchicken(event, key), name="soutubot.findchicken"
        )
        self._findchicken_tasks.add(task)
        task.add_done_callback(self._findchicken_tasks.discard)

    async def _findchicken_input(self, event):
        """Select attached image, explicit URL, quoted image or quoted URL."""
        chain = list(event.get_messages() or [])
        for component in chain:
            if isinstance(component, Comp.Image):
                return component, False
        text = str(getattr(event, "message_str", "") or "")
        found = URL_PATTERN.search(text)
        if found:
            return found.group().rstrip(".,;:!?)]}>，。；：！？）】》"), True
        image = await self._extract_image(event)
        if image is not None:
            return image, False
        for component in chain:
            if isinstance(component, Comp.Reply):
                found = URL_PATTERN.search(
                    str(getattr(component, "message_str", "") or "")
                )
                if found:
                    return found.group().rstrip(".,;:!?)]}>，。；：！？）】》"), True
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        segments = (
            raw.get("message", [])
            if isinstance(raw, dict)
            else getattr(raw, "message", [])
        )
        for segment in segments if isinstance(segments, (list, tuple)) else []:
            if isinstance(segment, dict) and segment.get("type") == "image":
                data = segment.get("data", {})
                url = data.get("url", "") if isinstance(data, dict) else ""
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    return url, False
        return None, False

    async def _background_findchicken(self, event, key):
        """Send the acknowledgement before image resolution or external IO."""
        umo = event.unified_msg_origin
        started_query = False
        acquired = False
        trace_id = uuid.uuid4().hex[:8]
        started = time.monotonic()
        stage = "发送接收提示"
        outcome = "处理中"
        logger.info("[找鸡:%s] 开始处理", trace_id)
        try:
            await self._send_chain(umo, MessageChain([Comp.Plain("收到，开始找鸡")]))
            async with asyncio.timeout(self._findchicken_timeout):
                stage = "等待查询名额"
                logger.info("[找鸡:%s] 等待查询名额", trace_id)
                await self._findchicken_semaphore.acquire()
                acquired = True
                stage = "准备图片"
                logger.info("[找鸡:%s] 开始获取并准备图片", trace_id)
                image, direct_url = await self._findchicken_input(event)
                if image is None:
                    outcome = "缺少图片"
                    logger.info("[找鸡:%s] 未取得图片，发送用法提示", trace_id)
                    await self._send_chain(umo, MessageChain([Comp.Plain(USAGE)]))
                    return
                stage = "检查查询配置"
                if not self._findchicken_client.api_key and not (
                    self._google_lens.findchicken_enabled and self._google_lens.api_key
                ):
                    raise FindChickenError("missing_key")
                prepared = None
                stage = "准备图片"
                if not isinstance(image, str):
                    data, filename = await self._materialize_image(image)
                    prepared = await prepare_image_async(data, filename)
                logger.info(
                    "[找鸡:%s] 图片准备完成（%s）",
                    trace_id,
                    "链接" if isinstance(image, str) else "图片",
                )
            started_query = True
            stage = "查询与作者筛选"
            text = await self._query_findchicken(
                prepared,
                image if isinstance(image, str) else "",
                direct_url,
                umo=umo,
                trace_id=trace_id,
            )
            outcome = (
                "无结果"
                if text == FINDCHICKEN_EMPTY
                else "查询错误"
                if text == FINDCHICKEN_ERROR
                else "有结果"
            )
            stage = "发送结果"
            logger.info("[找鸡:%s] 开始发送结果：%s", trace_id, outcome)
            await self._send_chain(umo, MessageChain([Comp.Plain(text)]))
            logger.info("[找鸡:%s] 结果发送完成", trace_id)
        except asyncio.CancelledError:
            outcome = "已取消"
            logger.info("[找鸡:%s] 任务取消：阶段=%s", trace_id, stage)
            raise
        except Exception as exc:
            outcome = "发生错误"
            kind = exc.kind if isinstance(exc, FindChickenError) else type(exc).__name__
            logger.warning(
                "[找鸡:%s] 处理失败：阶段=%s，类型=%s", trace_id, stage, kind
            )
            try:
                await self._send_chain(
                    umo, MessageChain([Comp.Plain(FINDCHICKEN_ERROR)])
                )
            except Exception:
                logger.warning("[找鸡:%s] 错误提示发送失败", trace_id)
        finally:
            if acquired:
                self._findchicken_semaphore.release()
            if not started_query:
                await self._findchicken_cooldown.release(key)
            logger.info(
                "[找鸡:%s] 请求结束：%s，总耗时=%.2fs",
                trace_id,
                outcome,
                time.monotonic() - started,
            )

    async def _run_findchicken_source(self, name, query, trace_id):
        """Log each provider as it completes, without waiting for slower peers."""
        started = time.monotonic()
        logger.info("[找鸡:%s] 开始查询：%s", trace_id, name)
        try:
            result = await query
        except asyncio.CancelledError:
            logger.info("[找鸡:%s] 查询取消：%s", trace_id, name)
            raise
        except Exception as exc:
            kind = (
                exc.kind
                if isinstance(exc, (FindChickenError, GoogleLensError))
                else type(exc).__name__
            )
            logger.warning(
                "[找鸡:%s] 查询失败：%s，类型=%s，耗时=%.2fs",
                trace_id,
                name,
                kind,
                time.monotonic() - started,
            )
            raise
        logger.info(
            "[找鸡:%s] 查询完成：%s，耗时=%.2fs",
            trace_id,
            name,
            time.monotonic() - started,
        )
        return result

    async def _query_findchicken(
        self, prepared, url, direct_url, *, umo="", trace_id=""
    ):
        """Query both providers concurrently and preserve partial success.

        Args:
            prepared: Preprocessed message image or None for a URL input.
            url: Image URL to submit directly to the providers.
            direct_url: Whether the command used an explicit text image URL.

        Returns:
            One result or a provider-neutral empty/error notice.
        """
        trace_id = trace_id or uuid.uuid4().hex[:8]
        names, queries = [], []
        if self._findchicken_client.api_key:
            names.append("SauceNAO")
            request = (
                self._findchicken_client.search(url=url)
                if url
                else self._findchicken_client.search(image=prepared)
            )
            queries.append(asyncio.wait_for(request, self._findchicken_timeout))
        if self._google_lens.findchicken_enabled:
            names.append("Google Lens")
            queries.append(
                self._google_lens.search(
                    prepared.data if prepared else None,
                    filename=prepared.filename if prepared else "image",
                    url=url,
                    limit=self._findchicken_results,
                    for_authors=True,
                )
            )
        logger.info("[找鸡:%s] 开始并行搜索：%d 个来源", trace_id, len(queries))
        responses = await asyncio.gather(
            *(
                self._run_findchicken_source(name, query, trace_id)
                for name, query in zip(names, queries)
            ),
            return_exceptions=True,
        )
        matches, google_results, successes = [], [], 0
        for name, response in zip(names, responses):
            if isinstance(response, asyncio.CancelledError):
                raise response
            if isinstance(response, Exception):
                continue
            successes += 1
            if name == "SauceNAO":
                matches = author_matches(
                    response, self._findchicken_threshold, self._findchicken_results
                )
            else:
                google_results = response.results
        logger.info(
            "[找鸡:%s] 搜索汇总：成功=%d，失败=%d，结构化结果=%d，网页结果=%d",
            trace_id,
            successes,
            sum(isinstance(r, Exception) for r in responses),
            len(matches),
            len(google_results),
        )
        if not successes:
            logger.info("[找鸡:%s] 无成功来源，返回错误提示", trace_id)
            return FINDCHICKEN_ERROR
        candidates = build_candidates(
            matches, google_results, self._findchicken_results
        )
        logger.info("[找鸡:%s] 候选整理完成：%d 条", trace_id, len(candidates))
        if not candidates:
            logger.info("[找鸡:%s] 无可展示候选，跳过作者筛选", trace_id)
            return (
                FINDCHICKEN_ERROR
                if any(isinstance(response, Exception) for response in responses)
                else FINDCHICKEN_EMPTY
            )
        selected = None
        details = {}
        if self._findchicken_llm_enabled:
            try:
                logger.info("[找鸡:%s] 开始补全作者信息", trace_id)
                await enrich_identities(
                    candidates, proxy=self._findchicken_client.proxy
                )
                logger.info("[找鸡:%s] 作者信息补全完成，开始 LLM 判断", trace_id)
                selected = await judge_author(
                    self.context,
                    candidates,
                    provider_id=self._findchicken_llm_provider,
                    umo=umo,
                    timeout=self._findchicken_llm_timeout,
                    diagnostics=details,
                )
                logger.info(
                    "[找鸡:%s] LLM 判断完成：%s",
                    trace_id,
                    "已选中结果" if selected is not None else "未选中结果",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[找鸡:%s] 作者信息补全或 LLM 判断失败：%s",
                    trace_id,
                    type(exc).__name__,
                )
                details["outcome"] = "model_error"
        else:
            logger.info("[找鸡:%s] LLM 已关闭，使用匹配来源兜底", trace_id)
        if selected is None:
            selected = best_source_fallback(candidates)
            logger.info(
                "[找鸡:%s] 匹配来源兜底：候选=%d，原因=%s",
                trace_id,
                len(candidates),
                details.get("outcome", "llm_disabled"),
            )
        if selected is None:
            logger.info("[找鸡:%s] 筛选结束，仍无可展示结果", trace_id)
            return (
                FINDCHICKEN_ERROR
                if details.get("outcome") == "model_error"
                or any(isinstance(response, Exception) for response in responses)
                else FINDCHICKEN_EMPTY
            )
        logger.info("[找鸡:%s] 筛选完成，准备展示 1 条结果", trace_id)
        return format_authors(
            [selected] if selected else [],
            direct_url=False,
            show_similarity=self._findchicken_show_similarity,
        )
