"""Offline checks of public entries and NH transport, without external requests."""
import asyncio
import importlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image
import test_main_contract as contract

nh = importlib.import_module(f"{contract.PACKAGE_NAME}.nh_downloader")


class Event:
    unified_msg_origin = "p:GroupMessage:1"
    message_str = ""
    stopped = False

    def stop_event(self):
        self.stopped = True

    def get_sender_id(self):
        return "2"

    def plain_result(self, text):
        return contract.Plain(text)


class EntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = contract.Main(types.SimpleNamespace(), {"nh_download_enabled": True})
        self.plugin._is_allowed = lambda event: True
        self.plugin._queue_download = AsyncMock(return_value="queued")
        self.event = Event()

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def consume(self, stream):
        return [r.text async for r in stream]

    async def test_direct_commands_and_tools_share_explicit_source_queue(self):
        for method, message, args, source in (
            ("jm_download_command", "/jm JM123", (), "jm"),
            ("nh_download_command", "/nh https://nhentai.net/g/123/", (), "nh"),
            ("jm_download_tool", "", ("123",), "jm"),
            ("nhentai_download_tool", "", ("123",), "nh"),
        ):
            with self.subTest(method=method):
                self.event.message_str = message
                self.plugin._queue_download.reset_mock()
                self.assertEqual(await self.consume(getattr(self.plugin, method)(self.event, *args)), ["queued"])
                self.plugin._queue_download.assert_awaited_once_with(self.event.unified_msg_origin, "2", source, "123")
                self.assertTrue(self.event.stopped)

    async def test_llm_switch_and_whitelist_block_all_tools(self):
        for allowed, enabled in ((True, False), (False, True)):
            self.plugin._llm_tools_enabled = enabled
            self.plugin._is_allowed = lambda event: allowed
            for method, args in (("soutubot_search_tool", ()), ("jm_search_tool", ("Title",)),
                                 ("jm_download_tool", ("123",)), ("nhentai_download_tool", ("123",))):
                self.assertTrue(await self.consume(getattr(self.plugin, method)(self.event, *args)))
            self.plugin._queue_download.assert_not_awaited()

    async def test_title_tool_preserves_manual_search_workflow(self):
        self.plugin._claim_request = AsyncMock(return_value=(True, ""))
        self.plugin._cooldown_key = lambda event: "key"
        self.plugin._background_manual_jm_search = AsyncMock()
        await self.consume(self.plugin.jm_search_tool(self.event, " Title "))
        await asyncio.gather(*tuple(self.plugin._tasks))
        self.plugin._background_manual_jm_search.assert_awaited_once_with(
            umo=self.event.unified_msg_origin, title="Title", cooldown_key="key", sender_id="2")

    async def test_image_tool_reuses_image_command(self):
        calls = []
        async def command(event):
            calls.append(event)
            yield event.plain_result("image task")
        self.plugin.search_doujin_command = command
        self.assertEqual(await self.consume(self.plugin.soutubot_search_tool(self.event)), ["image task"])
        self.assertEqual(calls, [self.event])

    def test_tool_docstrings_match_astrbot_argument_parser(self):
        import docstring_parser
        for name, expected in (("soutubot_search_tool", []), ("jm_search_tool", ["title"]),
                               ("jm_download_tool", ["album_id"]), ("nhentai_download_tool", ["gallery_id"])):
            parsed = docstring_parser.parse(getattr(contract.Main, name).__doc__)
            self.assertEqual([p.arg_name for p in parsed.params], expected)
            self.assertTrue(all(p.type_name == "string" for p in parsed.params))


class Response:
    def __init__(self, status, body=b""):
        self.status, self.body = status, body
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        yield self.body


class Session:
    def __init__(self, router):
        self.router, self.calls = router, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        self.calls.append(url)
        return self.router(url)


class NhTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.settings = nh.NhDownloadSettings(self.directory)
        image = io.BytesIO()
        Image.new("RGB", (3, 4)).save(image, format="PNG")
        self.image = image.getvalue()

    async def fetch(self, router, settings=None):
        self.session = Session(router)
        with patch("aiohttp.ClientSession", return_value=self.session):
            return await nh.NhImageSource(settings or self.settings).fetch("123", self.directory)

    async def test_v2_keeps_page_order_and_retries_unavailable_cdn(self):
        def router(url):
            if "/api/" in url:
                return Response(200, json.dumps({"title": {"pretty": "Title"}, "pages": [
                    {"path": "galleries/7/2.png"}, {"path": "galleries/7/1.png"}]}).encode())
            return Response(503) if "i1." in url else Response(200, self.image)
        bundle = await self.fetch(router)
        self.assertEqual(bundle.title, "Title")
        self.assertEqual([p.name for p in bundle.images], ["000001.png", "000002.png"])
        self.assertTrue(all(p.read_bytes() == self.image for p in bundle.images))
        self.assertTrue(any("i2." in url for url in self.session.calls))
        self.assertFalse(any("/api/gallery/" in url for url in self.session.calls))

    async def test_old_metadata_fallback_only_after_404(self):
        def router(url):
            if "/api/v2/" in url:
                return Response(404)
            if "/api/" in url:
                return Response(200, json.dumps({"media_id": "9", "images": {"pages": [{"t": "p"}]}}).encode())
            return Response(200, self.image)
        self.assertEqual(len((await self.fetch(router)).images), 1)
        self.assertIn("https://nhentai.net/api/gallery/123", self.session.calls)

    async def test_access_denied_stops_without_cdn_or_endpoint_retry(self):
        with self.assertRaises(nh.DownloadError):
            await self.fetch(lambda url: Response(403))
        self.assertEqual(len(self.session.calls), 1)

    async def test_page_limits_fail_before_image_requests(self):
        payload = {"pages": [{"path": "galleries/7/1.png"}, {"path": "galleries/7/2.png"}]}
        with self.assertRaises(nh.DownloadError):
            await self.fetch(lambda url: Response(200, json.dumps(payload).encode()),
                             nh.NhDownloadSettings(self.directory, max_pages=1))
        self.assertEqual(len(self.session.calls), 1)

    async def test_oversize_page_leaves_no_completed_image(self):
        def router(url):
            if "/api/" in url:
                return Response(200, b'{"pages":[{"path":"galleries/7/1.png"}]}')
            return Response(200, self.image)
        with self.assertRaises(nh.DownloadError):
            await self.fetch(router, nh.NhDownloadSettings(self.directory, max_image_bytes=2))
        self.assertFalse(list(self.directory.iterdir()))
