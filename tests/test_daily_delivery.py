import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytz
from botocore.exceptions import ClientError

from trendradar.core.daily_delivery import (
    DailyDeliveryAggregator,
    daily_delivery_window,
    parse_discovered_at,
)
from trendradar.core.rss_snapshot import item_identity
from trendradar.core.scheduler import Scheduler
from trendradar.crawler.news_search import normalize_title
from trendradar.storage.base import RSSData, RSSItem
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


def save_rss_day(backend, date_str, crawl_time, items):
    grouped = {}
    names = {}
    for item in items:
        grouped.setdefault(item.feed_id, []).append(item)
        names[item.feed_id] = item.feed_name or item.feed_id
    saved = backend.save_rss_data(RSSData(
        date=date_str,
        crawl_time=crawl_time,
        items=grouped,
        id_to_name=names,
        failed_ids=[],
    ))
    if not saved:
        raise AssertionError("RSS 测试数据保存失败")


class DailyDeliveryWindowTests(unittest.TestCase):
    def test_first_delivery_uses_previous_twenty_four_hours(self):
        now = shanghai(2026, 8, 9, 10, 0)

        window = daily_delivery_window(now, None, "Asia/Shanghai")

        self.assertEqual(window.start, shanghai(2026, 8, 8, 10, 0))
        self.assertEqual(window.end, now)
        self.assertEqual(window.storage_dates, ["2026-08-08", "2026-08-09"])
        self.assertFalse(window.contains(shanghai(2026, 8, 8, 10, 0)))
        self.assertTrue(window.contains(shanghai(2026, 8, 9, 10, 0)))

    def test_checkpoint_window_crosses_midnight_and_is_left_open_right_closed(self):
        window = daily_delivery_window(
            shanghai(2026, 8, 9, 0, 15),
            "2026-08-08 23:45:00",
            "Asia/Shanghai",
        )

        self.assertEqual(window.start, shanghai(2026, 8, 8, 23, 45))
        self.assertEqual(window.end, shanghai(2026, 8, 9, 0, 15))
        self.assertEqual(window.storage_dates, ["2026-08-08", "2026-08-09"])
        self.assertFalse(window.contains(shanghai(2026, 8, 8, 23, 45)))
        self.assertTrue(window.contains(shanghai(2026, 8, 9, 0, 15)))

    def test_time_only_first_seen_uses_own_database_date(self):
        parsed = parse_discovered_at("09-45", "2026-08-08", "Asia/Shanghai")
        self.assertEqual(parsed, shanghai(2026, 8, 8, 9, 45))

    def test_colon_time_only_uses_own_database_date(self):
        parsed = parse_discovered_at("09:45", "2026-08-08", "Asia/Shanghai")
        self.assertEqual(parsed, shanghai(2026, 8, 8, 9, 45))

    def test_complete_naive_sqlite_time_uses_configured_timezone(self):
        parsed = parse_discovered_at(
            "2026-08-08 09:45:30", "2026-08-09", "Asia/Shanghai"
        )
        self.assertEqual(
            parsed,
            pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 8, 9, 45, 30)
            ),
        )

    def test_iso_time_is_converted_to_configured_timezone(self):
        parsed = parse_discovered_at(
            "2026-08-08T01:45:00Z", "2026-08-09", "Asia/Shanghai"
        )
        self.assertEqual(parsed, shanghai(2026, 8, 8, 9, 45))

    def test_first_window_normalizes_dst_and_uses_local_calendar_dates(self):
        timezone = "America/New_York"
        now = pytz.timezone(timezone).localize(datetime(2026, 3, 9, 0, 30))

        window = daily_delivery_window(now, None, timezone)

        self.assertEqual(
            window.start.isoformat(),
            "2026-03-07T23:30:00-05:00",
        )
        self.assertEqual(window.end - window.start, timedelta(hours=24))
        self.assertEqual(
            window.storage_dates,
            ["2026-03-07", "2026-03-08", "2026-03-09"],
        )


class DailyDeliveryAggregatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.backend = LocalStorageBackend(
            data_dir=self.temp_dir.name,
            enable_txt=False,
            enable_html=False,
            timezone="Asia/Shanghai",
        )

    def tearDown(self):
        self.backend.cleanup()
        self.temp_dir.cleanup()

    def build(self, checkpoint="2026-08-08 10:00:00"):
        return DailyDeliveryAggregator(
            self.backend, "Asia/Shanghai"
        ).build(
            now=shanghai(2026, 8, 9, 10, 0),
            checkpoint=checkpoint,
        )

    def test_window_filter_is_left_open_right_closed_across_dates(self):
        save_rss_day(self.backend, "2026-08-08", "10-00", [RSSItem(
            title="At checkpoint", feed_id="journal",
            url="https://example.org/at-checkpoint",
            published_at="2026-08-08T10:00:00+08:00",
        )])
        save_rss_day(self.backend, "2026-08-09", "10-00", [RSSItem(
            title="At current run", feed_id="journal",
            url="https://example.org/at-current-run",
            published_at="2026-01-01T00:00:00Z",
        )])

        snapshot = self.build()

        self.assertEqual(
            [item.title for item in snapshot.iter_items()],
            ["At current run"],
        )
        self.assertEqual(snapshot.total_read, 2)
        self.assertEqual(snapshot.filtered_out, 1)

    def test_first_seen_is_preferred_over_later_crawl_time(self):
        item = RSSItem(
            title="Seen before checkpoint", feed_id="journal",
            url="https://example.org/seen-before",
        )
        save_rss_day(self.backend, "2026-08-09", "09-00", [item])
        save_rss_day(self.backend, "2026-08-09", "09-45", [item])

        snapshot = self.build(checkpoint="2026-08-09 09:30:00")

        self.assertEqual(list(snapshot.iter_items()), [])
        self.assertEqual(snapshot.filtered_out, 1)

    def test_missing_first_seen_falls_back_to_crawl_time(self):
        save_rss_day(self.backend, "2026-08-09", "09-30", [RSSItem(
            title="Crawl-time fallback", feed_id="legacy",
            url="https://example.org/crawl-time-fallback",
        )])
        conn = self.backend._get_connection("2026-08-09", db_type="rss")
        conn.execute(
            "UPDATE rss_items SET first_crawl_time = '' WHERE feed_id = ?",
            ("legacy",),
        )
        conn.commit()

        snapshot = self.build()

        self.assertEqual(
            [item.title for item in snapshot.iter_items()],
            ["Crawl-time fallback"],
        )

    def test_late_indexed_old_article_is_selected_by_first_seen(self):
        article = RSSItem(
            title="Old publication, newly indexed",
            feed_id="search",
            url="https://example.org/late",
            published_at="2026-07-01T00:00:00Z",
        )
        save_rss_day(self.backend, "2026-08-09", "09-30", [article])

        snapshot = self.build()

        self.assertEqual([item.title for item in snapshot.iter_items()], [
            "Old publication, newly indexed"
        ])

    def test_canonical_duplicate_merges_sources_and_keeps_richer_item(self):
        save_rss_day(self.backend, "2026-08-08", "11-00", [RSSItem(
            title="First title", feed_id="alpha", feed_name="Alpha",
            url="https://example.org/story?utm_source=alpha",
            summary="short", source_count=4, pre_hot_score=0.4,
            search_providers="google_news",
        )])
        save_rss_day(self.backend, "2026-08-09", "09-00", [RSSItem(
            title="Richer title", feed_id="beta", feed_name="Beta",
            url="https://example.org/story?utm_medium=rss",
            summary="a much richer summary", author="Researcher",
            source_count=2, pre_hot_score=0.9,
            search_providers="gdelt,google_news",
        )])

        snapshot = self.build()
        items = list(snapshot.iter_items())

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Richer title")
        self.assertEqual(
            items[0].url,
            "https://example.org/story?utm_medium=rss",
        )
        self.assertEqual(items[0].summary, "a much richer summary")
        self.assertEqual(items[0].source_count, 4)
        self.assertEqual(items[0].pre_hot_score, 0.9)
        self.assertEqual(items[0].search_providers, "gdelt,google_news")
        self.assertEqual(snapshot.duplicate_count, 1)

    def test_existing_tracking_url_is_reused_as_the_exact_snapshot_row(self):
        tracking_url = "https://example.org/exact?utm_source=current"
        save_rss_day(self.backend, "2026-08-09", "09-00", [RSSItem(
            title="Reuse tracking row", feed_id="journal", url=tracking_url,
        )])
        before = self.backend.get_all_rss_ids("2026-08-09")
        original_id = before[0]["id"]

        snapshot = self.build()
        after = self.backend.get_all_rss_ids("2026-08-09")

        self.assertEqual(len(after), 1)
        self.assertEqual(snapshot.allowed_rss_ids, {original_id})
        self.assertEqual(
            [item.url for item in snapshot.iter_items()],
            [tracking_url],
        )

    def test_same_title_and_canonical_url_resolves_only_actual_guid_row(self):
        first_url = "https://example.org/guid-story?utm_source=first"
        richer_url = "https://example.org/guid-story?utm_source=richer"
        save_rss_day(self.backend, "2026-08-09", "09-00", [
            RSSItem(
                title="Same normalized title", feed_id="journal",
                url=first_url, guid="guid-first", summary="short",
            ),
            RSSItem(
                title="Same normalized title", feed_id="journal",
                url=richer_url, guid="guid-richer",
                summary="a much richer summary",
            ),
        ])
        before = self.backend.get_all_rss_ids("2026-08-09")
        richer_id = next(
            row["id"] for row in before if row["url"] == richer_url
        )

        snapshot = self.build()

        self.assertEqual(snapshot.allowed_rss_ids, {richer_id})

    def test_get_all_rss_ids_exposes_guid_without_changing_existing_fields(self):
        save_rss_day(self.backend, "2026-08-09", "09-00", [RSSItem(
            title="GUID contract", feed_id="journal",
            url="https://example.org/guid-contract", guid="stable-guid",
        )])

        row = self.backend.get_all_rss_ids("2026-08-09")[0]

        self.assertEqual(row.get("guid"), "stable-guid")
        self.assertEqual(row["source_id"], "journal")
        self.assertEqual(row["url"], "https://example.org/guid-contract")

    def test_title_fallback_snapshot_ids_are_stable_and_idempotent(self):
        date_str = "2026-08-08"
        save_rss_day(self.backend, date_str, "11-00", [])
        conn = self.backend._get_connection(date_str, db_type="rss")
        conn.execute(
            "INSERT INTO rss_feeds (id, name) VALUES (?, ?)",
            ("legacy", "Legacy Feed"),
        )
        conn.execute(
            """
            INSERT INTO rss_items (
                title, feed_id, url, guid, published_at, summary, author,
                first_crawl_time, last_crawl_time
            ) VALUES (?, ?, '', '', ?, ?, '', ?, ?)
            """,
            (
                " Rice   Breeding ", "legacy", "2026-01-01T00:00:00Z",
                "legacy summary", "11-00", "11-00",
            ),
        )
        conn.commit()
        expected_guid = "daily-delivery-title:" + hashlib.sha256(
            f"legacy\0{normalize_title(' Rice   Breeding ')}".encode("utf-8")
        ).hexdigest()

        first = self.build()
        first_items = list(first.iter_items())
        first_identities = {item_identity(item) for item in first_items}
        second = self.build()
        second_items = list(second.iter_items())

        self.assertEqual([item.guid for item in first_items], [expected_guid])
        self.assertEqual(
            {item_identity(item) for item in second_items},
            first_identities,
        )
        self.assertEqual(second.allowed_rss_ids, first.allowed_rss_ids)
        self.assertEqual(len(first.allowed_rss_ids), 1)

    def test_snapshot_write_time_does_not_repeat_item_next_day(self):
        save_rss_day(self.backend, "2026-08-08", "11-00", [RSSItem(
            title="Deliver once", feed_id="journal",
            url="https://example.org/deliver-once",
        )])
        first = self.build()

        second = DailyDeliveryAggregator(
            self.backend, "Asia/Shanghai"
        ).build(
            now=shanghai(2026, 8, 10, 10, 0),
            checkpoint="2026-08-09 10:00:00",
        )

        self.assertEqual(
            [item.title for item in first.iter_items()],
            ["Deliver once"],
        )
        self.assertEqual(list(second.iter_items()), [])

    def test_backlog_older_than_two_days_reads_the_full_date_range(self):
        save_rss_day(self.backend, "2026-08-05", "09-00", [RSSItem(
            title="Long backlog item", feed_id="journal",
            url="https://example.org/long-backlog",
        )])

        snapshot = self.build(checkpoint="2026-08-04 10:00:00")

        self.assertEqual(snapshot.window.storage_dates, [
            "2026-08-04", "2026-08-05", "2026-08-06",
            "2026-08-07", "2026-08-08", "2026-08-09",
        ])
        self.assertEqual(
            [item.title for item in snapshot.iter_items()],
            ["Long backlog item"],
        )

    def test_all_window_databases_missing_raises(self):
        with self.assertRaisesRegex(RuntimeError, "日库全部缺失"):
            self.build()

    def test_successful_empty_database_returns_persisted_empty_snapshot(self):
        save_rss_day(self.backend, "2026-08-09", "09-30", [])

        snapshot = self.build()

        self.assertIsNotNone(snapshot.data)
        self.assertEqual(snapshot.data.items, {})
        self.assertEqual(snapshot.allowed_rss_ids, set())
        self.assertEqual(snapshot.missing_dates, ["2026-08-08"])
        self.assertEqual(
            self.backend.get_rss_data("2026-08-09").crawl_time,
            "2026-08-09 10:00:00",
        )

    def test_any_failed_ids_aborts_snapshot(self):
        self.assertTrue(self.backend.save_rss_data(RSSData(
            date="2026-08-09",
            crawl_time="09-30",
            items={},
            id_to_name={"broken-feed": "Broken Feed"},
            failed_ids=["broken-feed"],
        )))

        with self.assertRaisesRegex(RuntimeError, "失败源.*broken-feed"):
            self.build()

    def test_snapshot_save_failure_raises(self):
        save_rss_day(self.backend, "2026-08-08", "11-00", [RSSItem(
            title="Cannot persist", feed_id="journal",
            url="https://example.org/cannot-persist",
        )])

        with patch.object(self.backend, "save_rss_data", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "每日交付快照保存失败"):
                self.build()

    def test_snapshot_id_resolution_failure_raises(self):
        save_rss_day(self.backend, "2026-08-08", "11-00", [RSSItem(
            title="Missing ID", feed_id="journal",
            url="https://example.org/missing-id",
        )])

        with patch.object(self.backend, "get_all_rss_ids", return_value=[]):
            with self.assertRaisesRegex(
                RuntimeError,
                "每日交付快照 ID 解析失败：存在未持久化条目",
            ):
                self.build()

    def test_snapshot_id_resolution_rejects_multiple_exact_rows(self):
        url = "https://example.org/ambiguous-id"
        save_rss_day(self.backend, "2026-08-08", "11-00", [RSSItem(
            title="Ambiguous ID", feed_id="journal", url=url,
        )])
        duplicate_rows = [
            {
                "id": row_id, "source_id": "journal", "source_name": "Journal",
                "url": url, "guid": "",
            }
            for row_id in (101, 102)
        ]

        with patch.object(
            self.backend,
            "get_all_rss_ids",
            side_effect=[[], duplicate_rows],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "每日交付快照 ID 解析失败：存在未持久化条目",
            ):
                self.build()


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
