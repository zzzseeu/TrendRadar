import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from trendradar.notification.senders import send_to_wework
from trendradar.core.loader import _load_webhook_config
from trendradar.notification.wework_pdf import (
    MAX_WEWORK_FILE_BYTES,
    build_wework_pdf_preview,
    collect_highlights,
    send_wework_file,
    upload_wework_file,
)
from trendradar.report.pdf import generate_pdf_from_html


def _response(payload, status_code=200):
    response = MagicMock(status_code=status_code)
    response.json.return_value = payload
    return response


class WeWorkPdfPreviewTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            {
                "title": f"重点新闻 {rank}",
                "url": f"https://example.com/{rank}",
                "source_name": "测试源",
                "highlight_rank": rank,
                "ai_summary": f"这是第 {rank} 条新闻的人工智能摘要。",
            }
            for rank in range(1, 7)
        ]

    def test_collect_highlights_sorts_and_deduplicates_across_regions(self):
        duplicate = dict(self.items[0])
        duplicate["source_name"] = "重复分组"
        report_data = {
            "stats": [{"word": "标签", "titles": [self.items[2], duplicate]}]
        }
        rss_items = [
            {
                "word": "RSS",
                "titles": [
                    self.items[4],
                    self.items[1],
                    self.items[0],
                    self.items[3],
                    self.items[5],
                ],
            }
        ]

        highlights = collect_highlights(report_data, rss_items, limit=5)

        self.assertEqual(
            [item["highlight_rank"] for item in highlights],
            [1, 2, 3, 4, 5],
        )
        self.assertEqual(len({item["url"] for item in highlights}), 5)

    def test_preview_contains_brief_summary_and_exactly_five_highlights(self):
        report_data = {
            "stats": [],
            "rss_matched_count": 26,
            "rss_total_count": 120,
            "rss_source_total": 25,
            "rss_source_failed": 0,
        }
        rss_items = [{"word": "RSS", "titles": self.items}]
        ai_analysis = SimpleNamespace(
            success=True,
            core_trends="育种行业关注点集中在种质创新、抗逆性与基因编辑监管。",
        )
        self.items[0]["reader_url"] = (
            "https://r.jina.ai/http://www.sciencedirect.com/science/article/pii/"
            "S1672630826000879"
        )

        preview = build_wework_pdf_preview(
            report_data=report_data,
            rss_items=rss_items,
            ai_analysis=ai_analysis,
            report_type="当前榜单",
            top_n=5,
            max_bytes=4000,
        )

        self.assertIn("育种行业关注点集中", preview)
        self.assertIn("RSS：26 / 120", preview)
        for rank in range(1, 6):
            self.assertIn(f"重点新闻 {rank}", preview)
        self.assertNotIn("重点新闻 6", preview)
        self.assertIn("完整报告见 PDF 附件", preview)
        self.assertIn("📖 备用阅读", preview)
        self.assertIn(self.items[0]["reader_url"], preview)
        self.assertLessEqual(len(preview.encode("utf-8")), 4000)


class WeWorkPdfConfigTests(unittest.TestCase):
    def test_environment_enables_pdf_and_limits_top_n(self):
        config_data = {
            "notification": {
                "channels": {
                    "wework": {
                        "pdf_enabled": False,
                        "pdf_top_n": 3,
                    }
                }
            }
        }
        with patch.dict(
            os.environ,
            {"WEWORK_PDF_ENABLED": "true", "WEWORK_PDF_TOP_N": "50"},
            clear=False,
        ):
            config = _load_webhook_config(config_data)

        self.assertTrue(config["WEWORK_PDF_ENABLED"])
        self.assertEqual(config["WEWORK_PDF_TOP_N"], 10)


