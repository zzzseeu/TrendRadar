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
        self.assertNotIn("freshness_filter", config["rss"])
        feeds = {feed["id"]: feed for feed in config["rss"]["feeds"]}
        disabled_ids = {"hacker-news", "ruanyifeng", "yahoo-finance"}
        self.assertEqual(
            {feed_id for feed_id, feed in feeds.items()
             if not feed.get("enabled", True)},
            disabled_ids,
        )
        self.assertTrue(
            all("max_age_days" not in feed for feed in feeds.values())
        )

    def test_custom_timeline_collects_daily_and_delivers_weekly_on_monday(self):
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
                "collect": False,
                "analyze": False,
                "push": False,
                "report_mode": "current",
                "ai_mode": "follow_report",
                "once": {"analyze": False, "push": False},
            },
        )
        monday_weekly = timeline["periods"]["monday_weekly"]
        self.assertEqual(
            monday_weekly,
            {
                "name": "每周农业新闻 PDF",
                "start": "10:00",
                "end": "12:01",
                "collect": True,
                "analyze": True,
                "push": True,
                "report_mode": "weekly",
                "ai_mode": "weekly",
                "once": {"analyze": True, "push": True},
            },
        )
        self.assertEqual(
            timeline["day_plans"],
            {
                "monday": {"periods": ["monday_weekly"]},
                "collect_only": {"periods": ["daily_collect"]},
            },
        )
        self.assertEqual(
            timeline["week_map"],
            {1: "monday", 2: "collect_only", 3: "collect_only",
             4: "collect_only", 5: "collect_only", 6: "collect_only",
             7: "collect_only"},
        )

        resolved = Scheduler(
            config["schedule"], timeline_data, MagicMock(),
            lambda: datetime(2026, 8, 10, 10, 30),
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
        self.assertIn(
            'CRON_SCHEDULES="0 10 * * *;30 10 * * 1;'
            '0,30 11 * * 1;0 12 * * 1"',
            text,
        )
