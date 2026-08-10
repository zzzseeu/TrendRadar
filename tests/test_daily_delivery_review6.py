import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytz

from trendradar.__main__ import NewsAnalyzer
from trendradar.ai.analyzer import AIAnalysisResult
from trendradar.context import AppContext
from trendradar.core.daily_delivery import DailyDeliveryAggregator
from trendradar.core.scheduler import Scheduler
from trendradar.crawler.rss.fetcher import RSSFeedConfig, RSSFetcher
from trendradar.storage.local import LocalStorageBackend

from tests.test_daily_delivery_review4 import (
    _ConditionalS3,
    news_db_bytes,
    remote_backend,
)
from tests.test_daily_delivery_review3 import _FailSoftThirdPartyStorage
from tests.test_daily_delivery_review5 import _news_db_with_period
from tests.test_daily_delivery_schedule import (
    RSS_STAT,
    DailyDeliveryScheduleTests,
    delivery_schedule,
)


TIMEZONE = "Asia/Shanghai"


def shanghai(year, month, day, hour, minute, second=0):
    return pytz.timezone(TIMEZONE).localize(
        datetime(year, month, day, hour, minute, second)
    )


class _RSSResponse:
    apparent_encoding = "utf-8"
    encoding = "utf-8"

    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class _TwoFeedSession:
    def get(self, url, **_kwargs):
        slug = "feed-a" if "feed-a" in url else "feed-b"
        return _RSSResponse(
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<rss version='2.0'><channel><title>Test</title>"
            f"<item><title>{slug} rice update</title>"
            f"<link>https://example.org/{slug}/article</link>"
            f"<guid>https://example.org/{slug}/article</guid>"
            "<pubDate>Fri, 07 Aug 2026 15:59:00 GMT</pubDate>"
            "<description>Rice evidence</description></item>"
            "</channel></rss>"
        )


def two_feeds():
    return [
        RSSFeedConfig(
            id="feed-a",
            name="Feed A",
            url="https://example.org/feed-a.xml",
        ),
        RSSFeedConfig(
            id="feed-b",
            name="Feed B",
            url="https://example.org/feed-b.xml",
        ),
    ]


