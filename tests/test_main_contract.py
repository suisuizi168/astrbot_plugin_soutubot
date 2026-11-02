from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class Image:
    def __init__(self, file=None, **kwargs) -> None:
        self.file = file

    @staticmethod
    def fromURL(url: str):
        return Image(file=url)


class Reply:
    def __init__(self, chain=None, reply_id="reply-1") -> None:
        self.chain = chain
        self.id = reply_id


class Plain:
    def __init__(self, text: str) -> None:
        self.text = text


class File:
    def __init__(self, file=None, name=None) -> None:
        self.file = file
        self.name = name


class MessageChain(list):
    @property
    def chain(self):
        return self


class AstrMessageEvent:
    pass


class Context:
    pass


class Star:
    def __init__(self, context) -> None:
        self.context = context


class Filter:
    class CustomFilter:
        pass

    @staticmethod
    def custom_filter(*args, **kwargs):
        return lambda function: function

    @staticmethod
    def llm_tool(*args, **kwargs):
        return lambda function: function

    @staticmethod
    def command(*args, **kwargs):
        return lambda function: function

class Logger:
    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass


components = types.ModuleType("astrbot.api.message_components")
components.Image = Image
components.Reply = Reply
components.Plain = Plain
components.File = File

api = types.ModuleType("astrbot.api")
api.AstrBotConfig = dict
api.logger = Logger()

event = types.ModuleType("astrbot.api.event")
event.AstrMessageEvent = AstrMessageEvent
event.MessageChain = MessageChain
event.filter = Filter()

star = types.ModuleType("astrbot.api.star")
star.Context = Context
star.Star = Star

astrbot = types.ModuleType("astrbot")
astrbot_api = types.ModuleType("astrbot.api")
astrbot_api.__path__ = []
astrbot_api.AstrBotConfig = dict
astrbot_api.logger = api.logger

sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", astrbot_api)
sys.modules.setdefault("astrbot.api.message_components", components)
sys.modules.setdefault("astrbot.api.event", event)
sys.modules.setdefault("astrbot.api.star", star)

PACKAGE_NAME = Path(__file__).resolve().parents[1].name
main_module = importlib.import_module(f"{PACKAGE_NAME}.main")
models_module = importlib.import_module(f"{PACKAGE_NAME}.models")
Main = main_module.Main


class Event:
    def __init__(self, messages) -> None:
        self._messages = messages

    def get_messages(self):
        return self._messages


class MainContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_jm_help_lists_all_callable_commands(self) -> None:
        class HelpEvent:
            stopped = False

            def stop_event(self):
                self.stopped = True

            def plain_result(self, message):
                return Plain(message)

        event = HelpEvent()
        plugin = object.__new__(Main)
        results = [item async for item in plugin.jm_download_help_command(event)]
        self.assertTrue(event.stopped)
        self.assertEqual(len(results), 1)
        message = results[0].text
        for command in ("/找本", "/搜图", "/找鸡", "/搜本", "下载", "/jm 编号", "/nh 编号", "/搜图帮助", "/jm清理"):
            self.assertIn(command, message)
        self.assertNotIn("确认下载", message)
        self.assertIn("仅缓存管理员", message)
        for provider in ("Google", "谷歌", "SerpAPI"):
            self.assertNotIn(provider, message)

    async def test_jm_completion_survives_handler_close_after_notice(self) -> None:
        class CommandEvent:
            message_str = "/jm 123456"
            unified_msg_origin = "test-session"

            def stop_event(self):
                pass

            def plain_result(self, text):
                return Plain(text)

        for status in ("new", "joined"):
            for failed in (False, True):
                with self.subTest(status=status, failed=failed):
                    completion = asyncio.get_running_loop().create_future()
                    acquisition = types.SimpleNamespace(
                        status=status, wait=lambda: asyncio.shield(completion)
                    )

                    async def acquire(album_id, trace_id):
                        return acquisition

                    sent = []

                    async def send_message(umo, chain):
                        sent.append((umo, chain))

                    plugin = object.__new__(Main)
                    plugin._jm_download_enabled = True
                    plugin._closing = False
                    plugin._tasks = set()
                    plugin._is_allowed = lambda event: True
                    plugin._jm_download_service = types.SimpleNamespace(acquire=acquire)
                    plugin._jm_max_pdf_size = 100 * 1024 * 1024
                    plugin._jm_cleanup_pdf_after_send = False
                    plugin.context = types.SimpleNamespace(send_message=send_message)
                    handler = plugin.jm_download_command(CommandEvent())
                    notice = await anext(handler)
                    self.assertIn("完成后会自动发送 PDF", notice.text)
                    # AstrBot may stop consuming the handler after sending its first result.
                    await handler.aclose()
                    self.assertEqual(sent, [])
                    if failed:
                        completion.set_exception(main_module.JmDownloadTimeoutError())
                    else:
                        completion.set_result(types.SimpleNamespace(
                            pdf_path=Path("123456.pdf"), pdf_size=100,
                            title="", cache_hit=False,
                        ))
                    await asyncio.gather(*list(plugin._tasks))
                    # Consume the fake failure even when the old code created no sender.
                    if failed:
                        completion.exception()
                    self.assertEqual(len(sent), 1 if failed else 2)
                    self.assertEqual(sent[0][0], "test-session")
                    if failed:
                        self.assertIn("处理超时", sent[0][1][0].text)
                    else:
                        self.assertIn("开始上传", sent[0][1][0].text)
                        self.assertIsInstance(sent[1][1][0], File)
                        self.assertEqual(sent[1][1][0].name, "JM123456.pdf")

    async def test_jm_download_user_message_depends_on_acquisition_state(self) -> None:
        class Event:
            message_str = "/jm 123456"
            unified_msg_origin = "test"

            def stop_event(self):
                pass

            def plain_result(self, text):
                return Plain(text)

        for status, expected in (
            ("new", "⬇️ JM123456 正在下载处理中，完成后会自动发送 PDF。"),
            ("joined", "⏳ JM123456 已在处理中，完成后会自动发送 PDF。"),
            ("cache", None),
        ):
            plugin = object.__new__(Main)
            plugin._jm_download_enabled = True
            plugin._closing = False
            plugin._tasks = set()
            plugin._is_allowed = lambda event: True
            plugin._jm_download_service = types.SimpleNamespace(
                acquire=lambda album_id, trace_id: asyncio.sleep(
                    0, result=types.SimpleNamespace(status=status, artifact=None, task=None)
                )
            )
            invoked = asyncio.Event()

            async def background(**kwargs):
                invoked.set()

            plugin._background_download = background
            messages = [item async for item in plugin.jm_download_command(Event())]
            if expected is None:
                self.assertEqual(messages, [])
            else:
                self.assertEqual([item.text for item in messages], [expected])
            await asyncio.wait_for(invoked.wait(), timeout=1)

    async def test_jm_download_enforces_size_limit_and_preserves_pdf_on_send_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "123.pdf"
            pdf.write_bytes(b"pdf")
            artifact = types.SimpleNamespace(
                album_id="123", title="Test", pdf_path=pdf, pdf_size=101 * 1024 * 1024,
                cache_hit=True, duration=0.0,
            )

            class Service:
                async def get_or_download(self, album_id):
                    return artifact

            class SendContext:
                def __init__(self):
                    self.sent = []
                    self.fail_file = False

                async def send_message(self, umo, chain):
                    self.sent.append(chain)
                    if self.fail_file and any(isinstance(item, File) for item in chain):
                        raise RuntimeError("send failed")

            plugin = object.__new__(Main)
            plugin._jm_download_service = Service()
            plugin._jm_max_pdf_size = 100 * 1024 * 1024
            plugin._jm_cleanup_pdf_after_send = False
            plugin.context = SendContext()
            await plugin._background_jm_download(umo="test", album_id="123")
            self.assertFalse(any(isinstance(item, File) for chain in plugin.context.sent for item in chain))
            sent_text = "".join(item.text for chain in plugin.context.sent for item in chain if isinstance(item, Plain))
            self.assertNotIn("缓存", sent_text)
            self.assertIn("100 MB 的发送上限", sent_text)
            self.assertTrue(pdf.exists())

            plugin._jm_max_pdf_size = 200 * 1024 * 1024
            plugin.context = SendContext()
            plugin.context.fail_file = True
            await plugin._background_jm_download(umo="test", album_id="123")
            self.assertTrue(pdf.exists())
            self.assertTrue(any("JM123 未能确认 PDF 发送成功" in item.text for chain in plugin.context.sent for item in chain if isinstance(item, Plain)))

    async def test_jm_download_cache_sends_pdf_without_process_notice_or_wrong_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "123.pdf"
            pdf.write_bytes(b"pdf")
            artifact = types.SimpleNamespace(
                album_id="123", title="", pdf_path=pdf, pdf_size=3,
                cache_hit=True, duration=0.0,
            )

            class Service:
                async def get_or_download(self, album_id):
                    return artifact

            class SendContext:
                def __init__(self):
                    self.sent = []

                async def send_message(self, umo, chain):
                    self.sent.append(chain)

            plugin = object.__new__(Main)
            plugin._jm_download_service = Service()
            plugin._jm_max_pdf_size = 100
            plugin._jm_cleanup_pdf_after_send = False
            plugin.context = SendContext()
            await plugin._background_jm_download(umo="test", album_id="123")
            messages = [item.text for chain in plugin.context.sent for item in chain if isinstance(item, Plain)]
            files = [item for chain in plugin.context.sent for item in chain if isinstance(item, File)]
            self.assertEqual(len(messages), 1)
            self.assertIn("📚 JM123", messages[0])
            self.assertIn("已成功生成 PDF，开始上传...", messages[0])
            self.assertIn("密码：无", messages[0])
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].name, "JM123.pdf")

    async def test_jm_download_error_messages_are_user_friendly(self) -> None:
        cases = (
            (main_module.JmPartialDownloadError(), "❌ JM123 下载不完整，本次未能生成 PDF。\n可以稍后重新发送 /jm 123 重试。"),
            (main_module.JmDownloadTimeoutError(), "❌ JM123 处理超时，请稍后重试。"),
            (main_module.JmPdfGenerationError(), "❌ JM123 PDF 生成失败，请稍后重试。"),
        )

        class SendContext:
            def __init__(self):
                self.sent = []

            async def send_message(self, umo, chain):
                self.sent.append(chain)

        for failure, expected in cases:
            class Service:
                async def get_or_download(self, album_id):
                    raise failure

            plugin = object.__new__(Main)
            plugin._jm_download_service = Service()
            plugin.context = SendContext()
            await plugin._background_jm_download(umo="test", album_id="123")
            actual = "".join(item.text for chain in plugin.context.sent for item in chain if isinstance(item, Plain))
            self.assertEqual(actual, expected)

    async def test_jm_cleanup_without_arguments_clears_all_caches(self) -> None:
        class Event:
            role = "admin"
            message_str = "/jm清理"

            def stop_event(self):
                pass

            def get_sender_id(self):
                return "manager"

            def plain_result(self, text):
                return Plain(text)

        class Service:
            def __init__(self):
                self.calls = 0

            def clean_all(self):
                self.calls += 1
                return types.SimpleNamespace(files_deleted=4, bytes_freed=2 * 1024 * 1024, skipped_active=0)

        plugin = object.__new__(Main)
        plugin._is_allowed = lambda event: True
        plugin._jm_manage_users = {"manager"}
        plugin._jm_download_service = Service()
        messages = [item async for item in plugin.jm_cleanup_command(Event())]
        self.assertEqual(plugin._jm_download_service.calls, 1)
        self.assertEqual(len(messages), 1)
        self.assertIn("JM 缓存清理完成", messages[0].text)
        self.assertIn("释放空间：2.0 MB", messages[0].text)

    async def test_direct_image_has_priority(self) -> None:
        direct = Image()
        quoted = Image()
        found = await Main._extract_image(Event([Reply([quoted]), direct]))
        self.assertIs(found, direct)

    async def test_embedded_quoted_image_is_supported(self) -> None:
        quoted = Image()
        found = await Main._extract_image(Event([Reply([quoted])]))
        self.assertIs(found, quoted)

    async def test_quoted_image_falls_back_to_astrbot_resolver(self) -> None:
        async def resolver(event, reply):
            return ["https://img.example/quoted.jpg"]

        original = importlib.import_module(f"{PACKAGE_NAME}.search_workflow").extract_quoted_message_images
        importlib.import_module(f"{PACKAGE_NAME}.search_workflow").extract_quoted_message_images = resolver
        try:
            found = await Main._extract_image(Event([Reply(None)]))
        finally:
            importlib.import_module(f"{PACKAGE_NAME}.search_workflow").extract_quoted_message_images = original
        self.assertIsInstance(found, Image)
        self.assertEqual(found.file, "https://img.example/quoted.jpg")

    async def test_missing_image_returns_none(self) -> None:
        self.assertIsNone(await Main._extract_image(Event([Plain("/找本")])))

    async def test_result_chain_contains_only_high_similarity_images(self) -> None:
        plugin = object.__new__(Main)
        plugin._max_results = 3
        plugin._min_similarity = 80.0
        plugin._show_match_page_url = True
        plugin._show_detail_page_url = True
        plugin._show_preview_image = True
        response = models_module.SearchResponse(
            result_id="private-result-id",
            image_url=None,
            items=(
                models_module.SearchItem(
                    score=91.0,
                    title="High",
                    source_key="nhentai",
                    source_name="NHentai",
                    thumbnail_url="https://img.example/high.jpg",
                ),
                models_module.SearchItem(
                    score=79.9,
                    title="Low",
                    source_key="nhentai",
                    source_name="NHentai",
                    thumbnail_url="https://img.example/low.jpg",
                ),
            ),
        )
        chain = plugin._build_result_chain(response)
        images = [segment for segment in chain if isinstance(segment, Image)]
        text = "".join(
            segment.text for segment in chain if isinstance(segment, Plain)
        )
        self.assertEqual([image.file for image in images], ["https://img.example/high.jpg"])
        self.assertIn("91.00%", text)
        self.assertNotIn("79.90%", text)
        self.assertNotIn("private-result-id", text)
        self.assertIn("来源于搜图Bot酱", text)

    async def test_result_chain_respects_display_configuration(self) -> None:
        plugin = object.__new__(Main)
        plugin._max_results = 1
        plugin._min_similarity = 50.0
        plugin._show_match_page_url = False
        plugin._show_detail_page_url = False
        plugin._show_preview_image = False
        response = models_module.SearchResponse(
            result_id=None,
            image_url=None,
            items=(
                models_module.SearchItem(
                    score=60.0,
                    title="Shown",
                    source_key="nhentai",
                    source_name="NHentai",
                    thumbnail_url="https://img.example/preview.jpg",
                    page_url="https://example.test/match",
                    source_url="https://example.test/detail",
                ),
                models_module.SearchItem(
                    score=59.0,
                    title="Limited",
                    source_key="nhentai",
                    source_name="NHentai",
                ),
            ),
        )
        chain = plugin._build_result_chain(response)
        images = [segment for segment in chain if isinstance(segment, Image)]
        text = "".join(
            segment.text for segment in chain if isinstance(segment, Plain)
        )
        self.assertEqual(images, [])
        self.assertIn("达到 50%", text)
        self.assertIn("展示前 1 条", text)
        self.assertIn("Shown", text)
        self.assertNotIn("Limited", text)
        self.assertNotIn("匹配页", text)
        self.assertNotIn("详情页", text)

    async def test_result_chain_inserts_jm_before_footer(self) -> None:
        plugin = object.__new__(Main)
        plugin._max_results = 1
        plugin._min_similarity = 50.0
        plugin._show_match_page_url = False
        plugin._show_detail_page_url = False
        plugin._show_preview_image = False
        response = models_module.SearchResponse(
            result_id=None,
            image_url=None,
            items=(models_module.SearchItem(60.0, "Shown", "nhentai", "NHentai"),),
        )
        resolution = main_module.JmResolution(
            "Shown", "matched", main_module.JmCandidate("123", "Shown", 100.0)
        )
        text = "".join(
            segment.text
            for segment in plugin._build_result_chain(response, resolution)
            if isinstance(segment, Plain)
        )
        self.assertIn("JM 已确认：JM123（Shown｜匹配 100.0%）", text)
        self.assertIn('是否需要下载 JM123？', text)
        self.assertNotIn("下载：/jm", text)
        self.assertLess(text.index("JM 已确认：JM123"), text.index("来源于搜图Bot酱"))

    async def test_jm_only_resolves_first_displayed_item(self) -> None:
        plugin = object.__new__(Main)
        plugin._max_results = 3
        plugin._min_similarity = 50.0
        plugin._jm_semaphore = __import__("asyncio").Semaphore(1)
        plugin._jm_llm_enabled = False
        plugin._jm_max_queries = 3
        plugin._jm_max_authors = 2
        plugin._jm_query_timeout = 20
        plugin._jm_llm_verify_threshold = 85
        plugin._jm_settings = main_module.JmResolverSettings()
        plugin._jm_title_search_enabled = True
        plugin._jm_author_search_enabled = False
        calls = []

        class Resolver:
            async def resolve(self, title, *, total_timeout_seconds=None):
                calls.append(title)
                return main_module.JmResolution(title, "not_found")

        plugin._jm_resolver = Resolver()
        response = models_module.SearchResponse(
            result_id=None,
            image_url=None,
            items=(
                models_module.SearchItem(60.0, "First", "nhentai", "NHentai"),
                models_module.SearchItem(55.0, "Second", "nhentai", "NHentai"),
            ),
        )
        result = await plugin._resolve_jm_for_response(response)
        self.assertEqual(result.status, "not_found")
        self.assertEqual(calls, ["First"])

    async def test_jm_returns_immediately_when_one_parallel_query_matches(self) -> None:
        plugin = object.__new__(Main)
        plugin._jm_semaphore = __import__("asyncio").Semaphore(5)
        plugin._jm_settings = main_module.JmResolverSettings()
        slow_cancelled = False

        class Resolver:
            async def resolve(self, title):
                nonlocal slow_cancelled
                if title == "Hit":
                    return main_module.JmResolution(
                        title,
                        "matched",
                        main_module.JmCandidate("123", "Hit", 100.0),
                    )
                try:
                    await __import__("asyncio").sleep(5)
                except __import__("asyncio").CancelledError:
                    slow_cancelled = True
                    raise
                return main_module.JmResolution(title, "not_found")

        plugin._jm_resolver = Resolver()
        result = await plugin._resolve_jm_queries(("Hit", "Slow"), trace_id="test")
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.selected.album_id, "123")
        self.assertTrue(slow_cancelled)

    async def test_jm_skips_when_nothing_is_displayed(self) -> None:
        plugin = object.__new__(Main)
        plugin._max_results = 3
        plugin._min_similarity = 80.0
        response = models_module.SearchResponse(
            result_id=None,
            image_url=None,
            items=(models_module.SearchItem(79.0, "Low", "nhentai", "NHentai"),),
        )
        result = await plugin._resolve_jm_for_response(response)
        self.assertEqual(result.status, "skipped")


