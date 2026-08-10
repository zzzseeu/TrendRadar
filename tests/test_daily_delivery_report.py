import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytz
import yaml

from trendradar.crawler.news_search import (
    GDELTClient,
    GoogleNewsRSSClient,
    NewsSearchBounds,
)
from trendradar.notification.renderer import (
    render_dingtalk_content,
    render_feishu_content,
)
from trendradar.notification.splitter import split_content_into_batches
from trendradar.notification.dispatcher import NotificationDispatcher
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
    def test_custom_timeline_collects_daily_and_delivers_weekly(self):
        timeline = yaml.safe_load(
            (ROOT / "config/timeline.yaml").read_text(encoding="utf-8")
        )["custom"]
        self.assertEqual(
            list(timeline["periods"]), ["daily_collect", "monday_weekly"]
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
        daily_collect = timeline["periods"]["daily_collect"]
        self.assertTrue(daily_collect["collect"])
        self.assertFalse(daily_collect["analyze"])
        self.assertFalse(daily_collect["push"])
        monday_weekly = timeline["periods"]["monday_weekly"]
        self.assertEqual(monday_weekly["report_mode"], "weekly")
        self.assertEqual(
            monday_weekly["once"], {"analyze": True, "push": True}
        )
        config = yaml.safe_load(
            (ROOT / "config/config.yaml").read_text(encoding="utf-8")
        )
        self.assertNotIn("freshness_filter", config["rss"])
        self.assertTrue(all(
            "max_age_days" not in feed
            for feed in config["rss"].get("feeds", [])
        ))
        bounds = NewsSearchBounds(
            start=pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 3)
            ),
            end=pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10)
            ),
        )
        gdelt = GDELTClient().build_params("rice breeding", 10, bounds)
        self.assertEqual(gdelt["startdatetime"], "20260802155959")
        self.assertEqual(gdelt["enddatetime"], "20260809160000")
        self.assertNotIn("timespan", gdelt)
        google = GoogleNewsRSSClient().build_params("rice breeding", "en", bounds)
        self.assertIn(
            "after:2026-08-02 before:2026-08-10",
            google["q"],
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

    def test_hotlist_disabled_keeps_period_in_real_feishu_and_dingtalk_payloads(self):
        period = "2026-08-08 10:00—2026-08-09 10:00"
        report_data = dict(REPORT_DATA)
        report_data.update({
            "period_label": period,
            "stats": [{
                "word": "热榜",
                "count": 1,
                "titles": [{"title": "Must be hidden"}],
            }],
        })
        dispatcher = NotificationDispatcher(
            config={
                "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/webhook/test",
                "DINGTALK_WEBHOOK_URL": "https://oapi.dingtalk.com/robot/send",
                "MAX_ACCOUNTS_PER_CHANNEL": 3,
                "FEISHU_BATCH_SIZE": 29000,
                "DINGTALK_BATCH_SIZE": 20000,
                "BATCH_SEND_INTERVAL": 0,
                "DISPLAY": {"REGIONS": {
                    "HOTLIST": False,
                    "RSS": True,
                    "NEW_ITEMS": True,
                    "AI_ANALYSIS": True,
                    "STANDALONE": False,
                }},
                "AI_TRANSLATION": {"ENABLED": False},
            },
            get_time_func=lambda: pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 9, 10, 0)
            ),
            split_content_func=split_content_into_batches,
        )
        feishu_response = MagicMock(status_code=200)
        feishu_response.json.return_value = {"code": 0}
        dingtalk_response = MagicMock(status_code=200)
        dingtalk_response.json.return_value = {"errcode": 0}

        with patch(
            "trendradar.notification.senders.requests.post",
            side_effect=[feishu_response, dingtalk_response],
        ) as post:
            results = dispatcher.dispatch_all(
                report_data=report_data,
                report_type="每日新增",
                mode="daily_delivery",
                rss_items=[{
                    "word": "育种",
                    "count": 1,
                    "titles": [{
                        "title": "Rice breeding",
                        "source_name": "Journal",
                        "url": "https://example.org/rice",
                        "mobile_url": "",
                        "reader_url": "",
                        "ranks": [],
                        "rank_threshold": 5,
                        "time_display": "2026-08-09 09:30",
                        "count": 1,
                    }],
                }],
                require_all_targets=True,
            )

        payload_texts = [str(call.kwargs["json"]) for call in post.call_args_list]
        self.assertEqual(results, {"feishu": True, "dingtalk": True})
        self.assertEqual(len(payload_texts), 2)
        for payload in payload_texts:
            self.assertIn("每日新增", payload)
            self.assertIn(period, payload)
            self.assertNotIn("Must be hidden", payload)
