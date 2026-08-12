import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from trendradar.core.loader import _load_agro_weather_config
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
        disabled_ids = {
            "hacker-news", "ruanyifeng", "yahoo-finance",
            "philippines-da", "philrice-news", "natesc-rice",
            "lswz-control", "lswz-transactions", "hunan-rice",
            "jiangsu-rice", "jiangxi-rice", "amis-rice",
            "japan-maff-rice", "fao-rice", "usda-ers-rice",
            "india-agri-statistics", "india-food-distribution",
        }
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

    def test_weather_config_only_exposes_operational_settings(self):
        for relative_path in ("config/config.yaml", "config/config.en.yaml"):
            weather = yaml.safe_load(
                (ROOT / relative_path).read_text(encoding="utf-8")
            )["agro_weather"]
            self.assertEqual(set(weather), {"enabled", "url", "timeout"})
        self.assertEqual(
            set(_load_agro_weather_config({"agro_weather": {}})),
            {"ENABLED", "URL", "TIMEOUT"},
        )

    def test_readmes_link_to_supported_compatibility_surfaces(self):
        expectations = {
            "README.md": (
                "current", "daily", "docs/index.html",
                "README-MCP-FAQ.md", "notification",
            ),
            "README-EN.md": (
                "current", "daily", "docs/index.html",
                "README-MCP-FAQ-EN.md", "notification",
            ),
        }
        for filename, required in expectations.items():
            text = (ROOT / filename).read_text(encoding="utf-8")
            for token in required:
                with self.subTest(filename=filename, token=token):
                    self.assertIn(token, text)

    def test_weekly_public_contract_uses_four_modules(self):
        for relative in ("config/config.yaml", "config/config.en.yaml"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            config = yaml.safe_load(text)
            with self.subTest(relative=relative):
                self.assertEqual(config["ai_filter"]["min_score"], 0.5)
                self.assertNotIn("min_score: 0.7", text)

        prompt = (ROOT / "config/ai_filter/prompt.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("policy、industry、research 或 exclude", prompt)
        self.assertIn("species_scope", prompt)
        self.assertIn("政策优先", prompt)
        self.assertIn("领导调研", prompt)
        self.assertIn(
            "不得仅因会议、宣传稿或调研这种载体排除真实政策或科研信息",
            prompt,
        )
        self.assertNotIn("会议宣传、培训招生和纯营销内容", prompt)

        docs = {
            "README.md": (
                "周二至周日", "上一自然周", "四个独立模块", "水稻育种政策最多 20 条",
                "水稻产业时事动态最多 20 条", "农业育种科研文献最多 20 条",
                "气象", "独立", "0.5", "一个 PDF",
            ),
            "README-EN.md": (
                "Tuesday through Sunday", "previous natural week",
                "four independent modules", "Rice breeding policy",
                "Rice industry current affairs", "Breeding research",
                "weather", "0.5", "one PDF",
            ),
            "docs/news-push-technical-implementation.md": (
                "周二至周日", "上一自然周", "四个独立模块", "政策动态最多 20 条",
                "水稻产业动态最多 20 条", "科研进展最多 20 条",
                "气象", "独立", "0.5", "一个 PDF",
            ),
        }
        for filename, required in docs.items():
            text = (ROOT / filename).read_text(encoding="utf-8")
            for token in required:
                with self.subTest(filename=filename, token=token):
                    self.assertIn(token, text)

        weekly_analysis_prompt = (ROOT / "config/ai_analysis_prompt.txt").read_text(
            encoding="utf-8"
        )
        weekly_analysis_prompt = weekly_analysis_prompt[
            weekly_analysis_prompt.index("7. policy_trends："):
            weekly_analysis_prompt.index("字符串内部需要换行时")
        ]
        weekly_product_texts = {
            relative: (ROOT / relative).read_text(encoding="utf-8")
            for relative in (
                "README.md",
                "README-EN.md",
                "docs/news-push-technical-implementation.md",
            )
        }
        weekly_product_texts["config/ai_analysis_prompt.txt (weekly section)"] = (
            weekly_analysis_prompt
        )
        obsolete_weekly_phrases = (
            "AI 严格筛选最多 20 条",
            "strict AI up to 20 items",
            "重点新闻",
            "入选新闻",
            "weekly text preview",
            "weekly fallback",
            "周报文字预览",
            "周报文字回退",
        )
        for relative, text in weekly_product_texts.items():
            for phrase in obsolete_weekly_phrases:
                with self.subTest(relative=relative, phrase=phrase):
                    self.assertNotIn(phrase, text)
