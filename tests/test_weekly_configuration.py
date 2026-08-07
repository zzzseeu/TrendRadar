import unittest
from pathlib import Path

import yaml


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
        active = [
            feed for feed in config["rss"]["feeds"]
            if feed.get("enabled", True)
        ]
        self.assertTrue(all(feed.get("max_age_days") == 2 for feed in active))

    def test_custom_timeline_collects_daily_and_pushes_only_on_monday(self):
        timeline = yaml.safe_load(
            (ROOT / "config/timeline.yaml").read_text(encoding="utf-8")
        )["custom"]

        self.assertEqual(timeline["default"]["collect"], True)
        self.assertEqual(timeline["default"]["analyze"], False)
        self.assertEqual(timeline["default"]["push"], False)
        self.assertEqual(timeline["week_map"][1], "monday")
        monday = timeline["periods"]["monday_weekly"]
        self.assertEqual(monday["start"], "00:00")
        self.assertEqual(monday["end"], "23:59")
        self.assertEqual(monday["report_mode"], "weekly")
        self.assertTrue(monday["analyze"])
        self.assertTrue(monday["push"])
        self.assertTrue(monday["once"]["analyze"])
        self.assertTrue(monday["once"]["push"])
        for weekday in range(2, 8):
            self.assertEqual(timeline["week_map"][weekday], "silent")
        self.assertEqual(timeline["day_plans"]["silent"]["periods"], [])

    def test_example_cron_runs_daily_at_ten(self):
        text = (ROOT / "docker/.env.example").read_text(encoding="utf-8")
        self.assertIn('CRON_SCHEDULE="0 10 * * *"', text)
