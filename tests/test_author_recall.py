"""Regression tests for valid search titles previously rejected as unproven."""

import importlib
import json
import unittest

import test_main_contract as contract

judge = importlib.import_module(f"{contract.PACKAGE_NAME}.findchicken_judge")
models = importlib.import_module(f"{contract.PACKAGE_NAME}.findchicken_models")
platforms = importlib.import_module(f"{contract.PACKAGE_NAME}.author_platforms")
formatter = importlib.import_module(f"{contract.PACKAGE_NAME}.engines.formatter")


class RecallTests(unittest.TestCase):
    def test_display_name_on_x_is_bound_to_post_handle_without_oembed(self):
        row = formatter.SearchResult(
            '示例画师🐰 on X: "#art" / X',
            "X",
            "https://x.com/example_artist/status/123",
        )
        candidates = judge.build_candidates([], [row], 8)
        self.assertEqual(candidates[0].author, "示例画师🐰")
        details = {}
        selected = judge.validate_selection(
            json.dumps(
                dict(
                    candidate_id="c1",
                    confidence="medium",
                    attribution="likely_original",
                )
            ),
            candidates,
            diagnostics=details,
        )
        self.assertEqual(
            (selected.name, selected.account), ("示例画师🐰", "@example_artist")
        )
        self.assertFalse(selected.fallback)
        text = models.format_authors(
            [selected], direct_url=False, show_similarity=False
        )
        self.assertIn("已找到鸡", text)
        self.assertNotIn("没找到", text)
        self.assertEqual(text.count("👤"), 1)
        self.assertNotIn("📄 标题", text)
        self.assertEqual(details["outcome"], "selected")

    def test_bylines_do_not_cross_domains_or_accounts(self):
        name = platforms.publisher_display_name
        self.assertEqual(
            name("https://x.com/artist/status/1", 'Artist Name on Twitter: "work"'),
            "Artist Name",
        )
        self.assertEqual(
            name("https://x.com/artist", "Artist Name (@artist) / X"), "Artist Name"
        )
        self.assertEqual(
            name("https://x.com/reposter/status/1", "Artist Name (@artist) / X"), ""
        )
        self.assertEqual(
            name("https://example.org/post", 'Artist Name on X: "work"'), ""
        )
        self.assertEqual(
            name("https://x.com.evil.test/artist/status/1", 'Artist Name on X: "work"'),
            "",
        )

    def test_model_selects_id_but_cannot_supply_a_different_identity(self):
        candidates = judge.build_candidates(
            [],
            [
                formatter.SearchResult(
                    'Artist Name on X: "work"', "X", "https://x.com/artist/status/1"
                )
            ],
            8,
        )
        result = judge.validate_selection(
            json.dumps(
                dict(
                    candidate_id="c1",
                    confidence="high",
                    attribution="original",
                    account="@fake",
                    url="https://fake.test",
                )
            ),
            candidates,
        )
        self.assertEqual(result.account, "@artist")
        self.assertEqual(result.name, "Artist Name")
        self.assertEqual(result.source_url, "https://x.com/artist/status/1")

    def test_failures_have_different_diagnostic_codes(self):
        candidates = judge.build_candidates(
            [],
            [
                formatter.SearchResult(
                    "Work", "Pixiv", "https://www.pixiv.net/artworks/123"
                )
            ],
            8,
        )
        for content, reason in (
            ("broken", "invalid_json"),
            ('{"candidate_id":null}', "model_abstained"),
            ('{"candidate_id":"unknown"}', "unknown_candidate"),
        ):
            details = {}
            self.assertIsNone(
                judge.validate_selection(content, candidates, diagnostics=details)
            )
            self.assertEqual(details["outcome"], reason)

    def test_missing_identity_and_low_confidence_do_not_suppress_source(self):
        candidates = judge.build_candidates(
            [],
            [
                formatter.SearchResult(
                    "A work title", "Pixiv", "https://www.pixiv.net/artworks/123"
                )
            ],
            8,
        )
        result = judge.validate_selection(
            json.dumps(
                dict(candidate_id="c1", confidence="low", attribution="unknown")
            ),
            candidates,
        )
        self.assertEqual(result.name, "未提供")
        self.assertEqual(result.account, "未提供")
        self.assertEqual(result.source_url, candidates[0].url)

    def test_fallback_uses_actual_score_then_web_rank_without_inventing_identity(self):
        rows = [
            formatter.SearchResult(
                "A work title", "Gallery", "https://example.org/first"
            ),
            formatter.SearchResult(
                "Another work", "Gallery", "https://example.org/second"
            ),
        ]
        candidates = judge.build_candidates([], rows, 8)
        result = judge.best_source_fallback(candidates)
        self.assertEqual(result.source_url, rows[0].url)
        self.assertIsNone(result.similarity)
        self.assertEqual(result.name, "未提供")
        self.assertTrue(result.fallback)
        candidates[1].similarity = 82
        result = judge.best_source_fallback(candidates)
        self.assertEqual(result.source_url, rows[1].url)
        candidates[0].similarity = 95
        self.assertEqual(judge.best_source_fallback(candidates).source_url, rows[0].url)
        self.assertIsNone(judge.best_source_fallback([]))

    def test_plain_fenced_json_and_unknown_display_name(self):
        candidates = judge.build_candidates(
            [],
            [formatter.SearchResult("My work", "X", "https://x.com/artist/status/1")],
            8,
        )
        content = '```\n{"candidate_id":"c1","confidence":"medium","attribution":"likely_original"}\n```'
        result = judge.validate_selection(content, candidates)
        self.assertEqual(result.name, "artist（账号名）")

    def test_display_names_different_from_handles_work_across_platforms(self):
        for url, platform in (
            ("https://www.threads.com/@sample/post/ABC", "Threads"),
            ("https://www.tiktok.com/@sample/video/123", "TikTok"),
            ("https://www.facebook.com/sample/posts/123", "Facebook"),
            ("https://weibo.com/123456/Abcd", "微博"),
            ("https://space.bilibili.com/123456", "哔哩哔哩"),
            ("https://www.tumblr.com/sample/123/art", "Tumblr"),
            ("https://vk.com/sample", "VK"),
            ("https://www.flickr.com/photos/sample/123", "Flickr"),
        ):
            with self.subTest(platform=platform):
                candidates = judge.build_candidates(
                    [], [formatter.SearchResult("画师甲 | 新作发布", platform, url)], 8
                )
                data = dict(
                    candidate_id="c1",
                    attribution="likely_original",
                    confidence="medium",
                    name="画师甲",
                    name_field="title",
                    name_evidence="画师甲 | 新作发布",
                )
                result = judge.validate_selection(json.dumps(data), candidates)
                self.assertEqual(result.name, "画师甲")
                self.assertEqual(result.platform, platform)
                self.assertTrue(result.account)
                data["name"] = "另一个作者"
                self.assertIsNone(
                    judge.validate_selection(json.dumps(data), candidates)
                )

    def test_other_platform_credit_cannot_be_paired_with_reposter_account(self):
        row = formatter.SearchResult(
            "Repost, credit to Alice (@alice)",
            "Threads",
            "https://www.threads.com/@reposter/post/123",
        )
        candidates = judge.build_candidates([], [row], 8)
        data = dict(
            candidate_id="c1",
            attribution="likely_original",
            confidence="medium",
            name="Alice",
            name_field="title",
            name_evidence=row.title,
        )
        self.assertIsNone(judge.validate_selection(json.dumps(data), candidates))
