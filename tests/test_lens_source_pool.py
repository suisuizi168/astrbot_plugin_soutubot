"""Source expansion preserves model choice and does not discard primary matches."""

import asyncio
import importlib
import json
import types
import unittest
from unittest.mock import AsyncMock, Mock

import test_google_lens as existing
import test_main_contract as contract

lens = existing.lens
judge = importlib.import_module(f"{contract.PACKAGE_NAME}.findchicken_judge")


def payload(urls):
    return {"visual_matches": [{"title": "Example work", "link": url} for url in urls]}


class SourcePoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = lens.GoogleLensClient({"google_lens_api_key": "fake-key"})
        self.url = "https://example.org/image.png"

    async def asyncTearDown(self):
        await self.client.close()

    async def test_nonempty_without_x_expands_and_all_candidates_reach_model(self):
        self.client._request = AsyncMock(
            side_effect=[
                payload(
                    ["https://example.org/forum", "https://www.instagram.com/sample/"]
                ),
                payload(
                    ["https://x.com/artist/status/123", "https://example.org/forum"]
                ),
            ]
        )
        result = await self.client.search(url=self.url, limit=30, for_authors=True)
        self.assertEqual(self.client._request.await_count, 2)
        self.assertEqual(len(result.results), 3)
        candidates = judge.build_candidates([], result.results, 30)
        self.assertEqual(candidates[0].platform, "X (Twitter)")
        self.assertEqual(candidates[0].rank, 2)
        context = types.SimpleNamespace(
            get_provider_by_id=Mock(return_value=object()),
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(
                    completion_text='{"candidate_id":"c2"}'
                )
            ),
        )
        chosen = await judge.judge_author(
            context, candidates, provider_id="test", umo="", timeout=1
        )
        submitted = json.loads(context.llm_generate.call_args.kwargs["prompt"])[
            "candidates"
        ]
        self.assertEqual(len(submitted), 3)
        self.assertEqual(chosen.platform, "Instagram")
        self.assertIn("example.org/forum", str(submitted))

    async def test_lower_ranked_x_survives_model_pool_limit_and_shared_cache(self):
        rows = [f"https://example.org/{i}" for i in range(40)] + [
            "https://x.com/sample/status/123"
        ]
        self.client._request = AsyncMock(return_value=payload(rows))
        normal = await self.client.search(url=self.url)
        authors = await self.client.search(url=self.url, limit=30, for_authors=True)
        self.client._request.assert_awaited_once()
        self.assertEqual(normal.results[0].url, rows[0])
        self.assertEqual(authors.results[0].url, rows[-1])
        self.assertEqual(authors.results[0].extra["search_rank"], 41)
        self.assertEqual(len(authors.results), 30)
        self.assertEqual(
            (await self.client.search(url=self.url)).results[0].url, rows[0]
        )

    async def test_other_supported_platforms_survive_when_both_modes_lack_x(self):
        self.client._request = AsyncMock(
            side_effect=[
                payload(["https://example.org/forum"]),
                payload(
                    [
                        "https://www.tiktok.com/@artist/video/123",
                        "https://www.pixiv.net/artworks/123",
                    ]
                ),
            ]
        )
        result = await self.client.search(url=self.url, limit=30, for_authors=True)
        candidates = judge.build_candidates([], result.results, 30)
        self.assertEqual([c.platform for c in candidates[:2]], ["TikTok", "Pixiv"])
        self.assertEqual(len(candidates), 3)

    async def test_expansion_error_keeps_primary_and_does_not_cache_incomplete_pool(
        self,
    ):
        self.client._request = AsyncMock(
            side_effect=[
                payload(["https://example.org/forum"]),
                lens.GoogleLensError("quota"),
            ]
        )
        result = await self.client.search(url=self.url)
        self.assertTrue(result.results)
        self.assertTrue(result.meta["expansion_failed"])
        self.assertFalse(self.client._cache)

    async def test_expansion_deadline_keeps_primary_but_external_cancellation_propagates(
        self,
    ):
        self.client.timeout = 0.02
        calls = 0

        async def request(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return payload(["https://example.org/forum"])
            await asyncio.Event().wait()

        self.client._request = AsyncMock(side_effect=request)
        result = await self.client.search(url=self.url)
        self.assertTrue(result.results)
        self.assertFalse(self.client._cache)
        self.client._request = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.client.search(url=self.url)

    async def test_x_like_domains_do_not_satisfy_x_preference(self):
        self.client._request = AsyncMock(
            side_effect=[
                payload(["https://x.com.evil.test/artist/status/1"]),
                payload(["https://www.threads.com/@artist/post/ABC"]),
            ]
        )
        result = await self.client.search(url=self.url, limit=30, for_authors=True)
        self.assertEqual(self.client._request.await_count, 2)
        self.assertIn("threads.com", result.results[0].url)

    async def test_disabled_and_exact_modes_do_not_expand_nonempty_results(self):
        self.client.expand_sources = False
        self.client._request = AsyncMock(
            return_value=payload(["https://example.org/forum"])
        )
        await self.client.search(url=self.url)
        self.client._request.assert_awaited_once()
        self.client.expand_sources = True
        self.client.search_type = "exact_matches"
        self.client._request = AsyncMock(
            return_value={"exact_matches": [{"link": "https://example.org/forum"}]}
        )
        await self.client.search(url=self.url)
        self.client._request.assert_awaited_once()

    async def test_empty_and_source_expansion_together_never_exceed_two_searches(self):
        self.client._request = AsyncMock(
            side_effect=[{"visual_matches": []}, payload(["https://example.org/forum"])]
        )
        result = await self.client.search(url=self.url)
        self.assertTrue(result.results)
        self.assertEqual(self.client._request.await_count, 2)
