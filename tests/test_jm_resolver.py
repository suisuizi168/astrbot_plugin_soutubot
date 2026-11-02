from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jm_resolver import (  # noqa: E402
    JmResolver,
    JmResolverSettings,
    format_jm_resolution,
    normalize_jm_title,
    score_jm_title,
)


class Page:
    def __init__(self, entries):
        self.entries = entries

    def iter_id_title(self):
        return iter(self.entries)


class Client:
    def __init__(self, entries=(), error=None, delay=0):
        self.entries = entries
        self.error = error
        self.delay = delay
        self.query = None

    def search_site(self, query, *, page):
        self.query = query
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        self.page = page
        return Page(self.entries)

    def search_author(self, author, *, page):
        self.author = author
        self.author_page = page
        return Page(self.entries)


class JmResolverTests(unittest.IsolatedAsyncioTestCase):
    def test_normalization_removes_only_recognized_edge_metadata(self):
        self.assertEqual(normalize_jm_title("［中文］ＡＢＣ\u200b - Test (DL版)"), "abctest")
        self.assertEqual(normalize_jm_title("[系列名] 作品 (作者)"), "系列名作品作者")

    def test_score_contract(self):
        self.assertEqual(score_jm_title("ＡＢＣＤ", "abcd"), 100.0)
        self.assertEqual(score_jm_title("long title name", "title name"), 95.0)
        self.assertLess(score_jm_title("unrelated title", "完全不同的作品"), 55.0)
        self.assertGreaterEqual(score_jm_title("a", "b"), 0.0)
        self.assertLessEqual(score_jm_title("a", "b"), 100.0)

    async def test_selects_exact_match(self):
        client = Client([("12", "Other"), ("345", "Target Title")])
        resolver = JmResolver(
            JmResolverSettings(), client_factory=lambda settings: client
        )
        result = await resolver.resolve("Target Title")
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.selected.album_id, "345")
        self.assertEqual(client.page, 1)

    async def test_duplicate_sorting_and_candidate_limit(self):
        client = Client(
            [
                ("2", "Target title extra"),
                ("2", "Target title extra"),
                ("1", "Target title different"),
                ("3", "Target title another"),
                ("bad", "Target title"),
            ]
        )
        settings = JmResolverSettings(auto_select_threshold=100, auto_select_margin=30)
        result = await JmResolver(settings, client_factory=lambda settings: client).resolve("Target title")
        self.assertEqual(result.status, "candidates")
        self.assertEqual([item.album_id for item in result.candidates], ["1", "2", "3"])
        self.assertIn("请核对", format_jm_resolution(result))

    async def test_margin_and_threshold_boundaries(self):
        client = Client([("1", "Target title"), ("2", "Target title x")])
        result = await JmResolver(
            JmResolverSettings(auto_select_margin=8), client_factory=lambda settings: client
        ).resolve("Target title")
        self.assertEqual(result.status, "candidates")

        result = await JmResolver(
            JmResolverSettings(min_candidate_threshold=55),
            client_factory=lambda settings: Client([("3", "not related at all")]),
        ).resolve("Target title")
        self.assertEqual(result.status, "not_found")

    async def test_timeout_exception_and_short_title_are_contained(self):
        timeout_client = Client([("1", "Target")], delay=0.05)
        result = await JmResolver(
            JmResolverSettings(timeout_seconds=0.001),
            client_factory=lambda settings: timeout_client,
        ).resolve("Target")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "timeout")

        result = await JmResolver(JmResolverSettings(), client_factory=lambda settings: Client()).resolve("x")
        self.assertEqual(result.status, "skipped")

        result = await JmResolver(
            JmResolverSettings(), client_factory=lambda settings: Client(error=RuntimeError())
        ).resolve("Target title")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "request_error")

    async def test_only_twenty_raw_entries_are_read(self):
        entries = [(str(index), f"Target title {index}") for index in range(25)]
        client = Client(entries)
        result = await JmResolver(
            JmResolverSettings(auto_select_threshold=100),
            client_factory=lambda settings: client,
        ).resolve("Target title")
        self.assertLessEqual(len(result.candidates), 3)

    async def test_author_search_keeps_official_titles_without_title_score_filter(self):
        client = Client([("12", "完全不同语种的作品名"), ("13", "Another title")])
        resolver = JmResolver(JmResolverSettings(), client_factory=lambda settings: client)
        result = await resolver.search_author("Author")
        self.assertEqual(result.status, "candidates")
        self.assertEqual(result.candidates[0].album_id, "12")
        self.assertEqual(result.candidates[0].score, 0.0)
        self.assertEqual(client.author, "Author")


if __name__ == "__main__":
    unittest.main()
