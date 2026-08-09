import unittest
import tempfile
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytz

from trendradar.__main__ import NewsAnalyzer
from trendradar.ai.analyzer import AIAnalysisResult
from trendradar.ai.filter import AIFilterResult
from trendradar.core.daily_delivery import DailyDeliveryAggregator
from trendradar.core.scheduler import ResolvedSchedule, Scheduler
from trendradar.crawler.news_search import NewsSearchResult
from trendradar.storage.base import RSSData, RSSItem
from trendradar.storage.local import LocalStorageBackend
from trendradar.storage.manager import StorageManager


NOW = pytz.timezone("Asia/Shanghai").localize(
    datetime(2026, 8, 9, 10, 0)
)
RSS_STAT = {
    "word": "育种",
    "count": 1,
    "titles": [{"title": "Rice breeding", "url": "https://example.org/rice"}],
}
SCHEDULER_TIMELINE = {"custom": {
    "default": {
        "collect": True,
        "analyze": False,
        "push": False,
        "report_mode": "current",
        "ai_mode": "follow_report",
        "once": {"analyze": False, "push": False},
    },
    "periods": {},
    "day_plans": {"daily": {"periods": []}},
    "week_map": {day: "daily" for day in range(1, 8)},
}}


def delivery_schedule(**overrides):
    values = {
        "period_key": "daily_delivery",
        "period_name": "每日新增",
        "day_plan": "daily",
        "collect": True,
        "analyze": True,
        "push": True,
        "report_mode": "daily_delivery",
        "ai_mode": "daily_delivery",
        "once_analyze": True,
        "once_push": True,
        "frequency_file": None,
        "filter_method": None,
        "interests_file": None,
    }
    values.update(overrides)
    return ResolvedSchedule(**values)


def snapshot_rss_data(items=True, failed_ids=None):
    rss_items = []
    if items:
        rss_items = [RSSItem(
            title="Rice breeding",
            feed_id="journal",
            feed_name="Journal",
            url="https://example.org/rice",
            published_at="2025-01-01T00:00:00Z",
            first_time="2026-08-09 09:30:00",
        )]
    return RSSData(
        date="2026-08-09",
        crawl_time="2026-08-09 10:00:00",
        items={"journal": rss_items} if rss_items else {},
        id_to_name={"journal": "Journal"} if rss_items else {},
        failed_ids=failed_ids or [],
    )


def delivery_snapshot(data=None, allowed_ids=None):
    return SimpleNamespace(
        window=SimpleNamespace(
            label="2026-08-08 10:00—2026-08-09 10:00"
        ),
        data=data if data is not None else snapshot_rss_data(),
        allowed_rss_ids={17} if allowed_ids is None else allowed_ids,
        total_read=1,
        filtered_out=0,
        duplicate_count=0,
        missing_dates=[],
    )


