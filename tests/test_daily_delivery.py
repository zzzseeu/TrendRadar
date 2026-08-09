import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytz
from botocore.exceptions import ClientError

from trendradar.core.scheduler import Scheduler
from trendradar.storage.local import LocalStorageBackend
from trendradar.storage.remote import RemoteStorageBackend


def shanghai(year, month, day, hour, minute):
    return pytz.timezone("Asia/Shanghai").localize(
        datetime(year, month, day, hour, minute)
    )


TIMELINE = {"custom": {
    "default": {
        "collect": True, "analyze": False, "push": False,
        "report_mode": "current", "ai_mode": "follow_report",
        "once": {"analyze": False, "push": False},
    },
    "periods": {},
    "day_plans": {"default": {"periods": []}},
    "week_map": {day: "default" for day in range(1, 8)},
}}


class DailyDeliveryCheckpointTests(unittest.TestCase):
    def test_latest_success_checkpoint_crosses_daily_databases(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(data_dir=tmp, timezone="Asia/Shanghai")
            times = {
                "2026-08-07": shanghai(2026, 8, 7, 10, 0),
                "2026-08-08": shanghai(2026, 8, 8, 10, 2),
                "2026-08-09": shanghai(2026, 8, 9, 10, 4),
            }
            for date_str, now in times.items():
                with patch.object(backend, "_get_configured_time", return_value=now):
                    self.assertTrue(backend.record_period_execution(
                        date_str, "daily_delivery", "push"
                    ))

            self.assertEqual(
                backend.get_latest_period_execution(
                    "daily_delivery", "push", "2026-08-08"
                ),
                "2026-08-08 10:02:00",
            )
            self.assertIsNone(backend.get_latest_period_execution(
                "other", "push", "2026-08-09"
            ))
            backend.cleanup()

    def test_remote_checkpoint_lists_daily_databases_in_reverse_and_stops_at_first(self):
        backend = RemoteStorageBackend.__new__(RemoteStorageBackend)
        backend.bucket_name = "test-bucket"
        backend.s3_client = MagicMock()
        paginator = backend.s3_client.get_paginator.return_value
        paginator.paginate.return_value = [
            {"Contents": [
                {"Key": "news/2026-08-07.db"},
                {"Key": "news/not-a-date.db"},
                {"Key": "rss/2026-08-08.db"},
            ]},
            {"Contents": [
                {"Key": "news/2026-08-09.db"},
                {"Key": "news/2026-08-08.db"},
            ]},
        ]
        backend._get_period_execution_at_impl = MagicMock(
            side_effect=[None, "2026-08-08 10:02:00"]
        )

        self.assertEqual(
            backend.get_latest_period_execution(
                "daily_delivery", "push", "2026-08-09"
            ),
            "2026-08-08 10:02:00",
        )
        backend.s3_client.get_paginator.assert_called_once_with("list_objects_v2")
        paginator.paginate.assert_called_once_with(Bucket="test-bucket", Prefix="news/")
        self.assertEqual(
            backend._get_period_execution_at_impl.call_args_list,
            [
                call("2026-08-09", "daily_delivery", "push", strict_read=True),
                call("2026-08-08", "daily_delivery", "push", strict_read=True),
            ],
        )

    def test_remote_checkpoint_propagates_access_denied_during_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = RemoteStorageBackend.__new__(RemoteStorageBackend)
            backend.bucket_name = "test-bucket"
            backend.temp_dir = Path(tmp)
            backend.timezone = "Asia/Shanghai"
            backend._db_connections = {}
            backend._downloaded_files = []
            backend.s3_client = MagicMock()
            backend.s3_client.get_paginator.return_value.paginate.return_value = [
                {"Contents": [{"Key": "news/2026-08-09.db"}]}
            ]
            access_denied = ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "HeadObject",
            )
            backend.s3_client.head_object.side_effect = access_denied

            with self.assertRaisesRegex(
                RuntimeError, "读取周期执行时间失败"
            ) as raised:
                backend.get_latest_period_execution(
                    "daily_delivery", "push", "2026-08-09"
                )

            self.assertIs(raised.exception.__cause__, access_denied)

    def test_scheduler_latest_execution_forwards_all_arguments(self):
        storage = MagicMock()
        storage.get_latest_period_execution.return_value = "2026-08-08 10:02:00"
        scheduler = Scheduler(
            {"enabled": True, "preset": "custom"},
            TIMELINE,
            storage,
            lambda: shanghai(2026, 8, 9, 10, 0),
        )

        self.assertEqual(
            scheduler.latest_execution("daily_delivery", "push", "2026-08-09"),
            "2026-08-08 10:02:00",
        )
        storage.get_latest_period_execution.assert_called_once_with(
            "daily_delivery", "push", "2026-08-09"
        )