class JmConfirmationTests(unittest.IsolatedAsyncioTestCase):
    class ReplyEvent:
        def __init__(self, umo="group-one", sender="user-one", text="下载"):
            self.unified_msg_origin = umo
            self.sender = sender
            self.message_str = text
            self.stopped = False

        def get_sender_id(self):
            return self.sender

        def stop_event(self):
            self.stopped = True

        def plain_result(self, text):
            return Plain(text)

    async def asyncSetUp(self):
        main_module.JmDownloadReplyFilter.pending.clear()
        self.plugin = object.__new__(Main)
        self.plugin._jm_download_enabled = True
        self.plugin._policy = main_module.AccessPolicy()
        self.plugin._is_allowed = lambda event: True
        self.plugin._closing = False
        self.plugin._tasks = set()
        self.plugin._jm_max_pdf_size = 100000
        self.plugin._jm_cleanup_pdf_after_send = False
        self.sent = []

        async def send_message(umo, chain):
            self.sent.append((umo, chain))

        self.plugin.context = types.SimpleNamespace(send_message=send_message)

    async def asyncTearDown(self):
        main_module.JmDownloadReplyFilter.pending.clear()

    def match(self, album_id="123456"):
        return main_module.JmResolution(
            "A title", "matched", main_module.JmCandidate(album_id, "A title", 100)
        )

    async def test_confirmation_is_scoped_and_expires_without_waking(self):
        resolution = self.match()
        with patch.object(main_module.time, "monotonic", return_value=100):
            await self.plugin._send_jm_resolution("group-one", "user-one", resolution, "prompt")
        gate = main_module.JmDownloadReplyFilter()
        with patch.object(main_module.time, "monotonic", return_value=699.9):
            self.assertTrue(gate.filter(self.ReplyEvent(), {}))
            self.assertFalse(gate.filter(self.ReplyEvent(sender="user-two"), {}))
            self.assertFalse(gate.filter(self.ReplyEvent(umo="group-two"), {}))
            self.assertFalse(gate.filter(self.ReplyEvent(text="下载别的"), {}))
        with patch.object(main_module.time, "monotonic", return_value=700):
            expired = self.ReplyEvent()
            self.assertFalse(gate.filter(expired, {}))
            self.assertEqual([item async for item in self.plugin.jm_download_reply(expired)], [])
            self.assertFalse(expired.stopped)

    async def test_latest_match_consumed_once_and_delivers_after_notice(self):
        await self.plugin._send_jm_resolution("group-one", "user-one", self.match("111"), "first")
        await self.plugin._send_jm_resolution("group-one", "user-one", self.match("222"), "latest")
        completion = asyncio.get_running_loop().create_future()
        acquired_ids = []

        async def acquire(album_id, trace_id):
            acquired_ids.append(album_id)
            return types.SimpleNamespace(status="new", wait=lambda: asyncio.shield(completion))

        self.plugin._jm_download_service = types.SimpleNamespace(acquire=acquire)
        handler = self.plugin.jm_download_reply(self.ReplyEvent())
        self.assertIn("JM222", (await anext(handler)).text)
        await handler.aclose()
        repeated = self.ReplyEvent()
        self.assertEqual([item async for item in self.plugin.jm_download_reply(repeated)], [])
        self.assertFalse(repeated.stopped)
        completion.set_result(types.SimpleNamespace(
            title="A title", pdf_path=Path("222.pdf"), pdf_size=20,
            cache_hit=False, password="222",
        ))
        await asyncio.gather(*list(self.plugin._tasks))
        self.assertEqual(acquired_ids, ["222"])
        self.assertIn("密码：222", self.sent[-2][1][0].text)
        self.assertIn("A title", self.sent[-2][1][0].text)
        self.assertIsInstance(self.sent[-1][1][0], File)
        self.assertEqual(self.sent[-1][1][0].name, "JM222.pdf")

    async def test_failed_prompt_and_ambiguous_results_do_not_enable_reply(self):
        async def fail(*args):
            raise RuntimeError("send failed")

        self.plugin.context.send_message = fail
        with self.assertRaises(RuntimeError):
            await self.plugin._send_jm_resolution("group-one", "user-one", self.match(), "prompt")
        self.assertIsNone(main_module.JmDownloadReplyFilter.peek(self.ReplyEvent()))
        resolution = main_module.JmResolution("title", "candidates")
        self.assertNotIn("10 分钟", self.plugin._format_jm_resolution_for_user(resolution) or "")

    async def test_reply_obeys_whitelist_and_disabled_downloads(self):
        for enabled, allowed in ((False, True), (True, False)):
            main_module.JmDownloadReplyFilter.remember("group-one", "user-one", "123")
            self.plugin._jm_download_enabled = enabled
            self.plugin._is_allowed = lambda event: allowed
            # No download service is present; a policy violation must return before acquisition.
            messages = [item async for item in self.plugin.jm_download_reply(self.ReplyEvent())]
            self.assertEqual(len(messages), 1)
            self.assertFalse(self.plugin._tasks)

    async def test_fixed_password_is_shown_before_upload(self):
        async def download(album_id):
            return types.SimpleNamespace(
                title="Title", pdf_path=Path("123.pdf"), pdf_size=1024,
                cache_hit=True, password="008899",
            )

        self.plugin._jm_download_service = types.SimpleNamespace(get_or_download=download)
        await self.plugin._background_jm_download(umo="group-one", album_id="123")
        self.assertIn("密码：008899", self.sent[0][1][0].text)
        self.assertIn("Title", self.sent[0][1][0].text)
        self.assertIsInstance(self.sent[1][1][0], File)

    async def test_manual_search_keeps_original_confirmation(self):
        async def resolve(*args, **kwargs): return self.match()
        async def release(key): pass
        self.plugin._resolve_jm_for_response = resolve
        self.plugin._cooldown = types.SimpleNamespace(release=release)
        await self.plugin._background_manual_jm_search(
            umo="group-one", sender_id="user-one", title="A title", cooldown_key="key")
        self.assertIn("10 分钟", self.sent[-1][1][0].text)
        self.assertEqual(main_module.JmDownloadReplyFilter.peek(self.ReplyEvent()), "123456")


if __name__ == "__main__":
    unittest.main()
