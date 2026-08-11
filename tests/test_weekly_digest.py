import unittest
import hashlib
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytz

from trendradar.core.weekly import (
    WeeklyRSSAggregator,
    previous_natural_week,
)
from trendradar.core.rss_snapshot import (
    item_identity,
    item_richness,
    search_providers,
    stable_title_guid,
)
from trendradar.crawler.news_search import normalize_title
from trendradar.ai.filter import AIFilterResult
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.storage.base import RSSData, RSSItem
from trendradar.storage.sqlite_mixin import SQLiteStorageMixin


SHANGHAI = pytz.timezone("Asia/Shanghai")


def rss_data(date, *items):
    grouped = {}
    names = {}
    for item in items:
        grouped.setdefault(item.feed_id, []).append(item)
        names[item.feed_id] = item.feed_name or item.feed_id
    return RSSData(date=date, crawl_time="10-00", items=grouped, id_to_name=names)


class _WeeklyAIStorage:
    def get_all_news_ids(self):
        return []

    def get_analyzed_news_ids(self, source_type, interests_file):
        return set()

    def get_all_rss_ids(self):
        return [
            {"id": 1, "source_id": "journal",
             "published_at": "2026-08-03T00:00:00+08:00"},
            {"id": 2, "source_id": "journal",
             "published_at": "2026-08-09T23:59:59+08:00"},
            {"id": 3, "source_id": "journal",
             "published_at": "2026-08-10T00:00:00+08:00"},
        ]


class WeeklyAIFilterScopeTests(unittest.TestCase):
    def setUp(self):
        tz = pytz.timezone("Asia/Shanghai")
        window = previous_natural_week(
            tz.localize(datetime(2026, 8, 10, 10, 0)),
            "Asia/Shanghai",
        )
        self.pipeline = AIFilterPipeline(
            {
                "TIMEZONE": "Asia/Shanghai",
                "RSS": {"ENABLED": True, "FRESHNESS_FILTER": {
                    "ENABLED": True, "MAX_AGE_DAYS": 1,
                }},
                "AI_FILTER": {"MIN_SCORE": 0},
            },
            _WeeklyAIStorage(),
            lambda: None,
            rss_window=window,
            allowed_rss_ids={1, 2},
            rss_ids_authoritative=False,
        )

    def test_week_start_survives_but_current_monday_is_excluded(self):
        pending = self.pipeline._collect_pending_news("ai_interests.txt")
        self.assertEqual([item["id"] for item in pending[1]], [1, 2])
        self.assertEqual(pending[-1], 1)

    def test_report_conversion_rejects_unapproved_duplicate_id(self):
        result = AIFilterResult(success=True, tags=[{
            "tag": "育种", "count": 2, "items": [
                {"id": 1, "news_item_id": 1, "title": "Allowed", "source_type": "rss",
                 "source_id": "journal",
                 "first_time": "2026-08-03T00:00:00+08:00",
                 "relevance_score": 0.9},
                {"id": 9, "news_item_id": 9, "title": "Duplicate", "source_type": "rss",
                 "source_id": "journal",
                 "first_time": "2026-08-04T00:00:00+08:00",
                 "relevance_score": 0.9},
            ],
        }])

        _, rss_stats, _ = self.pipeline.convert_to_report_data(
            result, mode="weekly"
        )

        self.assertEqual(
            [item["title"] for item in rss_stats[0]["titles"]],
            ["Allowed"],
        )

    def test_partial_weekly_batch_failure_returns_failed_filter_result(self):
        storage = MagicMock()
        storage.get_latest_prompt_hash.return_value = "stable-hash"
        storage.get_active_ai_filter_tags.return_value = [{
            "id": 1, "tag": "育种", "priority": 1,
        }]
        storage.get_all_news_ids.return_value = []
        storage.get_all_rss_ids.return_value = [
            {
                "id": 1, "title": "First", "source_id": "journal",
                "published_at": "2026-08-05T08:00:00+08:00",
            },
            {
                "id": 2, "title": "Second", "source_id": "journal",
                "published_at": "2026-08-06T08:00:00+08:00",
            },
        ]
        storage.get_analyzed_news_ids.return_value = set()
        storage.get_active_ai_filter_results.return_value = []
        pipeline = AIFilterPipeline(
            {
                "TIMEZONE": "Asia/Shanghai",
                "RSS": {
                    "ENABLED": True,
                    "FRESHNESS_FILTER": {"ENABLED": False},
                },
                "AI_FILTER": {
                    "BATCH_SIZE": 1,
                    "BATCH_INTERVAL": 0,
                },
            },
            storage,
            lambda: None,
            rss_window=self.pipeline._rss_window,
            allowed_rss_ids={1, 2},
        )
        pipeline._enrich_pending_items = MagicMock(
            side_effect=lambda items, _label: items
        )

        with patch("trendradar.ai.filter_pipeline.AIFilter") as filter_class:
            ai_filter = filter_class.return_value
            ai_filter.load_interests_content.return_value = "育种"
            ai_filter.compute_interests_hash.return_value = "stable-hash"
            ai_filter.classify_batch.side_effect = [[], None]

            result = pipeline.run("weekly.txt")

        self.assertFalse(result.success)
        self.assertIn("批次", result.error)
        storage.end_batch.assert_called_once_with()


