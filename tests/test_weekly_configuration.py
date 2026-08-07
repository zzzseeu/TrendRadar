import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from trendradar.core.scheduler import Scheduler


ROOT = Path(__file__).resolve().parents[1]


class WeeklyConfigurationTests(unittest.TestCase):
    def test_runtime_uses_custom_weekly_schedule(self):
        config = yaml.safe_load(
            (ROOT / "config/config.yaml").read_text(encoding="utf-8")
        )

        self.assertTrue(config["schedule"]["enabled"])
        self.assertEqual(config["schedule"]["preset"], "custom")
        self.assertEqual(
            config["rss"]["freshness_filter"]["max_age_days"], 2
        )
        feeds = {feed["id"]: feed for feed in config["rss"]["feeds"]}
        disabled_ids = {"hacker-news", "ruanyifeng", "yahoo-finance"}
        self.assertEqual(
            {feed_id for feed_id, feed in feeds.items()
             if not feed.get("enabled", True)},
            disabled_ids,
        )
        self.assertTrue(
            all(feeds[feed_id]["max_age_days"] == 1 for feed_id in disabled_ids)
        )
        self.assertTrue(
            all(
                feed["max_age_days"] == 2
                for feed in feeds.values()
                if feed.get("enabled", True)
            )
        )

    def test_custom_timeline_collects_daily_and_pushes_only_on_monday(self):
        config = yaml.safe_load(
            (ROOT / "config/config.yaml").read_text(encoding="utf-8")
        )
        timeline_data = yaml.safe_load(
            (ROOT / "config/timeline.yaml").read_text(encoding="utf-8")
        )
        timeline = timeline_data["custom"]

        self.assertEqual(
            timeline["default"],
            {
                "collect": True,
                "analyze": False,
                "ai_mode": "follow_report",
                "push": False,
                "report_mode": "current",
                "once": {"analyze": False, "push": False},
            },
        )
        monday = timeline["periods"]["monday_weekly"]
        self.assertEqual(
            monday,
            {
                "name": "自然周周报",
                "start": "00:00",
                "end": "24:00",
                "collect": True,
                "analyze": True,
                "ai_mode": "follow_report",
                "push": True,
                "report_mode": "weekly",
                "once": {"analyze": True, "push": True},
            },
        )
        self.assertEqual(
            timeline["day_plans"],
            {"monday": {"periods": ["monday_weekly"]}, "silent": {"periods": []}},
        )
        self.assertEqual(
            timeline["week_map"],
            {1: "monday", 2: "silent", 3: "silent", 4: "silent", 5: "silent",
             6: "silent", 7: "silent"},
        )
        self.assertEqual(timeline["overlap"], {"policy": "error_on_overlap"})

        resolved = Scheduler(
            config["schedule"], timeline_data, MagicMock(),
            lambda: datetime(2026, 8, 10, 23, 59),
        ).resolve()
        self.assertEqual(resolved.period_key, "monday_weekly")
        self.assertTrue(resolved.collect)
        self.assertTrue(resolved.analyze)
        self.assertTrue(resolved.push)
        self.assertEqual(resolved.report_mode, "weekly")
        self.assertEqual(resolved.ai_mode, "weekly")
        self.assertTrue(resolved.once_analyze)
        self.assertTrue(resolved.once_push)

    def test_example_cron_runs_daily_at_ten(self):
        text = (ROOT / "docker/.env.example").read_text(encoding="utf-8")
        self.assertIn('CRON_SCHEDULE="0 10 * * *"', text)
