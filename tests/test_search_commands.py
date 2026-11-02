"""Exercise both public image commands with real routing and mocked network IO."""
import asyncio
import io
import types
import unittest
from unittest.mock import AsyncMock

from PIL import Image

import test_main_contract as contract
from test_modular_workflow import response


class Event:
    unified_msg_origin = "test:GroupMessage:1"
    stopped = False

    def __init__(self, messages):
        self.messages = messages

    def get_messages(self):
        return self.messages

    def get_platform_name(self):
        return "test"

    def get_group_id(self):
        return "1"

    def get_sender_id(self):
        return "2"

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return contract.Plain(text)


class SearchCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = contract.Main(types.SimpleNamespace(), {"cooldown_seconds": 0})
        self.plugin._send_chain = AsyncMock()
        self.plugin._search_bytes = AsyncMock(return_value=response())
        self.plugin._resolve_jm_for_response = AsyncMock()
        self.plugin._queue_download = AsyncMock(return_value="Queued")
        self.plugin._multi_search.search = AsyncMock(return_value="MULTI RESULT")
        image = io.BytesIO()
        Image.new("RGB", (5, 5)).save(image, format="PNG")
        self.plugin._materialize_image = AsyncMock(return_value=(image.getvalue(), "x.png"))
        self.event = Event([contract.Image(file="x.png")])

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def run_command(self, name, event=None):
        results = [r.text async for r in getattr(self.plugin, name)(event or self.event)]
        await asyncio.gather(*tuple(self.plugin._tasks))
        return results

    def sent_text(self):
        return "\n".join(
            item.text for call in self.plugin._send_chain.call_args_list for item in call.args[1]
            if isinstance(item, contract.Plain)
        )

    def assert_no_book_calls(self):
        self.plugin._search_bytes.assert_not_awaited()
        self.plugin._resolve_jm_for_response.assert_not_awaited()
        self.plugin._queue_download.assert_not_awaited()

    async def test_direct_image_search_only_calls_multi_even_with_downloads_enabled(self):
        results = await self.run_command("search_image_command")
        self.assertTrue(self.event.stopped)
        self.assertIn("多引擎", results[0])
        self.plugin._multi_search.search.assert_awaited_once()
        self.assertIn("MULTI RESULT", self.sent_text())
        self.assert_no_book_calls()

    async def test_find_book_preserves_match_and_waits_for_download_confirmation(self):
        results = await self.run_command("search_doujin_command")
        self.assertEqual(results[0], "正在查询图片来源，完成后会在当前会话发送结果。")
        self.plugin._search_bytes.assert_awaited_once()
        self.plugin._multi_search.search.assert_not_awaited()
        self.plugin._queue_download.assert_not_awaited()
        request = contract.main_module.JmDownloadReplyFilter.pending[(self.event.unified_msg_origin, "2")]
        self.assertEqual((request.source, request.album_id), ("nh", "123"))
        self.assertIn("是否需要下载 NH123", self.sent_text())

    async def test_find_book_falls_back_on_low_score_or_request_failure(self):
        for error in (None, RuntimeError("offline")):
            with self.subTest(error=error):
                self.plugin._search_bytes.return_value = response(score=79)
                self.plugin._search_bytes.side_effect = error
                self.plugin._multi_search.search.reset_mock()
                await self.run_command("search_doujin_command")
                self.plugin._multi_search.search.assert_awaited_once()
                self.plugin._queue_download.assert_not_awaited()

    async def test_both_commands_accept_quoted_images(self):
        quoted = contract.Image(file="quoted.png")
        event = Event([contract.Reply(chain=[quoted])])
        for name in ("search_image_command", "search_doujin_command"):
            with self.subTest(command=name):
                self.plugin._materialize_image.reset_mock()
                await self.run_command(name, event)
                self.plugin._materialize_image.assert_awaited_once_with(quoted)

    async def test_disabled_multi_rejects_direct_search_without_blocking_find_book(self):
        self.plugin._multi_search.enabled = False
        self.plugin._cooldown.cooldown_seconds = 30
        results = await self.run_command("search_image_command")
        self.assertIn("已关闭", results[0])
        self.plugin._materialize_image.assert_not_awaited()
        self.plugin._multi_search.search.assert_not_awaited()
        await self.run_command("search_doujin_command")
        self.plugin._search_bytes.assert_awaited_once()

    async def test_whitelist_blocks_both_commands_before_reading_images(self):
        self.plugin._policy.whitelist = frozenset({"another-group"})
        for name in ("search_image_command", "search_doujin_command"):
            self.assertIn("白名单", (await self.run_command(name))[0])
        self.plugin._materialize_image.assert_not_awaited()
        self.plugin._multi_search.search.assert_not_awaited()
        self.assert_no_book_calls()

    async def test_cooldown_is_shared_between_commands(self):
        self.plugin._cooldown.cooldown_seconds = 30
        await self.run_command("search_image_command")
        self.assertIn("冷却", (await self.run_command("search_doujin_command"))[0])
        self.assert_no_book_calls()

    async def test_missing_image_reports_own_command_and_releases_cooldown(self):
        self.plugin._cooldown.cooldown_seconds = 30
        for name, command in (("search_image_command", "/搜图"), ("search_doujin_command", "/找本")):
            with self.subTest(command=command):
                results = await self.run_command(name, Event([]))
                self.assertIn(command, results[0])
                self.assertEqual(await self.plugin._cooldown.claim(self.plugin._cooldown_key(self.event)), 0)
                await self.plugin._cooldown.release(self.plugin._cooldown_key(self.event))
        self.plugin._materialize_image.assert_not_awaited()

    async def test_pending_limit_and_shutdown_block_both_commands(self):
        for closing in (False, True):
            self.plugin._closing = closing
            self.plugin._max_pending_tasks = 20 if closing else 0
            for name in ("search_image_command", "search_doujin_command"):
                self.assertIn("稍后再试", (await self.run_command(name))[0])
        self.plugin._materialize_image.assert_not_awaited()
        self.assert_no_book_calls()

    async def test_direct_failure_stays_in_multi_search(self):
        self.plugin._multi_search.search.side_effect = RuntimeError("offline")
        await self.run_command("search_image_command")
        self.assertIn("多引擎搜图失败", self.sent_text())
        self.assert_no_book_calls()

    async def test_direct_search_reports_no_enabled_sources(self):
        self.plugin._multi_search.search = types.MethodType(
            type(self.plugin._multi_search).search, self.plugin._multi_search
        )
        self.plugin._multi_search.config.update(
            saucenao_enabled=False, yandex_enabled=False, ascii2d_enabled=False
        )
        self.plugin._google_lens.search_enabled = False
        await self.run_command("search_image_command")
        self.assertIn("没有启用任何搜索源", self.sent_text())
        self.assert_no_book_calls()
