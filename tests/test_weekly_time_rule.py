import inspect
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytz
import yaml

from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.core.loader import _load_rss_config
from trendradar.core.weekly import NaturalWeekWindow
from trendradar.crawler.rss.fetcher import RSSFeedConfig, RSSFetcher


ROOT = Path(__file__).resolve().parents[1]


class WeeklyTimeRuleRemovalTests(unittest.TestCase):
    def _pipeline_for_previous_week(self):
        timezone = "Asia/Shanghai"
        local_tz = pytz.timezone(timezone)
        window = NaturalWeekWindow(
            local_tz.localize(datetime(2026, 8, 3)),
            local_tz.localize(datetime(2026, 8, 10)),
            timezone,
        )
        return AIFilterPipeline(
            {"TIMEZONE": timezone, "RSS": {"ENABLED": True}, "AI_FILTER": {}},
            MagicMock(),
            lambda: window.end,
            rss_window=window,
        )

    def _rss_result(self, published_at, first_time):
        return {
            "news_item_id": 1,
            "tag": "水稻育种",
            "title": "跨周发布日期测试",
            "source_type": "rss",
            "source_id": "rice-science",
            "source_name": "Rice Science",
            "published_at": published_at,
            "first_time": first_time,
            "relevance_score": 0.9,
        }

    def test_runtime_rss_config_has_no_freshness_contract(self):
        loaded = _load_rss_config({"rss": {"enabled": True, "feeds": []}})
        self.assertNotIn("FRESHNESS_FILTER", loaded)
        self.assertNotIn("DEFAULT_MAX_AGE_DAYS", loaded)

    def test_feed_and_fetcher_have_no_age_options(self):
        self.assertNotIn("max_age_days", RSSFeedConfig.__dataclass_fields__)
        fetcher = RSSFetcher([])
        self.assertFalse(hasattr(fetcher, "freshness_enabled"))
        self.assertFalse(hasattr(fetcher, "default_max_age_days"))

    def test_active_yaml_has_no_removed_time_keys(self):
        for relative in ("config/config.yaml", "config/config.en.yaml"):
            raw = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
            self.assertNotIn("freshness_filter", raw["rss"])
            self.assertTrue(all(
                "max_age_days" not in feed
                for feed in raw["rss"].get("feeds", [])
            ))

    def test_ai_pipeline_has_no_freshness_filter(self):
        source = inspect.getsource(AIFilterPipeline)
        self.assertNotIn("_is_rss_item_fresh", source)
        self.assertNotIn("freshness_filtered_rss", source)

    def test_weekly_scope_uses_published_at_not_first_seen_time(self):
        pipeline = self._pipeline_for_previous_week()
        result = pipeline._build_filter_result(
            [self._rss_result(
                "2026-08-02T15:59:00+00:00",
                "2026-08-09T12:00:00+08:00",
            )],
            [{"tag": "水稻育种"}],
            1,
        )

        self.assertEqual(
            result.tags[0]["items"][0]["published_at"],
            "2026-08-02T15:59:00+00:00",
        )
        _, rss_stats, _ = pipeline.convert_to_report_data(result)
        self.assertEqual(rss_stats, [])

    def test_weekly_scope_excludes_missing_published_at_despite_first_seen_time(self):
        pipeline = self._pipeline_for_previous_week()
        result = pipeline._build_filter_result(
            [self._rss_result("", "2026-08-09T12:00:00+08:00")],
            [{"tag": "水稻育种"}],
            1,
        )

        _, rss_stats, _ = pipeline.convert_to_report_data(result)
        self.assertEqual(rss_stats, [])
