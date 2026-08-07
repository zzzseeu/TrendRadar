import unittest
from tempfile import TemporaryDirectory

from trendradar.__main__ import NewsAnalyzer
from trendradar.notification.renderer import (
    render_dingtalk_content,
    render_feishu_content,
)
from trendradar.notification.splitter import split_content_into_batches
from trendradar.report.generator import generate_html_report
from trendradar.report.html import render_html_content


REPORT_DATA = {
    "stats": [], "new_titles": [], "failed_ids": [],
    "total_new_count": 0, "rss_matched_count": 1,
    "rss_total_count": 1, "rss_source_total": 1,
    "rss_source_failed": 0,
    "period_label": "2026-08-03—2026-08-09",
}
RSS_STATS = [{"word": "育种", "count": 1, "titles": [{
    "title": "Weekly item", "source_name": "Journal",
    "url": "https://example.org/item", "mobile_url": "",
    "ranks": [], "rank_threshold": 10, "time_display": "08-05 10:00",
    "count": 1,
}]}]


class WeeklyReportOutputTests(unittest.TestCase):
    def test_weekly_strategy_has_explicit_type(self):
        self.assertEqual(
            NewsAnalyzer.MODE_STRATEGIES["weekly"]["report_type"],
            "上周周报",
        )

    def test_notification_header_contains_period(self):
        content = "\n".join(split_content_into_batches(
            REPORT_DATA,
            "wework",
            mode="weekly",
            report_type="上周周报",
            rss_items=RSS_STATS,
        ))
        self.assertIn("上周周报", content)
        self.assertIn("2026-08-03—2026-08-09", content)

    def test_html_renderer_contains_weekly_mode_and_period(self):
        html = render_html_content(
            report_data=REPORT_DATA,
            total_titles=1,
            mode="weekly",
            rss_items=RSS_STATS,
        )
        self.assertIn("上周周报", html)
        self.assertIn("2026-08-03—2026-08-09", html)

    def test_html_generator_forwards_period_metadata(self):
        captured = {}

        def render(report_data, total_titles, mode, update_info):
            captured.update(report_data)
            return "<html></html>"

        with TemporaryDirectory() as output_dir:
            generate_html_report(
                stats=[], total_titles=0, mode="weekly",
                output_dir=output_dir, date_folder="2026-08-10",
                time_filename="10-00", render_html_func=render,
                report_metadata={
                    "period_label": "2026-08-03—2026-08-09"
                },
            )

        self.assertEqual(
            captured["period_label"], "2026-08-03—2026-08-09"
        )

    def test_weekly_empty_states_are_explicit(self):
        report_data = {"stats": [], "new_titles": [], "failed_ids": []}
        self.assertIn(
            "上周周报模式下暂无匹配的热点词汇",
            render_feishu_content(report_data, mode="weekly"),
        )
        self.assertIn(
            "上周周报模式下暂无匹配的热点词汇",
            render_dingtalk_content(report_data, mode="weekly"),
        )
