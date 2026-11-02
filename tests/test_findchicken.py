"""Offline checks for the independent find-chicken feature."""

import asyncio
import importlib
import io
import json
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

import test_main_contract as contract
from test_search_commands import Event

models = importlib.import_module(f"{contract.PACKAGE_NAME}.findchicken_models")
client = importlib.import_module(f"{contract.PACKAGE_NAME}.findchicken_client")
workflow = importlib.import_module(f"{contract.PACKAGE_NAME}.findchicken_workflow")


def payload(index=41, score="88.00", **data):
    return {
        "header": {"status": 0},
        "results": [
            {
                "header": {"index_id": index, "similarity": score},
                "data": data or {"twitter_user_handle": "sample_artist"},
            }
        ],
    }


class ModelTests(unittest.TestCase):
    def test_twitter_output_matches_contract(self):
        text = models.format_authors(
            models.author_matches(payload(), 30, 1),
            direct_url=False,
            show_similarity=False,
        )
        self.assertEqual(
            text,
            "🎯 已找到鸡\n🌐 平台：X (Twitter)\n匹配：视觉匹配\n👤 名字：sample_artist\n🆔 账号：@sample_artist\nhttps://x.com/sample_artist\n\n⚠识图存在误差，请自行核对哦~",
        )

    def test_pixiv_uses_member_name_and_numeric_account(self):
        for index in (5, 6):
            result = models.author_matches(
                payload(index, member_name="Artist", member_id=123), 30, 1
            )[0]
            self.assertEqual(
                (result.platform, result.name, result.account),
                ("Pixiv", "Artist", "@123"),
            )
            self.assertEqual(result.url, "https://www.pixiv.net/users/123")

    def test_author_priority_and_handle_account(self):
        result = models.author_matches(
            payload(author_name="Display name", twitter_user_handle="@handle"), 30, 1
        )[0]
        self.assertEqual((result.name, result.account), ("Display name", "@handle"))

    def test_url_uses_same_visual_label_and_optional_score(self):
        matches = models.author_matches(payload(), 30, 1)
        self.assertIn(
            "视觉匹配（相似度 88.0%）",
            models.format_authors(matches, direct_url=False, show_similarity=True),
        )
        text = models.format_authors(matches, direct_url=True, show_similarity=True)
        self.assertIn("匹配：视觉匹配", text)
        self.assertIn("88", text)

    def test_threshold_limit_and_author_link(self):
        data = payload(score="29", twitter_user_handle="low")
        data["results"] += payload(
            score="90",
            twitter_user_handle="first",
            ext_urls=["https://x.com/first"],
            source="secret",
        )["results"]
        data["results"] += payload(score="99", twitter_user_handle="second")["results"]
        matches = models.author_matches(data, 30, 1)
        self.assertEqual(matches[0].name, "first")
        text = models.format_authors(matches, direct_url=False, show_similarity=False)
        self.assertIn("https://x.com/first", text)
        self.assertNotIn("secret", text)

    def test_source_link_fallback_and_invalid_links(self):
        result = models.author_matches(
            payload(
                9,
                creator="Artist",
                ext_urls=[
                    "javascript:alert(1)",
                    "https://danbooru.donmai.us/posts/123",
                ],
            ),
            30,
            1,
        )[0]
        self.assertEqual(result.url, "https://danbooru.donmai.us/posts/123")
        for value in (
            "javascript:alert(1)",
            "file:///tmp/a",
            "https://user:pass@example.com",
            "https://example.com/\nother",
            "https://[",
            "not a link",
            None,
        ):
            result = models.author_matches(
                payload(9, creator="Artist", ext_urls=[value], source=value), 30, 1
            )[0]
            self.assertEqual(result.url, "")
        result = models.author_matches(
            payload(9, creator="Artist", source="https://example.com/art/1"), 30, 1
        )[0]
        self.assertEqual(result.url, "https://example.com/art/1")
        result = models.author_matches(
            payload(
                twitter_user_handle="invalid/handle",
                ext_urls=["https://x.com/artist/status/1"],
            ),
            30,
            1,
        )[0]
        self.assertEqual(result.url, "https://x.com/artist/status/1")

    def test_profile_link_is_preferred_over_post_link(self):
        result = models.author_matches(
            payload(
                twitter_user_handle="@artist",
                ext_urls=["https://x.com/artist/status/1"],
            ),
            30,
            1,
        )[0]
        self.assertEqual(result.url, "https://x.com/artist")

    def test_malformed_and_unknown_values(self):
        for data in (
            None,
            [],
            {},
            {"results": {}},
            payload(score="nan"),
            payload(score="inf"),
        ):
            self.assertEqual(models.author_matches(data, 30, 1), [])
        data = {
            "results": [
                {
                    "header": {
                        "index_id": 999,
                        "index_name": "Index #999: NewSite - file.jpg",
                        "similarity": 90,
                    }
                }
            ]
        }
        match = models.author_matches(data, 30, 1)[0]
        self.assertEqual(
            (match.platform, match.name, match.account), ("NewSite", "未知", "未知")
        )
        self.assertIn(
            "未找到鸡，可能目前暂未收录",
            models.format_authors([], direct_url=False, show_similarity=False),
        )

    def test_configuration_group_is_contiguous_and_independent(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        schema = schema['findchicken']['items']
        keys = list(schema)
        positions = [i for i, key in enumerate(keys) if key.startswith("findchicken_")]
        self.assertEqual(positions, list(range(positions[0], positions[0] + 11)))
        self.assertEqual(schema["findchicken_api_key"]["default"], "")
        self.assertEqual(schema["findchicken_similarity_threshold"]["default"], 30)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = contract.Main(
            types.SimpleNamespace(),
            {
                "findchicken_api_key": "test-chicken-key",
                "findchicken_llm_enabled": False,
                "findchicken_cooldown_seconds": 0,
                "saucenao_api_key": "different-search-key",
                "multi_search_enabled": False,
                "jm_enabled": False,
                "nh_download_enabled": False,
                "llm_tools_enabled": False,
            },
        )
        self.plugin._send_chain = AsyncMock()
        self.plugin._findchicken_client.search = AsyncMock(return_value=payload())
        self.plugin._search_bytes = AsyncMock()
        self.plugin._multi_search.search = AsyncMock()
        self.plugin._queue_download = AsyncMock()
        self.plugin._resolve_jm_for_response = AsyncMock()
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buffer, format="PNG")
        self.plugin._materialize_image = AsyncMock(
            return_value=(buffer.getvalue(), "test.png")
        )

    async def asyncTearDown(self):
        await self.plugin.terminate()

    def event(self, messages=None, text="找鸡"):
        event = Event(
            [contract.Image(file="test.png")] if messages is None else messages
        )
        event.message_str = text
        return event

    async def run_command(self, event=None):
        outputs = [
            r.text async for r in self.plugin.findchicken_command(event or self.event())
        ]
        await asyncio.gather(*tuple(self.plugin._findchicken_tasks))
        return outputs

    def texts(self):
        return [
            item.text
            for call in self.plugin._send_chain.call_args_list
            for item in call.args[1]
        ]

    async def test_ack_is_delivered_before_any_image_or_network_work(self):
        gate = asyncio.Event()

        async def send(umo, chain):
            if chain[0].text == "收到，开始找鸡":
                await gate.wait()

        self.plugin._send_chain.side_effect = send
        results = [r async for r in self.plugin.findchicken_command(self.event())]
        self.assertEqual(results, [])
        await asyncio.sleep(0)
        self.plugin._materialize_image.assert_not_awaited()
        self.plugin._findchicken_client.search.assert_not_awaited()
        gate.set()
        await asyncio.gather(*tuple(self.plugin._findchicken_tasks))
        self.assertEqual(self.texts()[0], "收到，开始找鸡")
        self.assertIn("找到匹配来源", self.texts()[1])

    async def test_feature_runs_with_all_other_modules_disabled(self):
        await self.run_command()
        self.plugin._findchicken_client.search.assert_awaited_once()
        self.assertEqual(self.plugin._findchicken_client.api_key, "test-chicken-key")
        self.plugin._search_bytes.assert_not_awaited()
        self.plugin._multi_search.search.assert_not_awaited()
        self.plugin._queue_download.assert_not_awaited()
        self.plugin._resolve_jm_for_response.assert_not_awaited()
        self.assertFalse(self.plugin._tasks)

    async def test_explicit_url_input_and_quoted_url(self):
        for event in (
            self.event([], "找鸡 https://example.test/img.png。"),
            self.event([contract.Reply()]),
        ):
            if event.messages:
                event.messages[0].message_str = "https://example.test/img.png"
            self.plugin._findchicken_client.search.reset_mock()
            await self.run_command(event)
            self.plugin._findchicken_client.search.assert_awaited_once_with(
                url="https://example.test/img.png"
            )
            self.assertIn("匹配：视觉匹配", self.texts()[-1])

    async def test_attached_and_quoted_images(self):
        image = contract.Image(file="quoted.png")
        await self.run_command(self.event([contract.Reply(chain=[image])]))
        self.plugin._materialize_image.assert_awaited_once_with(image)
        self.assertIn("视觉匹配", self.texts()[-1])

    async def test_missing_key_and_missing_image_release_cooldown(self):
        self.plugin._findchicken_cooldown.cooldown_seconds = 60
        self.plugin._findchicken_client.api_key = ""
        await self.run_command()
        self.assertEqual("找鸡出现错误，请稍后重试", self.texts()[-1])
        self.plugin._findchicken_client.api_key = "key"
        await self.run_command(self.event([]))
        self.assertIn("附带或引用", self.texts()[-1])
        await self.run_command()
        self.plugin._findchicken_client.search.assert_awaited_once()

    async def test_whitelist_and_feature_switch_deny_before_ack(self):
        self.plugin._policy.whitelist = frozenset({"other"})
        self.assertIn("白名单", (await self.run_command())[0])
        self.plugin._policy.whitelist = frozenset()
        self.plugin._findchicken_enabled = False
        self.assertIn("已关闭", (await self.run_command())[0])
        self.plugin._send_chain.assert_not_awaited()

    async def test_cooldown_is_independent_and_duplicate_event_is_consumed_once(self):
        self.plugin._findchicken_cooldown.cooldown_seconds = 60
        event = self.event()
        await self.run_command(event)
        self.assertEqual(await self.run_command(event), [])
        self.assertIn("秒后再试", (await self.run_command())[0])
        self.assertEqual(
            await self.plugin._cooldown.claim(self.plugin._cooldown_key(event)), 0
        )
        self.plugin._findchicken_client.search.assert_awaited_once()

    async def test_queue_limit_shutdown_and_cancel(self):
        self.plugin._max_pending_tasks = 0
        self.assertIn("稍后再试", (await self.run_command())[0])
        self.plugin._max_pending_tasks = 20
        waiting = asyncio.Event()
        self.plugin._findchicken_client.search.side_effect = lambda **kw: None

        async def search(**kw):
            waiting.set()
            await asyncio.Event().wait()

        self.plugin._findchicken_client.search.side_effect = search
        _ = [r async for r in self.plugin.findchicken_command(self.event())]
        await asyncio.wait_for(waiting.wait(), 1)
        await self.plugin.terminate()
        self.assertFalse(self.plugin._findchicken_tasks)
        self.assertEqual(self.texts(), ["收到，开始找鸡"])

    async def test_key_error_has_no_secret_or_download_fallback(self):
        self.plugin._findchicken_client.search.side_effect = client.FindChickenError(
            "key"
        )
        await self.run_command()
        self.assertEqual("找鸡出现错误，请稍后重试", self.texts()[-1])
        self.assertNotIn("test-chicken-key", "".join(self.texts()))
        self.plugin._queue_download.assert_not_awaited()

    async def test_qq_reply_filter_only_matches_intended_shape(self):
        class At:
            def __init__(self, qq):
                self.qq = qq

        reply = contract.Reply()
        reply.sender_id = "123"
        event = self.event([reply, At("123"), contract.Plain("/找鸡")])
        event.get_platform_name = lambda: "aiocqhttp"
        event.get_self_id = lambda: "bot"
        with patch.object(workflow.Comp, "At", At, create=True):
            rule = workflow.FindChickenReplyFilter()
            self.assertTrue(rule.filter(event, {}))
            event.messages.append(contract.Plain("其他消息"))
            self.assertFalse(rule.filter(event, {}))
            event.messages = [reply, At("123"), At("bot"), contract.Plain("找鸡")]
            self.assertTrue(rule.filter(event, {}))
            event.messages[-1].text = "搜图"
            self.assertFalse(rule.filter(event, {}))