class PdfGenerationTests(unittest.TestCase):
    def test_generate_pdf_uses_headless_chromium_and_validates_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "report.html"
            html_path.write_text("<html><body>报告</body></html>", encoding="utf-8")

            def fake_run(command, **kwargs):
                output_arg = next(
                    value for value in command if value.startswith("--print-to-pdf=")
                )
                Path(output_arg.split("=", 1)[1]).write_bytes(
                    b"%PDF-1.4\n" + b"x" * 64
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch("trendradar.report.pdf.shutil.which", return_value="/usr/bin/chromium"),
                patch("trendradar.report.pdf.subprocess.run", side_effect=fake_run) as run,
            ):
                pdf_path = generate_pdf_from_html(str(html_path))

            self.assertTrue(Path(pdf_path).is_file())
            command = run.call_args.args[0]
            self.assertIn("--headless", command)
            self.assertIn(html_path.resolve().as_uri(), command)

    def test_generate_pdf_fails_when_chromium_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "report.html"
            html_path.write_text("<html></html>", encoding="utf-8")

            with patch("trendradar.report.pdf.shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "Chromium"):
                    generate_pdf_from_html(str(html_path))


class WeWorkFileApiTests(unittest.TestCase):
    def test_upload_pdf_returns_media_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "report.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n" + b"x" * 64)
            captured = {}

            def fake_post(url, **kwargs):
                captured["url"] = url
                captured["params"] = kwargs["params"]
                media = kwargs["files"]["media"]
                captured["filename"] = media[0]
                captured["content"] = media[1].read()
                captured["content_type"] = media[2]
                return _response({"errcode": 0, "media_id": "media-123"})

            with patch(
                "trendradar.notification.wework_pdf.requests.post",
                side_effect=fake_post,
            ):
                media_id = upload_wework_file(
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-key",
                    str(pdf_path),
                )

            self.assertEqual(media_id, "media-123")
            self.assertTrue(captured["url"].endswith("/cgi-bin/webhook/upload_media"))
            self.assertEqual(
                captured["params"],
                {"key": "secret-key", "type": "file"},
            )
            self.assertEqual(captured["filename"], "report.pdf")
            self.assertTrue(captured["content"].startswith(b"%PDF"))
            self.assertEqual(captured["content_type"], "application/pdf")

    def test_upload_rejects_files_over_twenty_megabytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "too-large.pdf"
            with pdf_path.open("wb") as file:
                file.truncate(MAX_WEWORK_FILE_BYTES + 1)

            with self.assertRaisesRegex(ValueError, "20MB"):
                upload_wework_file(
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-key",
                    str(pdf_path),
                )

    def test_send_file_posts_media_id_payload(self):
        with patch(
            "trendradar.notification.wework_pdf.requests.post",
            return_value=_response({"errcode": 0}),
        ) as post:
            result = send_wework_file(
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-key",
                "media-123",
            )

        self.assertTrue(result)
        self.assertEqual(
            post.call_args.kwargs["json"],
            {"msgtype": "file", "file": {"media_id": "media-123"}},
        )


class WeWorkSenderPdfModeTests(unittest.TestCase):
    def setUp(self):
        self.base_kwargs = {
            "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-key",
            "report_data": {"stats": [], "failed_ids": [], "new_titles": []},
            "report_type": "当前榜单",
            "split_content_func": MagicMock(return_value=["原始完整消息"]),
            "rss_items": [],
            "ai_analysis": SimpleNamespace(success=True, core_trends="摘要"),
            "msg_type": "markdown",
            "pdf_enabled": True,
            "pdf_top_n": 5,
            "html_file_path": "output/html/report.html",
        }

    def test_pdf_success_skips_original_multi_batch_message(self):
        with patch(
            "trendradar.notification.senders.send_wework_pdf_report",
            return_value=True,
        ) as send_pdf:
            result = send_to_wework(**self.base_kwargs)

        self.assertTrue(result)
        send_pdf.assert_called_once()
        self.base_kwargs["split_content_func"].assert_not_called()

    def test_pdf_failure_falls_back_to_original_multi_batch_message(self):
        with (
            patch(
                "trendradar.notification.senders.send_wework_pdf_report",
                return_value=False,
            ),
            patch(
                "trendradar.notification.senders.requests.post",
                return_value=_response({"errcode": 0}),
            ) as post,
            patch(
                "trendradar.notification.senders._render_ai_analysis",
                return_value="摘要",
            ),
        ):
            result = send_to_wework(**self.base_kwargs)

        self.assertTrue(result)
        self.base_kwargs["split_content_func"].assert_called_once()
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