class RSSFetcherFrozenRunClockTests(unittest.TestCase):
    def test_two_feeds_reuse_fetch_all_clock_for_discovery(self):
        run_at = shanghai(2026, 8, 9, 23, 59)
        after_midnight = shanghai(2026, 8, 10, 0, 1)
        fetcher = RSSFetcher(
            two_feeds(),
            request_interval=0,
            timezone=TIMEZONE,
        )
        fetcher.session = _TwoFeedSession()

        with patch(
            "trendradar.crawler.rss.fetcher.get_configured_time",
            side_effect=[run_at, after_midnight, after_midnight],
        ), patch(
            "trendradar.utils.time.get_configured_time",
            return_value=after_midnight,
        ):
            data = fetcher.fetch_all()

        self.assertEqual(sum(map(len, data.items.values())), 2)
        self.assertEqual(data.date, "2026-08-09")
        self.assertEqual(data.crawl_time, "2026-08-09 23:59:00")
        self.assertEqual(
            {
                item.first_time
                for items in data.items.values()
                for item in items
            },
            {"2026-08-09 23:59:00"},
        )

    def test_real_two_feed_main_path_keeps_items_inside_delivery_window(self):
        run_at = shanghai(2026, 8, 9, 23, 59)
        after_midnight = shanghai(2026, 8, 10, 0, 1)
        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone=TIMEZONE,
            )
            analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
            analyzer.report_mode = "daily_delivery"
            analyzer.proxy_url = None
            analyzer.storage_manager = storage
            analyzer._run_at = run_at
            analyzer._run_date = "2026-08-09"
            analyzer._run_time_filename = "23-59"
            analyzer.ctx = SimpleNamespace(
                rss_enabled=True,
                rss_feeds=[
                    {
                        "id": feed.id,
                        "name": feed.name,
                        "url": feed.url,
                    }
                    for feed in two_feeds()
                ],
                rss_config={
                    "REQUEST_INTERVAL": 0,
                    "TIMEOUT": 1,
                    "USE_PROXY": False,
                },
                config={"TIMEZONE": TIMEZONE, "DEBUG": False},
                timezone=TIMEZONE,
            )

            try:
                with patch.object(
                    RSSFetcher,
                    "_create_session",
                    return_value=_TwoFeedSession(),
                ), patch(
                    "trendradar.crawler.rss.fetcher.get_configured_time",
                    side_effect=[run_at, after_midnight, after_midnight],
                ):
                    analyzer._crawl_rss_data()

                saved = analyzer._daily_delivery_rss_data
                self.assertEqual(saved.date, "2026-08-09")
                self.assertEqual(
                    {
                        item.first_time
                        for items in saved.items.values()
                        for item in items
                    },
                    {"2026-08-09 23:59:00"},
                )

                snapshot = DailyDeliveryAggregator(
                    storage, TIMEZONE
                ).build(
                    run_at,
                    (run_at - timedelta(hours=24)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                )
                self.assertEqual(snapshot.window.end, run_at)
                self.assertEqual(len(snapshot.allowed_rss_ids), 2)
                self.assertEqual(
                    sum(len(items) for items in snapshot.data.items.values()),
                    2,
                )
            finally:
                storage.cleanup()


class RemoteAIMutationRollbackTests(unittest.TestCase):
    def test_nonbatch_strict_tag_cas_conflict_restores_then_refreshes_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(key, news_db_bytes(tmp, "tag-baseline"), "v1")
            backend = remote_backend(Path(tmp) / "cache", s3)
            try:
                backend.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                s3.before_condition = lambda changed_key: s3.set(
                    changed_key,
                    news_db_bytes(
                        tmp, "tag-winner", prompt_hash="winner-hash"
                    ),
                    "winner",
                )
                with self.assertRaises(Exception):
                    backend.replace_ai_filter_tags_strict(
                        [{
                            "tag": "失败标签",
                            "description": "不得残留",
                            "priority": 1,
                        }],
                        2,
                        "failed-hash",
                        "2026-08-09",
                        "rice.txt",
                    )

                refreshed = backend.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                self.assertEqual(refreshed["prompt_hash"], "winner-hash")
                self.assertNotIn(key, backend._strict_local_authoritative)
            finally:
                backend.cleanup()


class _CommitFailingConnection:
    def __init__(self, connection):
        self.connection = connection
        self.fail_next_commit = True

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise sqlite3.OperationalError("commit failed")
        return self.connection.commit()


class StrictPeriodMutationRollbackTests(unittest.TestCase):
    def test_period_mixin_rolls_back_when_commit_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone=TIMEZONE,
            )
            date = "2026-08-09"
            real = backend._get_connection(date)
            proxy = _CommitFailingConnection(real)
            try:
                with patch.object(
                    backend, "_get_connection", return_value=proxy
                ):
                    self.assertFalse(backend._record_period_execution_impl(
                        date, "daily_delivery", "push"
                    ))
                self.assertEqual(
                    real.execute(
                        "SELECT COUNT(*) FROM period_executions"
                    ).fetchone()[0],
                    0,
                )
            finally:
                backend.cleanup()

    def test_remote_strict_false_restores_pre_mutation_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(key, news_db_bytes(tmp, "period-false"), "v1")
            backend = remote_backend(Path(tmp) / "cache", s3)

            def commit_then_report_false(
                date_str, period_key, action, conn=None
            ):
                bound = conn or backend._get_ai_connection(
                    date_str, strict=True
                )
                bound.execute(
                    """INSERT INTO period_executions
                       (execution_date, period_key, action, executed_at)
                       VALUES (?, ?, ?, ?)""",
                    (date_str, period_key, action, "2026-08-09 23:59:00"),
                )
                bound.commit()
                return False

            try:
                with patch.object(
                    backend,
                    "_record_period_execution_impl",
                    side_effect=commit_then_report_false,
                ):
                    self.assertFalse(backend.record_period_execution_strict(
                        "2026-08-09", "daily_delivery", "push"
                    ))
                self.assertFalse(backend.has_period_executed_strict(
                    "2026-08-09", "daily_delivery", "push"
                ))
                self.assertNotIn(key, backend._strict_local_authoritative)
            finally:
                backend.cleanup()

    def test_remote_strict_period_reuses_single_bound_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(key, news_db_bytes(tmp, "period-bound"), "v1")
            backend = remote_backend(Path(tmp) / "cache", s3)
            original_get_connection = backend._get_connection
            calls = []

            def capture_connection(*args, **kwargs):
                calls.append((args, kwargs))
                return original_get_connection(*args, **kwargs)

            try:
                with patch.object(
                    backend,
                    "_get_connection",
                    side_effect=capture_connection,
                ):
                    self.assertTrue(backend.record_period_execution_strict(
                        "2026-08-09", "daily_delivery", "push"
                    ))
                self.assertEqual(len(calls), 1)
                self.assertTrue(backend.has_period_executed_strict(
                    "2026-08-09", "daily_delivery", "push"
                ))
            finally:
                backend.cleanup()


