import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from trendradar.ai.analyzer import (
    AIAnalyzer,
    AIAnalysisResult,
    has_required_narrative,
)
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

    def test_weekly_narrative_fields_accept_strings_and_lists(self):
        analyzer = AIAnalyzer.__new__(AIAnalyzer)

        result = analyzer._parse_response(json.dumps({
            "policy_trends": "政策事实与来源标题",
            "research_trends": ["科研进展", "证据为摘要"],
            "weather_risks": "官方气象风险与建议",
        }, ensure_ascii=False))

        self.assertEqual(result.policy_trends, "政策事实与来源标题")
        self.assertEqual(result.research_trends, "科研进展\n证据为摘要")
        self.assertEqual(result.weather_risks, "官方气象风险与建议")

    def test_weekly_narrative_rejects_object_or_scalar_shape(self):
        analyzer = AIAnalyzer.__new__(AIAnalyzer)

        for field, invalid in (
            ("policy_trends", {"summary": "不接受对象"}),
            ("research_trends", 42),
            ("weather_risks", True),
        ):
            with self.subTest(field=field):
                result = analyzer._parse_response(json.dumps({field: invalid}))
                self.assertFalse(result.success)
                self.assertIn(field, result.error)

    def test_weekly_requires_all_three_narratives_but_current_is_compatible(self):
        ordinary = AIAnalysisResult(success=True, core_trends="普通摘要")
        self.assertTrue(has_required_narrative(ordinary, report_mode="current"))

        complete = AIAnalysisResult(
            success=True,
            policy_trends="政策事实可追溯",
            research_trends="科研事实可追溯",
            weather_risks="气象事实可追溯",
        )
        self.assertTrue(has_required_narrative(complete, report_mode="weekly"))
        complete.weather_risks = ""
        self.assertFalse(has_required_narrative(complete, report_mode="weekly"))

    @staticmethod
    def _weekly_analyzer(*responses):
        analyzer = AIAnalyzer.__new__(AIAnalyzer)
        analyzer.ai_config = {"MODEL": "test", "TIMEOUT": 1, "MAX_TOKENS": 100}
        analyzer.analysis_config = {}
        analyzer.get_time_func = lambda: datetime(2026, 8, 10, 10, 0)
        analyzer.debug = False
        analyzer.client = MagicMock(api_key="secret")
        analyzer.client.chat.side_effect = responses
        analyzer.max_news = 50
        analyzer.include_rss = True
        analyzer.include_rank_timeline = False
        analyzer.include_standalone = False
        analyzer.grounding_review_enabled = True
        analyzer.language = "Chinese"
        analyzer.system_prompt = "分别输出政策、科研与气象叙事"
        analyzer.user_prompt_template = (
            "{report_mode}\n{rss_content}\n官方气象证据：\n{weather_content}\n"
            "{report_type}{current_time}{news_count}{rss_count}{platforms}"
            "{keywords}{news_content}{language}{standalone_content}"
        )
        return analyzer

    def test_weekly_prompt_serializes_modules_ranks_evidence_and_weather(self):
        response = json.dumps({
            "policy_trends": "政策趋势仅据「政策原文」 [policy:1]",
            "research_trends": "科研趋势仅据「科研摘要」 [research:1]",
            "weather_risks": "气象风险仅据中央气象台周报 [weather:official]",
        }, ensure_ascii=False)
        analyzer = self._weekly_analyzer(response, response)
        analyzer.max_news = 1
        weather = SimpleNamespace(
            title="全国农业气象周报",
            report_date="2026-08-10",
            reviewed_start="2026-08-03",
            reviewed_end="2026-08-09",
            impact="高温影响水稻扬花",
            outlook="未来局地强降水",
            recommendations="及时排涝",
            risk_regions=("江南",),
            risk_crops=("水稻",),
            source_url="https://www.nmc.cn/publish/agro/ten-week/index.html",
        )

        result = analyzer.analyze(
            stats=[{"word": "政策热榜", "titles": [{
                "title": "热榜政策原文",
                "module_type": "policy",
                "module_rank": 2,
                "content_level": "summary",
                "content_excerpt": "热榜政策证据",
            }]}],
            rss_stats=[{
                "word": "周报",
                "titles": [
                    {
                        "title": "政策原文",
                        "source_name": "部委",
                        "module_type": "policy",
                        "module_rank": 1,
                        "content_level": "full_text",
                        "content_excerpt": "支持育种创新平台建设",
                    },
                    {
                        "title": "科研摘要",
                        "source_name": "期刊",
                        "module_type": "research",
                        "module_rank": 1,
                        "content_level": "summary",
                        "content_excerpt": "高温胁迫表型分析",
                    },
                ],
            }],
            report_mode="weekly",
            report_type="自然周周报",
            strict=True,
            weather_report=weather,
        )

        self.assertTrue(result.success, result.error)
        first_prompt = analyzer.client.chat.call_args_list[0].args[0][-1]["content"]
        self.assertIn("模块：policy", first_prompt)
        self.assertIn("模块内排名：1", first_prompt)
        self.assertIn("证据内容：支持育种创新平台建设", first_prompt)
        self.assertIn("模块：research", first_prompt)
        self.assertIn("热榜政策证据", first_prompt)
        self.assertIn("证据ID：[policy:1]", first_prompt)
        self.assertIn("证据ID：[research:1]", first_prompt)
        self.assertIn("全国农业气象周报", first_prompt)
        self.assertIn("高温影响水稻扬花", first_prompt)
        review_prompt = analyzer.client.chat.call_args_list[1].args[0][-1]["content"]
        self.assertIn("官方气象证据", review_prompt)
        self.assertIn("高温影响水稻扬花", review_prompt)

    def test_weekly_rejects_news_without_module_before_model_call(self):
        analyzer = self._weekly_analyzer()

        result = analyzer.analyze(
            stats=[],
            rss_stats=[{"word": "周报", "titles": [{"title": "缺少模块"}]}],
            report_mode="weekly",
            strict=True,
            weather_report=SimpleNamespace(title="气象周报", impact="高温"),
        )

        self.assertFalse(result.success)
        self.assertIn("module_type", result.error)
        analyzer.client.chat.assert_not_called()

    def test_weekly_grounding_that_drops_a_required_section_fails(self):
        draft = json.dumps({
            "policy_trends": "有证据的政策事实 [policy:1]",
            "research_trends": "有证据的科研事实 [research:1]",
            "weather_risks": "有证据的气象事实 [weather:official]",
        }, ensure_ascii=False)
        reviewed = json.dumps({
            "policy_trends": "",
            "research_trends": "有证据的科研事实 [research:1]",
            "weather_risks": "有证据的气象事实 [weather:official]",
        }, ensure_ascii=False)
        analyzer = self._weekly_analyzer(draft, reviewed)

        result = analyzer.analyze(
            stats=[],
            rss_stats=[{"word": "周报", "titles": [
                {
                    "title": "政策事实", "module_type": "policy",
                    "module_rank": 1, "content_level": "summary",
                    "content_excerpt": "有证据的政策事实",
                },
                {
                    "title": "科研事实", "module_type": "research",
                    "module_rank": 1, "content_level": "summary",
                    "content_excerpt": "有证据的科研事实",
                },
            ]}],
            report_mode="weekly",
            strict=True,
            weather_report=SimpleNamespace(
                title="气象周报", impact="有证据的气象事实",
                outlook="气象展望", recommendations="官方建议",
                source_url="https://www.nmc.cn/publish/agro/ten-week/index.html",
            ),
        )

        self.assertFalse(result.success)
        self.assertIn("必要", result.error)

    def test_weekly_rejects_unknown_or_cross_module_grounding_citations(self):
        draft = json.dumps({
            "policy_trends": "政策事实 [policy:1]",
            "research_trends": "科研事实 [research:1]",
            "weather_risks": "气象事实 [weather:official]",
        }, ensure_ascii=False)
        invalid_reviews = (
            {"policy_trends": "虚构政策 [policy:99]"},
            {"policy_trends": "跨模块事实 [research:1]"},
        )
        for override in invalid_reviews:
            with self.subTest(override=override):
                reviewed = {
                    "policy_trends": "政策事实 [policy:1]",
                    "research_trends": "科研事实 [research:1]",
                    "weather_risks": "气象事实 [weather:official]",
                    **override,
                }
                analyzer = self._weekly_analyzer(
                    draft, json.dumps(reviewed, ensure_ascii=False)
                )
                result = analyzer.analyze(
                    stats=[],
                    rss_stats=[{"word": "周报", "titles": [
                        {"title": "政策事实", "module_type": "policy", "module_rank": 1},
                        {"title": "科研事实", "module_type": "research", "module_rank": 1},
                    ]}],
                    report_mode="weekly",
                    strict=True,
                    weather_report=SimpleNamespace(
                        title="气象周报", impact="气象事实",
                        outlook="气象展望", recommendations="官方建议",
                        source_url="https://www.nmc.cn/publish/agro/ten-week/index.html",
                    ),
                )
                self.assertFalse(result.success)
                self.assertIn("证据引用", result.error)

    def test_weekly_rejects_incomplete_weather_before_model_call(self):
        analyzer = self._weekly_analyzer()
        result = analyzer.analyze(
            stats=[], rss_stats=[], report_mode="weekly", strict=True,
            weather_report=SimpleNamespace(title="气象周报", impact="高温"),
        )

        self.assertFalse(result.success)
        self.assertIn("气象证据缺少", result.error)
        analyzer.client.chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
