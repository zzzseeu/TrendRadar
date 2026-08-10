# coding=utf-8
"""Regression coverage for the strict weekly WeCom PDF-only delivery path."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytz

from trendradar.__main__ import NewsAnalyzer
from trendradar.core.weekly import previous_natural_week
from trendradar.notification.dispatcher import NotificationDispatcher
from trendradar.notification.wework_pdf import (
    MAX_WEWORK_FILE_BYTES,
    send_wework_pdf_file,
)


WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test"


def response(payload, status_code=200):
    result = MagicMock(status_code=status_code)
    result.json.return_value = payload
    return result


def schedule(**overrides):
    values = {
        "period_key": "monday_weekly",
        "period_name": "自然周周报",
        "push": True,
        "report_mode": "weekly",
        "once_push": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class WeeklyPdfDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.schedule = schedule()

    def tearDown(self):
        self.tempdir.cleanup()

    def _pdf(self, name="weekly.pdf"):
        path = Path(self.tempdir.name) / name
        path.write_bytes(b"%PDF-1.4\n" + b"x" * 128)
        return path

    def _dispatcher(self, webhook=WEBHOOK):
        return NotificationDispatcher(
            {"WEWORK_WEBHOOK_URL": webhook, "MAX_ACCOUNTS_PER_CHANNEL": 3},
            lambda: None,
            MagicMock(),
        )

    def test_weekly_delivery_uploads_and_sends_only_one_file_message(self):
        pdf_path = self._pdf()
        responses = [
            response({"errcode": 0, "media_id": "media-1"}),
            response({"errcode": 0}),
        ]
        with patch(
            "trendradar.notification.wework_pdf.requests.post",
            side_effect=responses,
        ) as post:
            ok = send_wework_pdf_file(WEBHOOK, str(pdf_path))

        self.assertTrue(ok)
        self.assertEqual(post.call_count, 2)
        self.assertIn("upload_media", post.call_args_list[0].args[0])
        self.assertEqual(
            post.call_args_list[1].kwargs["json"],
            {"msgtype": "file", "file": {"media_id": "media-1"}},
        )
        payloads = [call.kwargs.get("json") for call in post.call_args_list]
        self.assertFalse(any(
            payload and payload.get("msgtype") in {"text", "markdown"}
            for payload in payloads
        ))

    def test_upload_failure_does_not_send_a_file_message(self):
        with patch(
            "trendradar.notification.wework_pdf.requests.post",
            return_value=response({"errcode": 1}),
        ) as post:
            with self.assertRaisesRegex(RuntimeError, "上传失败"):
                send_wework_pdf_file(WEBHOOK, str(self._pdf()))

        self.assertEqual(post.call_count, 1)

    def test_file_send_failure_returns_false_without_text_fallback(self):
        with patch(
            "trendradar.notification.wework_pdf.requests.post",
            side_effect=[
                response({"errcode": 0, "media_id": "media-1"}),
                response({"errcode": 1}),
            ],
        ) as post:
            self.assertFalse(send_wework_pdf_file(WEBHOOK, str(self._pdf())))

        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[1].kwargs["json"]["msgtype"], "file")

    def test_dispatcher_requires_every_configured_wework_account(self):
        dispatcher = self._dispatcher(f"{WEBHOOK};{WEBHOOK}2")
        with patch(
            "trendradar.notification.dispatcher.send_wework_pdf_file",
            side_effect=[True, False],
        ) as send:
            self.assertFalse(dispatcher.dispatch_weekly_pdf(str(self._pdf())))

        self.assertEqual(send.call_count, 2)

    def test_dispatcher_rejects_missing_wework_webhook(self):
        dispatcher = self._dispatcher("")
        with patch(
            "trendradar.notification.dispatcher.send_wework_pdf_file",
        ) as send:
            self.assertFalse(dispatcher.dispatch_weekly_pdf(str(self._pdf())))

        send.assert_not_called()

    def test_over_limit_pdf_never_reaches_wework(self):
        pdf_path = Path(self.tempdir.name) / "too-large.pdf"
        with pdf_path.open("wb") as file:
            file.truncate(MAX_WEWORK_FILE_BYTES + 1)
        with patch("trendradar.notification.wework_pdf.requests.post") as post:
            with self.assertRaisesRegex(ValueError, "20MB"):
                send_wework_pdf_file(WEBHOOK, str(pdf_path))
        post.assert_not_called()

    def test_invalid_pdf_never_reaches_wework(self):
        invalid_pdf = Path(self.tempdir.name) / "invalid.pdf"
        invalid_pdf.write_bytes(b"not a PDF")
        with patch("trendradar.notification.wework_pdf.requests.post") as post:
            with self.assertRaisesRegex(ValueError, "无效"):
                send_wework_pdf_file(WEBHOOK, str(invalid_pdf))
        post.assert_not_called()

    def test_pdf_failure_never_calls_text_sender_or_records_checkpoint(self):
        scheduler = MagicMock()
        dispatcher = MagicMock()
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            create_notification_dispatcher=MagicMock(return_value=dispatcher),
            create_scheduler=MagicMock(return_value=scheduler),
        )
        analyzer._weekly_pdf_path = None

        self.assertFalse(analyzer._deliver_weekly_pdf(self.schedule))
        dispatcher.dispatch_weekly_pdf.assert_not_called()
        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_successful_pdf_delivery_records_window_end_checkpoint(self):
        scheduler = MagicMock()
        scheduler.already_executed.return_value = False
        scheduler.record_execution.return_value = True
        dispatcher = MagicMock()
        dispatcher.dispatch_weekly_pdf.return_value = True
        run_at = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 12, 15, 0)
        )
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            create_notification_dispatcher=MagicMock(return_value=dispatcher),
            create_scheduler=MagicMock(return_value=scheduler),
            timezone="Asia/Shanghai",
            config={},
        )
        analyzer.proxy_url = ""
        analyzer._run_at = run_at
        analyzer._rss_window = previous_natural_week(run_at, "Asia/Shanghai")
        analyzer._weekly_pdf_path = str(self._pdf())

        self.assertTrue(analyzer._deliver_weekly_pdf(self.schedule))
        dispatcher.dispatch_weekly_pdf.assert_called_once_with(
            str(self._pdf()), ""
        )
        scheduler.record_execution.assert_called_once_with(
            "monday_weekly", "push", "2026-08-10"
        )

    def test_same_week_checkpoint_prevents_a_second_dispatch(self):
        scheduler = MagicMock()
        scheduler.already_executed.return_value = True
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            cleanup=MagicMock(),
            config={"DEBUG": False},
            timezone="Asia/Shanghai",
            get_time=MagicMock(return_value=pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 12, 15, 0)
            )),
            create_scheduler=MagicMock(return_value=scheduler),
            create_notification_dispatcher=MagicMock(),
        )
        analyzer._resolve_and_apply_schedule = MagicMock(return_value=self.schedule)
        lock = MagicMock()
        lock.acquire.return_value = True
        analyzer._create_weekly_attempt_lock = MagicMock(return_value=lock)
        analyzer._fetch_agro_weather = MagicMock()

        self.assertTrue(analyzer.run())
        analyzer._fetch_agro_weather.assert_not_called()
        analyzer.ctx.create_notification_dispatcher.assert_not_called()


if __name__ == "__main__":
    unittest.main()
