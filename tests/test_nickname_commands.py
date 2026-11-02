"""Nickname admission, routing and duplicate protection with no external IO."""

import asyncio
import importlib
import io
import types
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

import test_main_contract as contract
from test_search_commands import Event

nickname = importlib.import_module(f"{contract.PACKAGE_NAME}.nickname_commands")


class At:
    def __init__(self, qq):
        self.qq = qq


def event_for(text, quoted=False):
    image = contract.Image(file="fixture.png")
    attachment = contract.Reply(chain=[image]) if quoted else image
    attachment.sender_id = "author"
    event = Event([attachment, contract.Plain(text)])
    event.message_str = text
    event.get_self_id = lambda: "bot"
    event.get_platform_name = lambda: "aiocqhttp"
    event.is_at_or_wake_command = False
    return event


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.owner = object()
        nickname.NicknameCommandFilter.bind("小岁", self.owner)
        self.rule = nickname.NicknameCommandFilter()

    def tearDown(self):
        nickname.NicknameCommandFilter.unbind(self.owner)

    def test_exact_commands_with_spaces_images_or_replies(self):
        for command in ("找鸡", "找本", "搜图"):
            for space in ("", " ", "\u3000"):
                for quoted in (False, True):
                    event = event_for(f" 小岁{space}{command} ", quoted)
                    self.assertTrue(self.rule.filter(event, {}))
                    self.assertEqual(event._soutubot_nickname_command, command)
                    self.assertFalse(event.is_at_or_wake_command)

    def test_blank_nickname_disables_custom_entry(self):
        for value in ("", "   "):
            nickname.NicknameCommandFilter.bind(value, self.owner)
            self.assertFalse(self.rule.filter(event_for("小岁找鸡"), {}))
            self.assertIsNone(nickname.parse_nickname_command("找鸡", value))

    def test_rejects_prose_other_names_and_trailing_commands(self):
        for text in (
            "让小岁找鸡试试",
            "小岁找鸡了吗",
            "小岁找鸡 找本",
            "小年找鸡",
            "小岁",
            "找鸡",
            "/找鸡",
            "小岁找鸡！",
        ):
            self.assertFalse(self.rule.filter(event_for(text), {}), text)

    def test_regex_characters_in_nickname_are_literal(self):
        self.assertEqual(
            nickname.parse_nickname_command("小岁.+找鸡", "小岁.+"), "找鸡"
        )
        self.assertIsNone(nickname.parse_nickname_command("小岁abc找鸡", "小岁.+"))

    def test_quote_text_is_never_a_command(self):
        event = event_for("看看这张图", quoted=True)
        event.messages[0].message_str = "小岁找鸡"
        event.messages[0].chain.append(contract.Plain("小岁找鸡"))
        event.message_str = "小岁找鸡"
        self.assertFalse(self.rule.filter(event, {}))

    def test_original_plain_text_survives_global_wake_prefix_removal(self):
        event = event_for("小岁找本", quoted=True)
        event.message_str = "找本"
        event.is_at_or_wake_command = True
        self.assertTrue(self.rule.filter(event, {}))
        self.assertEqual(event._soutubot_nickname_command, "找本")

    def test_qq_reply_auto_mention_and_optional_bot_mention(self):
        with patch.object(nickname.Comp, "At", At, create=True):
            event = event_for("小岁找鸡", quoted=True)
            event.messages.insert(1, At("author"))
            self.assertTrue(self.rule.filter(event, {}))
            event.messages.insert(2, At("bot"))
            self.assertTrue(self.rule.filter(event, {}))
            event.messages[1] = At("someone-else")
            self.assertFalse(self.rule.filter(event, {}))

    def test_reload_and_old_instance_shutdown_cannot_reset_new_nickname(self):
        new_owner = object()
        nickname.NicknameCommandFilter.bind("小年", new_owner)
        nickname.NicknameCommandFilter.unbind(self.owner)
        self.assertFalse(self.rule.filter(event_for("小岁找鸡"), {}))
        self.assertTrue(self.rule.filter(event_for("小年找鸡"), {}))
        nickname.NicknameCommandFilter.unbind(new_owner)
        self.assertFalse(self.rule.filter(event_for("小年找鸡"), {}))


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = contract.Main(
            types.SimpleNamespace(),
            {
                "trigger_nickname": "小岁",
                "cooldown_seconds": 0,
                "findchicken_cooldown_seconds": 0,
                "findchicken_api_key": "fake-key",
            },
        )
        self.rule = nickname.NicknameCommandFilter()
        self.plugin._send_chain = AsyncMock()
        image = io.BytesIO()
        Image.new("RGB", (5, 5)).save(image, format="PNG")
        self.plugin._materialize_image = AsyncMock(
            return_value=(image.getvalue(), "fixture.png")
        )
        self.plugin._query_findchicken = AsyncMock(return_value="fixture result")
        self.plugin._background_search = AsyncMock()
        self.plugin._background_multi_search = AsyncMock()

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def call(self, event, method="nickname_image_command"):
        result = [r.text async for r in getattr(self.plugin, method)(event)]
        await asyncio.gather(
            *tuple(self.plugin._tasks), *tuple(self.plugin._findchicken_tasks)
        )
        return result

    async def test_each_command_routes_once_with_both_handler_orders(self):
        for command, standard in (
            ("找鸡", "findchicken_command"),
            ("找本", "search_doujin_command"),
            ("搜图", "search_image_command"),
        ):
            for standard_first in (False, True):
                for mock in (
                    self.plugin._query_findchicken,
                    self.plugin._background_search,
                    self.plugin._background_multi_search,
                ):
                    mock.reset_mock()
                event = event_for("小岁" + command, quoted=True)
                self.assertTrue(self.rule.filter(event, {}))
                methods = (
                    [standard, "nickname_image_command"]
                    if standard_first
                    else ["nickname_image_command", standard]
                )
                for method in methods:
                    await self.call(event, method)
                self.assertTrue(event.stopped)
                counts = [
                    self.plugin._query_findchicken.await_count,
                    self.plugin._background_search.await_count,
                    self.plugin._background_multi_search.await_count,
                ]
                self.assertEqual(
                    counts, [int(command == c) for c in ("找鸡", "找本", "搜图")]
                )

    async def test_whitelist_and_switches_apply_to_all_nickname_entries(self):
        for command in ("找鸡", "找本", "搜图"):
            event = event_for("小岁" + command)
            self.assertTrue(self.rule.filter(event, {}))
            self.plugin._policy.whitelist = frozenset({"other-group"})
            self.assertIn("白名单", "".join(await self.call(event)))
        self.plugin._policy.whitelist = frozenset()
        self.plugin._findchicken_enabled = False
        self.plugin._multi_search.enabled = False
        for command in ("找鸡", "搜图"):
            event = event_for("小岁" + command)
            self.rule.filter(event, {})
            self.assertIn("已关闭", "".join(await self.call(event)))
        self.plugin._materialize_image.assert_not_awaited()

    async def test_cooldown_and_missing_image_keep_existing_behavior(self):
        self.plugin._cooldown.cooldown_seconds = 30
        for command in ("找本", "搜图"):
            event = event_for("小岁" + command)
            self.rule.filter(event, {})
            result = await self.call(event)
            if command == "搜图":
                self.assertIn("冷却", "".join(result))
        missing = event_for("小岁找鸡")
        missing.messages = [contract.Plain("小岁找鸡")]
        self.rule.filter(missing, {})
        await self.call(missing)
        self.plugin._query_findchicken.assert_not_awaited()
        self.assertIn("附带或引用", self.plugin._send_chain.call_args.args[1][0].text)
