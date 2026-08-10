import shutil
import subprocess
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytz

from trendradar.report.weekly_pdf import (
    build_weekly_pdf,
    flatten_unique_news,
    render_weekly_pdf_html,
)


class WeeklyPdfReportTests(unittest.TestCase):
    def setUp(self):
        self.items = [{
            "title": f"新闻 {index}",
            "url": f"https://example.com/{index}",
            "reader_url": f"https://search.example.com/{index}",
            "source_name": "测试来源",
            "published_at": "2026-08-09",
            "ai_summary": f"摘要 {index}",
            "highlight_rank": index if index <= 5 else None,
            "tags": ["育种技术"],
        } for index in range(1, 8)]
        self.weather = SimpleNamespace(
            title="全国农业气象周报",
            impact="上周农业气象影响",
            outlook="未来10天风险",
            recommendations="及时排涝散墒",
            risk_regions=("东北",),
            risk_crops=("玉米",),
            source_url="https://www.nmc.cn/publish/agro/ten-week/index.html",
        )

    def test_template_contains_reference_sections_and_all_news(self):
        html = render_weekly_pdf_html(
            news_items=self.items,
            ai_analysis=SimpleNamespace(
                success=True,
                core_trends="本周核心趋势",
                sentiment_controversy="风险分类",
                signals="关注信号",
                outlook_strategy="后续建议",
            ),
            agro_weather=self.weather,
            period_label="2026-08-03 00:00—2026-08-10 00:00",
            generated_at=pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            ),
        )
        for heading in (
            "核心观点摘要", "重点新闻", "入选新闻",
            "农业气象与灾害风险", "趋势与指标", "数据与方法说明",
        ):
            self.assertIn(heading, html)
        for index in range(1, 8):
            self.assertIn(f"新闻 {index}", html)
        self.assertEqual(html.count("重点标记"), 5)
        self.assertIn('href="https://example.com/1"', html)
        self.assertIn('href="https://search.example.com/1"', html)
        self.assertIn("@page", html)
        self.assertIn("size: A4", html)
        self.assertIn("@top-center", html)

    def test_rejects_more_than_twenty_unselected_news_items(self):
        with self.assertRaisesRegex(ValueError, "20"):
            render_weekly_pdf_html(
                news_items=self.items * 3,
                ai_analysis=None,
                agro_weather=self.weather,
                period_label="period",
                generated_at=datetime(2026, 8, 10),
            )

    def test_news_deduplication_and_unsafe_external_content(self):
        groups = [{"titles": [
            {"title": "相同标题", "url": "https://example.com/news"},
            {"title": "相同标题", "url": "https://example.com/news?utm_source=x"},
            {"title": "<script>bad</script>", "url": "javascript:alert(1)"},
        ]}]
        selected = flatten_unique_news(groups)
        self.assertEqual(len(selected), 2)
        html = render_weekly_pdf_html(
            news_items=selected,
            ai_analysis=None,
            agro_weather=None,
            period_label='<img src=x onerror=alert(1)>',
            generated_at=datetime(2026, 8, 10),
        )
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;", html)
        self.assertNotIn('href="javascript:', html)
        self.assertNotIn("<img src=x", html)

    def test_builds_chinese_range_filename(self):
        def create_pdf(_html, pdf):
            Path(pdf).write_bytes(b"%PDF-1.4\ncontent")
            return pdf

        with TemporaryDirectory() as tmp, patch(
            "trendradar.report.weekly_pdf.generate_pdf_from_html",
            side_effect=create_pdf,
        ):
            path = build_weekly_pdf(
                tmp, date(2026, 8, 3), date(2026, 8, 10), "<html></html>"
            )
        self.assertEqual(
            Path(path).name, "农业育种新闻周报_2026-08-03至2026-08-09.pdf"
        )


