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


class WeeklyModuleSelectionCompatibilityTests(unittest.TestCase):
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

    def test_selects_two_top_twenty_lists_and_research_evidence_wins_identity(self):
        current_events = [_item("current_events", index) for index in range(25)]
        research = [_item("research", index) for index in range(25)]
        current_events.extend([
            _item(
                "current_events", 100, title="Current GUID duplicate", url="",
                guid="shared-guid", importance_score=0.6,
            ),
            _item(
                "current_events", 101, title="Normalized Duplicate", url="", guid="",
                importance_score=0.59,
            ),
        ])
        research.extend([
            _item(
                "research", 100, title="URL research owner", importance_score=2,
                url="https://example.org/current_events/0?utm_source=research",
            ),
            _item(
                "research", 101, title="GUID research owner", importance_score=2,
                url="", guid="shared-guid",
            ),
            _item(
                "research", 102, title="  normalized   duplicate  ",
                importance_score=2, url="", guid="",
            ),
            _item(
                "research", 103, title="Current rank 21 duplicate",
                importance_score=2,
                url="https://example.org/current_events/20?utm_medium=research",
            ),
        ])

        selection = select_weekly_modules(
            [*current_events, *research], min_score=0.5
        )

        self.assertEqual(len(selection.current_events), 20)
        self.assertEqual(len(selection.research), 20)
        self.assertEqual(
            [row["module_rank"] for row in selection.current_events],
            list(range(1, 21)),
        )
        self.assertEqual(
            [row["highlight_rank"] for row in selection.current_events[:5]],
            list(range(1, 6)),
        )
        self.assertTrue(all(
            "highlight_rank" not in row for row in selection.current_events[5:]
        ))
        current_identities = {
            _identity(row) for row in selection.current_events
        }
        research_identities = {_identity(row) for row in selection.research}
        self.assertTrue(current_identities.isdisjoint(research_identities))
        self.assertNotIn(
            ("url", "https://example.org/current_events/0"), current_identities
        )
        self.assertNotIn(("guid", "shared-guid"), current_identities)
        self.assertNotIn(
            ("title", normalize_title("normalized duplicate")),
            current_identities,
        )

    def test_ranking_uses_value_evidence_date_and_stable_fields_in_order(self):
        comparisons = [
            (
                _item("current_events", 1, importance_score=0.9, relevance_score=0.6),
                _item("current_events", 2, importance_score=0.8, relevance_score=1.0),
            ),
            (
                _item("current_events", 1, importance_score=0.8, relevance_score=0.9),
                _item("current_events", 2, importance_score=0.8, relevance_score=0.8),
            ),
            (
                _item("current_events", 1, importance_score=0.8, relevance_score=0.8,
                      content_level="full_text"),
                _item("current_events", 2, importance_score=0.8, relevance_score=0.8,
                      content_level="summary"),
            ),
            (
                _item("current_events", 1, importance_score=0.8, relevance_score=0.8,
                      content_level="summary"),
                _item("current_events", 2, importance_score=0.8, relevance_score=0.8,
                      content_level="title_only"),
            ),
            (
                _item("current_events", 1, importance_score=0.8, relevance_score=0.8,
                      content_level="title_only",
                      published_at="2026-08-09T08:00:00+08:00"),
                _item("current_events", 2, importance_score=0.8, relevance_score=0.8,
                      content_level="title_only",
                      published_at="2026-08-08T08:00:00+08:00"),
            ),
            (
                _item("current_events", 1, title="Same", source_name="Alpha", url="https://b",
                      published_at="2026-08-08T08:00:00+08:00"),
                _item("current_events", 2, title="Same", source_name="Bravo", url="https://a",
                      published_at="2026-08-08T08:00:00+08:00"),
            ),
            (
                _item("current_events", 1, title="Alpha", source_name="Same", url="https://b",
                      published_at="2026-08-08T08:00:00+08:00"),
                _item("current_events", 2, title="Bravo", source_name="Same", url="https://a",
                      published_at="2026-08-08T08:00:00+08:00"),
            ),
            (
                _item("current_events", 1, title="Same", source_name="Same", url="https://a",
                      published_at="2026-08-08T08:00:00+08:00"),
                _item("current_events", 2, title="Same", source_name="Same", url="https://b",
                      published_at="2026-08-08T08:00:00+08:00"),
            ),
        ]

        for expected_first, expected_second in comparisons:
            with self.subTest(first=expected_first, second=expected_second):
                selection = select_weekly_modules(
                    [expected_second, expected_first], min_score=0.5
                )
                self.assertEqual(
                    [row["url"] for row in selection.current_events],
                    [expected_first["url"], expected_second["url"]],
                )

    def test_threshold_and_empty_modules_do_not_pad(self):
        selection = select_weekly_modules([
            _item("current_events", 1, relevance_score=0.499),
            _item("current_events", 2, relevance_score=0.5),
        ], min_score=0.5)

        self.assertEqual(
            [row["title"] for row in selection.current_events],
            ["current_events item 02"],
        )
        self.assertEqual(selection.research, [])

    def test_analyzer_selects_once_saves_modules_and_groups_for_summary(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config={"AI_FILTER": {"MIN_SCORE": 0.5}}
        )
        rss_stats = [
            {"word": "时事", "titles": [_item("current_events", 1)]},
            {"word": "科研", "titles": [_item("research", 1)]},
        ]

        grouped = analyzer._select_weekly_rss_items(rss_stats)

        self.assertEqual(len(analyzer._weekly_news_modules.current_events), 1)
        self.assertEqual(len(analyzer._weekly_news_modules.research), 1)
        self.assertEqual(
            {row["module_type"] for group in grouped for row in group["titles"]},
            {"current_events", "research"},
        )

    def test_ai_prompt_requests_three_grounded_sections_without_carrier_blacklist(self):
        prompt = (
            Path(__file__).resolve().parents[1] / "config" / "ai_analysis_prompt.txt"
        ).read_text(encoding="utf-8")

        for field in (
            "current_events_trends", "research_trends", "weather_risks",
        ):
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
                "source_id": "current-source",
                "source_name": "时事来源",
                "source_type": "rss",
                "module_type": "current_events",
                "species_scope": "rice",
                "url": "https://example.org/current-item",
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
        self.assertEqual(len(selection.current_events), 1)
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