class RemoteAIMutationRollbackAdditionalTests(unittest.TestCase):
    def test_batch_strict_upload_failure_restores_first_mutation_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(key, news_db_bytes(tmp, "batch-baseline"), "v1")
            backend = remote_backend(Path(tmp) / "cache", s3)
            try:
                backend.begin_batch()
                backend.replace_ai_filter_tags_strict(
                    [{
                        "tag": "批次新标签",
                        "description": "不得残留",
                        "priority": 1,
                    }],
                    2,
                    "batch-new-hash",
                    "2026-08-09",
                    "rice.txt",
                )
                s3.fail_keys_once.add(key)
                with self.assertRaises(Exception):
                    backend.end_batch_strict()

                restored = backend.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                self.assertEqual(restored["prompt_hash"], "old-hash")
                self.assertNotIn(key, backend._strict_local_authoritative)
            finally:
                backend.cleanup()

    def test_batch_lower_false_restores_first_mutation_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(key, news_db_bytes(tmp, "batch-false-baseline"), "v1")
            backend = remote_backend(Path(tmp) / "cache", s3)
            try:
                backend.begin_batch()
                replaced = backend.replace_ai_filter_tags_strict(
                    [{
                        "tag": "批次临时标签",
                        "description": "不得提交",
                        "priority": 1,
                    }],
                    2,
                    "batch-false-hash",
                    "2026-08-09",
                    "rice.txt",
                )
                self.assertIsNotNone(replaced)
                with patch.object(
                    backend,
                    "_replace_ai_filter_batch_strict_impl",
                    return_value=False,
                ):
                    self.assertFalse(backend.replace_ai_filter_batch_strict(
                        [],
                        [],
                        [],
                        "rice.txt",
                        "batch-false-hash",
                        "2026-08-09",
                    ))

                with self.assertRaises(RuntimeError):
                    backend.end_batch_strict()
                snapshot = backend.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                self.assertEqual(snapshot["prompt_hash"], "old-hash")
            finally:
                backend.cleanup()


class DailyDeliveryAIAnalysisModeTests(unittest.TestCase):
    def _assert_mode_stays_authoritative_rss(self, configured_mode):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer._run_at = shanghai(2026, 8, 9, 10, 0)
        analyzer._run_date = "2026-08-09"
        analyzer._run_time_filename = "10-00"
        analyzer.frequency_file = None
        analyzer.ctx = SimpleNamespace(config={
            "AI": {},
            "AI_ANALYSIS": {
                "ENABLED": True,
                "MODE": configured_mode,
            },
            "DEBUG": False,
        })
        analyzer._prepare_ai_analysis_data = MagicMock(return_value=(
            [{
                "word": "hotlist-only",
                "count": 1,
                "titles": [{"title": "Forbidden hotlist"}],
            }],
            {"hot": "Hotlist"},
        ))
        ai = MagicMock()
        ai.analyze.return_value = AIAnalysisResult(
            success=True,
            rss_insights="Only the delivery RSS snapshot",
        )

        with patch("trendradar.__main__.AIAnalyzer", return_value=ai):
            result = analyzer._run_ai_analysis(
                stats=[],
                rss_items=[RSS_STAT],
                mode="daily_delivery",
                report_type="每日新增",
                id_to_name={"hot": "Hotlist"},
                current_results={
                    "hot": {"Forbidden hotlist": {"ranks": [1]}}
                },
                schedule=delivery_schedule(
                    once_analyze=False, once_push=False
                ),
            )

        self.assertTrue(result.success)
        self.assertEqual(result.ai_mode, "daily_delivery")
        analyzer._prepare_ai_analysis_data.assert_not_called()
        kwargs = ai.analyze.call_args.kwargs
        self.assertEqual(kwargs["stats"], [])
        self.assertEqual(kwargs["rss_stats"], [RSS_STAT])
        self.assertEqual(kwargs["report_mode"], "daily_delivery")
        self.assertEqual(kwargs["platforms"], [])

    def test_configured_daily_mode_cannot_escape_authoritative_rss(self):
        self._assert_mode_stays_authoritative_rss("daily")

    def test_configured_current_mode_cannot_escape_authoritative_rss(self):
        self._assert_mode_stays_authoritative_rss("current")

    def test_configured_incremental_mode_cannot_escape_authoritative_rss(self):
        self._assert_mode_stays_authoritative_rss("incremental")


