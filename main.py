from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star


from .client import SoutubotClient
from .config_layout import normalize_config
from .nickname_commands import NicknameCommandFilter
from .help_text import build_help
from .jm_send_patch import MessageNotSentError, safe_sender_id
from .jm_resolver import JmCandidate, JmResolution, JmResolver, JmResolverSettings
from .jm_downloader import JmDownloadService, JmDownloadSettings, JmDownloadTimeoutError, JmPartialDownloadError, JmPdfGenerationError, normalize_jm_id
from .policy import AccessPolicy, CooldownManager
from .nh_downloader import NhDownloadService, NhDownloadSettings, normalize_nh_id
from .multi_search import MultiSearchService
from .google_lens_client import GoogleLensClient
from .findchicken_client import FindChickenClient
from .findchicken_workflow import FindChickenReplyFilter, FindChickenWorkflow


from .workflow_common import JmDownloadReplyFilter, _bounded_int, _bounded_float, _string_list
from .jm_workflow import JmWorkflow
from .search_workflow import SearchWorkflow
from .download_workflow import DownloadWorkflow
from .download_commands import ExplicitDownloadFilter, parse_download_command

class Main(FindChickenWorkflow, SearchWorkflow, JmWorkflow, DownloadWorkflow, Star):
    """通过搜图Bot酱查询当前消息或引用消息中的图片。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = normalize_config(config or {})
        self._download_confirmation_timeout = _bounded_int(
            self.config.get("download_confirmation_timeout_seconds"), 600, 30, 3600
        )
        self._policy = AccessPolicy(self.config.get("whitelist", []))
        self._cooldown = CooldownManager(
            _bounded_int(self.config.get("cooldown_seconds"), 30, 0, 3600)
        )
        self._timeout = _bounded_int(
            self.config.get("timeout_seconds"), 300, 10, 600
        )
        self._search_result_timeout = _bounded_int(
            self.config.get("search_result_timeout_seconds"), 60, 5, 600
        )
        self._top_k = _bounded_int(self.config.get("top_k"), 25, 1, 50)
        self._max_results = _bounded_int(
            self.config.get("max_results"), 3, 1, 10
        )
        self._min_similarity = _bounded_float(
            self.config.get("min_similarity"), 80.0, 0.0, 100.0
        )
        self._show_match_page_url = bool(
            self.config.get("show_match_page_url", True)
        )
        self._show_detail_page_url = bool(
            self.config.get("show_detail_page_url", True)
        )
        self._show_preview_image = bool(
            self.config.get("show_preview_image", True)
        )
        self._max_pending_tasks = _bounded_int(
            self.config.get("max_pending_tasks"), 20, 1, 100
        )
        self._strict_default = bool(self.config.get("strict_mode", False))
        max_concurrency = _bounded_int(
            self.config.get("max_concurrency"), 2, 1, 8
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._llm_tools_enabled = bool(self.config.get("llm_tools_enabled", True))
        self._jm_llm_enabled = self._llm_tools_enabled and bool(self.config.get("jm_llm_enabled", True))
        self._jm_llm_timeout = _bounded_int(
            self.config.get("jm_llm_timeout_seconds"), 20, 3, 60
        )
        self._jm_max_queries = _bounded_int(
            self.config.get("jm_max_queries"), 3, 1, 5
        )
        self._jm_max_authors = _bounded_int(
            self.config.get("jm_max_authors"), 2, 1, 3
        )
        self._jm_llm_verify_threshold = _bounded_float(
            self.config.get("jm_llm_verify_threshold"), 85, 70, 100
        )
        self._jm_query_timeout = 20
        self._jm_title_search_enabled = bool(
            self.config.get("jm_title_search_enabled", True)
        )
        self._jm_author_search_enabled = bool(
            self.config.get("jm_author_search_enabled", True)
        )
        self._jm_author_query_timeout = 20
        self._jm_result_message_settle_seconds = 1.5
        self._jm_notice_sequence_delay_seconds = 0.8
        self._jm_semaphore = asyncio.Semaphore(
            _bounded_int(self.config.get("jm_max_concurrency"), 5, 1, 5)
        )
        self._jm_settings = JmResolverSettings(
                enabled=bool(self.config.get("jm_enabled", True)),
                timeout_seconds=20,
                retry_times=_bounded_int(self.config.get("jm_retry_times"), 0, 0, 1),
                domain_list=_string_list(self.config.get("jm_domain_list", [])),
                proxy=str(self.config.get("jm_proxy", "system") or ""),
                auto_select_threshold=_bounded_float(self.config.get("jm_auto_select_threshold"), 90, 80, 100),
                min_candidate_threshold=_bounded_float(self.config.get("jm_min_candidate_threshold"), 55, 0, 90),
                auto_select_margin=_bounded_float(self.config.get("jm_auto_select_margin"), 8, 0, 30),
                author_max_pages=_bounded_int(self.config.get("jm_author_max_pages"), 3, 1, 5),
                max_author_candidates=_bounded_int(self.config.get("jm_author_max_candidates"), 60, 10, 100),
            )
        self._jm_resolver = JmResolver(self._jm_settings)
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            astrbot_data = Path(get_astrbot_data_path())
        except Exception:
            # AstrBot's documented default is <AstrBot root>/data/plugin_data.
            astrbot_data = Path.cwd() / "data"
        download_root = astrbot_data / "plugin_data" / "astrbot_plugin_soutubot" / "jm"
        # One public JM switch controls queries, automatic routing and downloads.
        self._jm_download_enabled = self._jm_settings.enabled
        self._jm_max_pdf_size = _bounded_int(self.config.get("jm_max_pdf_size_mb"), 200, 1, 500) * 1024 * 1024
        self._jm_cleanup_pdf_after_send = bool(self.config.get("jm_cleanup_pdf_after_send", False))
        self._jm_manage_users = set(_string_list(self.config.get("jm_manage_users", [])))
        JmDownloadReplyFilter.pending.clear()
        self._jm_download_service = JmDownloadService(
            JmDownloadSettings(
                root_dir=download_root,
                max_pending_tasks=self._max_pending_tasks,
                timeout_seconds=_bounded_int(self.config.get("jm_download_timeout_seconds"), 600, 60, 3600),
                max_concurrency=_bounded_int(self.config.get("jm_download_max_concurrency"), 1, 1, 4),
                image_concurrency=_bounded_int(self.config.get("jm_download_image_concurrency"), 10, 1, 30),
                photo_concurrency=_bounded_int(self.config.get("jm_download_photo_concurrency"), 2, 1, 8),
                retry_times=self._jm_settings.retry_times,
                domain_list=self._jm_settings.domain_list,
                proxy=self._jm_settings.proxy,
                pdf_cache_enabled=bool(self.config.get("jm_pdf_cache_enabled", True)),
                pdf_cache_ttl_hours=_bounded_int(self.config.get("jm_pdf_cache_ttl_hours"), 168, 0, 8760),
                cleanup_images_after_pdf=bool(self.config.get("jm_cleanup_images_after_pdf", True)),
                pdf_password_enabled=bool(self.config.get("jm_pdf_password_enabled", True)),
                pdf_fixed_password=str(self.config.get("jm_pdf_fixed_password", "") or ""),
            )
        )
        self._nh_download_enabled = bool(self.config.get("nh_download_enabled", True))
        self._nh_max_pdf_size = _bounded_int(self.config.get("nh_max_pdf_size_mb"), 200, 1, 500) * 1024 * 1024
        self._google_lens = GoogleLensClient(self.config)
        self._multi_search = MultiSearchService(self.config, google_lens=self._google_lens)
        self._findchicken_enabled = bool(self.config.get("findchicken_enabled", True))
        self._findchicken_timeout = _bounded_int(self.config.get("findchicken_timeout_seconds"), 30, 5, 120)
        self._findchicken_results = _bounded_int(self.config.get("findchicken_candidate_limit"), 30, 1, 60)
        self._findchicken_llm_enabled = bool(self.config.get("findchicken_llm_enabled", True))
        self._findchicken_llm_provider = str(self.config.get("findchicken_llm_provider", "") or "").strip()
        self._findchicken_llm_timeout = _bounded_int(self.config.get("findchicken_llm_timeout_seconds"), 60, 10, 180)
        self._findchicken_threshold = _bounded_float(self.config.get("findchicken_similarity_threshold"), 30, 0, 100)
        self._findchicken_show_similarity = bool(self.config.get("findchicken_show_similarity", False))
        self._findchicken_cooldown = CooldownManager(
            _bounded_int(self.config.get("findchicken_cooldown_seconds"), 10, 0, 600)
        )
        self._findchicken_client = FindChickenClient(
            str(self.config.get("findchicken_api_key", "") or ""),
            str(self.config.get("findchicken_proxy", "") or ""),
            self._findchicken_timeout, self._findchicken_results,
        )
        self._findchicken_tasks: set[asyncio.Task] = set()
        self._findchicken_semaphore = asyncio.Semaphore(2)
        self._nh_download_service = NhDownloadService(NhDownloadSettings(
            root_dir=download_root.parent / "nh",
            timeout_seconds=_bounded_int(self.config.get("nh_download_timeout_seconds"), 600, 60, 3600),
            max_concurrency=_bounded_int(self.config.get("nh_download_max_concurrency"), 1, 1, 4),
            max_pending_tasks=self._max_pending_tasks,
            image_concurrency=_bounded_int(self.config.get("nh_image_concurrency"), 4, 1, 10),
            pdf_password_enabled=bool(self.config.get("nh_pdf_password_enabled", True)),
            pdf_fixed_password=str(self.config.get("nh_pdf_fixed_password", "") or ""),
            pdf_cache_enabled=bool(self.config.get("nh_pdf_cache_enabled", True)),
        ))
        self._deliveries = {}
        self._delivery_states = {}
        self._client = SoutubotClient(timeout_seconds=self._timeout)
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        self._nickname_trigger_token = object()
        NicknameCommandFilter.bind(
            str(self.config.get("trigger_nickname", "") or ""), self._nickname_trigger_token
        )

    @staticmethod
    def _cooldown_key(event: AstrMessageEvent) -> str:
        platform = event.get_platform_name() or "unknown"
        sender = event.get_sender_id() or "unknown"
        return f"{platform}:{sender}"

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        return self._policy.is_allowed(
            group_id=event.get_group_id(), sender_id=event.get_sender_id()
        )


    async def _claim_request(self, event: AstrMessageEvent) -> tuple[bool, str]:
        if not self._is_allowed(event):
            return False, "当前会话不在搜本子插件白名单内。"
        remaining = await self._cooldown.claim(self._cooldown_key(event))
        if remaining:
            return False, f"请求冷却中，请等待 {remaining} 秒后再试。"
        return True, ""


    async def _send_chain(self, umo: str, chain: MessageChain) -> None:
        if await self.context.send_message(umo, chain) is False:
            raise MessageNotSentError("未找到可用的发送平台")


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
        """先查 soutubot，未达标时多引擎兜底，达标作品等待回复确认下载。"""
        async for result in self._start_image_search(event, multi_only=False):
            yield result

    @filter.command("找鸡")
    async def findchicken_command(self, event: AstrMessageEvent):
        """Find the author of an attached, quoted or linked image."""
        async for result in self._start_findchicken(event):
            yield result

    @filter.custom_filter(FindChickenReplyFilter, priority=100)
    async def findchicken_quoted_command(self, event: AstrMessageEvent):
        """Handle QQ reply auto-mentions through the same admission rules."""
        async for result in self._start_findchicken(event):
            yield result

    @filter.command("搜图")
    async def search_image_command(self, event: AstrMessageEvent):
        """直接查询已开启的多引擎，只展示图片来源，不下载作品。"""
        async for result in self._start_image_search(event, multi_only=True):
            yield result

    @filter.custom_filter(NicknameCommandFilter, priority=100)
    async def nickname_image_command(self, event: AstrMessageEvent):
        """Route an exact nickname request through the existing admission checks."""
        command = getattr(event, "_soutubot_nickname_command", None)
        if command == "找鸡":
            operation = self._start_findchicken(event)
        elif command in ("找本", "搜图"):
            operation = self._start_image_search(event, multi_only=command == "搜图")
        else:
            return
        async for result in operation:
            yield result

    async def _start_image_search(self, event: AstrMessageEvent, *, multi_only: bool):
        """Share image acquisition and admission checks across both commands."""

        # A global wake word may also activate the standard command handler.
        if getattr(event, "_soutubot_nickname_command", None):
            if getattr(event, "_soutubot_nickname_started", False):
                return
            event._soutubot_nickname_started = True
        event.stop_event()
        command = "搜图" if multi_only else "找本"
        allowed, message = await self._claim_request(event)
        if not allowed:
            logger.info("搜图指令被策略拒绝: %s", message)
            yield event.plain_result(message)
            return
        if multi_only and not self._multi_search.enabled:
            await self._cooldown.release(self._cooldown_key(event))
            yield event.plain_result("多引擎搜图当前已关闭，请管理员在插件配置中开启后再使用 /搜图。")
            return
        image = await self._extract_image(event)
        if image is None:
            logger.info("搜图指令未找到可用图片")
            await self._cooldown.release(self._cooldown_key(event))
            yield event.plain_result(
                f"请在同一条消息中附带图片，或引用一条图片消息后发送 /{command}。"
            )
            return
        if self._closing or len(self._tasks) >= self._max_pending_tasks:
            logger.warning(
                "搜图指令拒绝排队: closing=%s, pending=%d, max_pending=%d",
                self._closing,
                len(self._tasks),
                self._max_pending_tasks,
            )
            await self._cooldown.release(self._cooldown_key(event))
            yield event.plain_result("当前待处理任务较多，请稍后再试。")
            return
        try:
            image_bytes, filename = await asyncio.wait_for(
                self._materialize_image(image), timeout=60
            )
        except Exception as exc:
            logger.warning("获取待搜图片失败: %s", type(exc).__name__)
            await self._cooldown.release(self._cooldown_key(event))
            yield event.plain_result(self._user_error(exc))
            return

        if multi_only:
            operation = self._background_multi_search(
                umo=event.unified_msg_origin,
                image_bytes=image_bytes,
                filename=filename,
            )
        else:
            operation = self._background_search(
                umo=event.unified_msg_origin,
                image_bytes=image_bytes,
                filename=filename,
                strict=self._strict_default,
                sender_id=str(event.get_sender_id()),
            )
        task = asyncio.create_task(
            operation,
            name="astrbot_plugin_soutubot.multi_search" if multi_only else "astrbot_plugin_soutubot.search",
        )
        self._track_task(task)
        yield event.plain_result(
            "正在通过多引擎查询图片来源，完成后会在当前会话发送结果。"
            if multi_only else "正在查询图片来源，完成后会在当前会话发送结果。"
        )

    @filter.command("搜本")
    async def search_jm_title_command(self, event: AstrMessageEvent):
        """直接按名称检索 JM；也可回复标题消息后发送 /搜本。"""

        event.stop_event()
        if not self._jm_settings.enabled:
            yield event.plain_result("JM 功能当前已关闭，查询和下载均不可用。")
            return
        allowed, message = await self._claim_request(event)
        if not allowed:
            logger.info("JM 名称检索指令被策略拒绝: %s", message)
            yield event.plain_result(message)
            return

        title = self._extract_manual_jm_title(event)
        if not title:
            await self._cooldown.release(self._cooldown_key(event))
            yield event.plain_result("请使用 /搜本 标题，或回复一条包含作品名称的消息后发送 /搜本。")
            return
        if len(title) > 1000:
            await self._cooldown.release(self._cooldown_key(event))
            yield event.plain_result("标题文本过长，请只发送作品名称后重试。")
            return
        if self._closing or len(self._tasks) >= self._max_pending_tasks:
            await self._cooldown.release(self._cooldown_key(event))
            yield event.plain_result("当前待处理任务较多，请稍后再试。")
            return

        logger.info("手动 JM 检索请求已接收: title_length=%d", len(title))
        task = asyncio.create_task(
            self._background_manual_jm_search(
                umo=event.unified_msg_origin,
                title=title,
                cooldown_key=self._cooldown_key(event),
                sender_id=str(event.get_sender_id()),
            ),
            name="astrbot_plugin_soutubot.manual_jm_search",
        )
        self._track_task(task)
        yield event.plain_result("正在分析名称并检索 JM 作品，请稍候。")


    @filter.custom_filter(ExplicitDownloadFilter, priority=100)
    async def explicit_download_command(self, event: AstrMessageEvent):
        """Dispatch a standalone JM/NH ID directly, independent of LLM settings."""
        request = parse_download_command(event.message_str)
        if request is None:
            return
        # Stop ordinary handlers and the conversational LLM before any await;
        # spaced commands may also match their original CommandFilter.
        event.stop_event()
        if not self._is_allowed(event):
            yield event.plain_result("当前会话不在插件白名单内。")
            return
        source, album_id = request
        logger.info("明确编号下载指令: source=%s, id=%s", source.upper(), album_id)
        message = await self._queue_download(
            event.unified_msg_origin, safe_sender_id(event), source, album_id
        )
        if message:
            yield event.plain_result(message)

    @filter.command("jm", alias={"JM", "Jm", "jM"})
    async def jm_download_command(self, event: AstrMessageEvent):
        """Download one JM album as a PDF after an explicit user request."""
        event.stop_event()
        if not self._jm_download_enabled:
            yield event.plain_result("JM 功能当前已关闭，查询和下载均不可用。")
            return
        if not self._is_allowed(event):
            yield event.plain_result("当前会话不在搜本子插件白名单内。")
            return
        try:
            command_parts = (event.message_str or "").split()
            raw_id = command_parts[-1] if command_parts else ""
            album_id = normalize_jm_id(raw_id)
        except ValueError:
            yield event.plain_result("请使用 /jm 123456 或 /jm JM123456。")
            return
        message = await self._start_jm_download(event, album_id)
        if message:
            yield event.plain_result(message)

    @filter.command("nh", alias={"NH", "Nh", "nH"})
    async def nh_download_command(self, event: AstrMessageEvent):
        """Download an explicit NH gallery, always generate PDF and send it."""
        event.stop_event()
        if not self._is_allowed(event):
            yield event.plain_result("当前会话不在插件白名单内。")
            return
        try:
            parts = (event.message_str or "").split()
            album_id = normalize_nh_id(parts[-1] if parts else "")
        except ValueError:
            yield event.plain_result("请使用 /nh 编号 或 /nh https://nhentai.net/g/编号/。")
            return
        message = await self._queue_download(event.unified_msg_origin, safe_sender_id(event), "nh", album_id)
        if message:
            yield event.plain_result(message)

    def _tool_denied(self, event):
        if not self._llm_tools_enabled:
            return "本插件的 LLM 总开关已关闭，请使用固定指令；JM 查询将直接使用原始标题。"
        if not self._is_allowed(event):
            return "当前会话不在插件白名单内。"
        return ""

    @filter.llm_tool(name="soutubot_search_tool")
    async def soutubot_search_tool(self, event: AstrMessageEvent):
        """仅在用户明确要求根据当前或引用图片找本子/漫画作品时调用，对应 /找本。

        先查 soutubot；达标作品先询问是否下载，用户回复“下载”后才进入 NH/JM 下载、PDF 生成和发送。
        未达标时才运行已开启的多引擎搜图。普通发图不调用此工具。
        通用图片出处查询请使用 /搜图，它直接查询多引擎且不下载作品。
        """
        denied = self._tool_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        async for result in self.search_doujin_command(event):
            yield result

    @filter.llm_tool(name="jm_search_tool")
    async def jm_search_tool(self, event: AstrMessageEvent, title: str):
        """用户明确要求按作品名称搜索 JM 时调用，返回匹配或候选。

        Args:
            title(string): 用户提供的作品名称或搜索结果中已有的标题，不编造标题。
        """
        denied = self._tool_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        event.stop_event()
        if not self._jm_settings.enabled:
            yield event.plain_result("JM 功能当前已关闭，查询和下载均不可用。")
            return
        allowed, message = await self._claim_request(event)
        if not allowed:
            yield event.plain_result(message)
            return
        title = str(title or "").strip()
        if not title or len(title) > 1000 or self._closing or len(self._tasks) >= self._max_pending_tasks:
            await self._cooldown.release(self._cooldown_key(event))
            yield event.plain_result("请提供有效作品名称，并在待处理任务减少后重试。")
            return
        self._track_task(asyncio.create_task(self._background_manual_jm_search(
            umo=event.unified_msg_origin, title=title, cooldown_key=self._cooldown_key(event), sender_id=safe_sender_id(event))))
        yield event.plain_result("正在分析名称并检索 JM 作品，请稍候。")

    @filter.llm_tool(name="jm_download_tool")
    async def jm_download_tool(self, event: AstrMessageEvent, album_id: str):
        """用户明确要求下载指定 JM 编号时调用，下载后生成 PDF 并发送。

        JM 来源标记不区分大小写。识别“下载 JM123456”“帮我把 Jm 123456
        转成 PDF”等自然语言下载请求，提取编号 123456 调用本工具。
        单独的 JM123456、jm 123456、/Jm123456 已由固定入口处理，无需重复调用。
        仅讨论、询问或否定下载的消息不调用；编号只能来自用户或本次检索结果。

        Args:
            album_id(string): 用户明确给出或本次检索已返回的 JM 编号，不能猜测或编造。
        """
        denied = self._tool_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        event.stop_event()
        try:
            identifier = normalize_jm_id(album_id)
        except ValueError:
            yield event.plain_result("请提供明确的 JM 编号。")
            return
        message = await self._start_jm_download(event, identifier)
        if message:
            yield event.plain_result(message)

    @filter.llm_tool(name="nhentai_download_tool")
    async def nhentai_download_tool(self, event: AstrMessageEvent, gallery_id: str):
        """用户明确要求下载指定 NH 作品时调用，下载后生成 PDF 并发送。

        NH 来源标记不区分大小写。识别“下载 NH123456”“帮我把 Nh 123456
        转成 PDF”或要求下载 nhentai.net 作品链接的自然语言请求。
        单独的 NH123456、nh 123456、/Nh123456 已由固定入口处理，无需重复调用。
        仅讨论、询问或否定下载的消息不调用；编号只能来自用户或本次检索结果。

        Args:
            gallery_id(string): 用户明确给出或本次检索已返回的 NH 编号或 nhentai.net 作品链接，不能猜测或编造。
        """
        denied = self._tool_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        event.stop_event()
        try:
            identifier = normalize_nh_id(gallery_id)
        except ValueError:
            yield event.plain_result("请提供明确的 NH 编号或作品链接。")
            return
        message = await self._queue_download(event.unified_msg_origin, safe_sender_id(event), "nh", identifier)
        if message:
            yield event.plain_result(message)

    @filter.custom_filter(JmDownloadReplyFilter, priority=100)
    async def jm_download_reply(self, event: AstrMessageEvent):
        """Consume a searcher's one-time confirmation without requiring a prefix."""
        request = JmDownloadReplyFilter.peek_request(event)
        if request is None:
            return
        # Pop before the first await so two incoming replies cannot consume it twice.
        JmDownloadReplyFilter.pending.pop(
            (event.unified_msg_origin, str(event.get_sender_id())), None
        )
        event.stop_event()
        if not getattr(self, f"_{request.source}_download_enabled", False):
            yield event.plain_result(f"{request.source.upper()} 下载功能当前已关闭。")
            return
        if not self._is_allowed(event):
            yield event.plain_result("当前会话不在搜本子插件白名单内。")
            return
        logger.info("[下载确认] 已确认：%s%s", request.source.upper(), request.album_id)
        message = await self._queue_download(event.unified_msg_origin, safe_sender_id(event), request.source, request.album_id)
        if message:
            yield event.plain_result(message)


    @filter.command("搜图帮助", alias={"jm帮助", "JM帮助"})
    async def jm_download_help_command(self, event: AstrMessageEvent):
        event.stop_event()
        yield event.plain_result(build_help(
            str(getattr(self, "config", {}).get("trigger_nickname", "") or ""),
        ))

    @filter.command("jm清理")
    async def jm_cleanup_command(self, event: AstrMessageEvent):
        event.stop_event()
        sender = str(event.get_sender_id() or "")
        is_admin = getattr(event, "role", "") == "admin"
        if not self._is_allowed(event) or not (self._jm_manage_users and (is_admin or sender in self._jm_manage_users)):
            yield event.plain_result("当前用户没有 JM 缓存管理权限。请在 jm_manage_users 配置管理员用户 ID。")
            return
        try:
            result = self._jm_download_service.clean_all()
        except OSError as exc:
            logger.error("[JM清理] 缓存清理失败 | error=%s", type(exc).__name__, exc_info=True)
            yield event.plain_result("❌ JM 缓存清理失败，请稍后重试。")
            return
        logger.info(
            "[JM清理] 完成 | files=%d | bytes=%d | skipped_active=%d",
            result.files_deleted,
            result.bytes_freed,
            result.skipped_active,
        )
        yield event.plain_result(
            f"🧹 JM 缓存清理完成\n🗑️ 删除文件：{result.files_deleted} 个\n💾 释放空间：{result.bytes_freed / 1024 / 1024:.1f} MB"
            + (f"\n⏳ 正在处理的作品已跳过：{result.skipped_active} 个" if result.skipped_active else "")
        )

    async def terminate(self) -> None:
        self._closing = True
        NicknameCommandFilter.unbind(getattr(self, "_nickname_trigger_token", None))
        chicken_tasks = tuple(self._findchicken_tasks)
        for task in chicken_tasks:
            task.cancel()
        if chicken_tasks:
            await asyncio.gather(*chicken_tasks, return_exceptions=True)
        self._findchicken_tasks.clear()
        await self._findchicken_client.close()
        JmDownloadReplyFilter.pending.clear()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        await self._client.close()
        self._jm_resolver.close()
        await asyncio.gather(self._jm_download_service.close(), self._nh_download_service.close(), self._multi_search.close(), return_exceptions=True)
        await self._google_lens.close()
