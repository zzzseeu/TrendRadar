import unittest
from unittest.mock import Mock

from trendradar.ai.filter import AIFilter
from trendradar.ai.filter_pipeline import AIFilterPipeline


class ClassificationResponseResilienceTests(unittest.TestCase):
    def setUp(self):
        self.ai_filter = AIFilter.__new__(AIFilter)
        self.ai_filter.debug = False
        self.ai_filter.classify_system = "只返回 JSON"
        self.ai_filter.classify_user = "{interests_content}\n{tags_list}\n{news_count}\n{news_list}"
        self.ai_filter.summary_grounding_review_enabled = False
        self.ai_filter.client = Mock()
        self.titles = [{
            "id": 1,
            "title": "木豆端粒到端粒基因组完成",
            "content": "木豆端粒到端粒基因组完成",
            "content_level": "title_only",
        }]
        self.tags = [{"id": 18, "tag": "其他作物育种", "description": "其他作物育种进展"}]

    def test_valid_match_uses_zero_temperature(self):
        self.ai_filter.client.chat.return_value = (
            '[{"id":1,"tag_id":18,"score":0.82,'
            '"importance_score":0.78,"summary":"仅标题显示：木豆基因组完成"}]'
        )
        result = self.ai_filter.classify_batch(self.titles, self.tags, "育种")
        self.assertEqual(result[0]["news_item_id"], 1)
        self.assertEqual(self.ai_filter.client.chat.call_args.kwargs["temperature"], 0)

    def test_valid_empty_array_is_success_without_retry(self):
        self.ai_filter.client.chat.return_value = "[]"
        self.assertEqual(self.ai_filter.classify_batch(self.titles, self.tags, "育种"), [])
        self.assertEqual(self.ai_filter.client.chat.call_count, 1)

    def test_empty_response_retries_and_recovers(self):
        self.ai_filter.client.chat.side_effect = ["", "[]"]
        self.assertEqual(self.ai_filter.classify_batch(self.titles, self.tags, "育种"), [])
        self.assertEqual(self.ai_filter.client.chat.call_count, 2)

    def test_malformed_json_retries_and_recovers(self):
        self.ai_filter.client.chat.side_effect = ["[{", "[]"]
        self.assertEqual(self.ai_filter.classify_batch(self.titles, self.tags, "育种"), [])
        retry_messages = self.ai_filter.client.chat.call_args_list[1].args[0]
        self.assertEqual(retry_messages[-2], {"role": "assistant", "content": "[{"})
        self.assertIn("严格 JSON 数组", retry_messages[-1]["content"])

    def test_two_invalid_responses_return_failure(self):
        self.ai_filter.client.chat.side_effect = ["", "not-json"]
        self.assertIsNone(self.ai_filter.classify_batch(self.titles, self.tags, "育种"))
        self.assertEqual(self.ai_filter.client.chat.call_count, 2)


class ClassificationPipelineRetryTests(unittest.TestCase):
    def test_failed_rss_batch_does_not_return_succeeded_ids(self):
        pipeline = AIFilterPipeline.__new__(AIFilterPipeline)
        pipeline._content_config = {"ENABLED": False}
        pipeline._rss_use_proxy = False
        pipeline._rss_proxy_url = ""
        pipeline._enrich_pending_items = Mock(side_effect=lambda items, _label: items)
        ai_filter = Mock()
        ai_filter.classify_batch.return_value = None
        pending_rss = [{
            "id": 6,
            "title": "木豆基因组突破",
            "summary": "",
            "content": "木豆基因组突破",
            "content_level": "title_only",
        }]

        results, succeeded_news, succeeded_rss = pipeline._classify_batches(
            ai_filter,
            [],
            pending_rss,
            [{"id": 18, "tag": "其他作物育种"}],
            "育种",
            {"BATCH_SIZE": 10, "BATCH_INTERVAL": 0},
        )

        self.assertEqual(results, [])
        self.assertEqual(succeeded_news, [])
        self.assertEqual(succeeded_rss, [])


if __name__ == "__main__":
    unittest.main()