class RemoteAIMutationRollbackMoreTests(unittest.TestCase):
    def test_strict_classification_upload_failure_removes_local_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(key, news_db_bytes(tmp, "result-baseline"), "v1")
            backend = remote_backend(Path(tmp) / "cache", s3)
            try:
                tag = backend.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )["tags"][0]
                s3.fail_keys_once.add(key)
                with self.assertRaises(Exception):
                    backend.replace_ai_filter_batch_strict(
                        [{
                            "news_item_id": 7,
                            "source_type": "rss",
                            "tag_id": tag["id"],
                            "relevance_score": 0.9,
                            "importance_score": 0.8,
                            "ai_summary": "不得残留的结果",
                        }],
                        [],
                        [7],
                        "rice.txt",
                        "old-hash",
                        "2026-08-09",
                    )

                self.assertEqual(
                    backend.get_active_ai_filter_results_strict(
                        "2026-08-09", "rice.txt"
                    ),
                    [],
                )
                self.assertNotIn(key, backend._strict_local_authoritative)
            finally:
                backend.cleanup()


class StrictLatestPeriodCapabilityTests(unittest.TestCase):
    def test_third_party_weak_latest_does_not_satisfy_daily_strict_read(self):
        backend = _FailSoftThirdPartyStorage(None)
        scheduler = Scheduler.__new__(Scheduler)
        scheduler.storage = backend
        scheduler.timeline = {
            "default": {"report_mode": "current"},
            "periods": {
                "delivery": {"report_mode": "daily_delivery"},
            },
        }
        scheduler.fallback_report_mode = "current"

        self.assertIsNone(backend.get_latest_period_execution(
            "daily_delivery", "push", "2026-08-09"
        ))
        with self.assertRaises(NotImplementedError):
            scheduler.latest_execution(
                "delivery", "push", "2026-08-09"
            )

    def test_local_strict_latest_reads_record_and_propagates_corrupt_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone=TIMEZONE,
            )
            try:
                with patch.object(
                    backend,
                    "_get_configured_time",
                    return_value=shanghai(2026, 8, 9, 10, 2),
                ):
                    self.assertTrue(backend.record_period_execution_strict(
                        "2026-08-09", "daily_delivery", "push"
                    ))
                self.assertEqual(
                    backend.get_latest_period_execution_strict(
                        "daily_delivery", "push", "2026-08-09"
                    ),
                    "2026-08-09 10:02:00",
                )

                conn = backend._get_connection("2026-08-09")
                conn.execute("DROP TABLE period_executions")
                conn.execute(
                    "CREATE TABLE period_executions (broken TEXT)"
                )
                conn.commit()
                with self.assertRaises(RuntimeError):
                    backend.get_latest_period_execution_strict(
                        "daily_delivery", "push", "2026-08-09"
                    )
            finally:
                backend.cleanup()

    def test_remote_strict_latest_reads_authoritative_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(
                key,
                _news_db_with_period(tmp, "latest-remote", executed=True),
                "v1",
            )
            backend = remote_backend(Path(tmp) / "cache", s3)
            try:
                self.assertIsNotNone(
                    backend.get_latest_period_execution_strict(
                        "daily_delivery", "push", "2026-08-09"
                    )
                )
            finally:
                backend.cleanup()

    def test_remote_strict_latest_propagates_access_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(
                key,
                _news_db_with_period(tmp, "latest-denied", executed=True),
                "v1",
            )
            original_head = s3.head_object

            def deny(Bucket, Key):
                if Key == key:
                    from botocore.exceptions import ClientError

                    raise ClientError(
                        {"Error": {"Code": "AccessDenied"}},
                        "HeadObject",
                    )
                return original_head(Bucket=Bucket, Key=Key)

            s3.head_object = deny
            backend = remote_backend(Path(tmp) / "cache", s3)
            try:
                with self.assertRaises(Exception) as raised:
                    backend.get_latest_period_execution_strict(
                        "daily_delivery", "push", "2026-08-09"
                    )
                self.assertNotIsInstance(raised.exception, AttributeError)
            finally:
                backend.cleanup()

