from __future__ import annotations

import asyncio
import time
import uuid

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain


from .jm_resolver import JmCandidate, JmResolution, format_jm_resolution
from .jm_llm import (
    LLM_QUERY_SYSTEM_PROMPT,
    LLM_VERIFY_SYSTEM_PROMPT,
    JmSearchPlan,
    build_llm_query_prompt,
    build_llm_verification_prompt,
    expand_traditional_title_queries,
    is_cjk_title_query,
    parse_llm_candidate_matches,
    parse_llm_search_plan,
)
from .models import SearchItem, SearchResponse, select_search_items


class JmWorkflow:
    def _format_jm_resolution_for_user(self, resolution: JmResolution | None) -> str | None:
        message = format_jm_resolution(resolution)
        if resolution and resolution.status == "matched" and resolution.selected:
            if getattr(self, "_jm_download_enabled", True):
                return f'{message}\n{self._download_confirmation_notice("jm", resolution.selected.album_id)}'
            return message
        if resolution and resolution.status == "candidates":
            return f"{message}\n\n确认编号后可发送：/jm JM号"
        return message


    async def _send_jm_resolution(
        self, umo: str, sender_id: str, resolution: JmResolution, message: str
    ) -> None:
        if (
            sender_id and self._jm_download_enabled
            and resolution.status == "matched" and resolution.selected
        ):
            await self._send_download_confirmation(umo, sender_id, "jm", resolution.selected.album_id, message)
        else:
            await self._send_chain(umo, MessageChain([Comp.Plain(message)]))


    async def _resolve_jm_for_response(
        self,
        response: SearchResponse,
        *,
        trace_id: str = "internal",
        umo: str | None = None,
        announce_title_search: bool = True,
    ) -> JmResolution:
        selected, _ = select_search_items(
            response,
            min_similarity=self._min_similarity,
            max_results=self._max_results,
        )
        if not selected:
            logger.info("[搜图:%s] 无达到展示阈值的结果，跳过 JM 查询", trace_id)
            return JmResolution("", "skipped", reason="no_displayed_item")
        item = selected[0]
        if not self._jm_settings.enabled:
            logger.info("[搜图:%s] JM 功能已关闭，跳过 LLM 和 JM 查询", trace_id)
            return JmResolution(item.title, "skipped", reason="disabled")
        if item.source_key in {"jmcomic", "18comic"} and (item.source_id or "").isdigit():
            logger.info(
                "[搜图:%s] 首条结果已含 JM 来源 ID，直接使用: source=%s, "
                "source_id=%s, title=%s",
                trace_id,
                item.source_key,
                item.source_id,
                item.title,
            )
            return JmResolution(
                item.title,
                "matched",
                selected=JmCandidate(item.source_id or "", item.title, 100.0),
            )
        try:
            qualified = tuple(
                sorted(
                    (
                        candidate
                        for candidate in response.items
                        if candidate.score >= self._min_similarity
                    ),
                    key=lambda candidate: candidate.score,
                    reverse=True,
                )
            )
            titles = tuple(candidate.title for candidate in qualified)
            logger.info(
                "[搜图:%s] 标题检索阶段开始: 可用标题=%d",
                trace_id,
                len(titles),
            )
            if self._jm_title_search_enabled and announce_title_search:
                await self._notify_jm_progress(
                    umo, trace_id, "正在根据标题检索 JM 作品，请稍候。"
                )
            else:
                logger.info("[搜图:%s] 标题检索阶段结果: 配置已关闭", trace_id)
            try:
                plan = await self._extract_jm_plan(titles, trace_id=trace_id)
            except asyncio.TimeoutError:
                logger.info("[搜图:%s] 标题检索阶段结果: 关键词分析超时", trace_id)
                return JmResolution(item.title, "failed", reason="llm_timeout")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info(
                    "[搜图:%s] 标题检索阶段结果: 关键词分析失败（%s）",
                    trace_id,
                    type(exc).__name__,
                )
                return JmResolution(item.title, "failed", reason="llm_error")
            title_resolution = JmResolution(
                item.title,
                "not_found",
                reason="title_disabled" if not self._jm_title_search_enabled else None,
            )
            title_verified_resolution: JmResolution | None = None
            if self._jm_title_search_enabled and plan.queries:
                title_resolution = await self._resolve_jm_queries(
                    plan.queries,
                    trace_id=trace_id,
                    total_timeout_seconds=self._jm_query_timeout,
                )
                if title_resolution.status == "matched":
                    result = JmResolution(
                        title_resolution.query_title,
                        "matched",
                        selected=title_resolution.selected,
                        reason="title_match",
                    )
                    logger.info("[搜图:%s] 标题检索阶段结果: 命中", trace_id)
                    return result
                verified, _verification_error = await self._verify_jm_candidates(
                    titles,
                    plan.authors,
                    self._resolution_candidates(title_resolution),
                    stage="标题候选",
                    trace_id=trace_id,
                )
                title_verified_resolution = self._resolution_from_verified(
                    plan.queries[0], verified
                )
                if (
                    title_verified_resolution is not None
                    and title_verified_resolution.status == "matched"
                ):
                    result = JmResolution(
                        title_verified_resolution.query_title,
                        "matched",
                        selected=title_verified_resolution.selected,
                        reason="title_match",
                    )
                    logger.info("[搜图:%s] 标题检索阶段结果: 候选核验命中", trace_id)
                    return result
            elif self._jm_title_search_enabled:
                logger.info("[搜图:%s] 标题检索关键词分析结果: 无可用查询词", trace_id)

            can_search_author = self._jm_author_search_enabled and bool(plan.authors)
            if not can_search_author:
                result = title_verified_resolution or title_resolution
                reason = (
                    "title_timeout"
                    if result.status == "failed"
                    and "timeout" in (result.reason or "")
                    else "title_error"
                    if result.status == "failed"
                    else "title_not_found"
                )
                if not self._jm_title_search_enabled:
                    reason = "title_disabled"
                elif not plan.authors:
                    reason = "title_no_author"
                elif not self._jm_author_search_enabled:
                    reason = "title_author_disabled"
                logger.info("[搜图:%s] 标题检索阶段结果: 未找到可确认作品", trace_id)
                return JmResolution(item.title, result.status, result.selected, result.candidates, reason)
            logger.info(
                "[搜图:%s] 标题检索阶段结果: 未命中，开始作者检索阶段",
                trace_id,
            )
            if not self._jm_title_search_enabled:
                title_progress = None
            elif title_resolution.status == "failed":
                title_progress = (
                    "JM 标题检索超时，未能确认对应作品。"
                    if "timeout" in (title_resolution.reason or "")
                    or title_resolution.reason == "partial_timeout"
                    else "JM 标题检索失败，未能确认对应作品。"
                )
            else:
                title_progress = "未通过 JM 标题检索到对应作品。"
            if title_progress:
                await self._notify_jm_progress(umo, trace_id, title_progress)
            await self._notify_jm_progress(
                umo,
                trace_id,
                "正在检索作者作品并匹配目标标题，请稍候。",
            )
            author_resolution = await self._resolve_jm_authors(
                plan.authors,
                trace_id=trace_id,
                total_timeout_seconds=self._jm_author_query_timeout,
            )
            verified, verification_error = await self._verify_jm_candidates(
                titles,
                plan.authors,
                self._resolution_candidates(author_resolution),
                stage="作者候选",
                trace_id=trace_id,
            )
            author_verified_resolution = self._resolution_from_verified(item.title, verified)
            if author_verified_resolution is not None:
                if author_verified_resolution.status == "matched":
                    logger.info("[搜图:%s] 作者检索阶段结果: 命中", trace_id)
                    return JmResolution(
                        author_verified_resolution.query_title,
                        "matched",
                        selected=author_verified_resolution.selected,
                        reason="author_match",
                    )
            if verification_error:
                reason = (
                    "author_verify_timeout"
                    if "timeout" in verification_error
                    else "author_verify_error"
                )
                logger.info("[搜图:%s] 作者候选核验阶段结果: 失败（%s）", trace_id, reason)
                return JmResolution(item.title, "failed", reason=reason)
            if author_resolution.status == "failed":
                reason = "author_timeout" if "timeout" in author_resolution.reason else "author_error"
                result = JmResolution(item.title, "failed", reason=reason)
                logger.info("[搜图:%s] 作者检索阶段结果: 失败（%s）", trace_id, reason)
                return result
            if author_resolution.candidates:
                result = JmResolution(
                    item.title,
                    "not_found",
                    reason="author_unconfirmed",
                )
                logger.info("[搜图:%s] 作者检索阶段结果: 候选未能与标题确认", trace_id)
                return result
            result = JmResolution(item.title, "not_found", reason="author_not_found")
            logger.info("[搜图:%s] 作者检索阶段结果: 未找到匹配作品", trace_id)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[搜图:%s] JM 检索阶段结果: 异常（%s）", trace_id, type(exc).__name__)
            return JmResolution(item.title, "failed", reason="internal_error")


    async def _notify_jm_progress(
        self, umo: str | None, trace_id: str, message: str
    ) -> None:
        if not umo:
            return
        try:
            await self._send_chain(umo, MessageChain([Comp.Plain(message)]))
            await asyncio.sleep(self._jm_notice_sequence_delay_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[搜图:%s] 阶段提示发送结果: 失败（%s）", trace_id, type(exc).__name__
            )


    async def _extract_jm_plan(
        self, titles: tuple[str, ...], *, trace_id: str
    ) -> JmSearchPlan:
        if not self._jm_llm_enabled or not getattr(self, "_llm_tools_enabled", True):
            fallback = tuple(title for title in titles if title.strip())[:1]
            logger.info("[搜图:%s] 标题关键词分析开始: 使用搜图标题", trace_id)
            logger.info("[搜图:%s] 标题关键词分析结果: 查询词=%d, 作者=0", trace_id, len(fallback))
            return JmSearchPlan(queries=fallback)
        logger.info(
            "[搜图:%s] 标题关键词分析开始: 标题=%d",
            trace_id,
            len(titles),
        )
        provider = await self.context.get_using_provider_async()
        if provider is None:
            logger.info("[搜图:%s] 标题关键词分析结果: 默认主模型不可用", trace_id)
            return JmSearchPlan()
        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=build_llm_query_prompt(titles),
                system_prompt=LLM_QUERY_SYSTEM_PROMPT,
                request_max_retries=1,
            ),
            timeout=self._jm_llm_timeout,
        )
        raw_text = str(getattr(response, "completion_text", "") or "")
        raw_plan = parse_llm_search_plan(
            raw_text,
            maximum_queries=self._jm_max_queries,
            maximum_authors=self._jm_max_authors,
        )
        cjk_queries = tuple(query for query in raw_plan.queries if is_cjk_title_query(query))
        ignored_queries = tuple(query for query in raw_plan.queries if query not in cjk_queries)
        queries = expand_traditional_title_queries(
            cjk_queries,
            maximum=self._jm_max_queries * 2,
        )
        plan = JmSearchPlan(queries=queries, authors=raw_plan.authors)
        logger.info(
            "[搜图:%s] LLM 标题分析结果: 基础词=%s, 作者=%s, 已忽略非中日文=%d",
            trace_id,
            cjk_queries,
            plan.authors,
            len(ignored_queries),
        )
        if queries != cjk_queries:
            logger.info("[搜图:%s] 简繁转换结果: JM 查询词=%s", trace_id, queries)
        if not plan.queries and not plan.authors:
            logger.info("[搜图:%s] 标题关键词分析结果: 未提取到可用关键词或作者", trace_id)
        return plan


    @staticmethod
    def _resolution_candidates(resolution: JmResolution) -> tuple[JmCandidate, ...]:
        values = resolution.candidates + ((resolution.selected,) if resolution.selected else ())
        unique: dict[str, JmCandidate] = {}
        for value in values:
            if value.album_id not in unique or value.score > unique[value.album_id].score:
                unique[value.album_id] = value
        return tuple(unique.values())


    def _resolution_from_verified(
        self, query: str, candidates: tuple[JmCandidate, ...]
    ) -> JmResolution | None:
        if not candidates:
            return None
        ranked = tuple(sorted(candidates, key=lambda item: (-item.score, item.album_id)))
        first = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        margin = first.score - second.score if second else float("inf")
        if (
            first.score >= self._jm_llm_verify_threshold
            and margin >= self._jm_settings.auto_select_margin
        ):
            return JmResolution(query, "matched", selected=first)
        return JmResolution(
            query,
            "candidates",
            candidates=ranked[: self._jm_settings.max_display_candidates],
        )


    async def _verify_jm_candidates(
        self,
        titles: tuple[str, ...],
        authors: tuple[str, ...],
        candidates: tuple[JmCandidate, ...],
        *,
        stage: str,
        trace_id: str,
    ) -> tuple[tuple[JmCandidate, ...], str | None]:
        if not candidates or not self._jm_llm_enabled or not getattr(self, "_llm_tools_enabled", True):
            return (), None
        candidate_map = {candidate.album_id: candidate for candidate in candidates}
        logger.info(
            "[搜图:%s] %s 候选核验开始: 候选=%d", trace_id, stage, len(candidates)
        )
        provider = await self.context.get_using_provider_async()
        if provider is None:
            logger.info("[搜图:%s] %s 候选核验结果: 默认主模型不可用", trace_id, stage)
            return (), "model_unavailable"
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=build_llm_verification_prompt(
                        titles,
                        authors,
                        ((candidate.album_id, candidate.title) for candidate in candidates),
                    ),
                    system_prompt=LLM_VERIFY_SYSTEM_PROMPT,
                    request_max_retries=1,
                ),
                timeout=self._jm_llm_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info(
                "[搜图:%s] %s 候选核验结果: 失败（%s）",
                trace_id, stage, type(exc).__name__
            )
            return (), type(exc).__name__.lower()
        raw_text = str(getattr(response, "completion_text", "") or "")
        matches = parse_llm_candidate_matches(
            raw_text,
            allowed_album_ids=candidate_map,
            maximum=self._jm_settings.max_display_candidates,
        )
        verified = tuple(
            JmCandidate(album_id, candidate_map[album_id].title, confidence)
            for album_id, confidence in matches
        )
        logger.info(
            "[搜图:%s] %s 候选核验结果: 确认=%d", trace_id, stage, len(verified)
        )
        return verified, None


    async def _resolve_jm_queries(
        self,
        queries: tuple[str, ...],
        *,
        trace_id: str,
        total_timeout_seconds: float | None = None,
    ) -> JmResolution:
        started = time.monotonic()
        logger.info("[搜图:%s] JM 标题查询开始: 查询词=%d", trace_id, len(queries))

        async def resolve_one(query: str) -> JmResolution:
            query_started = time.monotonic()
            logger.info("[搜图:%s] JM 标题请求开始: title=%s", trace_id, query)
            async with self._jm_semaphore:
                if total_timeout_seconds is None:
                    result = await self._jm_resolver.resolve(query)
                else:
                    result = await self._jm_resolver.resolve(
                        query, total_timeout_seconds=total_timeout_seconds
                    )
            logger.info(
                "[搜图:%s] JM 标题请求结果: title=%s, status=%s, candidates=%d, elapsed=%.2fs",
                trace_id,
                query,
                result.status,
                len(result.candidates) + (1 if result.selected else 0),
                time.monotonic() - query_started,
            )
            return result

        task_queries = {
            asyncio.create_task(resolve_one(query)): query for query in queries
        }
        results: list[JmResolution] = []
        pending: set[asyncio.Task[JmResolution]] = set(task_queries)
        timed_out = False
        timeout = (
            total_timeout_seconds
            if total_timeout_seconds is not None
            else self._jm_settings.timeout_seconds + 1
        )
        deadline = started + timeout
        try:
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                done, pending = await asyncio.wait(
                    pending,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    timed_out = True
                    break
                for task in done:
                    try:
                        result = task.result()
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        query = task_queries[task]
                        result = JmResolution(query, "failed", reason="task_error")
                    results.append(result)
                    if result.status == "matched" and result.selected is not None:
                        logger.info("[搜图:%s] JM 标题查询结果: 命中", trace_id)
                        return result
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        candidates: dict[str, JmCandidate] = {}
        for result in results:
            items = result.candidates + ((result.selected,) if result.selected else ())
            for candidate in items:
                existing = candidates.get(candidate.album_id)
                if existing is None or candidate.score > existing.score:
                    candidates[candidate.album_id] = candidate
        ranked = tuple(sorted(candidates.values(), key=lambda value: (-value.score, value.album_id)))
        logger.info(
            "[搜图:%s] JM 标题查询结果: %s, 候选=%d",
            trace_id,
            "超时" if timed_out else ("请求失败" if not ranked and any(r.status == "failed" for r in results) else "未命中"),
            len(ranked),
        )
        if not ranked:
            if timed_out:
                return JmResolution(queries[0], "failed", reason="partial_timeout")
            if any(result.status == "failed" for result in results):
                return JmResolution(queries[0], "failed", reason="all_queries_failed")
            return JmResolution(queries[0], "not_found")
        first = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        margin = first.score - second.score if second else float("inf")
        if (
            first.score >= self._jm_settings.auto_select_threshold
            and margin >= self._jm_settings.auto_select_margin
        ):
            return JmResolution(queries[0], "matched", selected=first)
        return JmResolution(
            queries[0],
            "candidates",
            candidates=ranked[: self._jm_settings.max_display_candidates],
        )


    async def _resolve_jm_authors(
        self,
        authors: tuple[str, ...],
        *,
        trace_id: str,
        total_timeout_seconds: float | None = None,
    ) -> JmResolution:
        logger.info("[搜图:%s] JM 作者查询开始: 作者=%d", trace_id, len(authors))

        async def resolve_one(author: str) -> JmResolution:
            author_started = time.monotonic()
            logger.info("[搜图:%s] JM 作者请求开始: author=%s", trace_id, author)
            async with self._jm_semaphore:
                if total_timeout_seconds is None:
                    result = await self._jm_resolver.search_author(author)
                else:
                    result = await self._jm_resolver.search_author(
                        author, total_timeout_seconds=total_timeout_seconds
                    )
            logger.info(
                "[搜图:%s] JM 作者请求结果: author=%s, status=%s, candidates=%d, elapsed=%.2fs",
                trace_id,
                author,
                result.status,
                len(result.candidates) + (1 if result.selected else 0),
                time.monotonic() - author_started,
            )
            return result

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(resolve_one(author) for author in authors)),
                timeout=(
                    total_timeout_seconds
                    if total_timeout_seconds is not None
                    else self._jm_settings.timeout_seconds + 1
                ),
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.info("[搜图:%s] JM 作者查询结果: 超时", trace_id)
            return JmResolution(authors[0], "failed", reason="author_timeout")
        except Exception as exc:
            logger.info("[搜图:%s] JM 作者查询结果: 请求失败（%s）", trace_id, type(exc).__name__)
            return JmResolution(authors[0], "failed", reason="author_error")
        unique: dict[str, JmCandidate] = {}
        for result in results:
            for candidate in result.candidates:
                unique.setdefault(candidate.album_id, candidate)
        candidates = tuple(sorted(unique.values(), key=lambda item: item.album_id))
        has_timeout = any(result.reason == "timeout" for result in results)
        logger.info(
            "[搜图:%s] JM 作者查询结果: %s, 候选=%d",
            trace_id,
            "超时" if has_timeout else ("请求失败" if not candidates and any(result.status == "failed" for result in results) else "完成"),
            len(candidates),
        )
        if not candidates:
            if any(result.status == "failed" for result in results):
                return JmResolution(
                    authors[0], "failed",
                    reason="author_timeout" if has_timeout else "author_error"
                )
            return JmResolution(authors[0], "not_found")
        return JmResolution(authors[0], "candidates", candidates=candidates)


    @staticmethod
    def _extract_manual_jm_title(event: AstrMessageEvent) -> str:
        """Read a title argument, or plain text from the quoted message."""
        reply = None
        current_parts: list[str] = []
        for segment in list(event.get_messages() or []):
            if isinstance(segment, Comp.Reply):
                reply = segment
            elif isinstance(segment, Comp.Plain):
                current_parts.append(str(getattr(segment, "text", "") or ""))

        current_text = "".join(current_parts).strip()
        if not current_text:
            current_text = str(event.get_message_str() or "").strip()
        for prefix in ("/搜本", "搜本"):
            if current_text.startswith(prefix):
                current_text = current_text[len(prefix):].strip()
                break
        if current_text:
            return current_text

        if reply is None:
            return ""
        quoted_parts = [
            str(getattr(segment, "text", "") or "")
            for segment in list(getattr(reply, "chain", None) or [])
            if isinstance(segment, Comp.Plain)
        ]
        quoted_text = "".join(quoted_parts).strip()
        if not quoted_text:
            quoted_text = str(getattr(reply, "message_str", "") or "").strip()
        return quoted_text


    async def _background_manual_jm_search(self, *, umo: str, title: str, cooldown_key: str, sender_id: str = "") -> None:
        trace_id = uuid.uuid4().hex[:8]
        started = time.monotonic()
        logger.info("[手动搜本:%s] 名称检索任务开始: title=%s", trace_id, title)
        response = SearchResponse(
            result_id=None,
            image_url=None,
            items=(SearchItem(100.0, title, "manual", "手动标题"),),
        )
        try:
            resolution = await self._resolve_jm_for_response(
                response,
                trace_id=trace_id,
                umo=umo,
                announce_title_search=False,
            )
            message = self._format_jm_resolution_for_user(resolution)
            if resolution.reason == "disabled":
                message = "JM 查询功能当前已关闭。"
            if not message:
                message = "未能从该名称检索到 JM 作品。"
            await self._send_jm_resolution(umo, sender_id, resolution, message)
            logger.info(
                "[手动搜本:%s] 名称检索任务完成: status=%s, elapsed=%.2fs",
                trace_id,
                resolution.status,
                time.monotonic() - started,
            )
        except asyncio.CancelledError:
            logger.info("[手动搜本:%s] 名称检索任务已取消", trace_id)
            raise
        except Exception as exc:
            logger.error(
                "[手动搜本:%s] 名称检索任务失败: error=%s",
                trace_id,
                type(exc).__name__,
                exc_info=True,
            )
            try:
                await self._send_chain(
                    umo,
                    MessageChain([Comp.Plain("JM 名称检索发生错误，请稍后重试。")]),
                )
            except Exception:
                logger.warning("[手动搜本:%s] 错误提示发送失败", trace_id)
        finally:
            await self._cooldown.release(cooldown_key)