class NaturalWeekWindowTests(unittest.TestCase):
    def test_previous_week_is_local_half_open_and_date_only_safe(self):
        now = SHANGHAI.localize(datetime(2026, 8, 10, 10, 0))
        window = previous_natural_week(now, "Asia/Shanghai")
        self.assertEqual(window.start.isoformat(), "2026-08-03T00:00:00+08:00")
        self.assertEqual(window.end.isoformat(), "2026-08-10T00:00:00+08:00")
        self.assertTrue(window.contains("2026-08-03"))
        self.assertTrue(window.contains("2026-08-09T23:59:59+08:00"))
        self.assertFalse(window.contains("2026-08-10"))
        self.assertFalse(window.contains(""))

    def test_weekly_aggregator_reads_previous_seven_days_and_run_day(self):
        window = previous_natural_week(
            SHANGHAI.localize(datetime(2026, 8, 10, 10, 0)),
            "Asia/Shanghai",
        )
        self.assertEqual(window.storage_dates[0], "2026-08-03")
        self.assertEqual(window.storage_dates[-1], "2026-08-10")
        self.assertEqual(len(window.storage_dates), 8)

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

    def test_naive_iso_time_is_not_eligible_for_weekly_window(self):
        window = previous_natural_week(
            SHANGHAI.localize(datetime(2026, 8, 10, 10, 0)),
            "Asia/Shanghai",
        )
        self.assertFalse(window.contains("2026-08-03T00:00:00"))

    def test_rss_read_doc_describes_zero_item_all_source_failure(self):
        self.assertIn(
            "全源失败且零条目时返回空 RSSData",
            SQLiteStorageMixin._get_rss_data_impl.__doc__,
        )


