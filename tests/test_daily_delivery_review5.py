import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from botocore.exceptions import ClientError
import pytz

from trendradar.__main__ import NewsAnalyzer
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.core.scheduler import Scheduler
from trendradar.storage.base import NewsData, NewsItem, RSSItem

from tests.test_daily_delivery_review4 import (
    _ConditionalS3,
    local_backend,
    remote_backend,
    rss_data,
)
from tests.test_daily_delivery_schedule import (
    DailyDeliveryScheduleTests,
    RSS_STAT,
    delivery_snapshot,
    snapshot_rss_data,
)


def _raw_rss_versions(tmp):
    data_dir = Path(tmp) / "raw-source"
    backend = local_backend(data_dir)
    date = "2026-08-09"
    first = RSSItem(
        title="Generation one",
        feed_id="journal",
        url="https://example.org/generation-one",
    )
    second = RSSItem(
        title="Generation two",
        feed_id="journal",
        url="https://example.org/generation-two",
    )
    try:
        success, *_ = backend._save_rss_data_impl(
            rss_data(date, "2026-08-09 09:00:01", first)
        )
        if not success:
            raise AssertionError("failed to build generation one")
        db_path = data_dir / "rss" / f"{date}.db"
        generation_one = db_path.read_bytes()

        success, *_ = backend._save_rss_data_impl(
            rss_data(date, "2026-08-09 09:05:01", second)
        )
        if not success:
            raise AssertionError("failed to build generation two")
        generation_two = db_path.read_bytes()
        return generation_one, generation_two
    finally:
        backend.cleanup()


class FirstSeenSnapshotConsistencyTests(unittest.TestCase):
    def test_remote_version_change_after_snapshot_read_is_not_marked_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            generation_one, generation_two = _raw_rss_versions(tmp)
            s3 = _ConditionalS3()
            source_key = "rss/2026-08-09.db"
            s3.set(source_key, generation_one, "v2")
            backend = remote_backend(Path(tmp) / "cache", s3)
            original_version = backend._get_rss_source_version_strict
            version_reads = 0

            def change_to_v3(date):
                nonlocal version_reads
                version_reads += 1
                # inventory、read-before、snapshot-bind 后，在事务已读完的
                # final provenance check 才切到 v3。
                if version_reads == 4:
                    s3.set(source_key, generation_two, "v3")
                return original_version(date)

            identity = ("url", "https://example.org/generation-two")
            try:
                with patch.object(
                    backend,
                    "_get_rss_source_version_strict",
                    side_effect=change_to_v3,
                ):
                    with self.assertRaisesRegex(RuntimeError, "版本.*变化"):
                        backend.get_earliest_rss_discoveries_strict(
                            {identity}, "2026-08-09"
                        )

                earliest = backend.get_earliest_rss_discoveries_strict(
                    {identity}, "2026-08-09"
                )
                self.assertEqual(
                    earliest[identity],
                    ("2026-08-09 09:05:01", "2026-08-09"),
                )
                ledger = backend._get_first_seen_ledger_connection(strict=True)
                source_version = ledger.execute(
                    """SELECT source_version FROM rss_first_seen_sources
                       WHERE source_key = ?""",
                    ("2026-08-09",),
                ).fetchone()[0]
                self.assertIn("v3", source_version)
            finally:
                backend.cleanup()

    def test_local_wal_change_after_snapshot_read_is_not_marked_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = local_backend(tmp)
            date = "2026-08-09"
            first = RSSItem(
                title="Local generation one",
                feed_id="journal",
                url="https://example.org/local-generation-one",
            )
            second = RSSItem(
                title="Local generation two",
                feed_id="journal",
                url="https://example.org/local-generation-two",
            )
            third = RSSItem(
                title="Local generation three",
                feed_id="journal",
                url="https://example.org/local-generation-three",
            )
            conn = backend._get_rss_connection(date)
            conn.execute("PRAGMA journal_mode=WAL").fetchone()
            conn.commit()
            self.assertTrue(backend.save_rss_data(rss_data(
                date, "2026-08-09 09:00:01", first
            )))
            success, *_ = backend._save_rss_data_impl(rss_data(
                date, "2026-08-09 09:05:01", second
            ))
            self.assertTrue(success)
            original_version = backend._get_rss_source_version_strict
            version_reads = 0

            def write_next_generation(changed_date):
                nonlocal version_reads
                version_reads += 1
                # Local inventory reads file provenance directly; patched calls
                # are read-before, snapshot-bind, then final-check.
                if version_reads == 3:
                    success, *_ = backend._save_rss_data_impl(rss_data(
                        date, "2026-08-09 09:10:01", third
                    ))
                    self.assertTrue(success)
                return original_version(changed_date)

            identity = ("url", "https://example.org/local-generation-three")
            try:
                with patch.object(
                    backend,
                    "_get_rss_source_version_strict",
                    side_effect=write_next_generation,
                ):
                    with self.assertRaisesRegex(RuntimeError, "版本.*变化"):
                        backend.get_earliest_rss_discoveries_strict(
                            {identity}, date
                        )

                earliest = backend.get_earliest_rss_discoveries_strict(
                    {identity}, date
                )
                self.assertEqual(
                    earliest[identity],
                    ("2026-08-09 09:10:01", date),
                )
            finally:
                backend.cleanup()


