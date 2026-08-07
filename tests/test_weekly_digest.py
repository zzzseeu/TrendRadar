import unittest
from datetime import datetime
from unittest.mock import MagicMock

import pytz

from trendradar.core.weekly import WeeklyRSSAggregator, previous_natural_week
from trendradar.storage.base import RSSData, RSSItem
from trendradar.utils.time import parse_iso_datetime


def rss_data(date, *items):
    grouped = {}
    names = {}
    for item in items:
        grouped.setdefault(item.feed_id, []).append(item)
        names[item.feed_id] = item.feed_name or item.feed_id
    return RSSData(date=date, crawl_time="10-00", items=grouped, id_to_name=names)


class NaturalWeekWindowTests(unittest.TestCase):
    def test_previous_week_is_monday_to_monday_in_shanghai(self):
        tz = pytz.timezone("Asia/Shanghai")
        window = previous_natural_week(
            tz.localize(datetime(2026, 8, 10, 10, 0)),
            "Asia/Shanghai",
        )

        self.assertEqual(window.start.isoformat(), "2026-08-03T00:00:00+08:00")
        self.assertEqual(window.end.isoformat(), "2026-08-10T00:00:00+08:00")
        self.assertEqual(window.label, "2026-08-03—2026-08-09")
        self.assertEqual(
            window.storage_dates,
            [
                "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
                "2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10",
            ],
        )

    def test_window_is_half_open_across_year_boundary(self):
        tz = pytz.timezone("Asia/Shanghai")
        window = previous_natural_week(
            tz.localize(datetime(2027, 1, 4, 10, 0)),
            "Asia/Shanghai",
        )

        self.assertTrue(window.contains("2026-12-28T00:00:00+08:00"))
        self.assertTrue(window.contains("2027-01-03T23:59:59+08:00"))
        self.assertFalse(window.contains("2027-01-04T00:00:00+08:00"))
        self.assertFalse(window.contains(""))
        self.assertFalse(window.contains("invalid"))

    def test_naive_iso_time_keeps_existing_utc_assumption(self):
        parsed = parse_iso_datetime("2026-08-02T16:00:00", "Asia/Shanghai")
        self.assertEqual(parsed.isoformat(), "2026-08-03T00:00:00+08:00")