class RemoteAIOrdinaryMutationRollbackTests(unittest.TestCase):
    def test_ordinary_ai_wrapper_returns_zero_and_restores_on_upload_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(key, news_db_bytes(tmp, "ordinary-baseline"), "v1")
            backend = remote_backend(Path(tmp) / "cache", s3)
            try:
                backend.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                s3.fail_keys_once.add(key)
                saved = backend.save_ai_filter_tags(
                    [{
                        "tag": "普通新标签",
                        "description": "不得残留",
                        "priority": 2,
                    }],
                    2,
                    "ordinary-new-hash",
                    "2026-08-09",
                    "rice.txt",
                )
                refreshed = backend.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                self.assertEqual(saved, 0)
                self.assertEqual(refreshed["prompt_hash"], "old-hash")
                self.assertEqual(len(refreshed["tags"]), 1)
            finally:
                backend.cleanup()

    def test_ordinary_ai_wrapper_returns_zero_when_baseline_read_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(key, news_db_bytes(tmp, "ordinary-head-baseline"), "v1")
            original_head = s3.head_object

            def deny(Bucket, Key):
                if Key == key:
                    from botocore.exceptions import ClientError

                    raise ClientError(
                        {"Error": {"Code": "AccessDenied"}},
                        "HeadObject",
                    )
                return original_head(Bucket=Bucket, Key=Key)

            s3.head_object = deny
            backend = remote_backend(Path(tmp) / "cache", s3)
            try:
                saved = backend.save_ai_filter_tags(
                    [{
                        "tag": "普通新标签",
                        "description": "不得写入",
                        "priority": 2,
                    }],
                    2,
                    "ordinary-denied-hash",
                    "2026-08-09",
                    "rice.txt",
                )
                self.assertEqual(saved, 0)

                s3.head_object = original_head
                snapshot = backend.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                self.assertEqual(snapshot["prompt_hash"], "old-hash")
            finally:
                backend.cleanup()

    def test_ordinary_batch_zero_noop_keeps_prior_successful_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(key, news_db_bytes(tmp, "ordinary-noop-baseline"), "v1")
            backend = remote_backend(Path(tmp) / "cache", s3)
            try:
                backend.begin_batch()
                self.assertEqual(backend.update_ai_filter_tags_hash(
                    "rice.txt", "ordinary-noop-hash", "2026-08-09"
                ), 1)
                with patch.object(
                    backend,
                    "_clear_unmatched_analyzed_news_impl",
                    return_value=0,
                ):
                    self.assertEqual(backend.clear_unmatched_analyzed_news(
                        "2026-08-09", "rice.txt"
                    ), 0)

                self.assertTrue(backend.end_batch())
                snapshot = backend.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                self.assertEqual(
                    snapshot["prompt_hash"], "ordinary-noop-hash"
                )
            finally:
                backend.cleanup()