class FirstSeenIncrementalWatermarkTests(unittest.TestCase):
    def test_second_generation_does_not_replay_old_outbox_or_fallback_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = local_backend(tmp)
            date = "2026-08-09"
            old_identity = ("url", "https://example.org/old-generation")
            new_identity = ("url", "https://example.org/new-generation")
            self.assertTrue(backend.save_rss_data(rss_data(
                date,
                "2026-08-09 09:00:01",
                RSSItem(
                    title="Old generation",
                    feed_id="journal",
                    url=old_identity[1],
                ),
            )))

            original_upsert = backend._upsert_first_seen_rows
            replayed = []

            def capture_rows(conn, rows):
                rows = list(rows)
                replayed.extend(identity for identity, *_ in rows)
                return original_upsert(conn, rows)

            try:
                with patch.object(
                    backend,
                    "_upsert_first_seen_rows",
                    side_effect=capture_rows,
                ):
                    self.assertTrue(backend.save_rss_data(rss_data(
                        date,
                        "2026-08-09 09:05:01",
                        RSSItem(
                            title="New generation",
                            feed_id="journal",
                            url=new_identity[1],
                        ),
                    )))
                self.assertIn(new_identity, replayed)
                self.assertNotIn(old_identity, replayed)
            finally:
                backend.cleanup()

    def test_failed_incremental_consume_does_not_advance_watermark(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = local_backend(tmp)
            date = "2026-08-09"
            self.assertTrue(backend.save_rss_data(rss_data(
                date,
                "2026-08-09 09:00:01",
                RSSItem(
                    title="Committed generation",
                    feed_id="journal",
                    url="https://example.org/committed-generation",
                ),
            )))
            ledger = backend._get_first_seen_ledger_connection(strict=True)
            before = ledger.execute(
                """SELECT watermark FROM rss_first_seen_sources
                   WHERE source_key = ?""",
                (date,),
            ).fetchone()[0]
            success, *_ = backend._save_rss_data_impl(rss_data(
                date,
                "2026-08-09 09:05:01",
                RSSItem(
                    title="Pending generation",
                    feed_id="journal",
                    url="https://example.org/pending-generation",
                ),
            ))
            self.assertTrue(success)

            try:
                with patch.object(
                    backend,
                    "_upsert_first_seen_rows",
                    side_effect=sqlite3.DatabaseError("ledger write failed"),
                ):
                    with self.assertRaisesRegex(
                        sqlite3.DatabaseError, "ledger write failed"
                    ):
                        backend.get_earliest_rss_discoveries_strict(
                            {("url", "https://example.org/pending-generation")},
                            date,
                        )
                after = ledger.execute(
                    """SELECT watermark FROM rss_first_seen_sources
                       WHERE source_key = ?""",
                    (date,),
                ).fetchone()[0]
                self.assertEqual(after, before)
            finally:
                backend.cleanup()


def _news_data(date="2026-08-09"):
    return NewsData(
        date=date,
        crawl_time="10:00",
        items={"hot": [NewsItem(
            title="Fresh hotlist item",
            source_id="hot",
            source_name="Hotlist",
            rank=1,
            url="https://example.org/hotlist-item",
        )]},
        id_to_name={"hot": "Hotlist"},
        failed_ids=[],
    )


def _news_db_with_period(tmp, name, *, executed=False):
    data_dir = Path(tmp) / name
    backend = local_backend(data_dir)
    date = "2026-08-09"
    try:
        backend.replace_ai_filter_tags_strict(
            [{"tag": "稻作", "description": "水稻", "priority": 1}],
            1,
            "prompt-v1",
            date,
            "rice.txt",
        )
        if executed:
            backend.record_period_execution_strict(
                date, "daily_delivery", "push"
            )
    finally:
        backend.cleanup()
    return (data_dir / "news" / f"{date}.db").read_bytes()


class SharedNewsDatabaseCASTests(unittest.TestCase):
    def test_stale_hotlist_writer_preserves_newer_checkpoint_and_ai_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(key, _news_db_with_period(tmp, "baseline"), "v1")
            writer_a = remote_backend(Path(tmp) / "writer-a", s3)
            writer_b = remote_backend(Path(tmp) / "writer-b", s3)
            try:
                # 两个进程都先读取 v1；A 随后以 CAS 写入 checkpoint。
                writer_a.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                writer_b.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                self.assertTrue(writer_a.record_period_execution_strict(
                    "2026-08-09", "daily_delivery", "push"
                ))

                # B 的热榜写必须刷新/合并或因冲突失败，绝不能无条件覆盖 A。
                writer_b.save_news_data(_news_data())

                verifier = remote_backend(Path(tmp) / "verifier", s3)
                try:
                    conn = verifier._get_ai_connection(
                        "2026-08-09", strict=True
                    )
                    self.assertIsNotNone(conn.execute(
                        """SELECT 1 FROM period_executions
                           WHERE execution_date = ? AND period_key = ?
                             AND action = ?""",
                        ("2026-08-09", "daily_delivery", "push"),
                    ).fetchone())
                    self.assertEqual(
                        conn.execute(
                            "SELECT title FROM news_items"
                        ).fetchone()[0],
                        "Fresh hotlist item",
                    )
                    self.assertEqual(
                        conn.execute(
                            """SELECT prompt_hash FROM ai_filter_tags
                               WHERE status = 'active'"""
                        ).fetchone()[0],
                        "prompt-v1",
                    )
                finally:
                    verifier.cleanup()
            finally:
                writer_a.cleanup()
                writer_b.cleanup()

    def test_daily_delivery_stops_when_hotlist_storage_fails(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.report_mode = "daily_delivery"
        analyzer.request_interval = 0
        analyzer.data_fetcher = MagicMock()
        analyzer.data_fetcher.crawl_websites.return_value = (
            {"hot": {"Fresh": {"ranks": [1], "url": "https://e/h"}}},
            {"hot": "Hotlist"},
            [],
        )
        analyzer.storage_manager = MagicMock()
        analyzer.storage_manager.save_news_data.return_value = False
        analyzer.ctx = SimpleNamespace(
            platforms=[{"id": "hot", "name": "Hotlist"}],
            config={"PLATFORMS_API_URL": ""},
            format_time=MagicMock(return_value="10-00"),
            format_date=MagicMock(return_value="2026-08-09"),
        )

        with self.assertRaisesRegex(RuntimeError, "热榜.*保存失败"):
            analyzer._crawl_data()
        analyzer.storage_manager.save_txt_snapshot.assert_not_called()


class StrictPeriodReadTests(unittest.TestCase):
    def test_local_corrupt_period_table_raises_only_in_strict_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = local_backend(tmp)
            date = "2026-08-09"
            conn = backend._get_connection(date)
            conn.execute("DROP TABLE period_executions")
            conn.execute("CREATE TABLE period_executions (broken TEXT)")
            conn.commit()
            try:
                self.assertFalse(backend.has_period_executed(
                    date, "daily_delivery", "push"
                ))
                with self.assertRaises(sqlite3.DatabaseError):
                    backend.has_period_executed_strict(
                        date, "daily_delivery", "push"
                    )
            finally:
                backend.cleanup()

    def test_remote_strict_period_read_refreshes_updated_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(key, _news_db_with_period(tmp, "before"), "v1")
            backend = remote_backend(Path(tmp) / "cache", s3)
            try:
                self.assertFalse(backend.has_period_executed_strict(
                    "2026-08-09", "daily_delivery", "push"
                ))
                s3.set(
                    key,
                    _news_db_with_period(tmp, "after", executed=True),
                    "v2",
                )
                self.assertTrue(backend.has_period_executed_strict(
                    "2026-08-09", "daily_delivery", "push"
                ))
            finally:
                backend.cleanup()

    def test_remote_strict_period_read_propagates_access_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(key, _news_db_with_period(tmp, "denied"), "v1")
            backend = remote_backend(Path(tmp) / "cache", s3)
            original_head = s3.head_object

            def deny(Bucket, Key):
                if Key == key:
                    raise ClientError(
                        {"Error": {"Code": "AccessDenied"}}, "HeadObject"
                    )
                return original_head(Bucket=Bucket, Key=Key)

            s3.head_object = deny
            try:
                with self.assertRaises(ClientError):
                    backend.has_period_executed_strict(
                        "2026-08-09", "daily_delivery", "push"
                    )
            finally:
                backend.cleanup()

    def test_scheduler_routes_strict_period_read_by_report_mode(self):
        scheduler = Scheduler.__new__(Scheduler)
        scheduler.storage = MagicMock()
        scheduler.timeline = {
            "default": {"report_mode": "current"},
            "periods": {
                "delivery": {"report_mode": "daily_delivery"},
            },
        }
        scheduler.fallback_report_mode = "current"
        scheduler.storage.has_period_executed_strict.return_value = True

        self.assertTrue(scheduler.already_executed(
            "delivery", "push", "2026-08-09"
        ))
        scheduler.storage.has_period_executed_strict.assert_called_once_with(
            "2026-08-09", "delivery", "push"
        )


class FrozenOperationDateTests(unittest.TestCase):
    def test_cross_midnight_run_keeps_snapshot_ai_notification_and_checkpoint_on_n(self):
        helper = DailyDeliveryScheduleTests()
        analyzer, scheduler, dispatcher = helper.build_analyzer(
            filter_method="ai",
        )
        timezone = pytz.timezone("Asia/Shanghai")
        run_at = timezone.localize(datetime(2026, 8, 9, 23, 59, 59))
        after_midnight = timezone.localize(datetime(2026, 8, 10, 0, 1, 0))
        analyzer.ctx.get_time.side_effect = [run_at, after_midnight]
        analyzer.ctx.format_date.return_value = "2026-08-10"
        analyzer.ctx.format_time.return_value = "00-01"
        storage = MagicMock()
        storage.save_rss_data.return_value = True
        analyzer.storage_manager = storage
        after_midnight_rss = snapshot_rss_data()
        after_midnight_rss.date = "2026-08-10"
        after_midnight_rss.crawl_time = "2026-08-10 00:01:00"

        with patch(
            "trendradar.__main__.DailyDeliveryAggregator"
        ) as aggregator_class, patch(
            "trendradar.core.analyzer.count_rss_frequency"
        ) as count_rss_frequency, patch(
            "trendradar.crawler.rss.RSSFetcher"
        ) as fetcher_class:
            aggregator_class.return_value.build.return_value = (
                delivery_snapshot()
            )
            count_rss_frequency.return_value = ([RSS_STAT], 1)
            fetcher_class.return_value.fetch_all.return_value = (
                after_midnight_rss
            )
            self.assertTrue(analyzer.run())

        self.assertEqual(analyzer.ctx.get_time.call_count, 1)
        saved_rss = storage.save_rss_data.call_args.args[0]
        self.assertEqual(saved_rss.date, "2026-08-09")
        self.assertEqual(saved_rss.crawl_time, "2026-08-09 23:59:59")
        scheduler.resolve.assert_called_once_with(run_at)
        aggregator_class.return_value.build.assert_called_once_with(
            run_at, scheduler.latest_execution.return_value
        )
        scheduler.latest_execution.assert_called_once_with(
            "daily_delivery", "push", "2026-08-09"
        )
        self.assertTrue(all(
            call.args[2] == "2026-08-09"
            for call in scheduler.already_executed.call_args_list
        ))
        self.assertEqual(
            analyzer.ctx.run_ai_filter.call_args.kwargs["operation_date"],
            "2026-08-09",
        )
        self.assertEqual(
            analyzer.ctx.convert_ai_filter_to_report_data.call_args.kwargs[
                "operation_date"
            ],
            "2026-08-09",
        )
        dispatcher.dispatch_all.assert_called_once()
        scheduler.record_execution.assert_called_once_with(
            "daily_delivery", "push", "2026-08-09"
        )

    def test_strict_pipeline_reads_and_writes_only_explicit_operation_date(self):
        storage = MagicMock()
        storage.backend_name = "fake"
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
        n_row = {
            "id": 7,
            "title": "N-day rice result",
            "source_id": "journal",
            "source_name": "Journal",
            "url": "https://example.org/n-day",
            "published_at": "2026-08-09T15:00:00+08:00",
            "summary": "N-day evidence",
        }
        n_plus_one_row = {
            **n_row,
            "title": "N+1 colliding id",
            "url": "https://example.org/n-plus-one",
        }

        def rss_rows(*, date=None):
            return [n_row] if date == "2026-08-09" else [n_plus_one_row]

        storage.get_all_rss_ids_strict.side_effect = rss_rows
        storage.get_analyzed_news_ids_strict.return_value = set()
        storage.replace_ai_filter_batch_strict.return_value = {
            "results": 1,
            "analyzed": 1,
        }
        storage.get_active_ai_filter_results_strict.return_value = [{
            **n_row,
            "news_item_id": 7,
            "source_type": "rss",
            "tag_id": 1,
            "tag": "育种",
            "tag_description": "",
            "tag_priority": 1,
            "relevance_score": 0.9,
            "importance_score": 0.8,
            "ai_summary": "N-day summary",
        }]
        config = {
            "TIMEZONE": "Asia/Shanghai",
            "RSS": {
                "ENABLED": True,
                "FEEDS": [{"id": "journal", "name": "Journal"}],
                "FRESHNESS_FILTER": {"ENABLED": False},
            },
            "AI": {},
            "AI_FILTER": {
                "INTERESTS_FILE": "rice.txt",
                "BATCH_SIZE": 10,
                "BATCH_INTERVAL": 0,
            },
            "FILTER": {},
        }
        pipeline = AIFilterPipeline(
            config,
            storage,
            lambda: pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 0, 1)
            ),
            allowed_rss_ids={7},
            rss_ids_authoritative=True,
            strict=True,
            operation_date="2026-08-09",
        )
        pipeline._enrich_pending_items = lambda items, _label: items
        ai_filter = MagicMock()
        ai_filter.load_interests_content.return_value = "育种"
        ai_filter.compute_interests_hash.return_value = "stable"
        ai_filter.classify_batch.return_value = [{
            "news_item_id": 7,
            "tag_id": 1,
            "relevance_score": 0.9,
            "importance_score": 0.8,
            "ai_summary": "N-day summary",
        }]

        with patch(
            "trendradar.ai.filter_pipeline.AIFilter",
            return_value=ai_filter,
        ):
            result = pipeline.run()

        self.assertTrue(result.success)
        classified = ai_filter.classify_batch.call_args.args[0]
        self.assertEqual([row["title"] for row in classified], [
            "N-day rice result"
        ])
        storage.get_ai_filter_tag_snapshot_strict.assert_called_once_with(
            date="2026-08-09", interests_file="rice.txt"
        )
        storage.get_all_rss_ids_strict.assert_called_once_with(
            date="2026-08-09"
        )
        storage.get_analyzed_news_ids_strict.assert_called_once_with(
            "rss", date="2026-08-09", interests_file="rice.txt"
        )
        self.assertEqual(
            storage.replace_ai_filter_batch_strict.call_args.kwargs["date"],
            "2026-08-09",
        )
        storage.get_active_ai_filter_results_strict.assert_called_once_with(
            date="2026-08-09", interests_file="rice.txt"
        )
