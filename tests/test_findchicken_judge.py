"""Regression checks for evidence grounding, model routing and single output."""

import asyncio
import importlib
import json
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_main_contract as contract
from test_findchicken import payload

judge = importlib.import_module(f"{contract.PACKAGE_NAME}.findchicken_judge")
models = importlib.import_module(f"{contract.PACKAGE_NAME}.findchicken_models")
formatter = importlib.import_module(f"{contract.PACKAGE_NAME}.engines.formatter")


def decision(candidate_id="c1", name="Artist", **kwargs):
    return json.dumps(
        dict(
            candidate_id=candidate_id,
            name=name,
            name_field="author",
            name_evidence=name,
            confidence="high",
            attribution="original",
            **kwargs,
        )
    )


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.candidates = judge.build_candidates(
            models.author_matches(
                payload(
                    author_name="Artist",
                    twitter_user_handle="artist",
                    ext_urls=["https://x.com/artist/status/123"],
                ),
                30,
                8,
            ),
            [],
            8,
        )

    def test_valid_identity_preserves_profile_and_work(self):
        match = judge.validate_selection(decision(), self.candidates)
        self.assertEqual(match.name, "Artist")
        self.assertEqual(match.account, "@artist")
        text = models.format_authors(
            [match, match], direct_url=True, show_similarity=False
        )
        self.assertEqual(text.count("👤"), 1)
        self.assertTrue(text.startswith("🎯 已找到鸡\n🌐"))
        self.assertIn("匹配：视觉匹配", text)
        self.assertIn("🔗 出处：https://x.com/artist/status/123", text)
        self.assertNotIn("引擎", text)

    def test_hallucinations_and_untrusted_output_do_not_escape(self):
        for content in (
            "not json",
            "[]",
            "{}",
            decision("missing"),
            decision(name="Invented"),
            decision(name="Artist\nInjected"),
            '{"candidate_id":null}',
            "x" * 12001,
        ):
            self.assertIsNone(judge.validate_selection(content, self.candidates))
        content = decision(url="https://evil.test", account="@fake")
        match = judge.validate_selection(content, self.candidates)
        self.assertEqual(match.account, "@artist")
        self.assertEqual(match.source_url, "https://x.com/artist/status/123")

    def test_title_is_not_an_identity_and_pixiv_artwork_is_not_user(self):
        rows = [
            formatter.SearchResult(
                title="Illustration",
                source="Pixiv",
                url="https://www.pixiv.net/artworks/456",
            )
        ]
        candidates = judge.build_candidates([], rows, 8)
        self.assertFalse(candidates[0].account)
        self.assertIsNone(
            judge.validate_selection(decision(name="Illustration"), candidates)
        )
        match = models.author_matches(
            payload(9, title="Random post", creator="Person"), 30, 8
        )[0]
        self.assertEqual(match.account, "未知")

    def test_explicit_account_url_and_title_evidence(self):
        row = formatter.SearchResult(
            title="Painting by Alice (@alice)",
            source="Twitter",
            url="https://x.com/alice/status/123",
        )
        candidates = judge.build_candidates([], [row], 8)
        data = dict(
            candidate_id="c1",
            name="Alice",
            name_field="title",
            name_evidence="by Alice (@alice)",
            confidence="high",
            attribution="original",
        )
        match = judge.validate_selection(json.dumps(data), candidates)
        self.assertEqual(match.account, "@alice")
        self.assertIsNone(match.similarity)
        data.update(name=row.title, name_evidence=row.title)
        self.assertIsNone(judge.validate_selection(json.dumps(data), candidates))
        for url in (
            "https://x.com/i/status/123",
            "https://x.com/intent/tweet",
            "https://x.com.evil.test/alice/status/123",
        ):
            row.url = url
            self.assertFalse(judge.build_candidates([], [row], 8)[0].account)

    def test_source_conflict_is_rejected_and_only_same_work_is_merged(self):
        matches = models.author_matches(
            payload(
                author_name="Artist",
                twitter_user_handle="artist",
                ext_urls=["https://x.com/other/status/1"],
            ),
            30,
            8,
        )
        self.assertFalse(judge.build_candidates(matches, [], 8)[0].account)
        row = formatter.SearchResult(
            title="By Artist", source="X", url="https://twitter.com/artist/status/123"
        )
        matches = models.author_matches(
            payload(
                author_name="Artist",
                twitter_user_handle="artist",
                ext_urls=["https://x.com/artist/status/123"],
            ),
            30,
            8,
        )
        self.assertEqual(len(judge.build_candidates(matches, [row], 8)), 1)
        row.url = "https://x.com/artist/status/999"
        self.assertEqual(len(judge.build_candidates(matches, [row], 8)), 2)

    def test_llm_can_select_original_even_when_repost_has_higher_similarity(self):
        stronger = models.AuthorMatch(
            "Other",
            "Other Artist",
            "@other",
            99,
            "https://example.org/other",
            "https://example.org/post/1",
        )
        lower = models.AuthorMatch(
            "X (Twitter)",
            "Artist",
            "@artist",
            86,
            "https://x.com/artist",
            "https://x.com/artist/status/123",
        )
        candidates = judge.build_candidates([stronger, lower], [], 8)
        self.assertEqual(
            judge.validate_selection(decision("c2"), candidates).account, "@artist"
        )

    def test_credit_name_must_not_be_paired_with_reposters_account(self):
        row = formatter.SearchResult(
            "Reposted with credit to Alice (@alice)",
            "X",
            "https://x.com/reposter/status/123",
        )
        candidates = judge.build_candidates([], [row], 8)
        data = dict(
            candidate_id="c1",
            name="Alice",
            name_field="title",
            name_evidence="Alice (@alice)",
            confidence="high",
            attribution="original",
        )
        self.assertIsNone(judge.validate_selection(json.dumps(data), candidates))


class ModelCallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.candidates = judge.build_candidates(
            models.author_matches(
                payload(author_name="Artist", twitter_user_handle="artist"), 30, 8
            ),
            [],
            8,
        )
        self.context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="session-model"),
            get_provider_by_id=Mock(
                return_value=types.SimpleNamespace(
                    provider_config={"modalities": ["text", "image"]}
                )
            ),
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(completion_text=decision())
            ),
        )

    async def test_explicit_provider_no_history_tools_or_engine_identity(self):
        match = await judge.judge_author(
            self.context,
            self.candidates,
            provider_id="selected",
            umo="session",
            timeout=1,
        )
        self.assertEqual(match.account, "@artist")
        self.context.get_current_chat_provider_id.assert_not_awaited()
        call = self.context.llm_generate.call_args.kwargs
        self.assertEqual(call["chat_provider_id"], "selected")
        self.assertEqual(call["contexts"], [])
        self.assertIsNone(call["tools"])
        self.assertEqual(call["image_urls"], [])

    async def test_current_provider_and_text_only_model(self):
        self.context.get_provider_by_id.return_value.provider_config["modalities"] = [
            "text"
        ]
        await judge.judge_author(
            self.context, self.candidates, provider_id="", umo="session", timeout=1
        )
        self.context.get_current_chat_provider_id.assert_awaited_once_with("session")
        self.assertEqual(self.context.llm_generate.call_args.kwargs["image_urls"], [])

    async def test_timeout_cancellation_and_empty_pool(self):
        async def waiting(**kwargs):
            await asyncio.Event().wait()

        self.context.llm_generate.side_effect = waiting
        with self.assertRaises(TimeoutError):
            await judge.judge_author(
                self.context,
                self.candidates,
                provider_id="selected",
                umo="",
                timeout=0.01,
            )
        self.context.llm_generate.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await judge.judge_author(
                self.context, self.candidates, provider_id="selected", umo="", timeout=1
            )
        self.context.llm_generate.reset_mock()
        self.assertIsNone(
            await judge.judge_author(
                self.context, [], provider_id="selected", umo="", timeout=1
            )
        )
        self.context.llm_generate.assert_not_awaited()


class MetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_origins_resolve_pixiv_user_and_validate_x_account(self):
        class Response:
            status = 200

            def __init__(self, data):
                self.data = json.dumps(data).encode()
                self.content = self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def iter_chunked(self, size):
                yield self.data

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        session = Session()
        session.get = Mock(
            side_effect=[
                Response(
                    {
                        "error": False,
                        "body": {"userId": "123", "userName": "Pixiv Artist"},
                    }
                ),
                Response(
                    {"author_name": "Wrong Person", "author_url": "https://x.com/other"}
                ),
                Response(
                    {
                        "author_name": "Alice",
                        "author_url": "https://www.tiktok.com/@alice.art",
                        "title": "Repost with credit to original artist",
                    }
                ),
                Response(
                    {
                        "code": 0,
                        "data": {
                            "owner": {"mid": 789, "name": "Uploader"},
                            "copyright": 2,
                            "desc": "转载，原作者在 Instagram",
                        },
                    }
                ),
            ]
        )
        rows = [
            formatter.SearchResult(
                "Work", "Pixiv", "https://www.pixiv.net/artworks/456"
            ),
            formatter.SearchResult("A post", "X", "https://x.com/artist/status/123"),
            formatter.SearchResult(
                "Spoof", "Pixiv", "https://pixiv.net.evil.test/artworks/456"
            ),
            formatter.SearchResult(
                "Post", "TikTok", "https://www.tiktok.com/@alice.art/video/123"
            ),
            formatter.SearchResult(
                "Video", "Bilibili", "https://www.bilibili.com/video/BV17x411w7KC"
            ),
        ]
        candidates = judge.build_candidates([], rows, 8)
        with patch.object(judge.aiohttp, "ClientSession", return_value=session):
            await judge.enrich_identities(candidates)
        self.assertEqual(session.get.call_count, 4)
        self.assertEqual(
            session.get.call_args_list[0].args[0],
            "https://www.pixiv.net/ajax/illust/456",
        )
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertEqual(candidates[0].account, "@123")
        self.assertEqual(candidates[0].author, "Pixiv Artist")
        self.assertFalse(candidates[1].author)
        self.assertFalse(candidates[2].account)
        self.assertEqual(candidates[3].author, "Alice")
        self.assertIn("Repost", candidates[3].source_excerpt)
        self.assertEqual(candidates[4].account, "@789")
        self.assertIn("转载", candidates[4].publisher_evidence)
        self.assertIn("原作者", candidates[4].source_excerpt)
