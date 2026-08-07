import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from trendradar.notification.dispatcher import NotificationDispatcher


class EmailDeliveryTests(unittest.TestCase):
    def dispatch_email(self, refused_recipients, require_all_targets):
        config = {
            "MAX_ACCOUNTS_PER_CHANNEL": 3,
            "EMAIL_FROM": "sender@example.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_TO": "first@example.com, second@example.com",
            "EMAIL_SMTP_SERVER": "smtp.example.com",
            "EMAIL_SMTP_PORT": 587,
            "DISPLAY": {"REGIONS": {}},
        }
        dispatcher = NotificationDispatcher(
            config,
            lambda: datetime(2026, 8, 10, 10, 0),
            MagicMock(),
        )

        with TemporaryDirectory() as temp_dir:
            html_path = Path(temp_dir) / "report.html"
            html_path.write_text("<html><body>weekly report</body></html>", encoding="utf-8")

            with patch("trendradar.notification.senders.smtplib.SMTP") as smtp_class:
                server = smtp_class.return_value
                server.send_message.return_value = refused_recipients
                results = dispatcher.dispatch_all(
                    report_data={},
                    report_type="自然周周报",
                    mode="weekly" if require_all_targets else "daily",
                    html_file_path=str(html_path),
                    require_all_targets=require_all_targets,
                )

        server.send_message.assert_called_once()
        sent_message = server.send_message.call_args.args[0]
        self.assertEqual(
            sent_message["To"],
            "first@example.com, second@example.com",
        )
        return results["email"]

    def test_partial_refusal_requires_all_for_weekly_but_not_daily(self):
        refused = {
            "second@example.com": (550, b"mailbox unavailable"),
        }

        self.assertFalse(self.dispatch_email(refused, require_all_targets=True))
        self.assertTrue(self.dispatch_email(refused, require_all_targets=False))

    def test_all_recipients_refused_fails_in_both_modes(self):
        refused = {
            "first@example.com": (550, b"mailbox unavailable"),
            "second@example.com": (550, b"mailbox unavailable"),
        }

        self.assertFalse(self.dispatch_email(refused, require_all_targets=True))
        self.assertFalse(self.dispatch_email(refused, require_all_targets=False))

    def test_all_recipients_accepted_succeeds_in_both_modes(self):
        self.assertTrue(self.dispatch_email({}, require_all_targets=True))
        self.assertTrue(self.dispatch_email({}, require_all_targets=False))


if __name__ == "__main__":
    unittest.main()
