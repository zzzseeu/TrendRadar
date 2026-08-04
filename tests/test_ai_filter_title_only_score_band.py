import json
import unittest

from trendradar.ai.filter import AIFilter


class TitleOnlyScoreBandTests(unittest.TestCase):
    def setUp(self):
        self.ai_filter = AIFilter.__new__(AIFilter)
        self.ai_filter.debug = False
        self.tags = [{"id": 1, "tag": "水稻育种"}]

    def parse(self, response, content_level="title_only"):
        return self.ai_filter._parse_classify_response(
            json.dumps(response),
            [{"id": 1, "title": "测试新闻", "content_level": content_level}],
            self.tags,
        )

    def test_title_only_valid_tag_low_score_is_raised_to_minimum(self):
        results = self.parse([{"id": 1, "tag_id": 1, "score": 0.58}])

        self.assertEqual(results[0]["relevance_score"], 0.70)

    def test_title_only_valid_tag_high_score_is_lowered_to_maximum(self):
        results = self.parse([{"id": 1, "tag_id": 1, "score": 0.95}])

        self.assertEqual(results[0]["relevance_score"], 0.78)

    def test_summary_score_is_not_normalized(self):
        results = self.parse(
            [{"id": 1, "tag_id": 1, "score": 0.58}], content_level="summary"
        )

        self.assertEqual(results[0]["relevance_score"], 0.58)

    def test_absent_ai_response_does_not_create_match(self):
        self.assertEqual(self.parse([]), [])


if __name__ == "__main__":
    unittest.main()
