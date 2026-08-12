import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from trendradar.ai.analyzer import AIAnalyzer, AIAnalysisResult
from trendradar.ai.client import AIClient
from trendradar.ai.filter import AIFilter, CLASSIFY_JSON_REPAIR_PROMPT


ROOT = Path(__file__).resolve().parents[1]
JSON_OBJECT = {"type": "json_object"}


def classification_items(*, valid=True, tag_id=11):
    first = {
        "id": 1,
        "module_type": "policy",
        "tag_id": tag_id,
        "score": 0.8,
        "importance_score": 0.7,
        "summary": "政策部署",
    }
    if not valid:
        first.pop("tag_id")
    return {
        "items": [
            first,
            {
                "id": 2,
                "module_type": "exclude",
                "score": 0.1,
                "importance_score": 0.1,
                "summary": "内容无关",
            },
        ]
    }


class DeepSeekJsonObjectContractTests(unittest.TestCase):
    def _filter(self):
        ai_filter = AIFilter.__new__(AIFilter)
        ai_filter.debug = False
        ai_filter.classify_system = "只返回 JSON 对象"
        ai_filter.classify_user = (
            "{interests_content}\n{tags_list}\n{news_count}\n{news_list}"
        )
        ai_filter.summary_grounding_review_enabled = False
        ai_filter.client = MagicMock()
        return ai_filter

    def test_strict_classification_reads_items_from_json_object(self):
        ai_filter = self._filter()
        results = ai_filter._parse_classify_response(
            json.dumps(classification_items()),
            [
                {"id": 1, "title": "政策", "content_level": "summary"},
                {"id": 2, "title": "无关", "content_level": "summary"},
            ],
            [{"id": 11, "tag": "政策"}],
            strict=True,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["module_type"], "policy")

    def test_classification_and_repair_both_request_json_object(self):
        ai_filter = self._filter()
        ai_filter.client.chat.side_effect = [
            json.dumps(classification_items(valid=False, tag_id=1)),
            json.dumps(classification_items(tag_id=1)),
        ]

        results = ai_filter.classify_batch(
            [
                {"id": 1, "title": "政策", "content": "政策"},
                {"id": 2, "title": "无关", "content": "无关"},
            ],
            [{"id": 11, "tag": "政策"}],
            "政策优先",
            strict=True,
        )

        self.assertEqual([row["news_item_id"] for row in results], [1])
        self.assertEqual(ai_filter.client.chat.call_count, 2)
        for call in ai_filter.client.chat.call_args_list:
            self.assertEqual(call.kwargs["response_format"], JSON_OBJECT)

    def test_batch_uses_local_ids_and_maps_results_back_to_storage_ids(self):
        ai_filter = self._filter()
        response = json.dumps(classification_items(tag_id=1))
        ai_filter.client.chat.side_effect = [response, response]

        results = ai_filter.classify_batch(
            [
                {"id": 81, "title": "政策", "content": "政策"},
                {"id": 97, "title": "无关", "content": "无关"},
            ],
            [{"id": 11, "tag": "政策"}],
            "政策优先",
            strict=True,
        )

        self.assertEqual([row["news_item_id"] for row in results], [81])
        prompt = ai_filter.client.chat.call_args_list[0].args[0][-1]["content"]
        self.assertIn("### 新闻 1", prompt)
        self.assertIn("### 新闻 2", prompt)
        self.assertNotIn("### 新闻 81", prompt)
        self.assertNotIn("### 新闻 97", prompt)

    def test_batch_uses_local_tag_ids_and_maps_back_to_storage_ids(self):
        ai_filter = self._filter()
        ai_filter.client.chat.return_value = json.dumps({
            "items": [
                {
                    "id": 1,
                    "module_type": "policy",
                    "tag_id": 1,
                    "score": 0.8,
                    "importance_score": 0.7,
                    "summary": "政策部署",
                },
                {
                    "id": 2,
                    "module_type": "exclude",
                    "score": 0.1,
                    "importance_score": 0.1,
                    "summary": "内容无关",
                },
            ]
        })

        results = ai_filter.classify_batch(
            [
                {"id": 81, "title": "政策", "content": "政策"},
                {"id": 97, "title": "无关", "content": "无关"},
            ],
            [{"id": 41, "tag": "政策"}, {"id": 55, "tag": "科研"}],
            "政策优先",
            strict=True,
        )

        self.assertEqual(results[0]["tag_id"], 41)
        prompt = ai_filter.client.chat.call_args.args[0][-1]["content"]
        self.assertIn("1. 政策", prompt)
        self.assertIn("2. 科研", prompt)
        self.assertNotIn("41. 政策", prompt)
        self.assertNotIn("55. 科研", prompt)

    def test_filter_prompts_define_items_object_not_top_level_array(self):
        prompt = (ROOT / "config/ai_filter/prompt.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn('"items": [', prompt)
        self.assertIn("JSON 对象", CLASSIFY_JSON_REPAIR_PROMPT)
        self.assertIn('"items"', CLASSIFY_JSON_REPAIR_PROMPT)

    def test_tag_extraction_requests_json_object(self):
        ai_filter = self._filter()
        ai_filter.extract_system = "返回 JSON"
        ai_filter.extract_user = "{interests_content}"
        ai_filter.client.chat.return_value = '{"tags": []}'

        self.assertEqual(ai_filter.extract_tags("水稻"), [])
        self.assertEqual(
            ai_filter.client.chat.call_args.kwargs["response_format"],
            JSON_OBJECT,
        )

    def test_analysis_and_json_repair_request_json_object(self):
        analyzer = AIAnalyzer.__new__(AIAnalyzer)
        analyzer.system_prompt = "返回 JSON"
        analyzer.client = MagicMock()
        analyzer.client.chat.return_value = "{}"

        analyzer._call_ai("分析")
        self.assertEqual(
            analyzer.client.chat.call_args.kwargs["response_format"],
            JSON_OBJECT,
        )

        analyzer._parse_response = MagicMock(
            return_value=AIAnalysisResult(success=True)
        )
        analyzer._retry_fix_json("{", "invalid")
        self.assertEqual(
            analyzer.client.chat.call_args.kwargs["response_format"],
            JSON_OBJECT,
        )

    def test_json_object_empty_content_retries_with_explicit_reminder(self):
        client = AIClient({
            "MODEL": "openai/deepseek-v4-flash",
            "API_KEY": "test",
            "API_BASE": "https://api.deepseek.com",
            "MAX_TOKENS": 10000,
            "NUM_RETRIES": 0,
        })
        empty = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=""))]
        )
        valid = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"ok": true}')
            )]
        )

        with patch("trendradar.ai.client.completion", side_effect=[empty, valid]) as call:
            response = client.chat(
                [{"role": "user", "content": "返回 JSON"}],
                response_format=JSON_OBJECT,
            )

        self.assertEqual(response, '{"ok": true}')
        self.assertEqual(call.call_count, 2)
        retry_messages = call.call_args_list[1].kwargs["messages"]
        self.assertIn("非空 JSON 对象", retry_messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
