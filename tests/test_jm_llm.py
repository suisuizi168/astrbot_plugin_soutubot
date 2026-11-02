from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jm_llm import (  # noqa: E402
    build_llm_query_prompt,
    is_cjk_title_query,
    parse_llm_candidate_matches,
    parse_llm_queries,
    parse_llm_search_plan,
)


class JmLlmTests(unittest.TestCase):
    def test_prompt_keeps_titles_as_data(self) -> None:
        prompt = build_llm_query_prompt(["First", "Second"])
        self.assertIn("1. First", prompt)
        self.assertIn("2. Second", prompt)

    def test_parser_accepts_bounded_json(self) -> None:
        self.assertEqual(
            parse_llm_queries(
                '```json\n{"queries":["Romanized title", "中文标题", "third"]}\n```'
            ),
            ("Romanized title", "中文标题"),
        )

    def test_parser_rejects_non_json_and_invalid_values(self) -> None:
        self.assertEqual(parse_llm_queries("queries: title"), ())
        self.assertEqual(parse_llm_queries('{"queries":["abc", 1, "ＡＢＣＤ"]}'), ("ＡＢＣＤ",))

    def test_plan_keeps_authors_and_marks_english_for_exclusion(self) -> None:
        plan = parse_llm_search_plan(
            '{"queries":["點了個應召女郎來的居然是前女友的媽媽", "English title"], "authors":["ほたてちゃんぽん"]}'
        )
        self.assertEqual(plan.authors, ("ほたてちゃんぽん",))
        self.assertTrue(is_cjk_title_query(plan.queries[0]))
        self.assertFalse(is_cjk_title_query(plan.queries[1]))

    def test_verifier_accepts_only_returned_album_ids(self) -> None:
        self.assertEqual(
            parse_llm_candidate_matches(
                '{"matches":[{"album_id":"12","confidence":91},{"album_id":"999","confidence":100}]}',
                allowed_album_ids=("12", "34"),
            ),
            (("12", 91.0),),
        )


if __name__ == "__main__":
    unittest.main()