class WeeklyPdfGenerationValidationTests(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("chromium") or shutil.which("chromium-browser")
        or shutil.which("google-chrome"),
        "requires Chromium",
    )
    def test_actual_chromium_output_has_pdf_header_and_chinese_text(self):
        html = render_weekly_pdf_html(
            news_items=[{
                "title": "水稻育种技术进展",
                "url": "https://example.com/rice",
                "source_name": "测试来源",
                "published_at": "2026-08-09",
                "ai_summary": "中文摘要内容",
                "highlight_rank": 1,
                "tags": ["育种"],
            }],
            ai_analysis=SimpleNamespace(
                success=True, core_trends="核心趋势", sentiment_controversy="风险",
                signals="信号", outlook_strategy="建议",
            ),
            agro_weather=None,
            period_label="2026-08-03 00:00—2026-08-10 00:00",
            generated_at=datetime(2026, 8, 10, 10, 0),
        )
        with TemporaryDirectory() as tmp:
            pdf = Path(build_weekly_pdf(
                tmp, date(2026, 8, 3), date(2026, 8, 10), html
            ))
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
            self.assertGreater(pdf.stat().st_size, 5)
            self.assertLessEqual(pdf.stat().st_size, 20 * 1024 * 1024)
            if shutil.which("pdftotext"):
                text = subprocess.run(
                    ["pdftotext", str(pdf), "-"],
                    capture_output=True, text=True, check=True,
                ).stdout
                self.assertIn("水稻育种技术进展", text)

    def test_invalid_or_oversized_pdf_is_deleted(self):
        from trendradar.report.pdf import generate_pdf_from_html

        with TemporaryDirectory() as tmp:
            html = Path(tmp) / "report.html"
            pdf = Path(tmp) / "report.pdf"
            html.write_text("<html></html>", encoding="utf-8")

            def invalid_output(command, **kwargs):
                pdf.write_bytes(b"not a PDF")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("trendradar.report.pdf.shutil.which", return_value="chromium"), patch(
                "trendradar.report.pdf.subprocess.run", side_effect=invalid_output
            ):
                with self.assertRaisesRegex(RuntimeError, "PDF"):
                    generate_pdf_from_html(str(html), str(pdf))
            self.assertFalse(pdf.exists())

    def test_oversized_pdf_is_deleted(self):
        from trendradar.report.pdf import MAX_PDF_BYTES, generate_pdf_from_html

        with TemporaryDirectory() as tmp:
            html = Path(tmp) / "report.html"
            pdf = Path(tmp) / "report.pdf"
            html.write_text("<html></html>", encoding="utf-8")

            def oversized_output(command, **kwargs):
                with pdf.open("wb") as file:
                    file.write(b"%PDF-1.4\n")
                    file.truncate(MAX_PDF_BYTES + 1)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("trendradar.report.pdf.shutil.which", return_value="chromium"), patch(
                "trendradar.report.pdf.subprocess.run", side_effect=oversized_output
            ):
                with self.assertRaisesRegex(RuntimeError, "20MB"):
                    generate_pdf_from_html(str(html), str(pdf))
            self.assertFalse(pdf.exists())

    def test_missing_chromium_fails_without_pdf_residue(self):
        from trendradar.report.pdf import generate_pdf_from_html

        with TemporaryDirectory() as tmp:
            html = Path(tmp) / "report.html"
            pdf = Path(tmp) / "report.pdf"
            html.write_text("<html></html>", encoding="utf-8")
            pdf.write_bytes(b"old")
            with patch("trendradar.report.pdf.shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "Chromium"):
                    generate_pdf_from_html(str(html), str(pdf))
            self.assertFalse(pdf.exists())


class WeeklyPdfAnalyzerIntegrationTests(unittest.TestCase):
    def test_weather_only_weekly_report_still_builds_dedicated_pdf(self):
        from trendradar.__main__ import NewsAnalyzer

        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer._rss_window = SimpleNamespace(
            start=pytz.timezone("Asia/Shanghai").localize(datetime(2026, 8, 3)),
            end=pytz.timezone("Asia/Shanghai").localize(datetime(2026, 8, 10)),
            label="2026-08-03—2026-08-09",
        )
        analyzer._agro_weather_report = self_weather = SimpleNamespace(
            title="全国农业气象周报", impact="影响", outlook="展望",
            recommendations="建议", risk_regions=(), risk_crops=(),
            source_url="https://www.nmc.cn/publish/agro/ten-week/index.html",
        )
        analyzer._operation_run_at = lambda: pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 10, 10, 0)
        )
        with patch("trendradar.__main__.render_weekly_pdf_html", return_value="<html />") as render, patch(
            "trendradar.__main__.build_weekly_pdf", return_value="output/report.pdf"
        ) as build:
            result = analyzer._generate_weekly_pdf_report([], None)

        self.assertEqual(result, "output/report.pdf")
        self.assertEqual(analyzer._weekly_pdf_path, "output/report.pdf")
        self.assertIs(render.call_args.kwargs["agro_weather"], self_weather)
        self.assertEqual(build.call_args.args[1:3], (date(2026, 8, 3), date(2026, 8, 10)))

    def test_weekly_report_fails_only_when_news_and_weather_are_empty(self):
        from trendradar.__main__ import NewsAnalyzer

        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer._rss_window = SimpleNamespace(
            start=pytz.timezone("Asia/Shanghai").localize(datetime(2026, 8, 3)),
            end=pytz.timezone("Asia/Shanghai").localize(datetime(2026, 8, 10)),
            label="2026-08-03—2026-08-09",
        )
        analyzer._agro_weather_report = None
        with self.assertRaisesRegex(RuntimeError, "新闻和农业气象"):
            analyzer._generate_weekly_pdf_report([], None)

    def test_news_weekly_report_requires_successful_ai_summary(self):
        from trendradar.__main__ import NewsAnalyzer

        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer._agro_weather_report = None
        with self.assertRaisesRegex(RuntimeError, "AI 摘要"):
            analyzer._generate_weekly_pdf_report(
                [{"word": "育种", "titles": [{
                    "title": "普通新闻", "url": "https://example.com/news",
                }]}],
                None,
            )

if __name__ == "__main__":
    unittest.main()