class Response:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = json.dumps(payload() if body is None else body).encode()
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        yield self.body


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_and_upload_use_private_settings(self):
        transport = client.FindChickenClient(
            "private-key", "http://proxy.test:8080", 30, 1
        )
        session = types.SimpleNamespace(
            closed=False, post=lambda *args, **kwargs: Response(), close=AsyncMock()
        )
        with patch.object(session, "post", wraps=session.post) as post:
            transport._session = session
            await transport.search(url="https://example.test/img.jpg")
            args = post.call_args.kwargs
            self.assertEqual(args["params"]["db"], "999")
            self.assertEqual(args["params"]["api_key"], "private-key")
            self.assertEqual(args["proxy"], "http://proxy.test:8080")
            self.assertFalse(args["allow_redirects"])
            await transport.search(
                image=types.SimpleNamespace(
                    data=b"bytes", filename="a.jpg", content_type="image/jpeg"
                )
            )
            self.assertNotIn("url", post.call_args.kwargs["params"])
            self.assertIsNotNone(post.call_args.kwargs["data"])
        await transport.close()
        session.close.assert_awaited_once()

    async def test_http_and_api_errors_are_classified(self):
        for status, body, kind in (
            (403, {"header": {"status": -1, "message": "API key invalid"}}, "key"),
            (429, {}, "rate_limit"),
            (200, [], "response"),
            (500, {}, "response"),
        ):
            transport = client.FindChickenClient("key", "", 30, 1)
            transport._session = types.SimpleNamespace(
                closed=False, post=lambda *a, **k: Response(status, body)
            )
            with self.assertRaises(client.FindChickenError) as caught:
                await transport.search(url="https://example.test/a.jpg")
            self.assertEqual(caught.exception.kind, kind)

    async def test_invalid_url_and_oversized_response(self):
        transport = client.FindChickenClient("key", "", 30, 1)
        for url in (
            "file:///private",
            "http://user:password@example.test/a.jpg",
            "http://[",
        ):
            with self.assertRaises(client.FindChickenError):
                await transport.search(url=url)
        response = Response()
        response.body = b"x" * (2 * 1024 * 1024 + 1)
        transport._session = types.SimpleNamespace(
            closed=False, post=lambda *a, **k: response
        )
        with self.assertRaises(client.FindChickenError):
            await transport.search(url="https://example.test/a.jpg")
