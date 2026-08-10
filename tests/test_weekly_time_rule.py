import inspect
import unittest
from pathlib import Path

import yaml

from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.core.loader import _load_rss_config
from trendradar.crawler.rss.fetcher import RSSFeedConfig, RSSFetcher


ROOT = Path(__file__).resolve().parents[1]


class WeeklyTimeRuleRemovalTests(unittest.TestCase):
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
