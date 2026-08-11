import os
import re
import shutil
import subprocess
import unittest
import xml.etree.ElementTree as ElementTree
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytz

from trendradar.report.weekly_pdf import (
    _validate_weekly_pdf,
    build_weekly_pdf,
    render_weekly_pdf_html,
)
from trendradar.core.weekly import WeeklyNewsSelection


class _ModuleHeadingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.section_stack = []
        self.current_heading = None
        self.primary_headings = []
        self.non_primary_h2 = []

    def handle_starttag(self, tag, attrs):
        if tag == "section":
            classes = dict(attrs).get("class", "").split()
            self.section_stack.append("primary-module" in classes)
        elif tag == "h2":
            self.current_heading = []

    def handle_data(self, data):
        if self.current_heading is not None:
            self.current_heading.append(data)

    def handle_endtag(self, tag):
        if tag == "h2" and self.current_heading is not None:
            heading = "".join(self.current_heading).strip()
            if self.section_stack and self.section_stack[-1]:
                self.primary_headings.append(heading)
            else:
                self.non_primary_h2.append(heading)
            self.current_heading = None
        elif tag == "section" and self.section_stack:
            self.section_stack.pop()


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

    def test_template_renders_each_module_once_by_rank_with_inline_top_five(self):
        policy = [
            {
                **item,
                "title": f"政策 {item['module_rank']}",
                "url": f"https://example.com/policy/{item['module_rank']}",
            }
            for item in (
                {**self.items[index - 1], "module_rank": index}
                for index in (3, 1, 7, 2, 6, 5, 4)
            )
        ]
        research = [
            {
                **item,
                "title": f"文献 {item['module_rank']}",
                "url": f"https://example.com/research/{item['module_rank']}",
            }
            for item in (
                {**self.items[index - 1], "module_rank": index}
                for index in (7, 2, 4, 1, 6, 3, 5)
            )
        ]
        html = render_weekly_pdf_html(
            policy_items=policy,
            research_items=research,
            ai_analysis=SimpleNamespace(
                success=True,
                policy_trends="政策趋势分析 [policy:1]",
                research_trends="科研趋势分析 [research:1]",
                weather_risks="气象风险分析 [weather:official]",
            ),
            agro_weather=self.weather,
            period_label="2026-08-03 00:00—2026-08-10 00:00",
            generated_at=pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            ),
        )
        for heading in ("政策动态", "科研进展", "农业气象与灾害风险", "趋势与指标"):
            self.assertIn(heading, html)
        self.assertNotIn("核心观点摘要", html)
        self.assertNotIn("重点新闻</h2>", html)
        self.assertNotIn("入选新闻</h2>", html)
        parser = _ModuleHeadingParser()
        parser.feed(html)
        self.assertEqual(
            parser.primary_headings,
            ["一、政策动态", "二、科研进展", "三、农业气象与灾害风险"],
        )
        self.assertEqual(parser.non_primary_h2, [])
        self.assertIn("<aside", html)
        self.assertIn("<h3>趋势与指标</h3>", html)
        self.assertIn("<h3>数据与方法说明</h3>", html)
        self.assertEqual(html.count("重点政策"), 5)
        self.assertEqual(html.count("重点文献"), 5)
        self.assertIn("政策趋势分析 [policy:1]", html)
        self.assertIn("科研趋势分析 [research:1]", html)
        self.assertIn("气象风险分析 [weather:official]", html)
        for module, title in (("policy", "政策"), ("research", "文献")):
            positions = []
            for index in range(1, 8):
                self.assertEqual(html.count(f">{title} {index}</h3>"), 1)
                self.assertEqual(
                    html.count(f'href="https://example.com/{module}/{index}"'), 1
                )
                positions.append(html.index(f">{title} {index}</h3>"))
            self.assertEqual(positions, sorted(positions))
        self.assertIn("@page", html)
        self.assertIn("size: A4", html)
        self.assertIn("@top-center", html)
        self.assertNotIn("position: fixed", html)

    def test_rejects_more_than_twenty_unselected_news_items(self):
        with self.assertRaisesRegex(ValueError, "20"):
            render_weekly_pdf_html(
                policy_items=[],
                research_items=self.items * 3,
                ai_analysis=None,
                agro_weather=self.weather,
                period_label="period",
                generated_at=datetime(2026, 8, 10),
            )

    def test_rejects_cross_module_duplicate_identity(self):
        duplicated = {"title": "相同标题", "url": "https://example.com/news"}
        with self.assertRaisesRegex(ValueError, "全局唯一"):
            render_weekly_pdf_html(
                policy_items=[{**duplicated, "module_rank": 1}],
                research_items=[{**duplicated, "module_rank": 1}],
                ai_analysis=None,
                agro_weather=None,
                period_label="period",
                generated_at=datetime(2026, 8, 10),
            )

    def test_rejects_non_contiguous_duplicate_module_ranks(self):
        invalid = [
            {
                "title": f"政策 {index}",
                "url": f"https://example.com/policy/{index}",
                "module_rank": 1,
            }
            for index in range(1, 7)
        ]
        with self.assertRaisesRegex(ValueError, "module_rank"):
            render_weekly_pdf_html(
                policy_items=invalid,
                research_items=[],
                ai_analysis=None,
                agro_weather=None,
                period_label="period",
                generated_at=datetime(2026, 8, 10),
            )

    def test_unsafe_external_content_is_escaped_and_unsafe_link_is_hidden(self):
        selected = [
            {"title": "相同标题", "url": "https://example.com/news"},
            {"title": "<script>bad</script>", "url": "javascript:alert(1)"},
        ]
        for index, item in enumerate(selected, start=1):
            item["module_rank"] = index
        html = render_weekly_pdf_html(
            policy_items=[],
            research_items=selected,
            ai_analysis=None,
            agro_weather=None,
            period_label='<img src=x onerror=alert(1)>',
            generated_at=datetime(2026, 8, 10),
        )
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;", html)
        self.assertNotIn('href="javascript:', html)
        self.assertNotIn("<img src=x", html)

    def test_empty_policy_and_research_modules_use_exact_copy(self):
        html = render_weekly_pdf_html(
            policy_items=[], research_items=[], ai_analysis=None,
            agro_weather=self.weather, period_label="period",
            generated_at=datetime(2026, 8, 10),
        )
        self.assertIn("本周暂无符合条件的政策新闻", html)
        self.assertIn("本周暂无符合条件的科研文献", html)

    def test_builds_chinese_range_filename(self):
        def create_pdf(_html, pdf):
            Path(pdf).write_bytes(b"%PDF-1.4\ncontent")
            return pdf

        with TemporaryDirectory() as tmp, patch(
            "trendradar.report.weekly_pdf.generate_pdf_from_html",
            side_effect=create_pdf,
        ), patch("trendradar.report.weekly_pdf._validate_weekly_pdf"):
            path = build_weekly_pdf(
                tmp, date(2026, 8, 3), date(2026, 8, 10), "<html></html>"
            )
        self.assertEqual(
            Path(path).name, "农业育种新闻周报_三模块_2026-08-03至2026-08-09.pdf"
        )

    def test_build_replaces_both_artifacts_only_after_temp_validation(self):
        with TemporaryDirectory() as tmp:
            final_pdf = Path(tmp) / "pdf" / "2026-08-10" / (
                "农业育种新闻周报_三模块_2026-08-03至2026-08-09.pdf"
            )
            final_html = final_pdf.with_suffix(".html")
            final_pdf.parent.mkdir(parents=True)
            final_pdf.write_bytes(b"%PDF-old")
            final_html.write_text("old html", encoding="utf-8")

            def create_pdf(_html, pdf):
                Path(pdf).write_bytes(b"%PDF-new")
                return pdf

            with patch(
                "trendradar.report.weekly_pdf.generate_pdf_from_html",
                side_effect=create_pdf,
            ), patch("trendradar.report.weekly_pdf._validate_weekly_pdf"):
                path = build_weekly_pdf(
                    tmp, date(2026, 8, 3), date(2026, 8, 10), "new html"
                )

            self.assertEqual(Path(path), final_pdf)
            self.assertEqual(final_pdf.read_bytes(), b"%PDF-new")
            self.assertEqual(final_html.read_text(encoding="utf-8"), "new html")
            self.assertEqual(list(final_pdf.parent.glob("*.tmp*")), [])

    def test_build_failure_preserves_formal_artifacts_and_removes_temps(self):
        def chromium_failure(_html, _pdf):
            raise RuntimeError("Chromium failed")

        def invalid_header(_html, pdf):
            Path(pdf).write_bytes(b"not a PDF")
            return pdf

        def oversized(_html, pdf):
            from trendradar.report.pdf import MAX_PDF_BYTES
            with Path(pdf).open("wb") as file:
                file.write(b"%PDF")
                file.truncate(MAX_PDF_BYTES + 1)
            return pdf

        for case, generator in (
            ("chromium", chromium_failure),
            ("header", invalid_header),
            ("oversized", oversized),
        ):
            with self.subTest(case=case), TemporaryDirectory() as tmp:
                final_pdf = Path(tmp) / "pdf" / "2026-08-10" / (
                    "农业育种新闻周报_三模块_2026-08-03至2026-08-09.pdf"
                )
                final_html = final_pdf.with_suffix(".html")
                final_pdf.parent.mkdir(parents=True)
                final_pdf.write_bytes(b"%PDF-old")
                final_html.write_text("old html", encoding="utf-8")

                with patch(
                    "trendradar.report.weekly_pdf.generate_pdf_from_html",
                    side_effect=generator,
                ):
                    with self.assertRaises(RuntimeError):
                        build_weekly_pdf(
                            tmp, date(2026, 8, 3), date(2026, 8, 10), "new html"
                        )

                self.assertEqual(final_pdf.read_bytes(), b"%PDF-old")
                self.assertEqual(final_html.read_text(encoding="utf-8"), "old html")
                self.assertEqual(list(final_pdf.parent.glob("*.tmp*")), [])

    def test_second_replace_failure_rolls_back_both_formal_artifacts(self):
        with TemporaryDirectory() as tmp:
            final_pdf = Path(tmp) / "pdf" / "2026-08-10" / (
                "农业育种新闻周报_三模块_2026-08-03至2026-08-09.pdf"
            )
            final_html = final_pdf.with_suffix(".html")
            final_pdf.parent.mkdir(parents=True)
            final_pdf.write_bytes(b"%PDF-old")
            final_html.write_text("old html", encoding="utf-8")

            def create_pdf(_html, pdf):
                Path(pdf).write_bytes(b"%PDF-new")
                return pdf

            real_replace = os.replace
            failed = False

            def fail_pdf_replace_once(source, destination):
                nonlocal failed
                if Path(destination) == final_pdf and not failed:
                    failed = True
                    raise OSError("simulated PDF replace failure")
                return real_replace(source, destination)

            with patch(
                "trendradar.report.weekly_pdf.generate_pdf_from_html",
                side_effect=create_pdf,
            ), patch(
                "trendradar.report.weekly_pdf._validate_weekly_pdf"
            ), patch(
                "trendradar.report.weekly_pdf.os.replace",
                side_effect=fail_pdf_replace_once,
            ):
                with self.assertRaisesRegex(OSError, "PDF replace"):
                    build_weekly_pdf(
                        tmp, date(2026, 8, 3), date(2026, 8, 10), "new html"
                    )

            self.assertEqual(final_pdf.read_bytes(), b"%PDF-old")
            self.assertEqual(final_html.read_text(encoding="utf-8"), "old html")
            self.assertEqual(list(final_pdf.parent.glob("*.tmp*")), [])

    def test_rollback_failure_preserves_recoverable_backup_and_cleans_others(self):
        with TemporaryDirectory() as tmp:
            final_pdf = Path(tmp) / "pdf" / "2026-08-10" / (
                "农业育种新闻周报_三模块_2026-08-03至2026-08-09.pdf"
            )
            final_html = final_pdf.with_suffix(".html")
            final_pdf.parent.mkdir(parents=True)
            final_pdf.write_bytes(b"%PDF-old")
            final_html.write_text("old html", encoding="utf-8")

            def create_pdf(_html, pdf):
                Path(pdf).write_bytes(b"%PDF-new")
                return pdf

            real_replace = os.replace
            pdf_replace_failed = False

            def fail_pdf_and_html_rollback(source, destination):
                nonlocal pdf_replace_failed
                source = Path(source)
                destination = Path(destination)
                if destination == final_pdf and not pdf_replace_failed:
                    pdf_replace_failed = True
                    raise OSError("simulated PDF replace failure")
                if destination == final_html and source.name.endswith(
                    ".tmp.backup.html"
                ):
                    raise OSError("simulated HTML rollback failure")
                return real_replace(source, destination)

            with patch(
                "trendradar.report.weekly_pdf.generate_pdf_from_html",
                side_effect=create_pdf,
            ), patch(
                "trendradar.report.weekly_pdf._validate_weekly_pdf"
            ), patch(
                "trendradar.report.weekly_pdf.os.replace",
                side_effect=fail_pdf_and_html_rollback,
            ), self.assertLogs(
                "trendradar.report.weekly_pdf", level="WARNING"
            ) as logs:
                with self.assertRaisesRegex(OSError, "PDF replace"):
                    build_weekly_pdf(
                        tmp, date(2026, 8, 3), date(2026, 8, 10), "new html"
                    )

            backups = list(final_pdf.parent.glob("*.tmp.backup.html"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "old html")
            self.assertEqual(final_pdf.read_bytes(), b"%PDF-old")
            self.assertEqual(final_html.read_text(encoding="utf-8"), "new html")
            self.assertEqual(
                list(final_pdf.parent.glob("*.tmp.backup.pdf")), []
            )
            self.assertTrue(any("保留" in message for message in logs.output))
            self.assertTrue(
                any(str(backups[0]) in message for message in logs.output)
            )

    def test_backup_cleanup_failure_warns_but_keeps_successful_formal_pair(self):
        with TemporaryDirectory() as tmp:
            final_pdf = Path(tmp) / "pdf" / "2026-08-10" / (
                "农业育种新闻周报_三模块_2026-08-03至2026-08-09.pdf"
            )
            final_html = final_pdf.with_suffix(".html")
            final_pdf.parent.mkdir(parents=True)
            final_pdf.write_bytes(b"%PDF-old")
            final_html.write_text("old html", encoding="utf-8")

            def create_pdf(_html, pdf):
                Path(pdf).write_bytes(b"%PDF-new")
                return pdf

            real_replace = os.replace
            real_unlink = Path.unlink
            formal_pdf_replaced = False

            def track_replace(source, destination):
                nonlocal formal_pdf_replaced
                result = real_replace(source, destination)
                if Path(destination) == final_pdf:
                    formal_pdf_replaced = True
                return result

            def fail_html_backup_cleanup(path, *args, **kwargs):
                if formal_pdf_replaced and path.name.endswith(
                    ".tmp.backup.html"
                ):
                    raise OSError("simulated backup unlink failure")
                return real_unlink(path, *args, **kwargs)

            with patch(
                "trendradar.report.weekly_pdf.generate_pdf_from_html",
                side_effect=create_pdf,
            ), patch(
                "trendradar.report.weekly_pdf._validate_weekly_pdf"
            ), patch(
                "trendradar.report.weekly_pdf.os.replace",
                side_effect=track_replace,
            ), patch.object(
                Path, "unlink", autospec=True,
                side_effect=fail_html_backup_cleanup,
            ), self.assertLogs(
                "trendradar.report.weekly_pdf", level="WARNING"
            ) as logs:
                result = build_weekly_pdf(
                    tmp, date(2026, 8, 3), date(2026, 8, 10), "new html"
                )

            self.assertEqual(Path(result), final_pdf)
            self.assertEqual(final_pdf.read_bytes(), b"%PDF-new")
            self.assertEqual(final_html.read_text(encoding="utf-8"), "new html")
            backup_html = list(final_pdf.parent.glob("*.tmp.backup.html"))
            self.assertEqual(len(backup_html), 1)
            self.assertEqual(
                list(final_pdf.parent.glob("*.tmp.backup.pdf")), []
            )
            self.assertTrue(any("清理失败" in message for message in logs.output))


class WeeklyPdfGenerationValidationTests(unittest.TestCase):
    def test_poppler_commands_support_windows_paths_and_force_utf8(self):
        with TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "report.pdf"
            pdf.write_bytes(b"%PDF-valid")
            responses = [
                subprocess.CompletedProcess(
                    [], 0, "Pages: 2\nPage size: 595 x 842 pts\n", ""
                ),
                subprocess.CompletedProcess([], 0, "农业育种新闻周报", ""),
            ]
            with patch.dict(
                os.environ,
                {
                    "PDFINFO_BIN": r"C:\\poppler\\Library\\bin\\pdfinfo.exe",
                    "PDFTOTEXT_BIN": r"C:\\poppler\\Library\\bin\\pdftotext.exe",
                },
            ), patch(
                "trendradar.report.weekly_pdf.subprocess.run",
                side_effect=responses,
            ) as run:
                _validate_weekly_pdf(pdf)

        info_call, text_call = run.call_args_list
        self.assertEqual(
            info_call.args[0][0], r"C:\\poppler\\Library\\bin\\pdfinfo.exe"
        )
        self.assertEqual(
            text_call.args[0],
            [
                r"C:\\poppler\\Library\\bin\\pdftotext.exe",
                "-enc", "UTF-8", str(pdf), "-",
            ],
        )
        self.assertEqual(info_call.kwargs["encoding"], "utf-8")
        self.assertEqual(text_call.kwargs["encoding"], "utf-8")

    def test_long_weather_content_stays_above_repeated_page_furniture(self):
        period = "2026-08-03—2026-08-09"
        html = render_weekly_pdf_html(
            policy_items=[],
            research_items=[],
            ai_analysis=None,
            agro_weather=SimpleNamespace(
                title="全国农业气象周报",
                impact="气象影响正文。" * 260,
                outlook="未来十天天气展望。" * 80,
                recommendations="及时排涝散墒。" * 40,
                risk_regions=("东北", "华北"),
                risk_crops=("水稻", "玉米"),
                source_url="https://www.nmc.cn/publish/agro/ten-week/index.html",
            ),
            period_label=period,
            generated_at=datetime(2026, 8, 10, 10, 0),
        )
        with TemporaryDirectory() as tmp:
            pdf = Path(build_weekly_pdf(
                tmp, date(2026, 8, 3), date(2026, 8, 10), html
            ))
            bbox = Path(tmp) / "report.xml"
            subprocess.run(
                ["pdftotext", "-bbox-layout", str(pdf), str(bbox)],
                check=True,
            )
            pages = [
                node
                for node in ElementTree.parse(bbox).getroot().iter()
                if node.tag.endswith("page")
            ]

        checked_pages = 0
        for page in pages:
            words = [
                (
                    float(node.attrib["yMin"]),
                    (node.text or "").strip(),
                )
                for node in page.iter()
                if node.tag.endswith("word")
            ]
            repeated_headers = [
                y
                for y, text in words
                if text == "农业育种新闻周报"
            ]
            if not repeated_headers:
                continue
            checked_pages += 1
            self.assertTrue(
                all(y < 80 for y in repeated_headers), repeated_headers
            )
            bottom_words = [
                text
                for y, text in words
                if y > 790
            ]
            self.assertTrue(
                all(
                    text in {"第", "页"} or text.isdigit()
                    for text in bottom_words
                ),
                bottom_words[:5],
            )
        self.assertGreater(checked_pages, 0)

    def test_actual_chromium_output_is_a4_multipage_with_repeated_chinese_furniture(self):
        self.assertTrue(
            shutil.which("chromium") or shutil.which("chromium-browser")
            or shutil.which("google-chrome")
        )
        period = "2026-08-03 00:00—2026-08-10 00:00"
        def module_items(module):
            return [{
                "title": f"{module}水稻育种技术进展 {index}",
                "url": f"https://example.com/{module}/{index}",
                "source_name": "测试来源",
                "published_at": "2026-08-09",
                "ai_summary": "中文摘要内容" * 80,
                "module_rank": index,
            } for index in range(1, 21)]

        html = render_weekly_pdf_html(
            policy_items=module_items("政策"),
            research_items=module_items("科研"),
            ai_analysis=SimpleNamespace(
                success=True, policy_trends="政策趋势 [policy:1]",
                research_trends="科研趋势 [research:1]",
                weather_risks="暂无气象证据",
            ),
            agro_weather=None,
            period_label=period,
            generated_at=datetime(2026, 8, 10, 10, 0),
        )
        with TemporaryDirectory() as tmp:
            pdf = Path(build_weekly_pdf(
                tmp, date(2026, 8, 3), date(2026, 8, 10), html
            ))
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
            self.assertGreater(pdf.stat().st_size, 5)
            self.assertLessEqual(pdf.stat().st_size, 20 * 1024 * 1024)
            info = subprocess.run(
                ["pdfinfo", str(pdf)], capture_output=True, text=True, check=True,
            ).stdout
            pages = re.search(r"Pages:\s+(\d+)", info)
            self.assertIsNotNone(pages, info)
            self.assertGreaterEqual(int(pages.group(1)), 2)
            self.assertRegex(info, r"Page size:\s+59[4-6](?:\.\d+)? x 84[1-2](?:\.\d+)? pts")
            text = subprocess.run(
                ["pdftotext", str(pdf), "-"],
                capture_output=True, text=True, check=True,
            ).stdout
            self.assertIn("政策水稻育种技术进展 1", text)
            self.assertIn("科研水稻育种技术进展 20", text)
            header = rf"农业育种新闻周报\s+周期：{re.escape(period)}"
            self.assertGreaterEqual(len(re.findall(header, text)), 2)
            self.assertRegex(text, r"第\s*1\s*页")
            self.assertRegex(text, r"第\s*2\s*页")

    def test_invalid_pdf_is_deleted(self):
        from trendradar.report.pdf import generate_pdf_from_html

        with TemporaryDirectory() as tmp:
            html = Path(tmp) / "report.html"
            pdf = Path(tmp) / "report.pdf"
            html.write_text("<html></html>", encoding="utf-8")

            def invalid_output(command, **kwargs):
                output = next(
                    value for value in command if value.startswith("--print-to-pdf=")
                )
                Path(output.split("=", 1)[1]).write_bytes(b"not a PDF")
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
                output = next(
                    value for value in command if value.startswith("--print-to-pdf=")
                )
                with Path(output.split("=", 1)[1]).open("wb") as file:
                    file.write(b"%PDF-1.4\n")
                    file.truncate(MAX_PDF_BYTES + 1)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("trendradar.report.pdf.shutil.which", return_value="chromium"), patch(
                "trendradar.report.pdf.subprocess.run", side_effect=oversized_output
            ):
                with self.assertRaisesRegex(RuntimeError, "20MB"):
                    generate_pdf_from_html(str(html), str(pdf))
            self.assertFalse(pdf.exists())

class WeeklyPdfAnalyzerIntegrationTests(unittest.TestCase):
    def _weekly_pipeline_analyzer(self, filter_method):
        from trendradar.__main__ import NewsAnalyzer

        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config={
                "AI_FILTER": {"MIN_SCORE": 0.5},
                "AI_ANALYSIS": {"ENABLED": False},
                "AI_TRANSLATION": {"ENABLED": False},
                "STORAGE": {"FORMATS": {"HTML": False}},
            },
            display_mode="keyword", platform_ids=[],
            count_frequency=lambda *args, **kwargs: ([], 0),
        )
        analyzer.filter_method = filter_method
        analyzer.interests_file = None
        analyzer._rss_window = SimpleNamespace(
            start=pytz.timezone("Asia/Shanghai").localize(datetime(2026, 8, 3)),
            end=pytz.timezone("Asia/Shanghai").localize(datetime(2026, 8, 10)),
            label="2026-08-03—2026-08-09",
        )
        analyzer._allowed_rss_ids = set()
        analyzer._rss_ids_authoritative = False
        analyzer._agro_weather_report = SimpleNamespace(title="气象")
        analyzer._weekly_ai_filter_succeeded = False
        analyzer._weekly_news_modules = WeeklyNewsSelection(
            policy=[], research=[]
        )
        analyzer._rss_total_count = 0
        analyzer._rss_source_total = 0
        analyzer._rss_source_failed = 0
        analyzer._operation_run_at = lambda: pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 10, 10, 0)
        )
        return analyzer

    @staticmethod
    def _news_group():
        return [{"word": "育种", "count": 1, "titles": [{
            "title": "普通新闻", "url": "https://example.com/news",
            "ai_summary": "摘要", "module_type": "research",
            "relevance_score": 0.8, "importance_score": 0.8,
            "content_level": "summary",
        }]}]

    def test_keyword_weekly_news_fails_closed_without_pdf(self):
        analyzer = self._weekly_pipeline_analyzer("keyword")
        analyzer.ctx.count_frequency = MagicMock(return_value=([], 0))
        with patch("trendradar.__main__.build_weekly_pdf") as build:
            with self.assertRaisesRegex(RuntimeError, "仅支持严格 AI 筛选"):
                analyzer._run_analysis_pipeline(
                    {}, "weekly", {}, {}, [], [], {}, rss_items=self._news_group()
                )
        build.assert_not_called()
        analyzer.ctx.count_frequency.assert_not_called()

    def test_failed_strict_ai_filter_does_not_generate_pdf(self):
        analyzer = self._weekly_pipeline_analyzer("ai")
        analyzer.ctx.run_ai_filter = lambda **kwargs: SimpleNamespace(
            success=False, error="boom"
        )
        with patch("trendradar.__main__.build_weekly_pdf") as build:
            with self.assertRaisesRegex(RuntimeError, "AI 筛选失败"):
                analyzer._run_analysis_pipeline(
                    {}, "weekly", {}, {}, [], [], {}, rss_items=self._news_group()
                )
        build.assert_not_called()

    def test_successful_strict_ai_filter_is_required_before_pdf(self):
        analyzer = self._weekly_pipeline_analyzer("ai")
        captured = {}

        def run_filter(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(success=True, total_matched=1, tags=["育种"])

        analyzer.ctx.run_ai_filter = run_filter
        analyzer.ctx.convert_ai_filter_to_report_data = lambda *args, **kwargs: (
            [], self._news_group(), []
        )
        with patch("trendradar.__main__.render_weekly_pdf_html", return_value="<html>") as render, patch(
            "trendradar.__main__.build_weekly_pdf", return_value="output/report.pdf"
        ) as build:
            analyzer._run_analysis_pipeline({}, "weekly", {}, {}, [], [], {})
        self.assertTrue(captured["strict"])
        self.assertTrue(analyzer._weekly_ai_filter_succeeded)
        render.assert_called_once()
        build.assert_called_once()

    def test_real_renderer_accepts_twenty_items_and_five_highlights_per_module(self):
        from trendradar.__main__ import NewsAnalyzer

        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer._weekly_news_modules = WeeklyNewsSelection(
            policy=[{
                "title": f"政策新闻 {index}",
                "url": f"https://example.org/policy/{index}",
                "module_type": "policy",
                "module_rank": index,
                **({"highlight_rank": index} if index <= 5 else {}),
            } for index in range(1, 21)],
            research=[{
                "title": f"科研新闻 {index}",
                "url": f"https://example.org/research/{index}",
                "module_type": "research",
                "module_rank": index,
                **({"highlight_rank": index} if index <= 5 else {}),
            } for index in range(1, 21)],
        )
        analyzer._weekly_ai_filter_succeeded = True
        analyzer._agro_weather_report = None
        analyzer._rss_window = SimpleNamespace(
            start=pytz.timezone("Asia/Shanghai").localize(datetime(2026, 8, 3)),
            end=pytz.timezone("Asia/Shanghai").localize(datetime(2026, 8, 10)),
            label="2026-08-03—2026-08-09",
        )
        analyzer._operation_run_at = lambda: pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 10, 10, 0)
        )

        with patch(
            "trendradar.__main__.build_weekly_pdf",
            return_value="output/report.pdf",
        ) as build:
            try:
                analyzer._generate_weekly_pdf_report([], None)
            except (TypeError, ValueError) as exc:
                self.fail(f"真实 renderer 拒绝双模块已选结果: {exc}")

        html = build.call_args.args[3]
        for index in range(1, 21):
            self.assertIn(f"政策新闻 {index}", html)
            self.assertIn(f"科研新闻 {index}", html)
            self.assertEqual(
                html.count(f'href="https://example.org/policy/{index}"'), 1
            )
            self.assertEqual(
                html.count(f'href="https://example.org/research/{index}"'), 1
            )
        self.assertEqual(html.count("重点政策"), 5)
        self.assertEqual(html.count("重点文献"), 5)

    def test_missing_weekly_selection_state_fails_closed(self):
        from trendradar.__main__ import NewsAnalyzer

        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer._weekly_ai_filter_succeeded = True
        analyzer._agro_weather_report = SimpleNamespace(title="气象")
        analyzer._rss_window = SimpleNamespace(
            start=pytz.timezone("Asia/Shanghai").localize(datetime(2026, 8, 3)),
            end=pytz.timezone("Asia/Shanghai").localize(datetime(2026, 8, 10)),
            label="2026-08-03—2026-08-09",
        )
        analyzer._operation_run_at = lambda: pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 10, 10, 0)
        )

        with patch("trendradar.__main__.build_weekly_pdf") as build:
            with self.assertRaisesRegex(RuntimeError, "WeeklyNewsSelection"):
                analyzer._generate_weekly_pdf_report([], None)
        build.assert_not_called()

    def test_weather_only_weekly_report_still_builds_dedicated_pdf(self):
        from trendradar.__main__ import NewsAnalyzer

        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer._weekly_news_modules = WeeklyNewsSelection(
            policy=[], research=[]
        )
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
        analyzer._weekly_news_modules = WeeklyNewsSelection(
            policy=[], research=[]
        )
        analyzer._rss_window = SimpleNamespace(
            start=pytz.timezone("Asia/Shanghai").localize(datetime(2026, 8, 3)),
            end=pytz.timezone("Asia/Shanghai").localize(datetime(2026, 8, 10)),
            label="2026-08-03—2026-08-09",
        )
        analyzer._agro_weather_report = None
        with self.assertRaisesRegex(RuntimeError, "新闻和农业气象"):
            analyzer._generate_weekly_pdf_report([], None)

    def test_news_weekly_report_requires_successful_strict_ai_filter(self):
        from trendradar.__main__ import NewsAnalyzer

        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer._weekly_news_modules = WeeklyNewsSelection(
            policy=[],
            research=[{
                "title": "普通新闻",
                "url": "https://example.com/news",
                "module_type": "research",
            }],
        )
        analyzer._agro_weather_report = None
        analyzer._weekly_ai_filter_succeeded = False
        with self.assertRaisesRegex(RuntimeError, "严格 AI 筛选"):
            analyzer._generate_weekly_pdf_report(
                [],
                None,
            )

if __name__ == "__main__":
    unittest.main()