class WeeklyRSSAggregatorTests(unittest.TestCase):
    @staticmethod
    def _configure_complete_mock_week(storage):
        """Treat unspecified mock dates as explicitly saved successful empties."""
        weak_reader = storage.get_rss_data
        weak_id_reader = storage.get_all_rss_ids
        storage.get_rss_data_strict.side_effect = lambda date: (
            weak_reader(date) or rss_data(date)
        )
        storage.get_all_rss_ids_strict.side_effect = weak_id_reader

    def _save_complete_week(self, storage, now, by_date=None):
        by_date = by_date or {}
        for date in previous_natural_week(
            now, "Asia/Shanghai"
        ).storage_dates:
            self.assertTrue(storage.save_rss_data(
                by_date.get(date) or RSSData(
                    date=date,
                    crawl_time=f"{date} 10:00:00",
                    items={},
                    id_to_name={"journal": "Journal"},
                    failed_ids=[],
                )
            ))

    def test_snapshot_crawl_time_uses_frozen_run_at(self):
        storage = MagicMock()
        storage.get_rss_data.side_effect = lambda date: (
            rss_data(
                date,
                RSSItem(
                    title="Weekly item",
                    feed_id="journal",
                    url="https://example.org/weekly",
                    published_at="2026-08-05T08:00:00+08:00",
                ),
            ) if date == "2026-08-05" else None
        )
        storage.save_rss_data.return_value = True
        storage.get_all_rss_ids.return_value = [{
            "id": 1, "source_id": "journal", "title": "Weekly item",
            "url": "https://example.org/weekly",
        }]
        self._configure_complete_mock_week(storage)
        run_at = SHANGHAI.localize(datetime(2026, 8, 10, 10, 0))

        WeeklyRSSAggregator(storage, "Asia/Shanghai").build(run_at)

        self.assertEqual(
            storage.save_rss_data.call_args.args[0].crawl_time,
            "2026-08-10T10:00:00+08:00",
        )

    def test_shared_snapshot_identity_prefers_canonical_url(self):
        item = RSSItem(
            title="Rice breeding update",
            feed_id="feed-a",
            url="https://example.org/paper?utm_source=rss",
        )

        self.assertEqual(
            item_identity(item),
            ("url", "https://example.org/paper"),
        )

    def test_shared_snapshot_identity_uses_feed_and_normalized_title(self):
        item = RSSItem(title=" Rice   Breeding ", feed_id="feed-a")

        self.assertEqual(
            item_identity(item),
            ("title", "feed-a", "ricebreeding"),
        )

    def test_shared_title_guid_is_stable_and_namespaced(self):
        item = RSSItem(title=" Rice   Breeding ", feed_id="feed-a")

        first = stable_title_guid(item, namespace="weekly")
        second = stable_title_guid(item, namespace="weekly")

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("weekly-title:"))
        self.assertNotIn("Rice", first)

    def test_shared_snapshot_richness_preserves_weekly_ranking_tuple(self):
        item = RSSItem(
            title="Rice breeding update",
            feed_id="feed-a",
            summary="summary",
            source_count=3,
            pre_hot_score=0.8,
            author="Researcher",
        )

        self.assertEqual(item_richness(item), (7, 3, 0.8, True))

    def test_shared_search_providers_normalizes_and_deduplicates(self):
        item = RSSItem(
            title="Rice breeding update",
            feed_id="feed-a",
            search_providers=" google_news, ,gdelt, google_news, ",
        )

        self.assertEqual(search_providers(item), {"gdelt", "google_news"})

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
        self._configure_complete_mock_week(storage)

        result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
            tz.localize(datetime(2026, 8, 10, 10, 0))
        )

        self.assertEqual(storage.get_rss_data_strict.call_count, 8)
        self.assertEqual(
            [item.title for item in result.iter_items()],
            ["Week start", "Sunday night"],
        )
        self.assertEqual(result.allowed_rss_ids, {11, 12})
        self.assertEqual(result.filtered_out, 1)
        self.assertEqual(result.missing_dates, [])

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
        self._configure_complete_mock_week(storage)

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
        self._configure_complete_mock_week(storage)

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
        self._configure_complete_mock_week(storage)

        result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
            pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            )
        )

        self.assertEqual(
            [item.title for item in result.iter_items()],
            ["First alpha", "Middle zeta", "Last alpha"],
        )

    def test_all_eight_missing_databases_raise_clear_error(self):
        storage = MagicMock()
        storage.get_rss_data_strict.return_value = None

        with self.assertRaisesRegex(RuntimeError, "2026-08-03"):
            WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
                pytz.timezone("Asia/Shanghai").localize(
                    datetime(2026, 8, 10, 10, 0)
                )
            )

        storage.save_rss_data.assert_not_called()

    def test_empty_successful_sqlite_crawl_round_trips_as_rss_data(self):
        from tempfile import TemporaryDirectory
        from trendradar.storage.local import LocalStorageBackend

        with TemporaryDirectory() as data_dir:
            storage = LocalStorageBackend(
                data_dir=data_dir,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            self.assertIsNone(storage.get_rss_data("2026-08-05"))
            self.assertTrue(storage.save_rss_data(RSSData(
                date="2026-08-05",
                crawl_time="10-00",
                items={},
                id_to_name={"journal": "Journal"},
                failed_ids=[],
            )))

            restored = storage.get_rss_data("2026-08-05")

            self.assertIsNotNone(restored)
            self.assertEqual(restored.date, "2026-08-05")
            self.assertEqual(restored.crawl_time, "10-00")
            self.assertEqual(restored.items, {})
            self.assertEqual(restored.id_to_name, {"journal": "Journal"})
            self.assertEqual(restored.failed_ids, [])
            storage.cleanup()

    def test_one_empty_sqlite_day_does_not_hide_other_missing_days(self):
        from tempfile import TemporaryDirectory
        from trendradar.storage.local import LocalStorageBackend

        tz = pytz.timezone("Asia/Shanghai")
        with TemporaryDirectory() as data_dir:
            storage = LocalStorageBackend(
                data_dir=data_dir,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            storage.save_rss_data(RSSData(
                date="2026-08-05",
                crawl_time="10-00",
                items={},
                id_to_name={"journal": "Journal"},
                failed_ids=[],
            ))

            with self.assertRaisesRegex(RuntimeError, "2026-08-03"):
                WeeklyRSSAggregator(
                    storage, "Asia/Shanghai"
                ).build(tz.localize(datetime(2026, 8, 10, 10, 0)))
            storage.cleanup()

    def test_all_saved_empty_sqlite_days_make_an_empty_week_legal(self):
        from tempfile import TemporaryDirectory
        from trendradar.storage.local import LocalStorageBackend

        now = SHANGHAI.localize(datetime(2026, 8, 10, 10, 0))
        with TemporaryDirectory() as data_dir:
            storage = LocalStorageBackend(
                data_dir=data_dir,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            for date_str in previous_natural_week(
                now, "Asia/Shanghai"
            ).storage_dates:
                self.assertTrue(storage.save_rss_data(RSSData(
                    date=date_str,
                    crawl_time=f"{date_str} 10:00:00",
                    items={},
                    id_to_name={"journal": "Journal"},
                    failed_ids=[],
                )))

            snapshot = WeeklyRSSAggregator(
                storage, "Asia/Shanghai"
            ).build(now)

            self.assertIsNone(snapshot.data)
            self.assertEqual(snapshot.missing_dates, [])
            storage.cleanup()

    def test_missing_or_corrupt_sqlite_day_fails_weekly_strict_read(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from trendradar.storage.local import LocalStorageBackend

        now = SHANGHAI.localize(datetime(2026, 8, 10, 10, 0))
        dates = previous_natural_week(now, "Asia/Shanghai").storage_dates
        for broken_kind in ("missing", "corrupt"):
            with self.subTest(broken_kind=broken_kind), TemporaryDirectory() as data_dir:
                storage = LocalStorageBackend(
                    data_dir=data_dir,
                    enable_txt=False,
                    enable_html=False,
                    timezone="Asia/Shanghai",
                )
                for date_str in dates:
                    if broken_kind == "missing" and date_str == "2026-08-06":
                        continue
                    self.assertTrue(storage.save_rss_data(RSSData(
                        date=date_str,
                        crawl_time=f"{date_str} 10:00:00",
                        items={},
                        id_to_name={"journal": "Journal"},
                        failed_ids=[],
                    )))
                storage.cleanup()
                if broken_kind == "corrupt":
                    Path(data_dir, "rss", "2026-08-06.db").write_bytes(
                        b"not sqlite"
                    )

                with self.assertRaisesRegex(
                    (RuntimeError, Exception), "2026-08-06|database|SQLite|file"
                ):
                    WeeklyRSSAggregator(
                        storage, "Asia/Shanghai"
                    ).build(now)

    def test_historical_failed_source_status_fails_weekly(self):
        from tempfile import TemporaryDirectory
        from trendradar.storage.local import LocalStorageBackend

        now = SHANGHAI.localize(datetime(2026, 8, 10, 10, 0))
        with TemporaryDirectory() as data_dir:
            storage = LocalStorageBackend(
                data_dir=data_dir,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            for date_str in previous_natural_week(
                now, "Asia/Shanghai"
            ).storage_dates:
                self.assertTrue(storage.save_rss_data(RSSData(
                    date=date_str,
                    crawl_time=f"{date_str} 10:00:00",
                    items={},
                    id_to_name={"journal": "Journal"},
                    failed_ids=(
                        ["unavailable-feed"]
                        if date_str == "2026-08-06"
                        else []
                    ),
                )))

            with self.assertRaisesRegex(
                RuntimeError, "2026-08-06.*unavailable-feed"
            ):
                WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)
            storage.cleanup()

    def test_existing_daily_database_with_no_in_window_items_is_empty_week(self):
        storage = MagicMock()
        storage.get_rss_data.side_effect = lambda date: (
            rss_data(date, RSSItem(
                title="Outside window", feed_id="journal",
                url="https://example.org/old",
                published_at="2026-08-02T23:59:59+08:00",
            )) if date == "2026-08-03" else None
        )
        self._configure_complete_mock_week(storage)

        result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
            pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            )
        )

        self.assertIsNone(result.data)
        self.assertEqual(result.filtered_out, 1)
        storage.save_rss_data.assert_not_called()

    def test_failed_source_by_storage_date_fails_closed(self):
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
        self._configure_complete_mock_week(storage)

        with self.assertRaisesRegex(
            RuntimeError, "2026-08-05.*unavailable-feed"
        ):
            WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
                pytz.timezone("Asia/Shanghai").localize(
                    datetime(2026, 8, 10, 10, 0)
                )
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
        self._configure_complete_mock_week(storage)

        with self.assertRaisesRegex(RuntimeError, "周快照 ID 解析失败"):
            WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
                pytz.timezone("Asia/Shanghai").localize(
                    datetime(2026, 8, 10, 10, 0)
                )
            )

    def test_every_snapshot_identity_requires_a_resolved_database_id(self):
        storage = MagicMock()
        storage.get_rss_data.side_effect = lambda date: (
            rss_data(
                date,
                RSSItem(
                    title="Resolved", feed_id="journal",
                    url="https://example.org/resolved",
                    published_at="2026-08-05T08:00:00+08:00",
                ),
                RSSItem(
                    title="Missing", feed_id="journal",
                    url="https://example.org/missing",
                    published_at="2026-08-05T09:00:00+08:00",
                ),
            ) if date == "2026-08-05" else None
        )
        storage.save_rss_data.return_value = True
        storage.get_all_rss_ids.return_value = [{
            "id": 51, "source_id": "journal", "title": "Resolved",
            "url": "https://example.org/resolved",
        }]
        self._configure_complete_mock_week(storage)

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
        self._configure_complete_mock_week(storage)

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
            source = rss_data("2026-08-05", RSSItem(
                title="Stable item", feed_id="journal",
                url="https://example.org/stable",
                published_at="2026-08-05T08:00:00+08:00",
            ))
            self._save_complete_week(
                storage, now, {"2026-08-05": source}
            )

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
            source = rss_data("2026-08-05", RSSItem(
                title="GUID only", feed_id="journal", guid="stable-guid",
                published_at="2026-08-05T08:00:00+08:00",
            ))
            self._save_complete_week(
                storage, now, {"2026-08-05": source}
            )

            first = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)
            second = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)

            monday = storage.get_rss_data("2026-08-10")
            items = [item for values in monday.items.values() for item in values]
            self.assertEqual([item.guid for item in items], ["stable-guid"])
            self.assertEqual(len(first.allowed_rss_ids), 1)
            self.assertEqual(len(second.allowed_rss_ids), 1)
            storage.cleanup()

    def test_title_fallback_and_url_items_persist_idempotently_in_sqlite(self):
        from tempfile import TemporaryDirectory
        from trendradar.storage.local import LocalStorageBackend

        tz = pytz.timezone("Asia/Shanghai")
        now = tz.localize(datetime(2026, 8, 10, 10, 0))
        title = "Breeding: a title-only identity"
        expected_guid = "weekly-title:" + hashlib.sha256(
            f"journal\0{normalize_title(title)}".encode("utf-8")
        ).hexdigest()

        with TemporaryDirectory() as data_dir:
            backend = LocalStorageBackend(
                data_dir=data_dir,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            source_data = rss_data(
                "2026-08-05",
                RSSItem(
                    title=title, feed_id="journal", summary="first body",
                    published_at="2026-08-05T08:00:00+08:00",
                ),
                RSSItem(
                    title="Normal URL item", feed_id="journal",
                    url="https://example.org/normal",
                    published_at="2026-08-05T09:00:00+08:00",
                ),
            )
            self._save_complete_week(backend, now)

            class SnapshotStorage:
                def get_rss_data(self, date):
                    if date == "2026-08-05":
                        return source_data
                    return backend.get_rss_data(date)

                def get_rss_data_strict(self, date):
                    if date == "2026-08-05":
                        return source_data
                    return backend.get_rss_data_strict(date)

                def __getattr__(self, name):
                    return getattr(backend, name)

            storage = SnapshotStorage()

            first = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)
            second = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)

            monday = storage.get_rss_data("2026-08-10")
            items = sorted(
                (item for values in monday.items.values() for item in values),
                key=lambda item: item.title,
            )
            self.assertEqual([item.title for item in items], [
                "Breeding: a title-only identity", "Normal URL item",
            ])
            self.assertEqual(items[0].guid, expected_guid)
            self.assertEqual(items[1].guid, "")
            self.assertEqual(len(first.allowed_rss_ids), 2)
            self.assertEqual(len(second.allowed_rss_ids), 2)
            backend.cleanup()

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
            source = rss_data("2026-08-05", RSSItem(
                title="Canonical story", feed_id="journal",
                url="https://example.org/story?utm_source=newsletter",
                summary="short",
                published_at="2026-08-05T08:00:00+08:00",
            ))
            self._save_complete_week(
                storage, now, {"2026-08-05": source}
            )

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

    def test_rebuild_aborts_on_failed_source_at_its_original_date(self):
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
            self._save_complete_week(
                storage, now, {"2026-08-05": daily}
            )

            with self.assertRaisesRegex(
                RuntimeError, "2026-08-05.*unavailable-feed"
            ):
                WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)
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
            source = rss_data("2026-08-05", RSSItem(
                title="Shared story", feed_id="feed-a",
                url="https://example.org/story?utm_source=feed-a",
                summary="short", published_at="2026-08-05T08:00:00+08:00",
            ))
            self._save_complete_week(
                storage, now, {"2026-08-05": source}
            )
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

    def test_failed_day_without_items_is_not_treated_as_legal_empty(self):
        from tempfile import TemporaryDirectory
        from trendradar.storage.local import LocalStorageBackend

        tz = pytz.timezone("Asia/Shanghai")
        now = tz.localize(datetime(2026, 8, 10, 10, 0))
        with TemporaryDirectory() as data_dir:
            storage = LocalStorageBackend(
                data_dir=data_dir, enable_txt=False, enable_html=False,
                timezone="Asia/Shanghai",
            )
            failed = RSSData(
                date="2026-08-05", crawl_time="10-00", items={},
                failed_ids=["unavailable-feed"],
            )
            self._save_complete_week(
                storage, now, {"2026-08-05": failed}
            )

            daily = storage.get_rss_data("2026-08-05")
            with self.assertRaisesRegex(
                RuntimeError, "2026-08-05.*unavailable-feed"
            ):
                WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)

            self.assertEqual(daily.items, {})
            self.assertEqual(daily.failed_ids, ["unavailable-feed"])
            storage.cleanup()
