"""Regression coverage for missing Lens sections, empty caching and recovery."""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock

import test_google_lens as existing

lens = existing.lens


class ParsingTests(unittest.TestCase):
    def test_all_merges_sections_and_keeps_titleless_links(self):
        payload = {
            "exact_matches": [{"link": "https://example.org/exact"}],
            "visual_matches": [
                {"title": "duplicate", "link": "https://example.org/exact"},
                {"title": "Visual", "link": "https://example.org/visual"},
            ],
            "organic_results": [{"title": "Web", "link": "https://example.org/web"}],
            "related_content": [{"link": "https://example.org/not-a-source"}],
        }
        result = lens.parse_lens_results(payload, "all", 10)
        self.assertEqual(
            [r.url.rsplit("/", 1)[-1] for r in result.results],
            ["exact", "visual", "web"],
        )
        self.assertEqual(result.results[0].title, "（无标题）")
        self.assertEqual(result.results[-1].extra["match_type"], "网页相关")
        self.assertTrue(all(r.score is None for r in result.results))
        self.assertEqual(result.meta["raw_counts"]["visual_matches"], 2)

    def test_web_only_response_is_not_empty_in_all_mode(self):
        data = {
            "search_metadata": {"status": "Success"},
            "organic_results": [{"link": "https://example.org/work"}],
        }
        self.assertEqual(len(lens.parse_lens_results(data, "all", 10).results), 1)
        self.assertFalse(lens.parse_lens_results(data, "exact_matches", 10).results)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = lens.GoogleLensClient({"google_lens_api_key": "fake-key"})
        self.empty = {"search_metadata": {"status": "Success"}, "visual_matches": []}

    async def asyncTearDown(self):
        await self.client.close()

    async def test_empty_visual_mode_recovers_via_all_with_only_one_upload(self):
        import io
        from PIL import Image

        data = io.BytesIO()
        Image.new("RGB", (10, 10)).save(data, format="PNG")
        self.client.search_type = "visual_matches"
        self.client._request = AsyncMock(
            side_effect=[
                {"image_id": "uploaded"},
                self.empty,
                existing.response_payload(),
            ]
        )
        result = await self.client.search(data.getvalue(), filename="test.png")
        self.assertEqual(len(result.results), 3)
        calls = self.client._request.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [call.kwargs["params"]["type"] for call in calls[1:]],
            ["visual_matches", "all"],
        )
        self.assertTrue(
            all(call.kwargs["params"]["image_id"] == "uploaded" for call in calls[1:])
        )
        await self.client.search(data.getvalue(), filename="test.png")
        self.assertEqual(self.client._request.await_count, 3)

    async def test_empty_cache_is_short_and_expiry_bypasses_upstream_cache(self):
        self.client._request = AsyncMock(return_value=self.empty)
        await self.client.search(url="https://example.org/image.png")
        self.assertEqual(self.client._request.await_count, 2)
        key, (expires, result) = next(iter(self.client._cache.items()))
        self.assertLessEqual(expires - time.monotonic(), 30)
        await self.client.search(url="https://example.org/image.png")
        self.assertEqual(self.client._request.await_count, 2)
        self.client._cache[key] = (0, result)
        await self.client.search(url="https://example.org/image.png")
        self.assertEqual(self.client._request.await_count, 4)
        self.assertEqual(
            self.client._request.call_args_list[2].kwargs["params"]["no_cache"], "true"
        )

    async def test_empty_error_string_can_recover_but_quota_never_retries(self):
        self.client._request = AsyncMock(
            side_effect=[
                {"error": "Google Lens hasn't returned any results for this query."},
                existing.response_payload(),
            ]
        )
        self.assertTrue(
            (await self.client.search(url="https://example.org/a.png")).results
        )
        self.client._request = AsyncMock(side_effect=lens.GoogleLensError("quota"))
        with self.assertRaises(lens.GoogleLensError):
            await self.client.search(url="https://example.org/b.png")
        self.client._request.assert_awaited_once()

    async def test_exact_mode_and_disabled_recovery_make_one_request(self):
        self.client._request = AsyncMock(return_value=self.empty)
        self.client.search_type = "exact_matches"
        await self.client.search(url="https://example.org/exact.png")
        self.client._request.assert_awaited_once()
        self.client.search_type = "all"
        self.client.retry_empty = False
        self.client._request.reset_mock()
        await self.client.search(url="https://example.org/all.png")
        self.client._request.assert_awaited_once()

    async def test_recovery_failure_is_not_cached_as_a_successful_empty(self):
        self.client._request = AsyncMock(
            side_effect=[self.empty, lens.GoogleLensError("network")]
        )
        with self.assertRaises(lens.GoogleLensError) as caught:
            await self.client.search(url="https://example.org/a.png")
        self.assertEqual(caught.exception.kind, "network")
        self.assertFalse(self.client._cache)

    async def test_recovery_shares_total_deadline(self):
        self.client.timeout = 0.02
        calls = 0

        async def request(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return self.empty
            await asyncio.Event().wait()

        self.client._request = AsyncMock(side_effect=request)
        with self.assertRaises(lens.GoogleLensError) as caught:
            await self.client.search(url="https://example.org/a.png")
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertEqual(calls, 2)


class WorkflowEmptyTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_failure_is_not_misreported_as_no_results(self):
        harness = existing.WorkflowTests()
        await harness.asyncSetUp()
        try:
            harness.plugin._findchicken_client.search.return_value = {"results": []}
            harness.plugin._google_lens.search.side_effect = lens.GoogleLensError(
                "quota"
            )
            text = await harness.run_command()
            self.assertEqual("找鸡出现错误，请稍后重试", text)
            self.assertNotIn("额度不足", text)
            harness.plugin.context.llm_generate.assert_not_awaited()
        finally:
            await harness.asyncTearDown()

    async def test_actual_empty_is_not_blame_on_image_quality_or_model(self):
        harness = existing.WorkflowTests()
        await harness.asyncSetUp()
        try:
            harness.plugin._findchicken_client.search.return_value = {"results": []}
            harness.plugin._google_lens.search.return_value = lens.SourceOutcome()
            text = await harness.run_command()
            self.assertEqual("未找到鸡，可能目前暂未收录", text)
            self.assertNotIn("更清晰", text)
            harness.plugin.context.llm_generate.assert_not_awaited()
        finally:
            await harness.asyncTearDown()
