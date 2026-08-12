import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytz

from trendradar.__main__ import NewsAnalyzer
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.core.weekly import select_weekly_modules
from trendradar.crawler.news_search import canonicalize_url, normalize_title


def _identity(item):
    canonical_url = canonicalize_url(str(item.get("url") or ""))
    if canonical_url:
        return ("url", canonical_url)
    guid = str(item.get("guid") or "").strip()
    if guid:
        return ("guid", guid)
    return ("title", normalize_title(str(item.get("title") or "")))


def _item(module_type, index, **overrides):
    item = {
        "module_type": module_type,
        "species_scope": "rice",
        "title": f"{module_type} item {index:02d}",
        "url": f"https://example.org/{module_type}/{index}",
        "source_name": "Source",
        "relevance_score": 0.8,
        "importance_score": 1 - index / 100,
        "content_level": "full_text",
        "published_at": f"2026-08-{(index % 9) + 1:02d}T08:00:00+08:00",
    }
    item.update(overrides)
    return item


class WeeklyThreeModuleSelectionTests(unittest.TestCase):
    @staticmethod
    def _weekly_pipeline(*, max_news=0):
        tz = pytz.timezone("Asia/Shanghai")
        start = tz.localize(datetime(2026, 8, 3))
        from trendradar.core.weekly import NaturalWeekWindow

        return AIFilterPipeline(
            {
                "TIMEZONE": "Asia/Shanghai",
                "MAX_NEWS_PER_KEYWORD": max_news,
                "RSS": {"ENABLED": True, "FEEDS": []},
                "AI_FILTER": {"MIN_SCORE": 0.5, "HIGHLIGHT_TOP_N": 5},
                "FILTER": {},
            },
            storage_manager=None,
            get_time_func=lambda: None,
            rss_window=NaturalWeekWindow(start, start + timedelta(days=7), "Asia/Shanghai"),
            rss_ids_authoritative=True,
        )

    def test_selects_independent_top_twenty_and_reserves_all_policy_identities(self):
        policy = [_item("policy", index) for index in range(25)]
        research = [_item("research", index) for index in range(25)]
        research.extend([
            _item(
                "research", 100, title="URL duplicate", importance_score=2,
                url="https://example.org/policy/0?utm_source=research",
            ),
            _item(
                "research", 101, title="GUID duplicate", importance_score=2,
                url="", guid="shared-guid",
            ),
            _item(
                "research", 102, title="  normalized   duplicate  ",
                importance_score=2, url="", guid="",
            ),
            # The 21st-ranked policy identity must remain unavailable to research.
            _item(
                "research", 103, title="Policy rank 21 duplicate",
                importance_score=2,
                url="https://example.org/policy/20?utm_medium=research",
            ),
        ])
        policy.extend([
            _item(
                "policy", 100, title="Policy GUID owner", url="",
                guid="shared-guid", importance_score=0.6,
            ),
            _item(
                "policy", 101, title="Normalized Duplicate", url="", guid="",
                importance_score=0.59,
            ),
        ])

        selection = select_weekly_modules(
            [*research, *policy], min_score=0.5
        )

        self.assertEqual(len(selection.policy), 20)
        self.assertEqual(len(selection.research), 20)
        self.assertEqual(
            [row["module_rank"] for row in selection.policy],
            list(range(1, 21)),
        )
        self.assertEqual(
            [row["highlight_rank"] for row in selection.policy[:5]],
            list(range(1, 6)),
        )
        self.assertTrue(all(
            "highlight_rank" not in row for row in selection.policy[5:]
        ))
        policy_identities = {_identity(row) for row in selection.policy}
        research_identities = {_identity(row) for row in selection.research}
        self.assertTrue(policy_identities.isdisjoint(research_identities))
        self.assertNotIn(
            ("url", "https://example.org/policy/20"), research_identities
        )
        self.assertNotIn(("guid", "shared-guid"), research_identities)
        self.assertNotIn(
            ("title", normalize_title("normalized duplicate")),
            research_identities,
        )

    def test_ranking_uses_value_evidence_date_and_stable_fields_in_order(self):
        comparisons = [
            (
                _item("policy", 1, importance_score=0.9, relevance_score=0.6),
                _item("policy", 2, importance_score=0.8, relevance_score=1.0),
            ),
            (
                _item("policy", 1, importance_score=0.8, relevance_score=0.9),
                _item("policy", 2, importance_score=0.8, relevance_score=0.8),
            ),
            (
                _item("policy", 1, importance_score=0.8, relevance_score=0.8,
                      content_level="full_text"),
                _item("policy", 2, importance_score=0.8, relevance_score=0.8,
                      content_level="summary"),
            ),
            (
                _item("policy", 1, importance_score=0.8, relevance_score=0.8,
                      content_level="summary"),
                _item("policy", 2, importance_score=0.8, relevance_score=0.8,
                      content_level="title_only"),
            ),
            (
                _item("policy", 1, importance_score=0.8, relevance_score=0.8,
                      content_level="title_only",
                      published_at="2026-08-09T08:00:00+08:00"),
                _item("policy", 2, importance_score=0.8, relevance_score=0.8,
                      content_level="title_only",
                      published_at="2026-08-08T08:00:00+08:00"),
            ),
            (
                _item("policy", 1, title="Same", source_name="Alpha", url="https://b",
                      published_at="2026-08-08T08:00:00+08:00"),
                _item("policy", 2, title="Same", source_name="Bravo", url="https://a",
                      published_at="2026-08-08T08:00:00+08:00"),
            ),
            (
                _item("policy", 1, title="Alpha", source_name="Same", url="https://b",
                      published_at="2026-08-08T08:00:00+08:00"),
                _item("policy", 2, title="Bravo", source_name="Same", url="https://a",
                      published_at="2026-08-08T08:00:00+08:00"),
            ),
            (
                _item("policy", 1, title="Same", source_name="Same", url="https://a",
                      published_at="2026-08-08T08:00:00+08:00"),
                _item("policy", 2, title="Same", source_name="Same", url="https://b",
                      published_at="2026-08-08T08:00:00+08:00"),
            ),
        ]

        for expected_first, expected_second in comparisons:
            with self.subTest(first=expected_first, second=expected_second):
                selection = select_weekly_modules(
                    [expected_second, expected_first], min_score=0.5
                )
                self.assertEqual(
                    [row["url"] for row in selection.policy],
                    [expected_first["url"], expected_second["url"]],
                )

    def test_threshold_and_empty_modules_do_not_pad(self):
        selection = select_weekly_modules([
            _item("policy", 1, relevance_score=0.499),
            _item("policy", 2, relevance_score=0.5),
        ], min_score=0.5)

        self.assertEqual([row["title"] for row in selection.policy], ["policy item 02"])
        self.assertEqual(selection.research, [])

    def test_analyzer_selects_once_saves_modules_and_groups_for_summary(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config={"AI_FILTER": {"MIN_SCORE": 0.5}}
        )
        rss_stats = [
            {"word": "政策", "titles": [_item("policy", 1)]},
            {"word": "科研", "titles": [_item("research", 1)]},
        ]

        grouped = analyzer._select_weekly_rss_items(rss_stats)

        self.assertEqual(len(analyzer._weekly_news_modules.policy), 1)
        self.assertEqual(len(analyzer._weekly_news_modules.research), 1)
        self.assertEqual(
            {row["module_type"] for group in grouped for row in group["titles"]},
            {"policy", "research"},
        )

    def test_ai_prompt_requests_three_grounded_sections_without_carrier_blacklist(self):
        prompt = (
            Path(__file__).resolve().parents[1] / "config" / "ai_analysis_prompt.txt"
        ).read_text(encoding="utf-8")

        for field in ("policy_trends", "research_trends", "weather_risks"):
            self.assertIn(field, prompt)
        self.assertIn("会议", prompt)
        self.assertIn("调研", prompt)
        self.assertIn("企业稿", prompt)
        self.assertIn("核心政策或科研事实", prompt)
        self.assertIn("{weather_content}", prompt)

    def test_weekly_pipeline_keeps_same_title_with_distinct_module_urls(self):
        pipeline = self._weekly_pipeline()
        raw_results = [
            {
                "news_item_id": 1,
                "tag": "育种",
                "title": "同标题新闻",
                "source_id": "policy-source",
                "source_name": "政策来源",
                "source_type": "rss",
                "module_type": "policy",
                "species_scope": "rice",
                "url": "https://example.org/policy-item",
                "published_at": "2026-08-08T08:00:00+08:00",
                "relevance_score": 0.8,
                "importance_score": 0.8,
                "content_level": "summary",
            },
            {
                "news_item_id": 2,
                "tag": "育种",
                "title": "同标题新闻",
                "source_id": "research-source",
                "source_name": "科研来源",
                "source_type": "rss",
                "module_type": "research",
                "species_scope": "rice",
                "url": "https://example.org/research-item",
                "published_at": "2026-08-08T08:00:00+08:00",
                "relevance_score": 0.8,
                "importance_score": 0.8,
                "content_level": "summary",
            },
        ]

        result = pipeline._build_filter_result(
            raw_results, [{"tag": "育种", "priority": 1}], 2
        )
        _, rss_stats, _ = pipeline.convert_to_report_data(result, mode="weekly")
        selection = select_weekly_modules(
            rss_stats[0]["titles"], min_score=0.5
        )

        self.assertEqual(result.total_matched, 2)
        self.assertEqual(len(selection.policy), 1)
        self.assertEqual(len(selection.research), 1)

    def test_ordinary_pipeline_still_deduplicates_same_title(self):
        pipeline = self._weekly_pipeline()
        pipeline._rss_window = None
        pipeline._rss_ids_authoritative = False
        raw_results = [
            {
                "news_item_id": index,
                "tag": "育种",
                "title": "普通模式同标题",
                "source_id": f"source-{index}",
                "source_type": "rss",
                "module_type": "research",
                "url": f"https://example.org/{index}",
                "relevance_score": 0.8,
            }
            for index in (1, 2)
        ]

        result = pipeline._build_filter_result(
            raw_results, [{"tag": "育种", "priority": 1}], 2
        )

        self.assertEqual(result.total_matched, 1)

    def test_weekly_report_bypasses_per_keyword_cap_but_daily_keeps_it(self):
        pipeline = self._weekly_pipeline(max_news=1)
        raw_results = [
            {
                "news_item_id": index,
                "tag": "育种",
                "title": f"周报候选 {index}",
                "source_id": "journal",
                "source_type": "rss",
                "module_type": "research",
                "url": f"https://example.org/candidate/{index}",
                "published_at": "2026-08-08T08:00:00+08:00",
                "relevance_score": 0.8,
            }
            for index in range(1, 4)
        ]
        result = pipeline._build_filter_result(
            raw_results, [{"tag": "育种", "priority": 1}], 3
        )

        _, weekly_stats, _ = pipeline.convert_to_report_data(result, mode="weekly")
        _, daily_stats, _ = pipeline.convert_to_report_data(result, mode="daily")

        self.assertEqual(len(weekly_stats[0]["titles"]), 3)
        self.assertEqual(len(daily_stats[0]["titles"]), 1)


if __name__ == "__main__":
    unittest.main()
