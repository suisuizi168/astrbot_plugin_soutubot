from __future__ import annotations

import unittest

from models import (
    SearchItem,
    SearchResponse,
    format_search_response,
    parse_search_response,
    safe_http_url,
    select_search_items,
)


class CurrentResponseTests(unittest.TestCase):
    def test_current_response_is_parsed(self) -> None:
        payload = {
            "result_id": "202609170001",
            "image_url": "https://img.example/query.jpg",
            "timing": {"total_ms": 1250},
            "results": [
                {
                    "score": 92.5,
                    "path_segments": [
                        {
                            "source_key": "nhentai",
                            "external_id": "123",
                            "page_no": 7,
                            "metadata": {
                                "title": {"primary": "Example title"},
                                "source": {"key": "nhentai", "name": "NHentai"},
                            },
                            "links": {
                                "source_url": "https://nhentai.net/g/123/",
                                "page_url": "https://nhentai.net/g/123/7/",
                            },
                        }
                    ],
                }
            ],
        }
        response = parse_search_response(payload)
        self.assertEqual(response.result_id, "202609170001")
        self.assertEqual(response.elapsed_seconds, 1.25)
        self.assertEqual(response.items[0].source_id, "123")
        text = format_search_response(response, max_results=3)
        self.assertIn("92.50%", text)
        self.assertNotIn("soutubot.moe", text)
        self.assertIn("来源于搜图Bot酱", text)

    def test_only_results_at_or_above_eighty_are_shown(self) -> None:
        response = SearchResponse(
            result_id="secret-id",
            image_url=None,
            items=(
                SearchItem(80.0, "At threshold", "a", "A"),
                SearchItem(79.99, "Below threshold", "b", "B"),
            ),
        )
        selected, total = select_search_items(response)
        self.assertEqual(total, 1)
        self.assertEqual([item.title for item in selected], ["At threshold"])
        text = format_search_response(response)
        self.assertIn("At threshold", text)
        self.assertNotIn("Below threshold", text)
        self.assertNotIn("secret-id", text)

    def test_no_high_similarity_result_has_explicit_message(self) -> None:
        response = SearchResponse(
            result_id="secret-id",
            image_url=None,
            items=(SearchItem(60.0, "Low", "a", "A"),),
        )
        text = format_search_response(response)
        self.assertIn("没有找到相似度达到 80% 的结果", text)
        self.assertNotIn("Low", text)

    def test_custom_similarity_threshold_is_used(self) -> None:
        response = SearchResponse(
            result_id=None,
            image_url=None,
            items=(
                SearchItem(65.0, "Shown", "a", "A"),
                SearchItem(64.99, "Hidden", "b", "B"),
            ),
        )
        text = format_search_response(response, min_similarity=65)
        self.assertIn("Shown", text)
        self.assertNotIn("Hidden", text)
        self.assertIn("达到 65%", text)

    def test_result_urls_can_be_hidden_independently(self) -> None:
        response = SearchResponse(
            result_id=None,
            image_url=None,
            items=(
                SearchItem(
                    90.0,
                    "Example",
                    "nhentai",
                    "NHentai",
                    page_url="https://example.test/match",
                    source_url="https://example.test/detail",
                ),
            ),
        )
        text = format_search_response(
            response,
            show_match_page_url=False,
            show_detail_page_url=True,
        )
        self.assertNotIn("匹配页", text)
        self.assertIn("详情页：https://example.test/detail", text)

    def test_unsafe_urls_are_discarded(self) -> None:
        self.assertIsNone(safe_http_url("javascript:alert(1)"))
        self.assertIsNone(safe_http_url("file:///tmp/a.jpg"))


class LegacyResponseTests(unittest.TestCase):
    def test_legacy_response_is_parsed(self) -> None:
        payload = {
            "id": "old-id",
            "executionTime": 2.5,
            "data": [
                {
                    "source": "ehentai",
                    "title": "Legacy title",
                    "similarity": 77.7,
                    "page": 4,
                    "subjectPath": "/g/123/token/",
                    "pagePath": "/s/key/123-4",
                }
            ],
        }
        response = parse_search_response(payload)
        self.assertEqual(response.items[0].source_name, "E-Hentai")
        self.assertEqual(
            response.items[0].source_url,
            "https://e-hentai.org/g/123/token/",
        )


if __name__ == "__main__":
    unittest.main()
