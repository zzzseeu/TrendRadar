import unittest
from pathlib import Path

import yaml

from trendradar.crawler.news_search import GDELTClient, GoogleNewsRSSClient
from trendradar.notification.renderer import (
    render_dingtalk_content,
    render_feishu_content,
)
from trendradar.notification.splitter import split_content_into_batches
from trendradar.report.html import render_html_content


ROOT = Path(__file__).resolve().parents[1]
REPORT_DATA = {
    "stats": [],
    "new_titles": [],
    "failed_ids": [],
    "total_new_count": 0,
    "rss_matched_count": 0,
    "rss_total_count": 0,
    "rss_source_total": 0,
    "rss_source_failed": 0,
}


class DailyDeliveryReportTests(unittest.TestCase):
    def test_custom_timeline_runs_daily_delivery_every_day(self):
        timeline = yaml.safe_load(
            (ROOT / "config/timeline.yaml").read_text(encoding="utf-8")
        )["custom"]
        self.assertEqual(list(timeline["periods"]), ["daily_delivery"])
        self.assertEqual(
            timeline["day_plans"],
            {"daily": {"periods": ["daily_delivery"]}},
        )
        self.assertEqual(
            timeline["week_map"],
            {1: "daily", 2: "daily", 3: "daily", 4: "daily",
             5: "daily", 6: "daily", 7: "daily"},
        )
        period = timeline["periods"]["daily_delivery"]
        self.assertEqual(period["report_mode"], "daily_delivery")
        self.assertEqual(period["once"], {"analyze": True, "push": True})
        config = yaml.safe_load(
            (ROOT / "config/config.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(config["rss"]["freshness_filter"]["max_age_days"], 2)
        self.assertEqual(
            GDELTClient().build_params("rice breeding", 10)["timespan"],
            "48h",
        )
        self.assertIn(
            "when:2d",
            GoogleNewsRSSClient().build_params("rice breeding", "en")["q"],
        )

    def test_daily_delivery_header_contains_exact_window(self):
        report_data = dict(REPORT_DATA)
        report_data["period_label"] = "2026-08-08 10:00—2026-08-09 10:00"
        content = split_content_into_batches(
            report_data=report_data,
            format_type="wework",
            mode="daily_delivery",
            report_type="每日新增",
        )[0]
        self.assertIn("类型： 每日新增", content)
        self.assertIn("周期： 2026-08-08 10:00—2026-08-09 10:00", content)

    def test_daily_delivery_html_and_empty_notifications_are_labeled(self):
        report_data = dict(REPORT_DATA)
        html = render_html_content(
            report_data=report_data, total_titles=0, mode="daily_delivery"
        )
        self.assertIn("每日新增", html)
        expected = "每日新增模式下暂无匹配内容"
        self.assertIn(expected, render_feishu_content(report_data, mode="daily_delivery"))
        self.assertIn(expected, render_dingtalk_content(report_data, mode="daily_delivery"))
