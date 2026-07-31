import json
import unittest

from trendradar.ai.analyzer import AIAnalyzer
from trendradar.ai.formatter import render_ai_analysis_markdown


class AIAnalyzerResponseTests(unittest.TestCase):
    def test_structured_narrative_fields_are_normalized_to_text(self):
        analyzer = AIAnalyzer.__new__(AIAnalyzer)
        response = json.dumps(
            {
                "core_trends": {"summary": "育种动态", "evidence": "来源可核验"},
                "sentiment_controversy": ["证据有限", "需持续观察"],
                "signals": None,
                "rss_insights": "专业来源洞察",
                "outlook_strategy": {"科研人员": ["核验原文", "跟踪试验"]},
            },
            ensure_ascii=False,
        )

        result = analyzer._parse_response(response)

        self.assertEqual(result.core_trends, "summary：育种动态\nevidence：来源可核验")
        self.assertEqual(result.sentiment_controversy, "证据有限\n需持续观察")
        self.assertEqual(result.signals, "")
        self.assertEqual(result.rss_insights, "专业来源洞察")
        self.assertEqual(result.outlook_strategy, "科研人员：核验原文\n跟踪试验")
        self.assertIn("育种动态", render_ai_analysis_markdown(result))


if __name__ == "__main__":
    unittest.main()
