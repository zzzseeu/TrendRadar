import unittest
from datetime import datetime
from unittest.mock import MagicMock

import pytz
import requests

from trendradar.crawler.agro_weather import AgroWeatherClient, AgroWeatherFetchError
from trendradar.core.loader import _load_agro_weather_config


HTML = """
<html><body>
<article class="agro-report">
<h1>全国农业气象周报</h1>
<p>预报：李轩　签发：郑昌玲　2026 年 08 月 10 日</p>
<h2>本周西北地区东部阴雨寡照</h2>
<h3>一、本周天气特点及农业影响分析</h3>
<p>本周（2026年8月2日-2026年8月8日），东北农区光温适宜。</p>
<h3>二、未来天气对农业生产影响预估及建议</h3>
<p>未来10天，黄淮等地有强降雨，低洼农田渍涝风险高。</p>
<p>建议：及时排涝散墒，做好病虫害监测。</p>
</article>
</body></html>
"""


class AgroWeatherClientTests(unittest.TestCase):
    def make_session(self, html=HTML, status_code=200):
        session = MagicMock()
        response = session.get.return_value
        response.status_code = status_code
        response.text = html
        response.raise_for_status.return_value = None
        return session

    @staticmethod
    def run_at():
        return pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 10, 10, 0)
        )

    def test_parses_current_cycle_report(self):
        session = self.make_session()
        client = AgroWeatherClient(session=session)

        report = client.fetch_latest(self.run_at())

        self.assertEqual(report.report_date.isoformat(), "2026-08-10")
        self.assertEqual(report.reviewed_start.isoformat(), "2026-08-02")
        self.assertEqual(report.reviewed_end.isoformat(), "2026-08-08")
        self.assertIn("未来10天", report.outlook)
        self.assertIn("黄淮", report.outlook)
        self.assertIn("排涝", report.recommendations)
        self.assertEqual(report.risk_regions, ("黄淮",))
        self.assertEqual(report.risk_crops, ("农田",))
        session.get.assert_called_once_with(client.source_url, timeout=30)

    def test_accepts_sunday_early_report_for_just_ended_cycle(self):
        early = HTML.replace("08 月 10 日", "08 月 09 日")
        session = self.make_session(early)

        report = AgroWeatherClient(session=session).fetch_latest(self.run_at())

        self.assertIsNotNone(report)
        self.assertEqual(report.report_date.isoformat(), "2026-08-09")

    def test_rejects_stale_cycle(self):
        stale = HTML.replace("08 月 10 日", "08 月 03 日").replace(
            "8月2日-2026年8月8日", "7月26日-2026年8月1日"
        )
        session = self.make_session(stale)

        self.assertIsNone(AgroWeatherClient(session=session).fetch_latest(self.run_at()))

    def test_rejects_report_without_future_outlook(self):
        no_outlook = HTML.replace(
            "<p>未来10天，黄淮等地有强降雨，低洼农田渍涝风险高。</p>", ""
        )
        session = self.make_session(no_outlook)

        with self.assertRaisesRegex(AgroWeatherFetchError, "未来10天"):
            AgroWeatherClient(session=session).fetch_latest(self.run_at())

    def test_rejects_report_without_signing_date(self):
        no_signing_date = HTML.replace(
            "<p>预报：李轩　签发：郑昌玲　2026 年 08 月 10 日</p>", ""
        )
        session = self.make_session(no_signing_date)

        with self.assertRaisesRegex(AgroWeatherFetchError, "签发日期"):
            AgroWeatherClient(session=session).fetch_latest(self.run_at())

    def test_rejects_metadata_date_that_is_not_a_signing_date(self):
        no_signing_date = HTML.replace(
            "预报：李轩　签发：郑昌玲　2026 年 08 月 10 日",
            "页面更新时间：2026 年 08 月 10 日",
        )
        session = self.make_session(no_signing_date)

        with self.assertRaisesRegex(AgroWeatherFetchError, "签发日期"):
            AgroWeatherClient(session=session).fetch_latest(self.run_at())

    def test_rejects_page_update_date_after_the_signer_name(self):
        ambiguous_signing = HTML.replace(
            "签发：郑昌玲　2026 年 08 月 10 日",
            "签发：郑昌玲；页面更新时间：2026 年 08 月 10 日",
        )
        session = self.make_session(ambiguous_signing)

        with self.assertRaisesRegex(AgroWeatherFetchError, "签发日期"):
            AgroWeatherClient(session=session).fetch_latest(self.run_at())

    def test_rejects_impact_section_that_contains_only_review_dates(self):
        dates_only = HTML.replace(
            "本周（2026年8月2日-2026年8月8日），东北农区光温适宜。",
            "本周（2026年8月2日-2026年8月8日）。",
        )
        session = self.make_session(dates_only)

        with self.assertRaisesRegex(AgroWeatherFetchError, "农业影响"):
            AgroWeatherClient(session=session).fetch_latest(self.run_at())

    def test_does_not_use_later_sections_as_the_second_section_content(self):
        moved_outlook = HTML.replace(
            "<p>未来10天，黄淮等地有强降雨，低洼农田渍涝风险高。</p>\n"
            "<p>建议：及时排涝散墒，做好病虫害监测。</p>",
            "<h3>三、后续信息</h3>\n"
            "<p>未来10天，黄淮等地有强降雨，低洼农田渍涝风险高。</p>\n"
            "<p>建议：及时排涝散墒，做好病虫害监测。</p>",
        )
        session = self.make_session(moved_outlook)

        with self.assertRaisesRegex(AgroWeatherFetchError, "未来10天"):
            AgroWeatherClient(session=session).fetch_latest(self.run_at())

    def test_rejects_footer_text_when_the_second_section_body_is_empty(self):
        empty_second_section = """
        <html><body><article class="agro-report">
        <h1>全国农业气象周报</h1>
        <p>预报：李轩　签发：郑昌玲　2026 年 08 月 10 日</p>
        <h3>一、本周天气特点及农业影响分析</h3>
        <p>本周（2026年8月2日-2026年8月8日），东北农区水稻长势良好。</p>
        <h3>二、未来天气对农业生产影响预估及建议</h3>
        </article><footer>
        <p>未来10天，黄淮农田渍涝风险高。</p>
        <p>建议：及时排涝。</p>
        </footer></body></html>
        """
        session = self.make_session(empty_second_section)

        with self.assertRaisesRegex(AgroWeatherFetchError, "未来10天"):
            AgroWeatherClient(session=session).fetch_latest(self.run_at())

    def test_rejects_recommendation_label_without_text(self):
        label_only = HTML.replace("建议：及时排涝散墒，做好病虫害监测。", "建议：")
        session = self.make_session(label_only)

        with self.assertRaisesRegex(AgroWeatherFetchError, "农事建议"):
            AgroWeatherClient(session=session).fetch_latest(self.run_at())

    def test_rejects_outlook_without_structured_risk(self):
        no_risk = HTML.replace(
            "未来10天，黄淮等地有强降雨，低洼农田渍涝风险高。",
            "未来10天天气总体正常。",
        ).replace("及时排涝散墒，做好病虫害监测。", "做好田间观测。")
        session = self.make_session(no_risk)

        with self.assertRaisesRegex(AgroWeatherFetchError, "风险区域或作物"):
            AgroWeatherClient(session=session).fetch_latest(self.run_at())

    def test_rejects_navigation_title_when_article_title_does_not_match(self):
        navigation_title = HTML.replace(
            "<article class=\"agro-report\">\n<h1>全国农业气象周报</h1>",
            "<nav><h1>全国农业气象周报</h1></nav>\n"
            "<article class=\"agro-report\">\n<h1>农业气象服务</h1>",
        )
        session = self.make_session(navigation_title)

        with self.assertRaisesRegex(AgroWeatherFetchError, "标题"):
            AgroWeatherClient(session=session).fetch_latest(self.run_at())

    def test_rejects_non_official_source_url(self):
        with self.assertRaisesRegex(ValueError, "官方栏目"):
            AgroWeatherClient(source_url="https://example.com/agro.html")

    def test_parses_a_cross_year_review_cycle(self):
        cross_year = HTML.replace(
            "2026 年 08 月 10 日", "2026 年 01 月 05 日"
        ).replace("2026年8月2日-2026年8月8日", "12月28日-1月3日")
        session = self.make_session(cross_year)
        run_at = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 1, 5, 10, 0)
        )

        report = AgroWeatherClient(session=session).fetch_latest(run_at)

        self.assertEqual(report.reviewed_start.isoformat(), "2025-12-28")
        self.assertEqual(report.reviewed_end.isoformat(), "2026-01-03")

    def test_http_errors_include_source_context(self):
        for status_code in (403, 500):
            with self.subTest(status_code=status_code):
                session = self.make_session(status_code=status_code)
                session.get.return_value.raise_for_status.side_effect = requests.HTTPError(
                    f"{status_code} error"
                )

                with self.assertRaisesRegex(AgroWeatherFetchError, "nmc.cn"):
                    AgroWeatherClient(session=session).fetch_latest(self.run_at())

    def test_rejects_empty_page_as_a_structure_error(self):
        session = self.make_session("")

        with self.assertRaisesRegex(AgroWeatherFetchError, "nmc.cn"):
            AgroWeatherClient(session=session).fetch_latest(self.run_at())

    def test_fetches_the_official_page_on_each_call(self):
        session = self.make_session()
        client = AgroWeatherClient(session=session)

        client.fetch_latest(self.run_at())
        client.fetch_latest(self.run_at())

        self.assertEqual(session.get.call_count, 2)


class AgroWeatherConfigTests(unittest.TestCase):
    def test_loads_agro_weather_configuration(self):
        config = _load_agro_weather_config(
            {
                "agro_weather": {
                    "enabled": True,
                    "url": "https://www.nmc.cn/publish/agro/ten-week/index.html",
                    "timeout": 30,
                    "required_for_weekly": True,
                }
            }
        )

        self.assertEqual(
            config,
            {
                "ENABLED": True,
                "URL": "https://www.nmc.cn/publish/agro/ten-week/index.html",
                "TIMEOUT": 30,
                "REQUIRED_FOR_WEEKLY": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
