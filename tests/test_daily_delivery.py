import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytz
from botocore.exceptions import ClientError

from trendradar.ai.analyzer import AIAnalyzer
from trendradar.ai.filter import AIFilter, AIFilterResult
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.ai.translator import BatchTranslationResult, TranslationResult
from trendradar.context import AppContext
from trendradar.core.daily_delivery import (
    DailyDeliveryAggregator,
    daily_delivery_window,
    parse_discovered_at,
)
from trendradar.core.rss_snapshot import item_identity
from trendradar.core.scheduler import Scheduler
from trendradar.crawler.news_search import normalize_title
from trendradar.crawler.rss.fetcher import RSSFeedConfig, RSSFetcher
from trendradar.crawler.rss.parser import ParsedRSSItem
from trendradar.storage.base import RSSData, RSSItem
from trendradar.storage.local import LocalStorageBackend
from trendradar.storage.manager import StorageManager
from trendradar.storage.remote import RemoteStorageBackend
from trendradar.notification.dispatcher import NotificationDispatcher


def shanghai(year, month, day, hour, minute, second=0):
    return pytz.timezone("Asia/Shanghai").localize(
        datetime(year, month, day, hour, minute, second)
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

    def test_new_rss_fetch_writes_full_local_datetime_with_seconds(self):
        feed = RSSFeedConfig(
            id="journal",
            name="Journal",
            url="https://example.org/feed.xml",
        )
        fetcher = RSSFetcher(
            [feed], request_interval=0, freshness_enabled=False,
            timezone="Asia/Shanghai",
        )
        response = MagicMock(text="<rss/>")
        response.raise_for_status.return_value = None
        fetcher.session.get = MagicMock(return_value=response)
        fetcher.parser.parse = MagicMock(return_value=[ParsedRSSItem(
            title="Second precision",
            url="https://example.org/second-precision",
            published_at="2026-08-09T01:00:00Z",
        )])

        with patch(
            "trendradar.crawler.rss.fetcher.get_configured_time",
            return_value=shanghai(2026, 8, 9, 10, 0, 37),
        ):
            items, error = fetcher.fetch_feed(feed)
            data = fetcher.fetch_all()

        self.assertIsNone(error)
        self.assertEqual(items[0].crawl_time, "2026-08-09 10:00:37")
        self.assertEqual(items[0].first_time, "2026-08-09 10:00:37")
        self.assertEqual(data.crawl_time, "2026-08-09 10:00:37")
        self.assertEqual(
            data.items["journal"][0].first_time,
            "2026-08-09 10:00:37",
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
        save_rss_day(
            self.backend,
            "2026-08-08",
            "2026-08-08 10:00:00",
            [RSSItem(
                title="At checkpoint", feed_id="journal",
                url="https://example.org/at-checkpoint",
                published_at="2026-08-08T10:00:00+08:00",
            )],
        )
        save_rss_day(
            self.backend,
            "2026-08-09",
            "2026-08-09 10:00:00",
            [RSSItem(
                title="At current run", feed_id="journal",
                url="https://example.org/at-current-run",
                published_at="2026-01-01T00:00:00Z",
            )],
        )

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

    def test_canonical_first_seen_before_checkpoint_on_start_day_is_excluded(self):
        canonical = "https://example.org/checkpoint-story"
        save_rss_day(self.backend, "2026-08-08", "2026-08-08 09:00:00", [
            RSSItem(
                title="Seen before checkpoint",
                feed_id="alpha",
                url=f"{canonical}?utm_source=alpha",
            ),
        ])
        save_rss_day(self.backend, "2026-08-09", "2026-08-09 09:00:00", [
            RSSItem(
                title="Rediscovered after checkpoint",
                feed_id="beta",
                url=f"{canonical}?utm_source=beta",
            ),
        ])

        snapshot = self.build()

        self.assertEqual(list(snapshot.iter_items()), [])

    def test_canonical_history_before_window_dates_is_excluded(self):
        canonical = "https://example.org/older-story"
        save_rss_day(self.backend, "2026-08-07", "2026-08-07 09:00:00", [
            RSSItem(
                title="Seen on an older day",
                feed_id="alpha",
                url=f"{canonical}?utm_source=alpha",
            ),
        ])
        save_rss_day(self.backend, "2026-08-09", "2026-08-09 09:00:00", [
            RSSItem(
                title="Rediscovered in window",
                feed_id="beta",
                url=f"{canonical}?utm_source=beta",
            ),
        ])

        snapshot = self.build()

        self.assertEqual(list(snapshot.iter_items()), [])

    def test_canonical_first_seen_only_inside_window_is_included(self):
        save_rss_day(self.backend, "2026-08-09", "2026-08-09 09:00:00", [
            RSSItem(
                title="First seen in window",
                feed_id="journal",
                url="https://example.org/new-story?utm_source=rss",
            ),
        ])

        snapshot = self.build()

        self.assertEqual(
            [item.title for item in snapshot.iter_items()],
            ["First seen in window"],
        )

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
        # Build a genuine pre-outbox database.  Calling the current save API
        # first would advance the durable generation/watermark and a later raw
        # SQL insert would correctly be treated as an unsupported writer that
        # bypassed the outbox contract.
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

    def test_legacy_minute_bucket_overlapping_checkpoint_is_conservatively_kept(self):
        save_rss_day(self.backend, "2026-08-09", "10:01", [
            RSSItem(
                title="Legacy minute precision",
                feed_id="journal",
                feed_name="Journal",
                url="https://example.org/legacy-minute",
                first_time="10:00",
                crawl_time="10:00",
            ),
            RSSItem(
                title="Exact second before checkpoint",
                feed_id="journal",
                feed_name="Journal",
                url="https://example.org/exact-second",
                first_time="2026-08-09 10:00:00",
                crawl_time="2026-08-09 10:00:00",
            ),
        ])
        conn = self.backend._get_connection("2026-08-09", db_type="rss")
        conn.execute(
            "UPDATE rss_items SET first_crawl_time = ? WHERE title = ?",
            ("10:00", "Legacy minute precision"),
        )
        conn.execute(
            "UPDATE rss_items SET first_crawl_time = ? WHERE title = ?",
            ("2026-08-09 10:00:00", "Exact second before checkpoint"),
        )
        conn.commit()

        snapshot = DailyDeliveryAggregator(
            self.backend, "Asia/Shanghai"
        ).build(
            now=shanghai(2026, 8, 9, 10, 2),
            checkpoint="2026-08-09 10:00:30",
        )

        self.assertEqual(
            [item.title for item in snapshot.iter_items()],
            ["Legacy minute precision"],
        )

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

        with patch.object(
            self.backend, "get_all_rss_ids_strict", return_value=[]
        ):
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
            "get_all_rss_ids_strict",
            side_effect=[[], duplicate_rows],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "每日交付快照 ID 解析失败：存在未持久化条目",
            ):
                self.build()


class DailyDeliveryAIScopeTests(unittest.TestCase):
    @staticmethod
    def _rss_rows():
        return [
            {
                "id": 7,
                "news_item_id": 7,
                "tag": "育种",
                "title": "Approved old article",
                "source_type": "rss",
                "source_id": "journal",
                "source_name": "Journal",
                "published_at": "2026-07-01T00:00:00Z",
                "first_time": "2026-07-01T00:00:00Z",
                "relevance_score": 0.9,
            },
            {
                "id": 8,
                "news_item_id": 8,
                "tag": "育种",
                "title": "Unapproved fresh article",
                "source_type": "rss",
                "source_id": "journal",
                "source_name": "Journal",
                "published_at": "2026-08-09T01:00:00Z",
                "first_time": "2026-08-09T01:00:00Z",
                "relevance_score": 0.9,
            },
        ]

    def _pipeline_with_authoritative_ids(self, storage):
        return AIFilterPipeline(
            config={
                "TIMEZONE": "Asia/Shanghai",
                "RSS": {
                    "ENABLED": True,
                    "FRESHNESS_FILTER": {"ENABLED": True, "MAX_AGE_DAYS": 2},
                },
                "AI": {},
                "AI_FILTER": {},
                "FILTER": {},
            },
            storage_manager=storage,
            get_time_func=lambda: shanghai(2026, 8, 9, 10, 0),
            allowed_rss_ids={7},
            rss_ids_authoritative=True,
        )

    def test_app_context_passes_authoritative_ids_to_filter_and_conversion(self):
        allowed_ids = {7}
        context = AppContext({"FILTER": {"METHOD": "ai"}})
        context._storage_manager = MagicMock()
        ai_result = AIFilterResult(success=True)

        with patch("trendradar.context.AIFilterPipeline") as pipeline_class:
            pipeline_class.return_value.run.return_value = ai_result
            pipeline_class.return_value.convert_to_report_data.return_value = (
                [], [], [],
            )

            self.assertIs(
                context.run_ai_filter(
                    allowed_rss_ids=allowed_ids,
                    rss_ids_authoritative=True,
                ),
                ai_result,
            )
            context.convert_ai_filter_to_report_data(
                ai_result,
                allowed_rss_ids=allowed_ids,
                rss_ids_authoritative=True,
            )

        self.assertEqual(pipeline_class.call_count, 2)
        for call_args in pipeline_class.call_args_list:
            self.assertIs(call_args.kwargs["allowed_rss_ids"], allowed_ids)
            self.assertTrue(call_args.kwargs["rss_ids_authoritative"])

    def test_authoritative_snapshot_ids_override_publication_freshness(self):
        pipeline = AIFilterPipeline(
            config={
                "TIMEZONE": "Asia/Shanghai",
                "RSS": {
                    "ENABLED": True,
                    "FEEDS": [],
                    "FRESHNESS_FILTER": {"ENABLED": True, "MAX_AGE_DAYS": 2},
                },
                "AI": {},
                "AI_FILTER": {},
                "FILTER": {},
            },
            storage_manager=MagicMock(),
            get_time_func=lambda: shanghai(2026, 8, 9, 10, 0),
            allowed_rss_ids={7},
            rss_ids_authoritative=True,
        )

        self.assertTrue(pipeline._is_rss_item_in_scope(
            "search", "2026-07-01T00:00:00Z", 7
        ))
        self.assertFalse(pipeline._is_rss_item_in_scope(
            "search", "2026-08-09T01:00:00Z", 8
        ))

    def test_authoritative_scope_collects_only_approved_ids(self):
        storage = MagicMock()
        storage.get_all_news_ids.return_value = []
        storage.get_analyzed_news_ids.return_value = set()
        storage.get_all_rss_ids_strict.return_value = self._rss_rows()

        pending = self._pipeline_with_authoritative_ids(
            storage
        )._collect_pending_news("ai_interests.txt")

        self.assertEqual([item["id"] for item in pending[1]], [7])

    def test_authoritative_pipeline_never_classifies_or_returns_hotlist(self):
        storage = MagicMock()
        storage.get_latest_prompt_hash.return_value = "stable"
        storage.get_active_ai_filter_tags.return_value = [
            {"id": 1, "tag": "育种", "priority": 1}
        ]
        storage.get_all_news_ids.return_value = [{
            "id": 101,
            "title": "Hotlist outside the delivery snapshot",
            "source_id": "hot",
            "source_name": "Hotlist",
            "url": "https://example.org/hot",
        }]
        storage.get_all_rss_ids_strict.return_value = self._rss_rows()
        storage.get_analyzed_news_ids.return_value = set()
        storage.get_active_ai_filter_results.return_value = [
            {
                "news_item_id": 101,
                "tag": "育种",
                "title": "Hotlist outside the delivery snapshot",
                "source_type": "hotlist",
                "source_id": "hot",
                "source_name": "Hotlist",
                "relevance_score": 0.9,
            },
            self._rss_rows()[0],
        ]
        pipeline = self._pipeline_with_authoritative_ids(storage)

        with patch("trendradar.ai.filter_pipeline.AIFilter") as filter_class:
            ai_filter = filter_class.return_value
            ai_filter.load_interests_content.return_value = "育种"
            ai_filter.compute_interests_hash.return_value = "stable"
            ai_filter.classify_batch.return_value = []
            with patch.object(
                pipeline,
                "_enrich_pending_items",
                side_effect=lambda items, _label: items,
            ):
                result = pipeline.run()

        classified_titles = [
            item["title"]
            for call_args in ai_filter.classify_batch.call_args_list
            for item in call_args.args[0]
        ]
        hotlist_stats, rss_stats, _ = pipeline.convert_to_report_data(result)

        self.assertEqual(classified_titles, ["Approved old article"])
        self.assertEqual(hotlist_stats, [])
        self.assertEqual(
            [item["title"] for item in rss_stats[0]["titles"]],
            ["Approved old article"],
        )
        self.assertEqual(
            [item["source_type"] for item in result.tags[0]["items"]],
            ["rss"],
        )

    def test_strict_authoritative_run_reclassifies_ordinary_cached_rss(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            now = shanghai(2026, 8, 9, 10, 0)
            try:
                with patch.object(
                    storage, "_get_configured_time", return_value=now
                ):
                    save_rss_day(storage, "2026-08-09", "09:30", [RSSItem(
                        title="Cached ordinary classification",
                        feed_id="journal",
                        feed_name="Journal",
                        url="https://example.org/cached-classification",
                    )])
                    self.assertEqual(storage.save_ai_filter_tags(
                        [{
                            "tag": "育种",
                            "description": "育种进展",
                            "priority": 1,
                        }],
                        version=1,
                        prompt_hash="stable",
                        interests_file="ai_interests.txt",
                    ), 1)
                    rss_id = storage.get_all_rss_ids_strict(
                        "2026-08-09"
                    )[0]["id"]
                    config = {
                        "TIMEZONE": "Asia/Shanghai",
                        "RSS": {
                            "ENABLED": True,
                            "FRESHNESS_FILTER": {"ENABLED": False},
                        },
                        "AI": {},
                        "AI_FILTER": {"BATCH_INTERVAL": 0},
                        "FILTER": {},
                    }
                    fake_filter = MagicMock()
                    fake_filter.load_interests_content.return_value = "育种"
                    fake_filter.compute_interests_hash.return_value = "stable"
                    fake_filter.classify_batch.return_value = [{
                        "news_item_id": rss_id,
                        "tag_id": 1,
                        "relevance_score": 0.9,
                        "importance_score": 0.8,
                        "ai_summary": "水稻育种进展",
                    }]

                    with patch(
                        "trendradar.ai.filter_pipeline.AIFilter",
                        return_value=fake_filter,
                    ):
                        ordinary = AIFilterPipeline(
                            config, storage, lambda: now
                        )
                        ordinary._enrich_pending_items = (
                            lambda items, _label: items
                        )
                        self.assertTrue(ordinary.run().success)

                        strict = AIFilterPipeline(
                            config,
                            storage,
                            lambda: now,
                            allowed_rss_ids={rss_id},
                            rss_ids_authoritative=True,
                            strict=True,
                        )
                        strict._enrich_pending_items = (
                            lambda items, _label: items
                        )
                        self.assertTrue(strict.run().success)

                self.assertEqual(fake_filter.classify_batch.call_count, 2)
                self.assertTrue(
                    fake_filter.classify_batch.call_args.kwargs["strict"]
                )
            finally:
                storage.cleanup()

    def test_authoritative_scope_filters_active_results(self):
        storage = MagicMock()
        storage.get_latest_prompt_hash.return_value = "stable"
        storage.get_active_ai_filter_tags.return_value = [
            {"id": 1, "tag": "育种"}
        ]
        storage.get_all_news_ids.return_value = []
        storage.get_all_rss_ids_strict.return_value = self._rss_rows()
        storage.get_analyzed_news_ids.return_value = {7, 8}
        storage.get_active_ai_filter_results.return_value = self._rss_rows()
        pipeline = self._pipeline_with_authoritative_ids(storage)

        with patch("trendradar.ai.filter_pipeline.AIFilter") as ai_filter_class:
            ai_filter = ai_filter_class.return_value
            ai_filter.load_interests_content.return_value = "育种"
            ai_filter.compute_interests_hash.return_value = "stable"
            result = pipeline.run()

        self.assertEqual(
            [item["news_item_id"] for item in result.tags[0]["items"]],
            [7],
        )

    def test_authoritative_scope_filters_report_conversion(self):
        storage = MagicMock()
        pipeline = self._pipeline_with_authoritative_ids(storage)
        result = AIFilterResult(success=True, tags=[{
            "tag": "育种",
            "count": 2,
            "items": [
                {
                    **row,
                    "news_item_id": row["id"],
                    "url": f"https://example.org/{row['id']}",
                }
                for row in self._rss_rows()
            ],
        }])

        _, rss_stats, _ = pipeline.convert_to_report_data(result)

        self.assertEqual(
            [item["title"] for item in rss_stats[0]["titles"]],
            ["Approved old article"],
        )

    def test_authoritative_scope_rejects_partial_classification_results(self):
        storage = MagicMock()
        storage.get_latest_prompt_hash.return_value = "unchanged"
        storage.get_active_ai_filter_tags.return_value = [
            {"id": 1, "tag": "育种"}
        ]
        pipeline = AIFilterPipeline(
            config={
                "TIMEZONE": "Asia/Shanghai",
                "RSS": {"ENABLED": True, "FRESHNESS_FILTER": {}},
                "AI": {},
                "AI_FILTER": {},
                "FILTER": {},
            },
            storage_manager=storage,
            get_time_func=lambda: shanghai(2026, 8, 9, 10, 0),
            allowed_rss_ids={7, 8},
            rss_ids_authoritative=True,
        )
        pipeline._collect_pending_news = MagicMock(return_value=(
            [], [{"id": 7}, {"id": 8}], [], set(),
            [{"id": 7}, {"id": 8}], set(), 0,
        ))
        pipeline._classify_batches = MagicMock(return_value=([], [], [7]))
        pipeline._save_results = MagicMock()

        with patch("trendradar.ai.filter_pipeline.AIFilter") as ai_filter_class:
            ai_filter = ai_filter_class.return_value
            ai_filter.load_interests_content.return_value = "育种"
            ai_filter.compute_interests_hash.return_value = "unchanged"

            result = pipeline.run()

        self.assertFalse(result.success)
        self.assertEqual(
            result.error,
            "范围内 AI 分类批次失败，已拒绝使用部分结果",
        )
        storage.get_active_ai_filter_results.assert_not_called()


class DailyDeliveryStrictAIStageTests(unittest.TestCase):
    @staticmethod
    def _analyzer(grounding_review_enabled=False):
        analyzer = AIAnalyzer.__new__(AIAnalyzer)
        analyzer.ai_config = {"MODEL": "test", "TIMEOUT": 1, "MAX_TOKENS": 100}
        analyzer.analysis_config = {}
        analyzer.get_time_func = lambda: shanghai(2026, 8, 9, 10, 0)
        analyzer.debug = False
        analyzer.client = MagicMock(api_key="secret")
        analyzer.max_news = 50
        analyzer.include_rss = True
        analyzer.include_rank_timeline = False
        analyzer.include_standalone = False
        analyzer.grounding_review_enabled = grounding_review_enabled
        analyzer.language = "Chinese"
        analyzer.system_prompt = "system"
        analyzer.user_prompt_template = (
            "{report_mode}{report_type}{current_time}{news_count}{rss_count}"
            "{platforms}{keywords}{news_content}{rss_content}{language}"
            "{standalone_content}"
        )
        return analyzer

    @staticmethod
    def _rss_stats():
        return [{
            "word": "育种",
            "count": 1,
            "titles": [{
                "title": "Rice breeding",
                "source_name": "Journal",
                "time_display": "2026-08-09 09:30",
            }],
        }]

    def test_strict_analysis_rejects_json_parse_and_repair_failure(self):
        analyzer = self._analyzer()
        analyzer._call_ai = MagicMock(return_value="definitely not json")
        analyzer._retry_fix_json = MagicMock(return_value=None)

        result = analyzer.analyze(
            [], self._rss_stats(), report_mode="daily_delivery", strict=True
        )

        self.assertFalse(result.success)
        self.assertIn("JSON", result.error)

    def test_strict_analysis_rejects_grounding_review_failure(self):
        analyzer = self._analyzer(grounding_review_enabled=True)
        analyzer._call_ai = MagicMock(return_value=json.dumps({
            "core_trends": "Rice breeding",
        }))
        analyzer._review_grounding = MagicMock(return_value=None)

        result = analyzer.analyze(
            [], self._rss_stats(), report_mode="daily_delivery", strict=True
        )

        self.assertFalse(result.success)
        self.assertIn("校审", result.error)

    def test_strict_analysis_rejects_empty_json_summary(self):
        analyzer = self._analyzer()
        analyzer._call_ai = MagicMock(return_value="{}")

        result = analyzer.analyze(
            [], self._rss_stats(), report_mode="daily_delivery", strict=True
        )

        self.assertFalse(result.success)
        self.assertIn("摘要", result.error)

    @staticmethod
    def _filter(summary_review_response):
        ai_filter = AIFilter.__new__(AIFilter)
        ai_filter.debug = False
        ai_filter.classify_system = "只返回 JSON"
        ai_filter.classify_user = (
            "{interests_content}\n{tags_list}\n{news_count}\n{news_list}"
        )
        ai_filter.summary_grounding_review_enabled = True
        ai_filter.client = MagicMock()
        ai_filter.client.chat.side_effect = [
            json.dumps([{
                "id": 1,
                "tag_id": 18,
                "score": 0.82,
                "importance_score": 0.78,
                "summary": "仅标题显示：水稻育种",
            }]),
            summary_review_response,
        ]
        return ai_filter

    def test_strict_classification_rejects_item_summary_review_failure(self):
        ai_filter = self._filter("not-json")

        result = ai_filter.classify_batch(
            [{
                "id": 1,
                "title": "水稻育种",
                "content": "水稻育种",
                "content_level": "title_only",
            }],
            [{"id": 18, "tag": "作物育种", "description": "育种进展"}],
            "育种",
            strict=True,
        )

        self.assertIsNone(result)

    @staticmethod
    def _review_succeeds(reviewed):
        ai_filter = AIFilter.__new__(AIFilter)
        ai_filter.client = MagicMock()
        ai_filter.client.chat.return_value = json.dumps(
            reviewed, ensure_ascii=False
        )
        ai_filter.debug = False
        return ai_filter._review_item_summaries(
            [
                {"id": 1, "title": "第一条", "content": "证据一"},
                {"id": 2, "title": "第二条", "content": "证据二"},
            ],
            [
                {"news_item_id": 1, "ai_summary": "草稿一"},
                {"news_item_id": 2, "ai_summary": "草稿二"},
            ],
        )

    def test_item_summary_review_rejects_duplicate_ids(self):
        self.assertFalse(self._review_succeeds([
            {"id": 1, "summary": "校审一"},
            {"id": 1, "summary": "重复校审"},
        ]))

    def test_item_summary_review_rejects_unknown_ids(self):
        self.assertFalse(self._review_succeeds([
            {"id": 1, "summary": "校审一"},
            {"id": 2, "summary": "校审二"},
            {"id": 999, "summary": "未知"},
        ]))

    def test_item_summary_review_rejects_missing_id(self):
        self.assertFalse(self._review_succeeds([
            {"id": 1, "summary": "校审一"},
        ]))

    def test_item_summary_review_rejects_empty_summary(self):
        self.assertFalse(self._review_succeeds([
            {"id": 1, "summary": "校审一"},
            {"id": 2, "summary": ""},
        ]))


class DailyDeliveryStrictAIStorageTests(unittest.TestCase):
    @staticmethod
    def _config():
        return {
            "TIMEZONE": "Asia/Shanghai",
            "RSS": {
                "ENABLED": True,
                "FRESHNESS_FILTER": {"ENABLED": False},
            },
            "AI": {},
            "AI_FILTER": {"BATCH_INTERVAL": 0},
            "FILTER": {},
        }

    @staticmethod
    def _rss_row(news_id=7):
        return {
            "id": news_id,
            "title": "Strict storage article",
            "source_id": "journal",
            "source_name": "Journal",
            "url": "https://example.org/strict-storage",
            "published_at": "2026-08-09T01:00:00Z",
        }

    def _mock_pipeline(self, storage):
        storage.get_latest_prompt_hash.return_value = "stable"
        storage.get_active_ai_filter_tags.return_value = [
            {"id": 1, "tag": "育种", "priority": 1}
        ]
        storage.get_ai_filter_tag_snapshot_strict.return_value = {
            "tags": [{
                "id": 1,
                "tag": "育种",
                "description": "",
                "priority": 1,
                "version": 1,
                "prompt_hash": "stable",
            }],
            "prompt_hash": "stable",
            "version": 1,
            "latest_version": 1,
        }
        storage.get_all_rss_ids_strict.return_value = [self._rss_row()]
        storage.get_analyzed_news_ids.return_value = set()
        storage.get_active_ai_filter_results.return_value = [{
            **self._rss_row(),
            "news_item_id": 7,
            "source_type": "rss",
            "tag_id": 1,
            "tag": "育种",
            "relevance_score": 0.9,
            "ai_summary": "育种进展",
        }]
        pipeline = AIFilterPipeline(
            self._config(),
            storage,
            lambda: shanghai(2026, 8, 9, 10, 0),
            allowed_rss_ids={7},
            rss_ids_authoritative=True,
            strict=True,
        )
        pipeline._enrich_pending_items = lambda items, _label: items
        return pipeline

    @staticmethod
    def _fake_filter():
        ai_filter = MagicMock()
        ai_filter.load_interests_content.return_value = "育种"
        ai_filter.compute_interests_hash.return_value = "stable"
        ai_filter.classify_batch.return_value = [{
            "news_item_id": 7,
            "tag_id": 1,
            "relevance_score": 0.9,
            "importance_score": 0.8,
            "ai_summary": "育种进展",
        }]
        return ai_filter

    def test_strict_result_write_count_mismatch_fails_pipeline(self):
        storage = MagicMock()
        storage.replace_ai_filter_batch_strict.return_value = {
            "results": 0,
            "analyzed": 1,
        }
        storage.get_analyzed_news_ids_strict.return_value = {7}
        storage.get_active_ai_filter_results_strict.return_value = []
        pipeline = self._mock_pipeline(storage)

        with patch(
            "trendradar.ai.filter_pipeline.AIFilter",
            return_value=self._fake_filter(),
        ):
            result = pipeline.run()

        self.assertFalse(result.success)
        self.assertIn("存储", result.error)

    def test_strict_analyzed_id_read_failure_fails_pipeline(self):
        storage = MagicMock()
        storage.get_analyzed_news_ids_strict.side_effect = RuntimeError(
            "analyzed ids broken"
        )
        pipeline = self._mock_pipeline(storage)

        with patch(
            "trendradar.ai.filter_pipeline.AIFilter",
            return_value=self._fake_filter(),
        ):
            result = pipeline.run()

        self.assertFalse(result.success)
        self.assertIn("存储", result.error)

    def test_strict_active_result_read_failure_fails_pipeline(self):
        storage = MagicMock()
        storage.replace_ai_filter_batch_strict.return_value = {
            "results": 1,
            "analyzed": 1,
        }
        storage.get_analyzed_news_ids_strict.return_value = {7}
        storage.get_active_ai_filter_results_strict.side_effect = RuntimeError(
            "active results broken"
        )
        pipeline = self._mock_pipeline(storage)

        with patch(
            "trendradar.ai.filter_pipeline.AIFilter",
            return_value=self._fake_filter(),
        ):
            result = pipeline.run()

        self.assertFalse(result.success)
        self.assertIn("存储", result.error)

    def _run_real_pipeline(self, storage, rss_id):
        pipeline = AIFilterPipeline(
            self._config(),
            storage,
            lambda: shanghai(2026, 8, 9, 10, 0),
            allowed_rss_ids={rss_id},
            rss_ids_authoritative=True,
            strict=True,
        )
        pipeline._enrich_pending_items = lambda items, _label: items
        fake_filter = self._fake_filter()
        fake_filter.classify_batch.return_value[0]["news_item_id"] = rss_id
        with patch(
            "trendradar.ai.filter_pipeline.AIFilter",
            return_value=fake_filter,
        ):
            return pipeline.run()

    def _real_storage(self, tmp):
        storage = LocalStorageBackend(
            data_dir=tmp,
            enable_txt=False,
            enable_html=False,
            timezone="Asia/Shanghai",
        )
        now = shanghai(2026, 8, 9, 10, 0)
        time_patch = patch.object(
            storage, "_get_configured_time", return_value=now
        )
        time_patch.start()
        self.addCleanup(time_patch.stop)
        save_rss_day(storage, "2026-08-09", "09:30", [RSSItem(
            title="Strict storage article",
            feed_id="journal",
            feed_name="Journal",
            url="https://example.org/strict-storage",
        )])
        self.assertEqual(storage.save_ai_filter_tags(
            [{
                "tag": "育种",
                "description": "育种进展",
                "priority": 1,
            }],
            version=1,
            prompt_hash="stable",
            interests_file="ai_interests.txt",
        ), 1)
        rss_id = storage.get_all_rss_ids_strict("2026-08-09")[0]["id"]
        return storage, rss_id

    def test_strict_analyzed_write_exception_fails_real_sqlite_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage, rss_id = self._real_storage(tmp)
            try:
                conn = storage._get_connection("2026-08-09")
                conn.execute("""
                    CREATE TRIGGER fail_analyzed_write
                    BEFORE INSERT ON ai_filter_analyzed_news
                    BEGIN
                        SELECT RAISE(ABORT, 'analyzed write broken');
                    END
                """)
                conn.commit()

                result = self._run_real_pipeline(storage, rss_id)

                self.assertFalse(result.success)
                self.assertIn("存储", result.error)
            finally:
                storage.cleanup()

    def test_strict_result_table_damage_fails_real_sqlite_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage, rss_id = self._real_storage(tmp)
            try:
                conn = storage._get_connection("2026-08-09")
                conn.execute("DROP TABLE ai_filter_results")
                conn.execute("CREATE TABLE ai_filter_results (id INTEGER)")
                conn.commit()

                result = self._run_real_pipeline(storage, rss_id)

                self.assertFalse(result.success)
                self.assertIn("存储", result.error)
            finally:
                storage.cleanup()


class DailyDeliveryMixedRSSCrawlTimeTests(unittest.TestCase):
    @staticmethod
    def _storage(tmp):
        return LocalStorageBackend(
            data_dir=tmp,
            enable_txt=False,
            enable_html=False,
            timezone="Asia/Shanghai",
        )

    @staticmethod
    def _item(title, url):
        return RSSItem(
            title=title,
            feed_id="journal",
            feed_name="Journal",
            url=url,
        )

    def test_latest_batch_uses_crawl_record_order_with_mixed_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = self._storage(tmp)
            try:
                save_rss_day(storage, "2026-08-09", "20:00", [
                    self._item("Legacy evening", "https://example.org/legacy"),
                ])
                save_rss_day(storage, "2026-08-09", "2026-08-09 21:00:05", [
                    self._item("Precise latest", "https://example.org/latest"),
                ])

                latest = storage.get_latest_rss_data("2026-08-09")

                self.assertEqual(latest.crawl_time, "2026-08-09 21:00:05")
                self.assertEqual(
                    [item.title for item in latest.items["journal"]],
                    ["Precise latest"],
                )
            finally:
                storage.cleanup()

    def test_incremental_normalizes_legacy_and_precise_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = self._storage(tmp)
            try:
                old_item = self._item(
                    "Already seen", "https://example.org/already-seen"
                )
                save_rss_day(storage, "2026-08-09", "20:00", [old_item])
                current = RSSData(
                    date="2026-08-09",
                    crawl_time="2026-08-09 21:00:05",
                    items={"journal": [
                        old_item,
                        self._item("Actually new", "https://example.org/new"),
                    ]},
                    id_to_name={"journal": "Journal"},
                    failed_ids=[],
                )
                self.assertTrue(storage.save_rss_data(current))

                new_items = storage.detect_new_rss_items(current)

                self.assertEqual(
                    [item.title for item in new_items["journal"]],
                    ["Actually new"],
                )
            finally:
                storage.cleanup()


class DailyDeliveryStrictTranslationTests(unittest.TestCase):
    @staticmethod
    def _dispatcher(batch_results):
        translator = MagicMock()
        translator.enabled = True
        translator.target_language = "English"
        translator.scope = {"HOTLIST": True, "RSS": True, "STANDALONE": True}
        translator.translate_batch.side_effect = batch_results
        dispatcher = NotificationDispatcher(
            config={
                "AI_TRANSLATION": {"BATCH_SIZE": 2, "BATCH_INTERVAL": 0},
                "DEBUG": False,
            },
            get_time_func=lambda: shanghai(2026, 8, 9, 10, 0),
            split_content_func=MagicMock(),
            translator=translator,
        )
        return dispatcher

    @staticmethod
    def _report_data():
        return {"stats": [{
            "word": "育种",
            "titles": [
                {"title": "one"}, {"title": "two"}, {"title": "three"},
            ],
        }], "new_titles": []}

    def test_strict_translation_rejects_partial_batch_failure(self):
        dispatcher = self._dispatcher([
            BatchTranslationResult(
                results=[
                    TranslationResult("ONE", "one", True),
                    TranslationResult("TWO", "two", True),
                ],
                success_count=2,
                total_count=2,
            ),
            BatchTranslationResult(
                results=[TranslationResult(original_text="three", error="failed")],
                fail_count=1,
                total_count=1,
            ),
        ])

        with self.assertRaisesRegex(RuntimeError, "翻译.*失败"):
            dispatcher.translate_content(
                self._report_data(),
                display_regions={"HOTLIST": True},
                require_all=True,
            )

    def test_strict_translation_rejects_all_batches_failed(self):
        dispatcher = self._dispatcher([
            BatchTranslationResult(
                results=[
                    TranslationResult(original_text="one", error="failed"),
                    TranslationResult(original_text="two", error="failed"),
                ],
                fail_count=2,
                total_count=2,
            ),
            BatchTranslationResult(
                results=[TranslationResult(original_text="three", error="failed")],
                fail_count=1,
                total_count=1,
            ),
        ])

        with self.assertRaisesRegex(RuntimeError, "翻译.*失败"):
            dispatcher.translate_content(
                self._report_data(),
                display_regions={"HOTLIST": True},
                require_all=True,
            )


class DailyDeliveryStrictSenderBatchTests(unittest.TestCase):
    @staticmethod
    def _dispatcher(channel_config):
        config = {
            "MAX_ACCOUNTS_PER_CHANNEL": 3,
            "AI_TRANSLATION": {"ENABLED": False},
            "DISPLAY": {"REGIONS": {"HOTLIST": True}},
            "BATCH_SEND_INTERVAL": 0,
        }
        config.update(channel_config)
        return NotificationDispatcher(
            config=config,
            get_time_func=lambda: shanghai(2026, 8, 9, 10, 0),
            split_content_func=lambda *args, **kwargs: ["batch one", "batch two"],
        )

    @staticmethod
    def _response(status_code, body=None):
        response = MagicMock(status_code=status_code)
        response.json.return_value = body or {}
        response.text = "failed"
        return response

    def test_ntfy_strict_delivery_requires_every_sender_batch(self):
        dispatcher = self._dispatcher({
            "NTFY_SERVER_URL": "https://ntfy.example",
            "NTFY_TOPIC": "rice",
        })

        with patch(
            "trendradar.notification.senders.requests.post",
            side_effect=[self._response(200), self._response(500)],
        ), patch("trendradar.notification.senders.time.sleep"):
            strict = dispatcher.dispatch_all(
                report_data={"stats": [], "new_titles": []},
                report_type="每日新增",
                mode="daily_delivery",
                require_all_targets=True,
            )

        with patch(
            "trendradar.notification.senders.requests.post",
            side_effect=[self._response(200), self._response(500)],
        ), patch("trendradar.notification.senders.time.sleep"):
            ordinary = dispatcher.dispatch_all(
                report_data={"stats": [], "new_titles": []},
                report_type="当日汇总",
                mode="daily",
                require_all_targets=False,
            )

        self.assertFalse(strict["ntfy"])
        self.assertTrue(ordinary["ntfy"])

    def test_bark_strict_delivery_requires_every_sender_batch(self):
        dispatcher = self._dispatcher({
            "BARK_URL": "https://api.day.app/device-key",
            "BARK_BATCH_SIZE": 3600,
        })
        responses = [
            self._response(200, {"code": 200}),
            self._response(500),
        ]

        with patch(
            "trendradar.notification.senders.requests.post",
            side_effect=responses,
        ), patch("trendradar.notification.senders.time.sleep"):
            strict = dispatcher.dispatch_all(
                report_data={"stats": [], "new_titles": []},
                report_type="每日新增",
                mode="daily_delivery",
                require_all_targets=True,
            )

        with patch(
            "trendradar.notification.senders.requests.post",
            side_effect=[
                self._response(200, {"code": 200}),
                self._response(500),
            ],
        ), patch("trendradar.notification.senders.time.sleep"):
            ordinary = dispatcher.dispatch_all(
                report_data={"stats": [], "new_titles": []},
                report_type="当日汇总",
                mode="daily",
                require_all_targets=False,
            )

        self.assertFalse(strict["bark"])
        self.assertTrue(ordinary["bark"])

class DailyDeliveryRemoteStrictReadTests(unittest.TestCase):
    @staticmethod
    def _build_remote_with_readable_current_day(tmp):
        local = LocalStorageBackend(
            data_dir=tmp,
            enable_txt=False,
            enable_html=False,
            timezone="Asia/Shanghai",
        )
        save_rss_day(local, "2026-08-09", "09-00", [RSSItem(
            title="Readable current day",
            feed_id="journal",
            feed_name="Journal",
            url="https://example.org/current",
        )])
        local.cleanup()

        backend = RemoteStorageBackend.__new__(RemoteStorageBackend)
        backend.bucket_name = "test-bucket"
        backend.temp_dir = Path(tmp)
        backend.timezone = "Asia/Shanghai"
        backend._db_connections = {}
        backend._downloaded_files = []
        backend._remote_provenance = {}
        backend._strict_local_authoritative = {
            "rss/2026-08-09.db",
            "rss/first-seen-v1.db",
        }
        backend.s3_client = MagicMock()
        backend.save_rss_data = MagicMock(return_value=True)
        return backend

    @staticmethod
    def _build_snapshot(backend):
        return DailyDeliveryAggregator(
            backend, "Asia/Shanghai"
        ).build(
            now=shanghai(2026, 8, 9, 10, 0),
            checkpoint="2026-08-08 10:00:00",
        )

    def test_remote_access_denied_is_not_treated_as_missing_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._build_remote_with_readable_current_day(tmp)
            access_denied = ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "HeadObject",
            )
            backend.s3_client.head_object.side_effect = access_denied
            try:
                with self.assertRaises(ClientError) as raised:
                    self._build_snapshot(backend)
                self.assertIs(raised.exception, access_denied)
            finally:
                backend.cleanup()

    def test_remote_download_exception_is_not_treated_as_missing_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._build_remote_with_readable_current_day(tmp)
            backend.s3_client.head_object.return_value = {}
            backend.s3_client.get_object.side_effect = RuntimeError(
                "download failed"
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "download failed"):
                    self._build_snapshot(backend)
            finally:
                backend.cleanup()

    def test_remote_404_remains_a_real_missing_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._build_remote_with_readable_current_day(tmp)
            backend.s3_client.head_object.side_effect = ClientError(
                {"Error": {"Code": "404", "Message": "not found"}},
                "HeadObject",
            )
            try:
                snapshot = self._build_snapshot(backend)
                self.assertEqual(
                    [item.title for item in snapshot.iter_items()],
                    ["Readable current day"],
                )
                self.assertEqual(snapshot.missing_dates, ["2026-08-08"])
            finally:
                backend.cleanup()

    def test_remote_history_listing_failure_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._build_remote_with_readable_current_day(tmp)
            paginator = backend.s3_client.get_paginator.return_value
            paginator.paginate.side_effect = RuntimeError("history list failed")
            operation = getattr(
                backend,
                "get_earliest_rss_discoveries_strict",
                lambda *_args, **_kwargs: None,
            )
            try:
                with self.assertRaisesRegex(
                    RuntimeError, "history list failed"
                ):
                    operation(
                        {("url", "https://example.org/current")},
                        "2026-08-09",
                    )
            finally:
                backend.cleanup()


class DailyDeliveryStrictRSSIDReadTests(unittest.TestCase):
    def test_local_strict_ids_raise_for_corrupt_empty_snapshot_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            try:
                save_rss_day(backend, "2026-08-09", "09:30", [])
                conn = backend._get_connection("2026-08-09", db_type="rss")
                conn.execute("DROP TABLE rss_items")
                conn.commit()

                self.assertEqual(backend.get_all_rss_ids("2026-08-09"), [])
                with self.assertRaises(sqlite3.OperationalError):
                    backend.get_all_rss_ids_strict("2026-08-09")
            finally:
                backend.cleanup()

    def test_authoritative_pipeline_propagates_strict_id_read_failure(self):
        storage = MagicMock()
        storage.get_all_rss_ids_strict.side_effect = RuntimeError("broken ids")
        storage.get_all_rss_ids.return_value = []
        pipeline = AIFilterPipeline(
            config={
                "TIMEZONE": "Asia/Shanghai",
                "RSS": {"ENABLED": True, "FRESHNESS_FILTER": {}},
                "AI": {},
                "AI_FILTER": {},
                "FILTER": {},
            },
            storage_manager=storage,
            get_time_func=lambda: shanghai(2026, 8, 9, 10, 0),
            allowed_rss_ids={7},
            rss_ids_authoritative=True,
        )

        with self.assertRaisesRegex(RuntimeError, "broken ids"):
            pipeline._collect_pending_news("ai_interests.txt")

        storage.get_all_rss_ids.assert_not_called()

    def test_storage_manager_forwards_strict_id_read(self):
        manager = StorageManager.__new__(StorageManager)
        backend = MagicMock()
        backend.get_all_rss_ids_strict.return_value = [{"id": 7}]
        manager.get_backend = MagicMock(return_value=backend)

        rows = manager.get_all_rss_ids_strict("2026-08-09")

        self.assertEqual(rows, [{"id": 7}])
        backend.get_all_rss_ids_strict.assert_called_once_with("2026-08-09")

    @staticmethod
    def _remote(tmp):
        backend = RemoteStorageBackend.__new__(RemoteStorageBackend)
        backend.bucket_name = "test-bucket"
        backend.temp_dir = Path(tmp) / "remote-cache"
        backend.timezone = "Asia/Shanghai"
        backend._db_connections = {}
        backend._downloaded_files = []
        backend.s3_client = MagicMock()
        return backend

    def test_remote_strict_ids_keep_real_404_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._remote(tmp)
            backend.s3_client.head_object.side_effect = ClientError(
                {"Error": {"Code": "404", "Message": "not found"}},
                "HeadObject",
            )
            try:
                self.assertEqual(
                    backend.get_all_rss_ids_strict("2026-08-09"), []
                )
            finally:
                backend.cleanup()

    def test_remote_strict_ids_propagate_access_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._remote(tmp)
            denied = ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "HeadObject",
            )
            backend.s3_client.head_object.side_effect = denied
            try:
                with self.assertRaises(ClientError) as raised:
                    backend.get_all_rss_ids_strict("2026-08-09")
                self.assertIs(raised.exception, denied)
            finally:
                backend.cleanup()

    def test_remote_strict_ids_propagate_corrupt_downloaded_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._remote(tmp)
            backend.s3_client.head_object.return_value = {}
            body = MagicMock()
            body.iter_chunks.return_value = [b"not-a-sqlite-database"]
            backend.s3_client.get_object.return_value = {"Body": body}
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    backend.get_all_rss_ids_strict("2026-08-09")
            finally:
                backend.cleanup()


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
