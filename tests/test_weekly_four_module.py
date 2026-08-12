import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from trendradar.ai.analyzer import AIAnalysisResult, AIAnalyzer, has_required_narrative
from trendradar.ai.filter import AIFilter
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.core.weekly import WeeklyRSSAggregator, select_weekly_modules
from trendradar.report.weekly_pdf import render_weekly_pdf_html
from trendradar.storage.base import RSSData, RSSItem


ROOT = Path(__file__).resolve().parents[1]


def _item(module_type, species_scope, index, **overrides):
    item = {
        "module_type": module_type,
        "species_scope": species_scope,
        "title": f"{module_type}-{species_scope}-{index}",
        "url": f"https://example.org/{module_type}/{species_scope}/{index}",
        "source_name": "Source",
        "relevance_score": 0.8,
        "importance_score": 1 - index / 100,
        "content_level": "full_text",
        "published_at": "2026-08-09T08:00:00+08:00",
    }
    item.update(overrides)
    return item


class WeeklyFourModuleContractTests(unittest.TestCase):
    def test_strict_batch_retries_one_transient_classification_failure(self):
        pipeline = AIFilterPipeline.__new__(AIFilterPipeline)
        pipeline._strict = True
        pipeline._enrich_pending_items = lambda items, _label: items
        ai_filter = MagicMock()
        ai_filter.classify_batch.side_effect = [None, []]
        pending = [{
            "id": 1,
            "title": "水稻产业动态",
            "source_name": "Official",
            "url": "https://example.org/rice",
            "content": "水稻产业动态正文",
            "content_level": "full_text",
            "risk_warning": "",
        }]

        results, news_ids, rss_ids = pipeline._classify_batches(
            ai_filter,
            [],
            pending,
            [{"id": 1, "tag": "水稻产业"}],
            "水稻兴趣",
            {"BATCH_SIZE": 10, "BATCH_INTERVAL": 0},
        )

        self.assertEqual(results, [])
        self.assertEqual(news_ids, [])
        self.assertEqual(rss_ids, [1])
        self.assertEqual(ai_filter.classify_batch.call_count, 2)

    def test_strict_classification_preserves_industry_and_species_scope(self):
        ai_filter = AIFilter.__new__(AIFilter)
        ai_filter.debug = False
        titles = [
            {"id": 1, "title": "水稻最低收购价"},
            {"id": 2, "title": "水稻订单生产启动"},
            {"id": 3, "title": "小麦基因组研究"},
            {"id": 4, "title": "无关内容"},
        ]
        tags = [
            {"id": 11, "tag": "政策"},
            {"id": 12, "tag": "产业"},
            {"id": 13, "tag": "科研"},
        ]
        response = json.dumps({"items": [
            {"id": 1, "module_type": "policy", "species_scope": "rice",
             "tag_id": 11, "score": 0.8, "importance_score": 0.9,
             "summary": "稻谷政策"},
            {"id": 2, "module_type": "industry", "species_scope": "rice",
             "tag_id": 12, "score": 0.7, "importance_score": 0.8,
             "summary": "水稻产业动态"},
            {"id": 3, "module_type": "research", "species_scope": "other_crop",
             "tag_id": 13, "score": 0.6, "importance_score": 0.7,
             "summary": "其他作物科研"},
            {"id": 4, "module_type": "exclude", "species_scope": "not_applicable",
             "score": 0.1, "importance_score": 0.1, "summary": "无关"},
        ]}, ensure_ascii=False)

        results = ai_filter._parse_classify_response(
            response, titles, tags, strict=True
        )

        self.assertEqual(
            [(row["module_type"], row["species_scope"]) for row in results],
            [
                ("policy", "rice"),
                ("industry", "rice"),
                ("research", "other_crop"),
            ],
        )

    def test_selection_is_rice_only_for_policy_and_industry_and_rice_first_for_research(self):
        items = [
            _item("policy", "other_crop", 1, importance_score=1.0),
            _item("policy", "rice", 2),
            _item("industry", "other_crop", 3, importance_score=1.0),
            _item("industry", "rice", 4),
            _item("research", "other_crop", 5, importance_score=1.0),
            _item("research", "rice", 6, importance_score=0.4),
        ]

        selection = select_weekly_modules(
            items, min_score=0.5, limit_per_module=2
        )

        self.assertEqual([row["species_scope"] for row in selection.policy], ["rice"])
        self.assertEqual([row["species_scope"] for row in selection.industry], ["rice"])
        self.assertEqual(
            [row["species_scope"] for row in selection.research],
            ["rice", "other_crop"],
        )

    def test_classification_prompt_defines_four_modules_and_species_scope(self):
        prompt = (ROOT / "config/ai_filter/prompt.txt").read_text(encoding="utf-8")
        for token in (
            "policy", "industry", "research", "exclude",
            "rice", "other_crop", "not_applicable", "species_scope",
        ):
            self.assertIn(token, prompt)

    def test_weekly_narrative_requires_industry_section(self):
        result = AIAnalysisResult(
            success=True,
            policy_trends="政策判断 [policy:1]",
            research_trends="科研判断 [research:1]",
            weather_risks="气象判断 [weather:official]",
        )
        self.assertFalse(has_required_narrative(result, report_mode="weekly"))
        result.industry_trends = "产业判断 [industry:1]"
        self.assertTrue(has_required_narrative(result, report_mode="weekly"))

    def test_empty_news_module_gets_deterministic_narrative_only_for_none_evidence(self):
        result = AIAnalysisResult(
            success=True,
            industry_trends="产业判断 [industry:1]",
            research_trends="科研判断 [research:1]",
            weather_risks="气象判断 [weather:official]",
        )
        AIAnalyzer._fill_empty_weekly_module_narratives(
            result,
            {
                "policy": {"[policy:none]"},
                "industry": {"[industry:1]"},
                "research": {"[research:1]"},
                "weather": {"[weather:official]"},
            },
        )
        self.assertEqual(
            result.policy_trends,
            "本期无入选农业育种政策新闻 [policy:none]",
        )
        self.assertEqual(result.industry_trends, "产业判断 [industry:1]")

    def test_pdf_renders_three_news_modules_and_weather_once(self):
        policy = _item("policy", "rice", 1, module_rank=1,
                       ai_summary="政策正文总结")
        industry = _item("industry", "rice", 2, module_rank=1,
                         ai_summary="产业正文总结")
        research = _item("research", "rice", 3, module_rank=1,
                         ai_summary="科研正文总结")
        analysis = AIAnalysisResult(
            success=True,
            policy_trends="政策判断 [policy:1]",
            industry_trends="产业判断 [industry:1]",
            research_trends="科研判断 [research:1]",
            weather_risks="气象判断 [weather:official]",
        )
        weather = SimpleNamespace(
            title="全国农业气象周报", impact="影响", outlook="展望",
            recommendations="建议", source_url="https://example.org/weather",
            risk_regions=("长江中下游",), risk_crops=("水稻",),
        )

        html = render_weekly_pdf_html(
            policy_items=[policy], industry_items=[industry],
            research_items=[research], ai_analysis=analysis,
            agro_weather=weather, period_label="2026-08-03—2026-08-09",
            generated_at=__import__("datetime").datetime(2026, 8, 10, 10),
            missing_dates=["2026-08-04"],
            failed_sources={"2026-08-05": ["blocked-source"]},
        )

        for heading in ("一、政策动态", "二、水稻产业时事动态", "三、科研进展", "四、农业气象与灾害风险"):
            self.assertIn(heading, html)
        for evidence in ("[policy:1]", "[industry:1]", "[research:1]", "[weather:official]"):
            self.assertIn(evidence, html)
        for item in (policy, industry, research):
            self.assertEqual(html.count(item["url"]), 1)
        self.assertIn("摘要依据：基于正文", html)
        self.assertIn("来源采集状态", html)
        self.assertIn("blocked-source", html)

    def test_weekly_snapshot_keeps_partial_data_and_records_source_failures(self):
        from unittest.mock import MagicMock
        import pytz

        storage = MagicMock()
        available = RSSData(
            date="2026-08-05", crawl_time="2026-08-05 10:00:00",
            items={"journal": [RSSItem(
                title="Rice paper", feed_id="journal", feed_name="Journal",
                url="https://example.org/rice-paper",
                published_at="2026-08-05T08:00:00+08:00",
            )]}, id_to_name={"journal": "Journal"},
            failed_ids=["blocked-source"],
        )
        empty = RSSData(
            date="", crawl_time="10:00", items={},
            id_to_name={"journal": "Journal"}, failed_ids=[],
        )
        storage.get_rss_data_strict.side_effect = lambda day: (
            available if day == "2026-08-05" else
            RSSData(**{**empty.__dict__, "date": day})
        )
        storage.save_rss_data.return_value = True
        storage.get_all_rss_ids_strict.return_value = [{
            "id": 9, "source_id": "journal", "source_name": "Journal",
            "title": "Rice paper", "url": "https://example.org/rice-paper",
        }]

        snapshot = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
            pytz.timezone("Asia/Shanghai").localize(
                __import__("datetime").datetime(2026, 8, 10, 10)
            )
        )

        self.assertEqual(snapshot.failed_sources, {
            "2026-08-05": ["blocked-source"]
        })
        self.assertEqual([item.title for item in snapshot.iter_items()], ["Rice paper"])

    def test_weekly_snapshot_rejects_when_every_source_is_unavailable(self):
        from unittest.mock import MagicMock
        import pytz

        storage = MagicMock()
        storage.get_rss_data_strict.return_value = None
        with self.assertRaisesRegex(RuntimeError, "没有任何可用来源"):
            WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
                pytz.timezone("Asia/Shanghai").localize(
                    __import__("datetime").datetime(2026, 8, 10, 10)
                )
            )


if __name__ == "__main__":
    unittest.main()
