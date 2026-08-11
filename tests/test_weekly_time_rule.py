import inspect
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytz
import yaml

from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.core.loader import _load_rss_config
from trendradar.core.weekly import NaturalWeekWindow, previous_natural_week
from trendradar.crawler.rss.fetcher import RSSFeedConfig, RSSFetcher
from trendradar.storage.base import RSSData, RSSItem
from trendradar.storage.local import LocalStorageBackend


ROOT = Path(__file__).resolve().parents[1]


class WeeklyTimeRuleRemovalTests(unittest.TestCase):
    def _pipeline_for_previous_week(self, storage=None):
        timezone = "Asia/Shanghai"
        local_tz = pytz.timezone(timezone)
        window = NaturalWeekWindow(
            local_tz.localize(datetime(2026, 8, 3)),
            local_tz.localize(datetime(2026, 8, 10)),
            timezone,
        )
        return AIFilterPipeline(
            {"TIMEZONE": timezone, "RSS": {"ENABLED": True}, "AI_FILTER": {}},
            storage or MagicMock(),
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

    def test_runtime_and_user_docs_have_no_obsolete_delivery_terms(self):
        paths = (
            "trendradar", "config", "docker", "docs/index.html",
            "docs/assets/script.js", "docs/assets/i18n.js",
            "docs/news-push-technical-implementation.md", "README.md",
            "README-EN.md",
        )
        forbidden = (
            "freshness_filter", "max_age_days", "when:2d",
            '"timespan": "48h"', "每日新增推送",
            "WEWORK_PDF_TOP_N", "WEWORK_PDF_ENABLED",
        )
        for relative in paths:
            path = ROOT / relative
            files = [path] if path.is_file() else list(path.rglob("*"))
            text = "\n".join(
                file.read_text(encoding="utf-8", errors="ignore")
                for file in files
                if file.is_file() and "__pycache__" not in file.parts
            )
            for token in forbidden:
                self.assertNotIn(token, text, f"{relative}: {token}")

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

    def test_active_rss_results_keep_published_at_for_weekly_scope(self):
        timezone = "Asia/Shanghai"
        local_tz = pytz.timezone(timezone)
        now = local_tz.localize(datetime(2026, 8, 9, 10))
        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone=timezone,
            )
            try:
                with patch.object(
                    storage, "_get_configured_time", return_value=now
                ):
                    self.assertTrue(storage.save_rss_data(RSSData(
                        date="2026-08-09",
                        crawl_time="09:30",
                        items={"journal": [
                            RSSItem(
                                title="Published this week",
                                feed_id="journal",
                                feed_name="Journal",
                                url="https://example.org/published",
                                published_at="2026-08-05T08:00:00+08:00",
                            ),
                            RSSItem(
                                title="Missing publication date",
                                feed_id="journal",
                                feed_name="Journal",
                                url="https://example.org/missing",
                            ),
                        ]},
                        id_to_name={"journal": "Journal"},
                    )))
                    self.assertEqual(storage.save_ai_filter_tags(
                        [{"tag": "水稻育种", "priority": 1}],
                        version=1,
                        prompt_hash="weekly-scope",
                    ), 1)
                    tag_id = storage._get_connection("2026-08-09").execute(
                        "SELECT id FROM ai_filter_tags WHERE tag = ?",
                        ("水稻育种",),
                    ).fetchone()[0]
                    rss_ids = {
                        item["title"]: item["id"]
                        for item in storage.get_all_rss_ids("2026-08-09")
                    }
                    self.assertEqual(storage.save_ai_filter_results([
                        {
                            "news_item_id": rss_ids["Published this week"],
                            "source_type": "rss",
                            "tag_id": tag_id,
                            "module_type": "research",
                            "relevance_score": 0.9,
                        },
                        {
                            "news_item_id": rss_ids["Missing publication date"],
                            "source_type": "rss",
                            "tag_id": tag_id,
                            "module_type": "research",
                            "relevance_score": 0.9,
                        },
                    ]), 2)

                    active = storage.get_active_ai_filter_results()

                published = {
                    item["title"]: item["published_at"] for item in active
                }
                self.assertEqual(
                    published["Published this week"],
                    "2026-08-05T08:00:00+08:00",
                )
                self.assertEqual(published["Missing publication date"], "")

                window = previous_natural_week(
                    local_tz.localize(datetime(2026, 8, 10, 10)), timezone
                )
                pipeline = self._pipeline_for_previous_week(storage)
                pipeline._rss_window = window
                result = pipeline._build_filter_result(
                    active, [{"tag": "水稻育种", "priority": 1}], 2
                )
                _, rss_stats, _ = pipeline.convert_to_report_data(
                    result, mode="weekly"
                )
                self.assertEqual(
                    [item["title"] for item in rss_stats[0]["titles"]],
                    ["Published this week"],
                )
            finally:
                storage.cleanup()
