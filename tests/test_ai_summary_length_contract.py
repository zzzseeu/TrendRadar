import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from trendradar.ai.filter import AIFilter, _InvalidClassificationResponse


ROOT = Path(__file__).resolve().parents[1]


def strict_payload(summary: str) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "id": 1,
                    "include": True,
                    "species_scope": "rice",
                    "tag_id": 11,
                    "score": 0.8,
                    "importance_score": 0.8,
                    "summary": summary,
                }
            ]
        },
        ensure_ascii=False,
    )


class AISummaryLengthContractTests(unittest.TestCase):
    def setUp(self):
        self.filter = AIFilter.__new__(AIFilter)
        self.filter.debug = False
        self.titles = [
            {
                "id": 1,
                "title": "水稻产业动态",
                "content": "完整正文证据。",
                "content_level": "full_text",
                "module_type": "current_events",
            }
        ]
        self.tags = [{"id": 11, "tag": "水稻产业时事动态"}]

    def parse(self, summary):
        return self.filter._parse_classify_response(
            strict_payload(summary), self.titles, self.tags, strict=True
        )

    def test_complete_summary_up_to_450_characters_is_preserved(self):
        summary = "甲" * 449 + "。"

        result = self.parse(summary)

        self.assertEqual(result[0]["ai_summary"], summary)

    def test_summary_over_450_characters_is_rejected_without_truncation(self):
        with self.assertRaises(_InvalidClassificationResponse):
            self.parse("甲" * 450 + "。")

    def test_incomplete_summary_is_rejected_without_truncation(self):
        with self.assertRaises(_InvalidClassificationResponse):
            self.parse("甲" * 300)

    def test_grounding_review_rejects_incomplete_summary(self):
        self.filter.client = Mock()
        self.filter.client.chat.return_value = json.dumps(
            {"items": [{"id": 1, "summary": "乙" * 450}]},
            ensure_ascii=False,
        )
        results = [{"news_item_id": 1, "ai_summary": "原始完整摘要。"}]

        self.assertFalse(self.filter._review_item_summaries(self.titles, results))
        self.assertEqual(results[0]["ai_summary"], "原始完整摘要。")

    def test_grounding_review_prompt_requests_complete_summary_within_450_chars(self):
        self.filter.client = Mock()
        self.filter.client.chat.return_value = json.dumps(
            {"items": [{"id": 1, "summary": "校审后的完整摘要。"}]},
            ensure_ascii=False,
        )
        results = [{"news_item_id": 1, "ai_summary": "原始完整摘要。"}]

        self.assertTrue(self.filter._review_item_summaries(self.titles, results))
        messages = self.filter.client.chat.call_args.args[0]
        self.assertIn("450", messages[0]["content"])
        self.assertIn("完整句子", messages[0]["content"])

    def test_prompt_requires_450_character_complete_sentences(self):
        prompt = (ROOT / "config/ai_filter/prompt.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn("180-450 字", prompt)
        self.assertIn("完整句子", prompt)
        self.assertNotIn("180-300 字", prompt)


if __name__ == "__main__":
    unittest.main()
