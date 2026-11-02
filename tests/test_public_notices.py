"""Public responses hide provider details and distinguish empty/error outcomes."""

import asyncio
import importlib
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_main_contract as contract
from test_findchicken import payload
from test_search_commands import Event

workflow = importlib.import_module(f"{contract.PACKAGE_NAME}.findchicken_workflow")
lens = importlib.import_module(f"{contract.PACKAGE_NAME}.google_lens_client")
models = importlib.import_module(f"{contract.PACKAGE_NAME}.engines.formatter")

EMPTY = "未找到鸡，可能目前暂未收录"
ERROR = "找鸡出现错误，请稍后重试"


class PublicNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = contract.Main(
            types.SimpleNamespace(),
            {
                "findchicken_api_key": "fake-sauce",
                "google_lens_api_key": "fake-lens",
                "findchicken_llm_enabled": False,
                "findchicken_cooldown_seconds": 0,
                "saucenao_enabled": False,
                "yandex_enabled": False,
                "ascii2d_enabled": False,
            },
        )
        self.plugin._findchicken_client.search = AsyncMock(return_value={"results": []})
        self.plugin._google_lens.search = AsyncMock(return_value=models.SourceOutcome())

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def query(self):
        return await self.plugin._query_findchicken(
            None, "https://example.org/a.png", True
        )

    def assert_private(self, text):
        for word in (
            "google",
            "lens",
            "serpapi",
            "谷歌",
            "saucenao",
            "fake-",
            "private-error",
        ):
            self.assertNotIn(word, text.lower())

    async def test_successful_empty_skips_llm_and_returns_exact_notice(self):
        self.plugin._findchicken_llm_enabled = True
        with patch.object(workflow, "judge_author", new=AsyncMock()) as judge:
            self.assertEqual(await self.query(), EMPTY)
            judge.assert_not_awaited()

    async def test_no_above_threshold_or_usable_url_is_a_normal_empty(self):
        self.plugin._findchicken_threshold = 80
        self.plugin._findchicken_client.search.return_value = payload(score="79")
        self.assertEqual(await self.query(), EMPTY)

    async def test_empty_plus_error_and_all_errors_return_exact_error(self):
        for exc in (
            lens.GoogleLensError("quota"),
            lens.GoogleLensError("key"),
            lens.GoogleLensError("missing_key"),
            asyncio.TimeoutError(),
            RuntimeError("private-error"),
        ):
            self.plugin._google_lens.search.side_effect = exc
            for other in (None, RuntimeError("private-error")):
                self.plugin._findchicken_client.search.side_effect = other
                result = await self.query()
                self.assertEqual(result, ERROR)
                self.assert_private(result)

    async def test_disabled_provider_is_not_an_error(self):
        self.plugin._google_lens.findchicken_enabled = False
        self.assertEqual(await self.query(), EMPTY)
        self.plugin._google_lens.search.assert_not_awaited()
        self.plugin._findchicken_client.api_key = ""
        self.assertEqual(await self.query(), ERROR)

    async def test_partial_success_and_llm_failure_keep_one_source(self):
        self.plugin._findchicken_client.search.return_value = payload()
        self.plugin._google_lens.search.side_effect = lens.GoogleLensError("network")
        self.plugin._findchicken_llm_enabled = True
        with (
            patch.object(workflow, "enrich_identities", new=AsyncMock()),
            patch.object(
                workflow,
                "judge_author",
                new=AsyncMock(side_effect=RuntimeError("private-error")),
            ),
        ):
            text = await self.query()
        self.assertIn("sample_artist", text)
        self.assertEqual(text.count("👤"), 1)
        self.assert_private(text)

    async def test_outer_input_failure_and_no_keys_use_generic_error(self):
        self.plugin._send_chain = AsyncMock()
        for kind in ("input", "keys"):
            event = Event([])
            event.message_str = "找鸡 https://example.org/a.png"
            if kind == "input":
                self.plugin._findchicken_input = AsyncMock(
                    side_effect=RuntimeError("private-error")
                )
            else:
                self.plugin._findchicken_input = AsyncMock(
                    return_value=("https://example.org/a.png", True)
                )
                self.plugin._findchicken_client.api_key = (
                    self.plugin._google_lens.api_key
                ) = ""
            await self.plugin._background_findchicken(event, "test")
            text = self.plugin._send_chain.call_args.args[1][0].text
            self.assertEqual(text, ERROR)
            self.assert_private(text)

    async def test_multi_search_success_empty_error_and_warnings_hide_transport(self):
        prepared = types.SimpleNamespace(
            data=b"fixture", filename="a.png", content_type="image/png"
        )
        for rows in (
            [],
            [
                models.SearchResult(
                    title="A work",
                    source="Pixiv",
                    url="https://www.pixiv.net/artworks/123",
                )
            ],
        ):
            self.plugin._google_lens.search.return_value = models.SourceOutcome(
                results=rows, warnings=["Google Lens SerpAPI diagnostic"]
            )
            text = await self.plugin._multi_search.search(prepared)
            self.assertIn("【图片来源】", text)
            self.assert_private(text)
            if rows:
                self.assertIn("https://www.pixiv.net/artworks/123", text)
        self.plugin._google_lens.search.side_effect = lens.GoogleLensError("key")
        text = await self.plugin._multi_search.search(prepared)
        self.assertIn("查询出现错误，请稍后重试", text)
        self.assert_private(text)