class DailyDeliveryScheduleTests(unittest.TestCase):
    def build_analyzer(
        self,
        *,
        rss_items=None,
        raw_rss_items=None,
        filter_method="keyword",
        notification_results=None,
        enable_notification=True,
        has_notification=True,
        html_enabled=False,
    ):
        scheduler = MagicMock()
        scheduler.resolve.return_value = delivery_schedule(
            filter_method=filter_method
        )
        scheduler.latest_execution.return_value = "2026-08-08 10:00:00"
        scheduler.already_executed.return_value = False
        scheduler.record_execution.return_value = True

        dispatcher = MagicMock()
        dispatcher.dispatch_all.return_value = (
            notification_results
            if notification_results is not None
            else {"wework": True}
        )
        config = {
            "ENABLE_NOTIFICATION": enable_notification,
            "SHOW_VERSION_UPDATE": False,
            "AI_ANALYSIS": {"ENABLED": False},
            "AI_TRANSLATION": {"ENABLED": False},
            "STORAGE": {"FORMATS": {"HTML": html_enabled}},
            "DISPLAY": {
                "REGIONS": {"RSS": True, "STANDALONE": False},
                "STANDALONE": {},
            },
            "TIMEZONE": "Asia/Shanghai",
            "MAX_NEWS_PER_KEYWORD": 0,
            "SORT_BY_POSITION_FIRST": False,
            "DEBUG": False,
        }
        ctx = SimpleNamespace(
            config=config,
            cleanup=MagicMock(),
            create_scheduler=MagicMock(return_value=scheduler),
            create_notification_dispatcher=MagicMock(return_value=dispatcher),
            platform_ids=["hot"],
            filter_method=filter_method,
            display_mode="keyword",
            weight_config={},
            rank_threshold=5,
            timezone="Asia/Shanghai",
            get_time=MagicMock(return_value=NOW),
            format_date=MagicMock(return_value="2026-08-09"),
            format_time=MagicMock(return_value="10-00"),
            detect_new_titles=MagicMock(return_value={
                "hot": [{"title": "Current hotlist item"}]
            }),
            load_frequency_words=MagicMock(return_value=([], [], [])),
            count_frequency=MagicMock(return_value=([], 1)),
            run_ai_filter=MagicMock(return_value=AIFilterResult(
                success=True, total_matched=1, tags=[{"tag": "育种"}]
            )),
            convert_ai_filter_to_report_data=MagicMock(
                return_value=([], [RSS_STAT], None)
            ),
            prepare_report=MagicMock(return_value={}),
            generate_html=MagicMock(return_value="/tmp/report.html"),
            rss_enabled=True,
            rss_feeds=[{
                "id": "journal",
                "name": "Journal",
                "url": "https://example.org/rss.xml",
                "enabled": True,
            }],
            rss_config={
                "REQUEST_INTERVAL": 0,
                "TIMEOUT": 15,
                "USE_PROXY": False,
                "PROXY_URL": "",
                "FRESHNESS_FILTER": {"ENABLED": True, "MAX_AGE_DAYS": 1},
                "NEWS_SEARCH": {"ENABLED": False},
            },
        )

        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = ctx
        analyzer.report_mode = "daily_delivery"
        analyzer.frequency_file = None
        analyzer.filter_method = filter_method
        analyzer.interests_file = None
        analyzer.rank_threshold = 5
        analyzer.proxy_url = None
        analyzer.update_info = None
        analyzer.is_docker_container = False
        analyzer.is_github_actions = False
        analyzer._rss_source_total = 1
        analyzer._rss_source_failed = 0
        analyzer._rss_total_count = 1 if raw_rss_items else 0
        analyzer._rss_matched_count = 0
        analyzer._hotlist_total_count = 0
        analyzer._rss_window = None
        analyzer._allowed_rss_ids = None
        analyzer._rss_ids_authoritative = False
        analyzer._report_period_label = ""
        analyzer._initialize_and_check_config = MagicMock(return_value=True)
        analyzer._crawl_data = MagicMock(return_value=(
            {"hot": {"Current hotlist item": {"ranks": [1]}}},
            {"hot": "Hotlist"},
            [],
        ))
        analyzer._should_open_browser = MagicMock(return_value=False)
        analyzer._has_notification_configured = MagicMock(
            return_value=has_notification
        )
        if rss_items is not None or raw_rss_items is not None:
            analyzer._allowed_rss_ids = {17}
            analyzer._rss_ids_authoritative = True
            analyzer._crawl_rss_data = MagicMock(return_value=(
                rss_items,
                None,
                raw_rss_items,
                set(),
            ))
        return analyzer, scheduler, dispatcher

    @patch("trendradar.core.analyzer.count_rss_frequency")
    @patch("trendradar.__main__.DailyDeliveryAggregator", create=True)
    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_snapshot_checkpoint_scope_and_only_rss_flow_through_run(
        self, fetcher_class, aggregator_class, count_rss_frequency
    ):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            filter_method="ai"
        )
        current_rss = snapshot_rss_data()
        current_rss.items["journal"][0].title = "Current crawl RSS"
        snapshot_data = snapshot_rss_data()
        storage = MagicMock()
        storage.save_rss_data.return_value = True
        storage.get_rss_data.return_value = current_rss
        storage.detect_new_rss_items.return_value = {}
        analyzer.storage_manager = storage
        fetcher_class.return_value.fetch_all.return_value = current_rss

        window = SimpleNamespace(
            label="2026-08-08 10:00—2026-08-09 10:00"
        )
        aggregator_class.return_value.build.return_value = SimpleNamespace(
            window=window,
            data=snapshot_data,
            allowed_rss_ids={17},
            total_read=1,
            filtered_out=0,
            duplicate_count=0,
            missing_dates=[],
        )
        count_rss_frequency.return_value = ([RSS_STAT], 1)

        self.assertTrue(analyzer.run())

        scheduler.latest_execution.assert_called_once_with(
            "daily_delivery", "push", "2026-08-09"
        )
        aggregator_class.return_value.build.assert_called_once_with(
            NOW, "2026-08-08 10:00:00"
        )
        filter_kwargs = analyzer.ctx.run_ai_filter.call_args.kwargs
        conversion_kwargs = (
            analyzer.ctx.convert_ai_filter_to_report_data.call_args.kwargs
        )
        self.assertIsNone(filter_kwargs["rss_window"])
        self.assertIsNone(conversion_kwargs["rss_window"])
        self.assertEqual(filter_kwargs["allowed_rss_ids"], {17})
        self.assertEqual(conversion_kwargs["allowed_rss_ids"], {17})
        self.assertTrue(filter_kwargs["rss_ids_authoritative"])
        self.assertTrue(conversion_kwargs["rss_ids_authoritative"])
        converted_snapshot = count_rss_frequency.call_args.kwargs["rss_items"]
        self.assertEqual(
            [item["title"] for item in converted_snapshot],
            ["Rice breeding"],
        )
        self.assertEqual(
            converted_snapshot[0]["published_at"],
            "2025-01-01T00:00:00Z",
        )
        prepare_args = analyzer.ctx.prepare_report.call_args.args
        self.assertEqual(prepare_args[0], [])
        self.assertEqual(prepare_args[1], [])
        self.assertEqual(prepare_args[2], {})
        self.assertEqual(
            dispatcher.dispatch_all.call_args.kwargs["rss_items"],
            [RSS_STAT],
        )
        self.assertTrue(
            dispatcher.dispatch_all.call_args.kwargs["require_all_targets"]
        )
        scheduler.record_execution.assert_called_once_with(
            "daily_delivery", "push", "2026-08-09"
        )

    def test_partial_notification_failure_keeps_checkpoint_for_retry(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
            notification_results={"wework": True, "email": False},
        )

        self.assertFalse(analyzer.run())

        self.assertTrue(
            dispatcher.dispatch_all.call_args.kwargs["require_all_targets"]
        )
        scheduler.record_execution.assert_not_called()

    def test_empty_snapshot_records_push_without_analysis_report_or_dispatch(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[], raw_rss_items=[]
        )

        self.assertTrue(analyzer.run())

        analyzer.ctx.run_ai_filter.assert_not_called()
        analyzer.ctx.generate_html.assert_not_called()
        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_called_once_with(
            "daily_delivery", "push", "2026-08-09"
        )

    def test_same_day_success_still_collects_but_skips_analysis_and_notification(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
            filter_method="ai",
        )
        scheduler.already_executed.return_value = True

        self.assertTrue(analyzer.run())

        analyzer._crawl_data.assert_called_once_with()
        analyzer._crawl_rss_data.assert_called_once_with()
        analyzer.ctx.run_ai_filter.assert_not_called()
        analyzer.ctx.prepare_report.assert_not_called()
        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    @patch("trendradar.__main__.DailyDeliveryAggregator")
    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_same_day_success_saves_current_rss_without_building_snapshot(
        self, fetcher_class, aggregator_class
    ):
        analyzer, scheduler, dispatcher = self.build_analyzer()
        current_rss = snapshot_rss_data()
        storage = MagicMock()
        storage.save_rss_data.return_value = True
        analyzer.storage_manager = storage
        fetcher_class.return_value.fetch_all.return_value = current_rss
        aggregator_class.return_value.build.return_value = delivery_snapshot()
        scheduler.already_executed.return_value = True

        self.assertTrue(analyzer.run())

        storage.save_rss_data.assert_called_once_with(current_rss)
        aggregator_class.return_value.build.assert_not_called()
        analyzer.ctx.run_ai_filter.assert_not_called()
        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_hotlist_source_failure_makes_run_fail_without_push_checkpoint(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
        )
        analyzer._crawl_data.return_value = ({}, {}, ["zhihu", "weibo"])

        self.assertFalse(analyzer.run())

        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    @patch("trendradar.core.analyzer.count_rss_frequency")
    @patch("trendradar.__main__.DailyDeliveryAggregator")
    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_fixed_rss_source_failure_is_saved_then_fails_run(
        self, fetcher_class, aggregator_class, count_rss_frequency
    ):
        analyzer, scheduler, dispatcher = self.build_analyzer()
        current_rss = snapshot_rss_data(failed_ids=["journal"])
        storage = MagicMock()
        storage.save_rss_data.return_value = True
        analyzer.storage_manager = storage
        fetcher_class.return_value.fetch_all.return_value = current_rss
        aggregator_class.return_value.build.return_value = delivery_snapshot(
            current_rss
        )
        count_rss_frequency.return_value = ([RSS_STAT], 1)

        self.assertFalse(analyzer.run())

        storage.save_rss_data.assert_called_once_with(current_rss)
        aggregator_class.return_value.build.assert_not_called()
        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    @patch("trendradar.core.analyzer.count_rss_frequency")
    @patch("trendradar.__main__.DailyDeliveryAggregator")
    @patch("trendradar.__main__.AgriculturalNewsSearch")
    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_enabled_search_provider_failure_is_saved_then_fails_run(
        self,
        fetcher_class,
        search_class,
        aggregator_class,
        count_rss_frequency,
    ):
        analyzer, scheduler, dispatcher = self.build_analyzer()
        analyzer.ctx.rss_config["NEWS_SEARCH"] = {
            "ENABLED": True,
            "PROVIDERS": {"gdelt": True, "google_news": False},
            "TOPICS": [],
            "AUTHORITY_DOMAINS": [],
        }
        current_rss = snapshot_rss_data()
        storage = MagicMock()
        storage.save_rss_data.return_value = True
        analyzer.storage_manager = storage
        fetcher_class.return_value.fetch_all.return_value = current_rss
        search_class.return_value.search.return_value = NewsSearchResult(
            items=[], failed_providers=["gdelt"]
        )
        aggregator_class.return_value.build.return_value = delivery_snapshot(
            current_rss
        )
        count_rss_frequency.return_value = ([RSS_STAT], 1)

        self.assertFalse(analyzer.run())

        storage.save_rss_data.assert_called_once_with(current_rss)
        aggregator_class.return_value.build.assert_not_called()
        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    @patch("trendradar.core.analyzer.count_rss_frequency")
    @patch("trendradar.__main__.DailyDeliveryAggregator")
    @patch("trendradar.__main__.AgriculturalNewsSearch")
    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_enabled_search_exception_is_saved_then_fails_run(
        self,
        fetcher_class,
        search_class,
        aggregator_class,
        count_rss_frequency,
    ):
        analyzer, scheduler, dispatcher = self.build_analyzer()
        analyzer.ctx.rss_config["NEWS_SEARCH"] = {
            "ENABLED": True,
            "PROVIDERS": {"gdelt": True},
            "TOPICS": [],
            "AUTHORITY_DOMAINS": [],
        }
        current_rss = snapshot_rss_data()
        storage = MagicMock()
        storage.save_rss_data.return_value = True
        analyzer.storage_manager = storage
        fetcher_class.return_value.fetch_all.return_value = current_rss
        search_class.return_value.search.side_effect = RuntimeError(
            "provider outage"
        )
        aggregator_class.return_value.build.return_value = delivery_snapshot(
            current_rss
        )
        count_rss_frequency.return_value = ([RSS_STAT], 1)

        self.assertFalse(analyzer.run())

        storage.save_rss_data.assert_called_once_with(current_rss)
        aggregator_class.return_value.build.assert_not_called()
        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_rss_save_failure_makes_run_fail(self, fetcher_class):
        analyzer, scheduler, dispatcher = self.build_analyzer()
        storage = MagicMock()
        storage.save_rss_data.return_value = False
        analyzer.storage_manager = storage
        fetcher_class.return_value.fetch_all.return_value = snapshot_rss_data()

        self.assertFalse(analyzer.run())

        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_fixed_rss_fetch_exception_makes_run_fail(self, fetcher_class):
        analyzer, scheduler, dispatcher = self.build_analyzer()
        analyzer.storage_manager = MagicMock()
        fetcher_class.return_value.fetch_all.side_effect = RuntimeError(
            "fixed RSS outage"
        )

        self.assertFalse(analyzer.run())

        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    @patch("trendradar.__main__.DailyDeliveryAggregator")
    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_snapshot_failure_makes_run_fail(
        self, fetcher_class, aggregator_class
    ):
        analyzer, scheduler, dispatcher = self.build_analyzer()
        storage = MagicMock()
        storage.save_rss_data.return_value = True
        analyzer.storage_manager = storage
        fetcher_class.return_value.fetch_all.return_value = snapshot_rss_data()
        aggregator_class.return_value.build.side_effect = RuntimeError(
            "每日交付快照 ID 解析失败"
        )

        self.assertFalse(analyzer.run())

        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_multi_day_sql_status_failure_makes_run_fail_without_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = StorageManager(
                backend_type="local",
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            backend = manager.get_backend()
            try:
                self.assertTrue(
                    DailyDeliveryStorageContractTests._save_feed_status(
                        backend,
                        "2026-08-08",
                        "11:00",
                        "journal",
                        "success",
                    )
                )
                self.assertTrue(
                    DailyDeliveryStorageContractTests._save_feed_status(
                        backend,
                        "2026-08-09",
                        "09:00",
                        "journal",
                        "success",
                    )
                )
                current_rss = backend.get_rss_data("2026-08-09")
                broken = backend._get_connection(
                    "2026-08-08", db_type="rss"
                )
                broken.execute("DROP TABLE rss_crawl_status")
                broken.commit()

                analyzer, scheduler, dispatcher = self.build_analyzer(
                    rss_items=[RSS_STAT],
                    raw_rss_items=[{"title": "journal recovered"}],
                )
                analyzer.storage_manager = manager
                analyzer._rss_ids_authoritative = False
                analyzer._allowed_rss_ids = None

                def crawl_saved_rss():
                    analyzer._daily_delivery_rss_data = current_rss
                    return [RSS_STAT], None, [
                        {"title": "journal recovered"}
                    ], set()

                analyzer._crawl_rss_data = MagicMock(
                    side_effect=crawl_saved_rss
                )

                self.assertFalse(analyzer.run())

                dispatcher.dispatch_all.assert_not_called()
                scheduler.record_execution.assert_not_called()
            finally:
                manager.cleanup()

    def test_ai_filter_failure_aborts_without_keyword_fallback(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
            filter_method="ai",
        )
        analyzer.ctx.run_ai_filter.return_value = AIFilterResult(
            success=False, error="classification failed"
        )

        self.assertFalse(analyzer.run())

        analyzer.ctx.count_frequency.assert_not_called()
        analyzer.ctx.prepare_report.assert_not_called()
        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    @patch("trendradar.__main__.AIAnalyzer")
    def test_ai_summary_failure_aborts_before_report_and_notification(
        self, analyzer_class
    ):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
        )
        analyzer.ctx.config["AI"] = {}
        analyzer.ctx.config["AI_ANALYSIS"] = {
            "ENABLED": True,
            "MODE": "follow_report",
        }
        analyzer_class.return_value.analyze.return_value = AIAnalysisResult(
            success=False, error="summary failed"
        )

        self.assertFalse(analyzer.run())

        analyzer.ctx.generate_html.assert_not_called()
        analyzer.ctx.prepare_report.assert_not_called()
        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_enabled_html_failure_aborts_before_notification(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
            html_enabled=True,
        )
        analyzer.ctx.generate_html.return_value = None

        self.assertFalse(analyzer.run())

        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_no_ai_matches_records_push_without_summary_report_or_dispatch(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
            filter_method="ai",
            html_enabled=True,
        )
        analyzer.ctx.convert_ai_filter_to_report_data.return_value = (
            [], [], None
        )
        analyzer.ctx.config["AI_ANALYSIS"] = {"ENABLED": True}

        self.assertTrue(analyzer.run())

        analyzer.ctx.generate_html.assert_not_called()
        analyzer.ctx.prepare_report.assert_not_called()
        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_called_once_with(
            "daily_delivery", "push", "2026-08-09"
        )

    def test_content_with_notifications_disabled_does_not_advance_checkpoint(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
            enable_notification=False,
        )

        self.assertFalse(analyzer.run())

        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_content_with_no_usable_notification_config_does_not_advance(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
            has_notification=False,
        )

        self.assertFalse(analyzer.run())

        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_empty_dispatch_result_does_not_advance_checkpoint(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
            notification_results={},
        )

        self.assertFalse(analyzer.run())

        dispatcher.dispatch_all.assert_called_once()
        scheduler.record_execution.assert_not_called()

    def test_dispatch_exception_does_not_advance_checkpoint(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
        )
        dispatcher.dispatch_all.side_effect = RuntimeError("endpoint outage")

        self.assertFalse(analyzer.run())

        dispatcher.dispatch_all.assert_called_once()
        scheduler.record_execution.assert_not_called()

    def test_checkpoint_write_failure_marks_content_delivery_failed(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
        )
        scheduler.record_execution.return_value = False

        self.assertFalse(analyzer.run())

        dispatcher.dispatch_all.assert_called_once()
        scheduler.record_execution.assert_called_once_with(
            "daily_delivery", "push", "2026-08-09"
        )

    def test_empty_checkpoint_write_failure_marks_run_failed(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[], raw_rss_items=[]
        )
        scheduler.record_execution.return_value = False

        self.assertFalse(analyzer.run())

        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_called_once_with(
            "daily_delivery", "push", "2026-08-09"
        )

    @patch("trendradar.__main__.AIAnalyzer")
    def test_failed_delivery_retry_reanalyzes_until_push_is_recorded(
        self, analyzer_class
    ):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
            notification_results={"wework": True, "email": False},
        )
        analyzer.ctx.config["AI"] = {}
        analyzer.ctx.config["AI_ANALYSIS"] = {
            "ENABLED": True,
            "MODE": "follow_report",
        }
        executed = set()
        scheduler.already_executed.side_effect = (
            lambda period_key, action, date_str: action in executed
        )

        def record_execution(period_key, action, date_str):
            executed.add(action)
            return True

        scheduler.record_execution.side_effect = record_execution
        dispatcher.dispatch_all.side_effect = [
            {"wework": True, "email": False},
            {"wework": True, "email": True},
        ]
        analyzer_class.return_value.analyze.side_effect = [
            AIAnalysisResult(success=True),
            AIAnalysisResult(success=True),
        ]

        self.assertFalse(analyzer.run())
        self.assertIn("analyze", executed)
        self.assertNotIn("push", executed)
        self.assertTrue(analyzer.run())
        self.assertIn("push", executed)
        self.assertTrue(analyzer.run())

        self.assertEqual(analyzer_class.return_value.analyze.call_count, 2)
        self.assertEqual(dispatcher.dispatch_all.call_count, 2)
        push_records = [
            item
            for item in scheduler.record_execution.call_args_list
            if item.args[1] == "push"
        ]
        self.assertEqual(len(push_records), 1)

    def test_empty_snapshot_second_run_does_not_repeat_work(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[], raw_rss_items=[], filter_method="ai"
        )
        pushed = False

        def already_executed(period_key, action, date_str):
            return action == "push" and pushed

        def record_execution(period_key, action, date_str):
            nonlocal pushed
            if action == "push":
                pushed = True
            return True

        scheduler.already_executed.side_effect = already_executed
        scheduler.record_execution.side_effect = record_execution

        self.assertTrue(analyzer.run())
        self.assertTrue(analyzer.run())

        self.assertEqual(analyzer._crawl_data.call_count, 2)
        self.assertEqual(analyzer._crawl_rss_data.call_count, 2)
        analyzer.ctx.run_ai_filter.assert_not_called()
        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_called_once_with(
            "daily_delivery", "push", "2026-08-09"
        )

    def test_existing_modes_keep_non_strict_delivery_semantics(self):
        for mode in ("daily", "current", "incremental"):
            with self.subTest(mode=mode):
                self.assertTrue(NewsAnalyzer._notification_delivery_succeeded(
                    mode, {"wework": True, "email": False}
                ))
                self.assertTrue(NewsAnalyzer._should_fallback_ai_filter(mode))
        self.assertFalse(NewsAnalyzer._notification_delivery_succeeded(
            "daily_delivery", {"wework": True, "email": False}
        ))
        self.assertFalse(NewsAnalyzer._should_fallback_ai_filter(
            "daily_delivery"
        ))

    def test_normal_run_keeps_partial_endpoint_success_and_ai_fallback(self):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
            filter_method="ai",
            notification_results={"wework": True, "email": False},
        )
        scheduler.resolve.return_value = delivery_schedule(
            period_key="ordinary",
            period_name="普通推送",
            report_mode="incremental",
            ai_mode="follow_report",
            filter_method="ai",
            once_analyze=False,
            once_push=False,
        )
        analyzer.ctx.run_ai_filter.return_value = AIFilterResult(
            success=False, error="classifier unavailable"
        )

        self.assertTrue(analyzer.run())

        analyzer.ctx.run_ai_filter.assert_called_once()
        analyzer.ctx.count_frequency.assert_called_once()
        dispatcher.dispatch_all.assert_called_once()
        self.assertFalse(
            dispatcher.dispatch_all.call_args.kwargs["require_all_targets"]
        )
        scheduler.record_execution.assert_not_called()

    @patch("trendradar.__main__.AIAnalyzer")
    def test_analysis_only_content_succeeds_without_dispatch_or_push_checkpoint(
        self, analyzer_class
    ):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
            html_enabled=True,
        )
        scheduler.resolve.return_value = delivery_schedule(
            analyze=True, push=False
        )
        analyzer.ctx.config["AI"] = {}
        analyzer.ctx.config["AI_ANALYSIS"] = {
            "ENABLED": True,
            "MODE": "follow_report",
        }
        analyzer_class.return_value.analyze.return_value = AIAnalysisResult(
            success=True
        )

        self.assertTrue(analyzer.run())

        analyzer_class.return_value.analyze.assert_called_once()
        analyzer.ctx.generate_html.assert_called_once()
        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_called_once_with(
            "daily_delivery", "analyze", "2026-08-09"
        )

    @patch("trendradar.__main__.AIAnalyzer")
    def test_push_only_skips_enabled_ai_summary_and_delivers(
        self, analyzer_class
    ):
        analyzer, scheduler, dispatcher = self.build_analyzer(
            rss_items=[RSS_STAT],
            raw_rss_items=[{"title": "Rice breeding"}],
        )
        scheduler.resolve.return_value = delivery_schedule(
            analyze=False, push=True
        )
        analyzer.ctx.config["AI"] = {}
        analyzer.ctx.config["AI_ANALYSIS"] = {
            "ENABLED": True,
            "MODE": "follow_report",
        }

        self.assertTrue(analyzer.run())

        analyzer_class.return_value.analyze.assert_not_called()
        dispatcher.dispatch_all.assert_called_once()
        self.assertIsNone(
            dispatcher.dispatch_all.call_args.kwargs["ai_analysis"]
        )
        scheduler.record_execution.assert_called_once_with(
            "daily_delivery", "push", "2026-08-09"
        )

    def test_missing_authoritative_snapshot_never_advances_checkpoint(self):
        for configure in (
            lambda analyzer: setattr(analyzer.ctx, "rss_enabled", False),
            lambda analyzer: setattr(analyzer.ctx, "rss_feeds", []),
        ):
            with self.subTest(configure=configure):
                analyzer, scheduler, dispatcher = self.build_analyzer()
                configure(analyzer)

                self.assertFalse(analyzer.run())

                self.assertFalse(analyzer._rss_ids_authoritative)
                analyzer.ctx.run_ai_filter.assert_not_called()
                dispatcher.dispatch_all.assert_not_called()
                scheduler.record_execution.assert_not_called()

    def test_push_disabled_never_records_empty_or_no_match_checkpoint(self):
        empty, empty_scheduler, empty_dispatcher = self.build_analyzer(
            rss_items=[], raw_rss_items=[]
        )
        empty_scheduler.resolve.return_value = delivery_schedule(
            analyze=False, push=False
        )

        self.assertTrue(empty.run())
        empty_dispatcher.dispatch_all.assert_not_called()
        empty_scheduler.record_execution.assert_not_called()

        analyzed_empty, analyzed_empty_scheduler, analyzed_empty_dispatcher = (
            self.build_analyzer(rss_items=[], raw_rss_items=[])
        )
        analyzed_empty_scheduler.resolve.return_value = delivery_schedule(
            analyze=True, push=False
        )

        self.assertTrue(analyzed_empty.run())
        analyzed_empty_dispatcher.dispatch_all.assert_not_called()
        analyzed_empty_scheduler.record_execution.assert_not_called()

        no_match, no_match_scheduler, no_match_dispatcher = (
            self.build_analyzer(
                rss_items=[RSS_STAT],
                raw_rss_items=[{"title": "Rice breeding"}],
                filter_method="ai",
            )
        )
        no_match_scheduler.resolve.return_value = delivery_schedule(push=False)
        no_match.ctx.convert_ai_filter_to_report_data.return_value = (
            [], [], None
        )

        self.assertTrue(no_match.run())
        no_match_dispatcher.dispatch_all.assert_not_called()
        no_match_scheduler.record_execution.assert_not_called()


