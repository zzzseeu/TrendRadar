import json
import unittest
from unittest.mock import Mock, patch

from trendradar.ai.filter import AIFilter


class TagJsonResilienceTests(unittest.TestCase):
    def setUp(self):
        self.ai_filter = AIFilter.__new__(AIFilter)
        self.ai_filter.debug = False
        self.ai_filter.extract_system = "只返回 JSON"
        self.ai_filter.extract_user = "兴趣：{interests_content}"
        self.ai_filter.client = Mock()

    def test_valid_json_keeps_existing_behavior(self):
        response = json.dumps(
            {
                "tags": [
                    {"tag": "水稻育种", "description": "关注水稻遗传改良"}
                ]
            },
            ensure_ascii=False,
        )

        self.assertEqual(
            self.ai_filter._parse_tags_response(response),
            [{"tag": "水稻育种", "description": "关注水稻遗传改良"}],
        )

    def test_unescaped_control_characters_are_recovered(self):
        response = (
            '{"tags":[{"tag":"基因组育种",'
            '"description":"第一行' + "\n" + '第二行' + "\t" + '关键词"}]}'
        )

        self.assertEqual(
            self.ai_filter._parse_tags_response(response),
            [{"tag": "基因组育种", "description": "第一行\n第二行\t关键词"}],
        )

    def test_extract_tags_uses_low_temperature_and_retries_invalid_json_once(self):
        invalid_response = '{"tags":[}'
        valid_response = (
            '{"tags":[{"tag":"水稻育种","description":"有效"}]}'
        )
        self.ai_filter.client.chat.side_effect = [invalid_response, valid_response]

        tags = self.ai_filter.extract_tags("关注水稻育种")

        self.assertEqual(tags, [{"tag": "水稻育种", "description": "有效"}])
        self.assertEqual(self.ai_filter.client.chat.call_count, 2)
        for call in self.ai_filter.client.chat.call_args_list:
            self.assertEqual(call.kwargs["temperature"], 0)
        retry_messages = self.ai_filter.client.chat.call_args_list[1].args[0]
        self.assertEqual(retry_messages[-2], {"role": "assistant", "content": invalid_response})
        self.assertIn("仅返回一个 JSON 对象", retry_messages[-1]["content"])

    def test_extract_tags_stops_after_one_failed_retry(self):
        self.ai_filter.client.chat.side_effect = ['{"tags":[}', '{"tags":[}']

        self.assertEqual(self.ai_filter.extract_tags("关注水稻育种"), [])
        self.assertEqual(self.ai_filter.client.chat.call_count, 2)

    def test_extract_tags_retries_an_empty_response(self):
        self.ai_filter.client.chat.side_effect = [
            "",
            '{"tags":[{"tag":"水稻育种","description":"有效"}]}',
        ]

        tags = self.ai_filter.extract_tags("关注水稻育种")

        self.assertEqual(tags, [{"tag": "水稻育种", "description": "有效"}])
        self.assertEqual(self.ai_filter.client.chat.call_count, 2)

    def test_debug_mode_does_not_reparse_control_characters_strictly(self):
        response = (
            '{"tags":[{"tag":"基因组育种",'
            '"description":"第一行' + "\n" + '第二行"}]}'
        )
        self.ai_filter.debug = True
        self.ai_filter.client.chat.return_value = response

        with patch("builtins.print"):
            tags = self.ai_filter.extract_tags("关注基因组育种")

        self.assertEqual(
            tags,
            [{"tag": "基因组育种", "description": "第一行\n第二行"}],
        )
        self.assertEqual(self.ai_filter.client.chat.call_count, 1)


if __name__ == "__main__":
    unittest.main()