class WeeklyRSSAggregatorTests(unittest.TestCase):
    def test_rss_item_dict_round_trip_preserves_guid(self):
        item = RSSItem(title="GUID item", feed_id="journal", guid="stable-guid")

        restored = RSSItem.from_dict(item.to_dict())

        self.assertEqual(restored.guid, "stable-guid")

    def test_reads_eight_dates_and_uses_half_open_window(self):
        tz = pytz.timezone("Asia/Shanghai")
        storage = MagicMock()
        by_date = {
            "2026-08-03": rss_data("2026-08-03", RSSItem(
                title="Week start", feed_id="journal",
                url="https://example.org/start",
                published_at="2026-08-03T00:00:00+08:00",
            )),
            "2026-08-10": rss_data("2026-08-10",
                RSSItem(title="Sunday night", feed_id="journal",
                        url="https://example.org/sunday",
                        published_at="2026-08-09T23:59:59+08:00"),
                RSSItem(title="This Monday", feed_id="journal",
                        url="https://example.org/monday",
                        published_at="2026-08-10T00:00:00+08:00"),
            ),
        }
        storage.get_rss_data.side_effect = lambda date: by_date.get(date)
        storage.save_rss_data.return_value = True
        storage.get_all_rss_ids.return_value = [
            {"id": 11, "source_id": "journal", "title": "Week start",
             "url": "https://example.org/start"},
            {"id": 12, "source_id": "journal", "title": "Sunday night",
             "url": "https://example.org/sunday"},
            {"id": 13, "source_id": "journal", "title": "This Monday",
             "url": "https://example.org/monday"},
        ]

        result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
            tz.localize(datetime(2026, 8, 10, 10, 0))
        )

        self.assertEqual(storage.get_rss_data.call_count, 8)
        self.assertEqual(
            [item.title for item in result.iter_items()],
            ["Week start", "Sunday night"],
        )
        self.assertEqual(result.allowed_rss_ids, {11, 12})
        self.assertEqual(result.filtered_out, 1)
        self.assertEqual(len(result.missing_dates), 6)

    def test_canonical_url_dedup_keeps_richer_search_record(self):
        storage = MagicMock()
        first = RSSItem(
            title="Breeding result", feed_id="agri-news-search",
            url="https://example.org/story?utm_source=google", summary="short",
            published_at="2026-08-05T08:00:00Z", source_count=1,
            search_providers="google_news",
        )
        second = RSSItem(
            title="Breeding result", feed_id="agri-news-search",
            url="https://example.org/story", summary="a much richer summary",
            published_at="2026-08-05T08:00:00Z", source_count=3,
            pre_hot_score=0.8, search_providers="gdelt",
        )
        storage.get_rss_data.side_effect = lambda date: {
            "2026-08-05": rss_data(date, first),
            "2026-08-06": rss_data(date, second),
        }.get(date)
        storage.save_rss_data.return_value = True
        storage.get_all_rss_ids.return_value = [{
            "id": 21, "source_id": "agri-news-search",
            "title": "Breeding result", "url": "https://example.org/story",
        }]

        result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
            pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            )
        )
        merged = list(result.iter_items())[0]

        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(merged.summary, "a much richer summary")
        self.assertEqual(merged.source_count, 3)
        self.assertEqual(merged.search_providers, "gdelt,google_news")

    def test_title_fallback_deduplicates_when_urls_are_unusable(self):
        storage = MagicMock()
        first = RSSItem(
            title="Breeding: a breakthrough", feed_id="journal",
            published_at="2026-08-05T08:00:00+08:00",
        )
        second = RSSItem(
            title="Breeding a breakthrough!", feed_id="journal",
            published_at="2026-08-06T08:00:00+08:00",
        )
        storage.get_rss_data.side_effect = lambda date: {
            "2026-08-05": rss_data(date, first),
            "2026-08-06": rss_data(date, second),
        }.get(date)
        storage.save_rss_data.return_value = True
        storage.get_all_rss_ids.return_value = [{
            "id": 31, "source_id": "journal",
            "title": "Breeding: a breakthrough", "url": "",
        }]

        result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
            pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            )
        )

        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(len(list(result.iter_items())), 1)

    def test_iter_items_has_a_stable_global_order(self):
        storage = MagicMock()
        storage.get_rss_data.side_effect = lambda date: (
            rss_data(date,
                RSSItem(title="First alpha", feed_id="alpha",
                        url="https://example.org/first-alpha",
                        published_at="2026-08-05T08:00:00+08:00"),
                RSSItem(title="Middle zeta", feed_id="zeta",
                        url="https://example.org/middle-zeta",
                        published_at="2026-08-05T09:00:00+08:00"),
                RSSItem(title="Last alpha", feed_id="alpha",
                        url="https://example.org/last-alpha",
                        published_at="2026-08-05T10:00:00+08:00"),
            ) if date == "2026-08-05" else None
        )
        storage.save_rss_data.return_value = True
        storage.get_all_rss_ids.return_value = [
            {"id": 41, "source_id": "alpha", "title": "First alpha",
             "url": "https://example.org/first-alpha"},
            {"id": 42, "source_id": "zeta", "title": "Middle zeta",
             "url": "https://example.org/middle-zeta"},
            {"id": 43, "source_id": "alpha", "title": "Last alpha",
             "url": "https://example.org/last-alpha"},
        ]

        result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
            pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            )
        )

        self.assertEqual(
            [item.title for item in result.iter_items()],
            ["First alpha", "Middle zeta", "Last alpha"],
        )

    def test_empty_week_does_not_write_snapshot(self):
        storage = MagicMock()
        storage.get_rss_data.return_value = None

        result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
            pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            )
        )

        self.assertIsNone(result.data)
        storage.save_rss_data.assert_not_called()

    def test_records_failed_sources_by_storage_date(self):
        storage = MagicMock()
        data = rss_data("2026-08-05", RSSItem(
            title="Available", feed_id="journal", url="https://example.org/item",
            published_at="2026-08-05T08:00:00+08:00",
        ))
        data.failed_ids = ["unavailable-feed"]
        storage.get_rss_data.side_effect = lambda date: (
            data if date == "2026-08-05" else None
        )
        storage.save_rss_data.return_value = True
        storage.get_all_rss_ids.return_value = [{
            "id": 22, "source_id": "journal", "title": "Available",
            "url": "https://example.org/item",
        }]

        result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
            pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            )
        )

        self.assertEqual(result.data.failed_ids, [])
        self.assertEqual(result.failed_ids, ["unavailable-feed"])
        self.assertEqual(
            result.failed_sources,
            {"2026-08-05": ["unavailable-feed"]},
        )

    def test_nonempty_snapshot_requires_resolved_database_ids(self):
        storage = MagicMock()
        storage.get_rss_data.side_effect = lambda date: (
            rss_data(date, RSSItem(
                title="Available", feed_id="journal",
                url="https://example.org/item",
                published_at="2026-08-05T08:00:00+08:00",
            )) if date == "2026-08-05" else None
        )
        storage.save_rss_data.return_value = True
        storage.get_all_rss_ids.return_value = []

        with self.assertRaisesRegex(RuntimeError, "周快照 ID 解析失败"):
            WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
                pytz.timezone("Asia/Shanghai").localize(
                    datetime(2026, 8, 10, 10, 0)
                )
            )

    def test_save_failure_raises_clear_error(self):
        storage = MagicMock()
        storage.get_rss_data.side_effect = lambda date: (
            rss_data(date, RSSItem(
                title="Available", feed_id="journal",
                url="https://example.org/item",
                published_at="2026-08-05T08:00:00+08:00",
            )) if date == "2026-08-05" else None
        )
        storage.save_rss_data.return_value = False

        with self.assertRaisesRegex(RuntimeError, "周快照保存失败"):
            WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
                pytz.timezone("Asia/Shanghai").localize(
                    datetime(2026, 8, 10, 10, 0)
                )
            )

    def test_rebuilding_same_week_is_idempotent_in_sqlite(self):
        from tempfile import TemporaryDirectory
        from trendradar.storage.local import LocalStorageBackend

        tz = pytz.timezone("Asia/Shanghai")
        now = tz.localize(datetime(2026, 8, 10, 10, 0))
        with TemporaryDirectory() as data_dir:
            storage = LocalStorageBackend(
                data_dir=data_dir,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            storage.save_rss_data(rss_data("2026-08-05", RSSItem(
                title="Stable item", feed_id="journal",
                url="https://example.org/stable",
                published_at="2026-08-05T08:00:00+08:00",
            )))

            WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)
            WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)

            monday = storage.get_rss_data("2026-08-10")
            urls = [
                item.url for items in monday.items.values() for item in items
            ]
            self.assertEqual(urls.count("https://example.org/stable"), 1)
            storage.cleanup()

    def test_guid_only_item_persists_in_idempotent_sqlite_snapshot(self):
        from tempfile import TemporaryDirectory
        from trendradar.storage.local import LocalStorageBackend

        tz = pytz.timezone("Asia/Shanghai")
        now = tz.localize(datetime(2026, 8, 10, 10, 0))
        with TemporaryDirectory() as data_dir:
            storage = LocalStorageBackend(
                data_dir=data_dir,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            storage.save_rss_data(rss_data("2026-08-05", RSSItem(
                title="GUID only", feed_id="journal", guid="stable-guid",
                published_at="2026-08-05T08:00:00+08:00",
            )))

            first = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)
            second = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)

            monday = storage.get_rss_data("2026-08-10")
            items = [item for values in monday.items.values() for item in values]
            self.assertEqual([item.guid for item in items], ["stable-guid"])
            self.assertEqual(len(first.allowed_rss_ids), 1)
            self.assertEqual(len(second.allowed_rss_ids), 1)
            storage.cleanup()

    def test_canonical_snapshot_url_stays_idempotent_across_builds(self):
        from tempfile import TemporaryDirectory
        from trendradar.storage.local import LocalStorageBackend

        tz = pytz.timezone("Asia/Shanghai")
        now = tz.localize(datetime(2026, 8, 10, 10, 0))
        with TemporaryDirectory() as data_dir:
            storage = LocalStorageBackend(
                data_dir=data_dir,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            storage.save_rss_data(rss_data("2026-08-05", RSSItem(
                title="Canonical story", feed_id="journal",
                url="https://example.org/story?utm_source=newsletter",
                summary="short",
                published_at="2026-08-05T08:00:00+08:00",
            )))

            WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)

            storage.save_rss_data(rss_data("2026-08-06", RSSItem(
                title="Canonical story", feed_id="journal",
                url="https://example.org/story",
                summary="richer summary",
                published_at="2026-08-05T08:00:00+08:00",
            )))
            result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)

            monday = storage.get_rss_data("2026-08-10")
            items = [item for values in monday.items.values() for item in values]
            self.assertEqual([item.url for item in items], [
                "https://example.org/story",
            ])
            self.assertEqual([item.summary for item in items], ["richer summary"])
            self.assertEqual(len(result.allowed_rss_ids), 1)
            storage.cleanup()

    def test_rebuild_keeps_failed_sources_at_their_original_dates(self):
        from tempfile import TemporaryDirectory
        from trendradar.storage.local import LocalStorageBackend

        tz = pytz.timezone("Asia/Shanghai")
        now = tz.localize(datetime(2026, 8, 10, 10, 0))
        with TemporaryDirectory() as data_dir:
            storage = LocalStorageBackend(
                data_dir=data_dir,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            daily = rss_data("2026-08-05", RSSItem(
                title="Available", feed_id="journal",
                url="https://example.org/available",
                published_at="2026-08-05T08:00:00+08:00",
            ))
            daily.failed_ids = ["unavailable-feed"]
            storage.save_rss_data(daily)

            WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)
            rebuilt = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)

            self.assertEqual(
                rebuilt.failed_sources,
                {"2026-08-05": ["unavailable-feed"]},
            )
            storage.cleanup()

    def test_cross_feed_canonical_url_reuses_existing_snapshot_row(self):
        from tempfile import TemporaryDirectory
        from trendradar.storage.local import LocalStorageBackend

        tz = pytz.timezone("Asia/Shanghai")
        now = tz.localize(datetime(2026, 8, 10, 10, 0))
        with TemporaryDirectory() as data_dir:
            storage = LocalStorageBackend(
                data_dir=data_dir, enable_txt=False, enable_html=False,
                timezone="Asia/Shanghai",
            )
            storage.save_rss_data(rss_data("2026-08-05", RSSItem(
                title="Shared story", feed_id="feed-a",
                url="https://example.org/story?utm_source=feed-a",
                summary="short", published_at="2026-08-05T08:00:00+08:00",
            )))
            WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)

            storage.save_rss_data(rss_data("2026-08-06", RSSItem(
                title="Shared story", feed_id="feed-b",
                url="https://example.org/story", summary="richer summary",
                published_at="2026-08-05T08:00:00+08:00",
            )))
            result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)

            monday = storage.get_rss_data("2026-08-10")
            items = [item for values in monday.items.values() for item in values]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].feed_id, "feed-a")
            self.assertEqual(items[0].summary, "richer summary")
            self.assertEqual(len(result.allowed_rss_ids), 1)
            storage.cleanup()

    def test_failed_day_without_items_is_not_missing(self):
        from tempfile import TemporaryDirectory
        from trendradar.storage.local import LocalStorageBackend

        tz = pytz.timezone("Asia/Shanghai")
        now = tz.localize(datetime(2026, 8, 10, 10, 0))
        with TemporaryDirectory() as data_dir:
            storage = LocalStorageBackend(
                data_dir=data_dir, enable_txt=False, enable_html=False,
                timezone="Asia/Shanghai",
            )
            storage.save_rss_data(RSSData(
                date="2026-08-05", crawl_time="10-00", items={},
                failed_ids=["unavailable-feed"],
            ))

            daily = storage.get_rss_data("2026-08-05")
            result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)

            self.assertEqual(daily.items, {})
            self.assertEqual(daily.failed_ids, ["unavailable-feed"])
            self.assertIsNone(result.data)
            self.assertNotIn("2026-08-05", result.missing_dates)
            self.assertEqual(
                result.failed_sources,
                {"2026-08-05": ["unavailable-feed"]},
            )
            storage.cleanup()