class DailyDeliveryStorageContractTests(unittest.TestCase):
    @staticmethod
    def _save_feed_status(
        backend, date_str, crawl_time, feed_id, status, *, with_item=True
    ):
        items = {}
        failed_ids = []
        if status == "success":
            items[feed_id] = []
            if with_item:
                items[feed_id].append(RSSItem(
                    title=f"{feed_id} recovered",
                    feed_id=feed_id,
                    feed_name=feed_id.title(),
                    url=f"https://example.org/{feed_id}/{date_str}",
                    first_time=f"{date_str} {crawl_time}:00",
                ))
        else:
            failed_ids = [feed_id]
        return backend.save_rss_data(RSSData(
            date=date_str,
            crawl_time=crawl_time,
            items=items,
            id_to_name={feed_id: feed_id.title()},
            failed_ids=failed_ids,
        ))

    @staticmethod
    def _build_cross_day_snapshot(backend):
        tomorrow = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 10, 10, 0)
        )
        return DailyDeliveryAggregator(backend, "Asia/Shanghai").build(
            tomorrow, "2026-08-09 08:00:00"
        )

    def test_scheduler_reads_checkpoint_through_real_storage_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = StorageManager(
                backend_type="local",
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            scheduler = Scheduler(
                {"enabled": True, "preset": "custom"},
                SCHEDULER_TIMELINE,
                manager,
                lambda: NOW,
            )
            backend = manager.get_backend()
            try:
                with patch.object(
                    backend, "_get_configured_time", return_value=NOW
                ):
                    self.assertTrue(scheduler.record_execution(
                        "daily_delivery", "push", "2026-08-09"
                    ))

                self.assertEqual(
                    scheduler.latest_execution(
                        "daily_delivery", "push", "2026-08-09"
                    ),
                    "2026-08-09 10:00:00",
                )
            finally:
                manager.cleanup()

    def test_unrecovered_rss_failure_remains_in_daily_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            failed = RSSData(
                date="2026-08-09",
                crawl_time="09:00",
                items={},
                id_to_name={"journal": "Journal"},
                failed_ids=["journal"],
            )
            try:
                self.assertTrue(backend.save_rss_data(failed))
                self.assertEqual(
                    backend.get_rss_data("2026-08-09").failed_ids,
                    ["journal"],
                )
            finally:
                backend.cleanup()

    def test_later_failure_overrides_full_timestamp_snapshot_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            old_snapshot = snapshot_rss_data()
            old_snapshot.crawl_time = "2026-08-09 08:00:00"
            later_failure = RSSData(
                date="2026-08-09",
                crawl_time="09:30",
                items={},
                id_to_name={"journal": "Journal"},
                failed_ids=["journal"],
            )
            try:
                self.assertTrue(backend.save_rss_data(old_snapshot))
                self.assertTrue(backend.save_rss_data(later_failure))
                self.assertEqual(
                    backend.get_rss_data("2026-08-09").failed_ids,
                    ["journal"],
                )
            finally:
                backend.cleanup()

    def test_recovered_rss_failure_allows_daily_snapshot_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            failed = RSSData(
                date="2026-08-09",
                crawl_time="09:00",
                items={},
                id_to_name={"journal": "Journal"},
                failed_ids=["journal"],
            )
            recovered = snapshot_rss_data()
            recovered.crawl_time = "09:05"
            try:
                self.assertTrue(backend.save_rss_data(failed))

                self.assertTrue(backend.save_rss_data(recovered))
                self.assertEqual(
                    backend.get_rss_data("2026-08-09").failed_ids,
                    [],
                )
                snapshot = DailyDeliveryAggregator(
                    backend, "Asia/Shanghai"
                ).build(NOW, "2026-08-09 09:00:00")
                self.assertEqual(
                    [item.title for item in snapshot.iter_items()],
                    ["Rice breeding"],
                )
            finally:
                backend.cleanup()

    def test_cross_day_success_recovers_previous_day_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            try:
                self.assertTrue(self._save_feed_status(
                    backend, "2026-08-09", "09:00", "journal", "failed"
                ))
                self.assertTrue(self._save_feed_status(
                    backend, "2026-08-10", "09:00", "journal", "success"
                ))

                snapshot = self._build_cross_day_snapshot(backend)

                self.assertEqual(
                    [item.title for item in snapshot.iter_items()],
                    ["journal recovered"],
                )
            finally:
                backend.cleanup()

    def test_cross_day_sql_read_failure_is_not_treated_as_missing_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            try:
                self.assertTrue(self._save_feed_status(
                    backend, "2026-08-09", "09:00", "journal", "success"
                ))
                self.assertTrue(self._save_feed_status(
                    backend, "2026-08-10", "09:00", "journal", "success"
                ))
                broken = backend._get_connection(
                    "2026-08-09", db_type="rss"
                )
                broken.execute("DROP TABLE rss_items")
                broken.commit()

                with self.assertRaises(sqlite3.OperationalError):
                    self._build_cross_day_snapshot(backend)
            finally:
                backend.cleanup()

    def test_cross_day_failure_without_later_feed_status_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            try:
                self.assertTrue(self._save_feed_status(
                    backend, "2026-08-09", "09:00", "journal", "failed"
                ))
                self.assertTrue(self._save_feed_status(
                    backend, "2026-08-10", "09:00", "other", "success"
                ))

                with self.assertRaisesRegex(RuntimeError, "journal"):
                    self._build_cross_day_snapshot(backend)
            finally:
                backend.cleanup()

    def test_cross_day_later_failure_overrides_previous_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            try:
                self.assertTrue(self._save_feed_status(
                    backend, "2026-08-09", "09:00", "journal", "success"
                ))
                self.assertTrue(self._save_feed_status(
                    backend, "2026-08-10", "09:00", "journal", "failed"
                ))

                with self.assertRaisesRegex(RuntimeError, "journal"):
                    self._build_cross_day_snapshot(backend)
            finally:
                backend.cleanup()

    def test_cross_day_empty_success_recovers_previous_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            try:
                self.assertTrue(self._save_feed_status(
                    backend, "2026-08-09", "09:00", "journal", "failed"
                ))
                self.assertTrue(self._save_feed_status(
                    backend,
                    "2026-08-10",
                    "09:00",
                    "journal",
                    "success",
                    with_item=False,
                ))

                snapshot = self._build_cross_day_snapshot(backend)

                self.assertEqual(list(snapshot.iter_items()), [])
            finally:
                backend.cleanup()

    def test_rss_status_query_failure_is_not_treated_as_empty_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            try:
                self.assertTrue(self._save_feed_status(
                    backend, "2026-08-09", "09:00", "journal", "success"
                ))
                with patch.object(
                    backend,
                    "_read_latest_rss_feed_statuses",
                    side_effect=RuntimeError("status read failed"),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "status read failed"
                    ):
                        backend.get_rss_feed_statuses("2026-08-09")
            finally:
                backend.cleanup()


if __name__ == "__main__":
    unittest.main()
