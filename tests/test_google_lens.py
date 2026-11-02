"""Transport, quota preservation and parallel workflow regression tests."""

import asyncio
import importlib
import io
import json
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

import test_main_contract as contract
from test_findchicken import payload as sauce_payload
from test_search_commands import Event

lens = importlib.import_module(f"{contract.PACKAGE_NAME}.google_lens_client")
models = importlib.import_module(f"{contract.PACKAGE_NAME}.findchicken_models")


def response_payload():
    return {
        "visual_matches": [
            {
                "title": "An illustrated guide",
                "source": "Example Wiki",
                "link": "https://example.org/wiki/art",
                "similarity": 99,
            },
            {
                "title": "A video",
                "source": "YouTube",
                "link": "https://youtube.com/watch?v=1",
            },
            {
                "title": "Post by artist",
                "source": "X",
                "link": "https://x.com/artist/status/123",
            },
        ]
    }


class ParserTests(unittest.TestCase):
    def test_arbitrary_sites_do_not_become_authors_or_scores(self):
        outcome = lens.parse_lens_results(response_payload(), "visual_matches", 3)
        self.assertEqual(len(outcome.results), 3)
        text = models.format_authors(
            [], direct_url=False, show_similarity=True, google_results=outcome.results
        )
        self.assertEqual("未找到鸡，可能目前暂未收录", text)
        self.assertNotIn("Example Wiki", text)
        self.assertNotIn("📄 标题", text)
        self.assertNotIn("👤", text)
        self.assertNotIn("🆔", text)
        self.assertNotIn("99", text)

    def test_exact_type_dedup_and_unsafe_urls(self):
        data = {
            "exact_matches": [
                {
                    "title": "a",
                    "link": "https://twitter.com/artist/status/123?utm_source=a",
                },
                {"title": "duplicate", "link": "https://x.com/artist/status/123"},
                {"title": "unsafe", "link": "javascript:alert(1)"},
                {"title": "credentials", "link": "https://user:secret@example.org"},
                {"title": "broken", "link": "https://example.org:bad/"},
                {"title": "Other work", "link": "https://x.com/artist/status/456"},
            ]
        }
        rows = lens.parse_lens_results(data, "exact_matches", 5).results
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].extra["match_type"], "同图匹配")
        for data in (
            {},
            {"visual_matches": {}},
            {"search_metadata": {"status": "Processing"}},
        ):
            with self.assertRaises(lens.GoogleLensError):
                lens.parse_lens_results(data, "all", 3)

    def test_merge_same_work_but_not_same_author(self):
        matches = models.author_matches(
            sauce_payload(
                twitter_user_handle="artist",
                ext_urls=["https://twitter.com/artist/status/123"],
            ),
            30,
            1,
        )
        rows = lens.parse_lens_results(response_payload(), "all", 3).results
        text = models.format_authors(
            matches, direct_url=False, show_similarity=False, google_results=rows
        )
        self.assertNotIn("引擎", text)
        self.assertIn("🔗 出处：https://twitter.com/artist/status/123", text)
        self.assertNotIn("📄 标题：Post by artist", text)
        rows[-1].url = "https://x.com/artist/status/456"
        text = models.format_authors(
            matches, direct_url=False, show_similarity=False, google_results=rows
        )
        self.assertNotIn("SauceNAO / Google Lens", text)
        self.assertNotIn("📄 标题", text)
        # A profile is not proof that two results refer to the same work.
        matches = models.author_matches(
            sauce_payload(twitter_user_handle="artist"), 30, 1
        )
        rows[-1].url = "https://x.com/artist"
        text = models.format_authors(
            matches, direct_url=False, show_similarity=False, google_results=rows
        )
        self.assertNotIn("SauceNAO / Google Lens", text)

    def test_configuration_is_contiguous_and_key_is_secret(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        schema = schema['image_search']['items']
        positions = [
            i for i, name in enumerate(schema) if name.startswith("google_lens_")
        ]
        self.assertEqual(positions, list(range(positions[0], positions[0] + 10)))
        self.assertTrue(schema["google_lens_api_key"]["secret"])
        self.assertEqual(schema["google_lens_api_key"]["default"], "")


class Response:
    def __init__(self, status=200, payload=None, body=None):
        self.status = status
        self.body = (
            body
            if body is not None
            else json.dumps(payload or response_payload()).encode()
        )
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        yield self.body


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = lens.GoogleLensClient(
            {"google_lens_api_key": "private-test-secret"}
        )

    async def asyncTearDown(self):
        await self.client.close()

    async def test_url_single_request_and_upload_compression(self):
        self.client._request = AsyncMock(return_value=response_payload())
        await self.client.search(url="https://example.org/a.png")
        self.client._request.assert_awaited_once()
        params = self.client._request.call_args.kwargs["params"]
        self.assertEqual(params["type"], "all")
        self.assertEqual(params["no_cache"], "false")
        self.assertNotIn("image_id", params)
        data = io.BytesIO()
        Image.effect_noise((1200, 1200), 100).convert("RGB").save(data, format="PNG")
        self.assertGreater(len(data.getvalue()), 500_000)
        self.client._request = AsyncMock(
            side_effect=[{"image_id": "image-id"}, response_payload()]
        )
        await self.client.search(data.getvalue(), filename="large.png")
        self.assertEqual(self.client._request.await_count, 2)
        form = self.client._request.call_args_list[0].kwargs["data"]
        self.assertLessEqual(len(form._fields[1][2]), 500_000)
        self.assertEqual(
            self.client._request.call_args.kwargs["params"]["image_id"], "image-id"
        )

    async def test_shared_inflight_cache_and_copy_isolation(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def request(*args, **kwargs):
            started.set()
            await release.wait()
            return response_payload()

        self.client._request = AsyncMock(side_effect=request)
        first = asyncio.create_task(self.client.search(url="https://example.org/a.png"))
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(
            self.client.search(url="https://example.org/a.png")
        )
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        result = await asyncio.wait_for(second, 1)
        result.results.clear()
        again = await self.client.search(url="https://example.org/a.png")
        self.assertEqual(len(again.results), 3)
        self.client._request.assert_awaited_once()

    async def test_failure_is_not_cached_and_expiry_is_respected(self):
        self.client._request = AsyncMock(
            side_effect=[
                lens.GoogleLensError("quota"),
                response_payload(),
                response_payload(),
            ]
        )
        with self.assertRaises(lens.GoogleLensError):
            await self.client.search(url="https://example.org/a.png")
        await self.client.search(url="https://example.org/a.png")
        key, (_, result) = next(iter(self.client._cache.items()))
        self.client._cache[key] = (0, result)
        await self.client.search(url="https://example.org/a.png")
        self.assertEqual(self.client._request.await_count, 3)

    async def test_author_candidate_limit_reuses_full_cache_without_changing_search_limit(
        self,
    ):
        self.client.expand_sources = False
        data = {
            "visual_matches": [
                {"title": f"Work {i}", "link": f"https://example.org/{i}"}
                for i in range(10)
            ]
        }
        self.client._request = AsyncMock(return_value=data)
        normal = await self.client.search(url="https://example.org/a.png")
        candidates = await self.client.search(url="https://example.org/a.png", limit=8)
        again = await self.client.search(url="https://example.org/a.png")
        self.assertEqual(
            (len(normal.results), len(candidates.results), len(again.results)),
            (3, 8, 3),
        )
        self.client._request.assert_awaited_once()

    async def test_errors_are_bounded_without_key_leaks_or_redirects(self):
        for response, kind in (
            (Response(401), "key"),
            (Response(403), "key"),
            (Response(429), "quota"),
            (Response(302), "response"),
            (Response(body=b"not-json private-test-secret"), "response"),
            (Response(body=b"x" * (4 * 1024 * 1024 + 1)), "response"),
        ):
            session = types.SimpleNamespace(
                closed=False, request=Mock(return_value=response), close=AsyncMock()
            )
            self.client._session = session
            with self.assertRaises(lens.GoogleLensError) as caught:
                await self.client.search(url="https://example.org/a.png")
            self.assertEqual(caught.exception.kind, kind)
            self.assertNotIn("private-test-secret", str(caught.exception))
            self.assertFalse(session.request.call_args.kwargs["allow_redirects"])

    async def test_missing_key_and_invalid_input_never_request(self):
        self.client._request = AsyncMock()
        for url in (
            "file:///a",
            "http://user:password@example.org",
            "http://[",
            "https://example.org/\nabc",
        ):
            with self.assertRaises(lens.GoogleLensError):
                await self.client.search(url=url)
        self.client.api_key = ""
        with self.assertRaises(lens.GoogleLensError):
            await self.client.search(url="https://example.org/a.png")
        self.client._request.assert_not_awaited()

    async def test_timeout_and_close_cancel_background_jobs(self):
        started = asyncio.Event()

        async def request(*a, **kw):
            started.set()
            await asyncio.Event().wait()

        self.client._request = AsyncMock(side_effect=request)
        self.client.timeout = 0.02
        with self.assertRaises(lens.GoogleLensError) as caught:
            await self.client.search(url="https://example.org/a.png")
        self.assertEqual(caught.exception.kind, "timeout")
        self.client.timeout = 30
        started.clear()
        task = asyncio.create_task(self.client.search(url="https://example.org/a.png"))
        await asyncio.wait_for(started.wait(), 1)
        await self.client.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.client._inflight)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = contract.Main(
            types.SimpleNamespace(),
            {
                "findchicken_api_key": "sauce-key",
                "google_lens_api_key": "lens-key",
                "findchicken_cooldown_seconds": 0,
                "multi_search_enabled": False,
                "jm_enabled": False,
                "nh_download_enabled": False,
                "llm_tools_enabled": False,
            },
        )
        self.plugin._send_chain = AsyncMock()
        self.plugin._findchicken_client.search = AsyncMock(return_value=sauce_payload())
        self.plugin._google_lens.search = AsyncMock(
            return_value=lens.parse_lens_results(response_payload(), "all", 3)
        )

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def model_decision(self, **kwargs):
        candidates = json.loads(kwargs["prompt"])["candidates"]
        for c in candidates:
            if c["account"]:
                name = c["author"] or "artist"
                return types.SimpleNamespace(
                    completion_text=json.dumps(
                        {
                            "candidate_id": c["id"],
                            "name": name,
                            "name_field": "author" if c["author"] else "title",
                            "name_evidence": c["author"] or c["title"],
                            "confidence": "high",
                            "attribution": "original",
                        }
                    )
                )
        return types.SimpleNamespace(completion_text='{"candidate_id":null}')

    async def run_command(self):
        self.plugin.context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="test-model"),
            get_provider_by_id=Mock(return_value=types.SimpleNamespace()),
            llm_generate=AsyncMock(side_effect=self.model_decision),
        )
        event = Event([])
        event.message_str = "找鸡 https://example.org/a.png"
        results = [r async for r in self.plugin.findchicken_command(event)]
        self.assertFalse(results)
        with patch(
            f"{contract.PACKAGE_NAME}.findchicken_workflow.enrich_identities",
            new=AsyncMock(),
        ):
            await asyncio.gather(*tuple(self.plugin._findchicken_tasks))
        return [
            component.text
            for call in self.plugin._send_chain.call_args_list
            for component in call.args[1]
        ][-1]

    async def test_both_start_concurrently_before_either_finishes(self):
        sauce_started, lens_started = asyncio.Event(), asyncio.Event()

        async def sauce(**kw):
            sauce_started.set()
            await asyncio.wait_for(lens_started.wait(), 1)
            return sauce_payload()

        async def google(*a, **kw):
            lens_started.set()
            await asyncio.wait_for(sauce_started.wait(), 1)
            return lens.parse_lens_results(response_payload(), "all", 3)

        self.plugin._findchicken_client.search.side_effect = sauce
        self.plugin._google_lens.search.side_effect = google
        text = await self.run_command()
        self.assertIn("sample_artist", text)
        self.assertNotIn("Example Wiki", text)
        self.assertTrue(text.startswith("🎯 已找到鸡\n🌐"))
        self.assertEqual(text.count("👤"), 1)
        self.plugin.context.llm_generate.assert_awaited_once()

    async def test_google_failure_keeps_sauce_result(self):
        self.plugin._google_lens.search.side_effect = lens.GoogleLensError("quota")
        text = await self.run_command()
        self.assertIn("sample_artist", text)
        self.assertNotIn("引擎", text)

    async def test_sauce_timeout_keeps_google_result(self):
        self.plugin._findchicken_client.search.side_effect = asyncio.TimeoutError()
        text = await self.run_command()
        self.assertNotIn("Example Wiki", text)
        self.assertNotIn("SauceNAO", text)
        self.assertIn("🆔 账号：@artist", text)

    async def test_missing_sauce_key_still_queries_google(self):
        self.plugin._findchicken_client.api_key = ""
        text = await self.run_command()
        self.assertNotIn("Example Wiki", text)
        self.plugin._findchicken_client.search.assert_not_awaited()

    async def test_independent_google_switches(self):
        self.plugin._google_lens.findchicken_enabled = False
        text = await self.run_command()
        self.assertIn("sample_artist", text)
        self.plugin._google_lens.search.assert_not_awaited()
        self.plugin._multi_search.enabled = True
        self.plugin._multi_search.config.update(
            saucenao_enabled=False, yandex_enabled=False, ascii2d_enabled=False
        )
        self.assertIs(self.plugin._multi_search._google_lens, self.plugin._google_lens)
        text = await self.plugin._multi_search.search(
            types.SimpleNamespace(
                data=b"image", filename="a.png", content_type="image/png"
            )
        )
        self.assertIn("【图片来源】", text)
        self.assertNotIn("Google", text)
        self.plugin._google_lens.search.assert_awaited_once()

    async def test_all_errors_are_not_reported_as_no_matches(self):
        self.plugin._google_lens.search.side_effect = lens.GoogleLensError("network")
        self.plugin._findchicken_client.search.side_effect = RuntimeError(
            "private-secret"
        )
        text = await self.run_command()
        self.assertEqual(text, "找鸡出现错误，请稍后重试")
        self.assertNotIn("private-secret", text)

    async def test_model_error_keeps_best_source_without_using_web_title_as_name(self):
        self.model_decision = AsyncMock(
            side_effect=RuntimeError("private-model-secret")
        )
        text = await self.run_command()
        self.assertIn("找到匹配来源", text)
        self.assertIn("未确认原作者", text)
        self.assertNotIn("private-model-secret", text)
        self.assertNotIn("Example Wiki", text)
        self.assertEqual(text.count("👤"), 1)
        self.assertIn("sample_artist", text)

    async def test_abstention_and_bad_json_keep_candidate_above_configured_threshold(
        self,
    ):
        self.plugin._findchicken_threshold = 80
        response = sauce_payload()
        response["results"][0]["header"]["similarity"] = "82"
        self.plugin._findchicken_client.search.return_value = response
        for content in ('{"candidate_id":null}', "malformed response"):
            self.model_decision = AsyncMock(
                return_value=types.SimpleNamespace(completion_text=content)
            )
            text = await self.run_command()
            self.assertIn("找到匹配来源", text)
            self.assertIn("sample_artist", text)
            self.assertEqual(text.count("👤"), 1)
