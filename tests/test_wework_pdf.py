import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from trendradar.notification.wework_pdf import (
    MAX_WEWORK_FILE_BYTES,
    send_wework_file,
    upload_wework_file,
)
from trendradar.report.pdf import generate_pdf_from_html


def _response(payload, status_code=200):
    response = MagicMock(status_code=status_code)
    response.json.return_value = payload
    return response


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


if __name__ == "__main__":
    unittest.main()
