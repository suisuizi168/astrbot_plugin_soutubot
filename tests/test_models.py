from __future__ import annotations

import unittest

from models import format_search_response, parse_search_response, safe_http_url


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
        self.assertIn("https://soutubot.moe/results/202609170001", text)

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
