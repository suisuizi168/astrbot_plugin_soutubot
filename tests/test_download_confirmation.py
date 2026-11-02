"""Search requires a scoped reply while explicit downloads remain immediate."""

import asyncio
import importlib
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_main_contract as contract
from test_modular_workflow import response

common = importlib.import_module(f"{contract.PACKAGE_NAME}.workflow_common")
help_text = importlib.import_module(f"{contract.PACKAGE_NAME}.help_text")


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = contract.Main(
            types.SimpleNamespace(),
            {
                "download_confirmation_timeout_seconds": 90,
                "trigger_nickname": " 小岁 ",
            },
        )
        self.plugin._send_chain = AsyncMock()
        self.plugin._queue_download = AsyncMock(return_value="queued")
        self.plugin._search_bytes = AsyncMock(return_value=response())
        self.plugin._jm_result_message_settle_seconds = 0

    async def asyncTearDown(self):
        await self.plugin.terminate()

    def event(self, sender="user", umo="group", text="下载"):
        event = contract.JmConfirmationTests.ReplyEvent(umo, sender, text)
        event.get_group_id = lambda: umo
        return event

    async def reply(self, event=None):
        return [r async for r in self.plugin.jm_download_reply(event or self.event())]

    async def search(self):
        await self.plugin._background_search(
            umo="group",
            sender_id="user",
            image_bytes=b"fake",
            filename="test.png",
            strict=False,
        )

    async def test_nh_only_waits_then_confirm_downloads_once(self):
        self.plugin._jm_download_enabled = False
        await self.search()
        self.plugin._queue_download.assert_not_awaited()
        self.assertIn("NH123", self.plugin._send_chain.call_args.args[1][0].text)
        self.assertIn("90 秒", self.plugin._send_chain.call_args.args[1][0].text)
        for event in (
            self.event(sender="other"),
            self.event(umo="other"),
            self.event(text="不要下载"),
        ):
            self.assertEqual(await self.reply(event), [])
        await asyncio.gather(self.reply(), self.reply())
        self.plugin._queue_download.assert_awaited_once_with(
            "group", "user", "nh", "123"
        )

    async def test_confirmed_secondary_jm_keeps_original_route(self):
        self.plugin._search_bytes.return_value = response("ehentai")
        self.plugin._resolve_jm_for_response = AsyncMock(
            return_value=contract.main_module.JmResolution(
                "Title",
                "matched",
                contract.main_module.JmCandidate("456", "Title", 100),
            )
        )
        await self.search()
        self.plugin._queue_download.assert_not_awaited()
        await self.reply()
        self.plugin._queue_download.assert_awaited_once_with(
            "group", "user", "jm", "456"
        )

    async def test_actual_window_starts_after_prompt_delivery(self):
        clock = [100.0]

        async def send(*args):
            clock[0] = 150.0

        self.plugin._send_chain.side_effect = send
        with patch.object(common.time, "monotonic", side_effect=lambda: clock[0]):
            await self.plugin._send_download_confirmation("group", "user", "nh", "123")
            clock[0] = 239.9
            self.assertTrue(common.JmDownloadReplyFilter().filter(self.event(), {}))
            clock[0] = 240.0
            self.assertFalse(common.JmDownloadReplyFilter().filter(self.event(), {}))
            self.assertEqual(await self.reply(), [])
        self.plugin._queue_download.assert_not_awaited()

    async def test_failure_or_cancelled_prompt_leaves_no_confirmation(self):
        for failure in (RuntimeError("failed"), asyncio.CancelledError()):
            self.plugin._send_chain.side_effect = failure
            with self.assertRaises(type(failure)):
                await self.plugin._send_download_confirmation(
                    "group", "user", "nh", "123"
                )
            self.assertIsNone(common.JmDownloadReplyFilter.peek(self.event()))

    async def test_confirmation_rechecks_source_switch_and_whitelist(self):
        for source, enabled, allowed in (
            ("nh", False, True),
            ("jm", False, True),
            ("nh", True, False),
        ):
            common.JmDownloadReplyFilter.remember("group", "user", "123", source=source)
            setattr(self.plugin, f"_{source}_download_enabled", enabled)
            self.plugin._is_allowed = lambda event: allowed
            self.assertTrue(await self.reply())
            self.plugin._queue_download.assert_not_awaited()

    async def test_latest_prompt_retains_its_source_when_old_send_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(umo, chain):
            if "NH111" in chain[0].text:
                entered.set()
                await release.wait()

        self.plugin._send_chain.side_effect = send
        first = asyncio.create_task(
            self.plugin._send_download_confirmation("group", "user", "nh", "111")
        )
        await asyncio.wait_for(entered.wait(), 1)
        try:
            await self.plugin._send_download_confirmation("group", "user", "jm", "222")
        finally:
            release.set()
            await first
        await self.reply()
        self.plugin._queue_download.assert_awaited_once_with(
            "group", "user", "jm", "222"
        )

    async def test_help_uses_active_nickname_without_repeated_instructions(self):
        text = (await anext(self.plugin.jm_download_help_command(self.event()))).text
        self.assertNotIn("90 秒", text)
        self.assertNotIn("10 分钟", text)
        for command in ("搜图", "找鸡", "找本"):
            self.assertEqual(text.count(f"/{command} 或 小岁{command}"), 1)
        self.assertNotIn("小岁搜本", text)
        self.assertLess(text.index("🖼️ 搜图"), text.index("🐔 找鸡"))
        self.assertLess(text.index("🐔 找鸡"), text.index("🔎 找本"))
        self.assertIn("/搜图帮助", text)
        self.assertNotIn("soutubot", text.lower())

    async def test_manual_search_uses_same_configured_window(self):
        resolution = contract.main_module.JmResolution(
            "Title", "matched", contract.main_module.JmCandidate("456", "Title", 100)
        )
        message = self.plugin._format_jm_resolution_for_user(resolution)
        with patch.object(common.time, "monotonic", return_value=100):
            await self.plugin._send_jm_resolution("group", "user", resolution, message)
        self.assertIn("90 秒", message)
        self.assertEqual(
            common.JmDownloadReplyFilter.pending[("group", "user")].expires_at, 190
        )


class HelpTests(unittest.TestCase):
    def test_blank_nickname_keeps_concise_command_usage(self):
        text = help_text.build_help(" ")
        self.assertNotIn("确认下载", text)
        self.assertNotIn(" 或 小岁", text)
        self.assertNotIn("昵称", text)
        self.assertNotIn("bot名", text)
        self.assertIn("指令：/搜图\n", text)