class FrozenPresentationClockTests(unittest.TestCase):
    def test_main_passes_frozen_clock_to_html_and_notification_factory(self):
        run_at = shanghai(2026, 8, 9, 23, 59, 59)
        helper = DailyDeliveryScheduleTests()
        analyzer, _scheduler, _dispatcher = helper.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
            html_enabled=True,
        )
        analyzer.ctx.get_time.return_value = run_at

        self.assertTrue(analyzer.run())

        self.assertIs(
            analyzer.ctx.generate_html.call_args.kwargs["operation_at"],
            run_at,
        )
        analyzer.ctx.create_notification_dispatcher.assert_called_once_with(
            operation_at=run_at
        )

    def test_app_context_html_uses_frozen_path_and_renderer_clock(self):
        run_at = shanghai(2026, 8, 9, 23, 59, 59)
        ctx = AppContext({
            "TIMEZONE": TIMEZONE,
            "DISPLAY": {"REGIONS": {}, "REGION_ORDER": []},
        })
        ctx.render_html = MagicMock(return_value="<html>frozen</html>")

        with patch(
            "trendradar.context.generate_html_report",
            return_value="output/html/2026-08-09/23-59.html",
        ) as generate:
            result = ctx.generate_html([], 0, operation_at=run_at)

        self.assertEqual(result, "output/html/2026-08-09/23-59.html")
        kwargs = generate.call_args.kwargs
        self.assertEqual(kwargs["date_folder"], "2026-08-09")
        self.assertEqual(kwargs["time_filename"], "23-59")
        kwargs["render_html_func"]({}, 0, "daily_delivery", None)
        renderer_clock = ctx.render_html.call_args.kwargs["get_time_func"]
        self.assertIs(renderer_clock(), run_at)

    def test_app_context_dispatcher_freezes_real_webhook_payloads(self):
        run_at = shanghai(2026, 8, 9, 23, 59, 59)
        ctx = AppContext({
            "TIMEZONE": TIMEZONE,
            "MAX_ACCOUNTS_PER_CHANNEL": 3,
            "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/webhook/test",
            "DINGTALK_WEBHOOK_URL": "https://oapi.dingtalk.com/robot/send",
            "FEISHU_BATCH_SIZE": 29000,
            "DINGTALK_BATCH_SIZE": 20000,
            "BATCH_SEND_INTERVAL": 0,
            "DISPLAY": {"REGIONS": {
                "HOTLIST": False,
                "RSS": True,
                "NEW_ITEMS": True,
                "AI_ANALYSIS": True,
                "STANDALONE": False,
            }},
            "AI_TRANSLATION": {"ENABLED": False},
        })
        dispatcher = ctx.create_notification_dispatcher(operation_at=run_at)
        response = MagicMock(status_code=200)
        response.json.return_value = {"code": 0, "errcode": 0}
        report_data = {
            "stats": [],
            "new_titles": [],
            "failed_ids": [],
            "total_new_count": 0,
            "period_label": "2026-08-08 10:00—2026-08-09 23:59",
        }

        with patch(
            "trendradar.notification.senders.requests.post",
            side_effect=[response, response],
        ) as post:
            results = dispatcher.dispatch_all(
                report_data=report_data,
                report_type="每日新增",
                mode="daily_delivery",
                rss_items=[{
                    "word": "育种",
                    "count": 1,
                    "titles": [{
                        "title": "Rice breeding",
                        "source_name": "Journal",
                        "url": "https://example.org/rice",
                        "mobile_url": "",
                        "reader_url": "",
                        "ranks": [],
                        "rank_threshold": 5,
                        "time_display": "2026-08-09 23:30",
                        "count": 1,
                    }],
                }],
                require_all_targets=True,
            )

        self.assertEqual(results, {"feishu": True, "dingtalk": True})
        payloads = [str(call.kwargs["json"]) for call in post.call_args_list]
        self.assertEqual(len(payloads), 2)
        for payload in payloads:
            self.assertIn("2026-08-09 23:59:59", payload)
            self.assertNotIn("2026-08-10", payload)
            self.assertIn(report_data["period_label"], payload)

    def test_app_context_dispatcher_freezes_email_subject_and_body(self):
        run_at = shanghai(2026, 8, 9, 23, 59, 59)
        ctx = AppContext({
            "TIMEZONE": TIMEZONE,
            "MAX_ACCOUNTS_PER_CHANNEL": 3,
            "EMAIL_FROM": "sender@example.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_TO": "reader@example.com",
            "EMAIL_SMTP_SERVER": "smtp.example.com",
            "EMAIL_SMTP_PORT": 587,
            "DISPLAY": {"REGIONS": {}},
            "AI_TRANSLATION": {"ENABLED": False},
        })
        dispatcher = ctx.create_notification_dispatcher(operation_at=run_at)

        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "report.html"
            html_path.write_text("<html>daily</html>", encoding="utf-8")
            with patch(
                "trendradar.notification.senders.smtplib.SMTP"
            ) as smtp_class:
                smtp_class.return_value.send_message.return_value = {}
                results = dispatcher.dispatch_all(
                    report_data={},
                    report_type="每日新增",
                    mode="daily_delivery",
                    html_file_path=str(html_path),
                    require_all_targets=True,
                )

        self.assertEqual(results, {"email": True})
        message = smtp_class.return_value.send_message.call_args.args[0]
        subject = str(make_header(decode_header(message["Subject"])))
        text_body = message.get_payload()[0].get_payload(decode=True).decode()
        self.assertIn("08月09日 23:59", subject)
        self.assertIn("2026-08-09 23:59:59", text_body)
        self.assertNotIn("2026-08-10", subject + text_body)


if __name__ == "__main__":
    unittest.main()
