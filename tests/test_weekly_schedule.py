import builtins
import errno
import importlib.util
import unittest
import inspect
import multiprocessing
import os
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytz
import yaml

from trendradar import __main__ as main_module
from trendradar.__main__ import NewsAnalyzer
from trendradar.ai.analyzer import AIAnalysisResult
from trendradar.ai.filter import AIFilterResult
from trendradar.core.scheduler import ResolvedSchedule, Scheduler
from trendradar.core import scheduler as scheduler_module
from trendradar.core.weekly import previous_natural_week
from trendradar.notification.dispatcher import NotificationDispatcher
from trendradar.storage.base import RSSData, RSSItem


TIMELINE = {"custom": {
    "default": {
        "collect": False, "analyze": False, "push": False,
        "report_mode": "current", "ai_mode": "follow_report",
        "once": {"analyze": False, "push": False},
    },
    "periods": {
        "daily_collect": {
            "name": "每日静默采集", "start": "10:00", "end": "10:01",
            "collect": True, "analyze": False, "push": False,
            "report_mode": "current",
        },
        "monday_weekly": {
            "name": "自然周周报", "start": "10:00", "end": "12:01",
            "collect": True, "analyze": True, "push": True,
            "report_mode": "weekly", "ai_mode": "weekly",
            "once": {"analyze": True, "push": True},
        },
    },
    "day_plans": {
        "monday": {"periods": ["monday_weekly"]},
        "collect_only": {"periods": ["daily_collect"]},
    },
    "week_map": {
        1: "monday", 2: "collect_only", 3: "collect_only",
        4: "collect_only", 5: "collect_only", 6: "collect_only",
        7: "collect_only",
    },
}}


def at(year: int, month: int, day: int, hour: int, minute: int):
    return pytz.timezone("Asia/Shanghai").localize(
        datetime(year, month, day, hour, minute)
    )


RUN_AT = at(2026, 8, 10, 10, 0)
CURRENT_WEATHER = SimpleNamespace(
    report_date="2026-08-10",
    review_start="2026-08-02",
    review_end="2026-08-08",
)
ROOT = Path(__file__).resolve().parents[1]


def acquire_weekly_lock_in_child(data_dir, checkpoint_date, result_queue):
    lock = scheduler_module.WeeklyAttemptLock(data_dir, checkpoint_date)
    acquired = lock.acquire()
    result_queue.put(acquired)
    if acquired:
        lock.release()


def schedule(**overrides):
    values = {
        "period_key": "monday_weekly",
        "period_name": "自然周周报",
        "day_plan": "monday",
        "collect": True,
        "analyze": True,
        "push": True,
        "report_mode": "weekly",
        "ai_mode": "weekly",
        "once_analyze": True,
        "once_push": True,
        "frequency_file": None,
        "filter_method": None,
        "interests_file": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class WeeklyScheduleTests(unittest.TestCase):
    def resolve(self, when):
        return Scheduler(
            {"enabled": True, "preset": "custom"},
            TIMELINE, MagicMock(), lambda: when,
        ).resolve()

    def make_analyzer(self, run_at=RUN_AT):
        scheduler = MagicMock()
        scheduler.already_executed.return_value = False
        scheduler.record_execution.return_value = True
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            cleanup=MagicMock(),
            config={"DEBUG": False},
            timezone="Asia/Shanghai",
            get_time=MagicMock(return_value=run_at),
            create_scheduler=MagicMock(return_value=scheduler),
        )
        analyzer.report_mode = "weekly"
        analyzer._initialize_and_check_config = MagicMock(return_value=True)
        analyzer._resolve_and_apply_schedule = MagicMock(return_value=schedule())
        attempt_lock = MagicMock()
        attempt_lock.acquire.return_value = True
        analyzer._create_weekly_attempt_lock = MagicMock(
            return_value=attempt_lock
        )
        analyzer._fetch_agro_weather = MagicMock(return_value=CURRENT_WEATHER)
        analyzer._crawl_data = MagicMock(return_value=({}, {}, []))
        analyzer._crawl_rss_data = MagicMock(return_value=(None, None, [], set()))
        analyzer._execute_mode_strategy = MagicMock(return_value=True)
        return analyzer

    def test_scheduler_module_import_on_windows_does_not_require_fcntl(self):
        module_name = "_trendradar_scheduler_without_fcntl"
        module_path = ROOT / "trendradar" / "core" / "scheduler.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        real_import = builtins.__import__

        def import_without_fcntl(name, *args, **kwargs):
            if name == "fcntl":
                raise ModuleNotFoundError("fcntl is unavailable on Windows")
            return real_import(name, *args, **kwargs)

        sys.modules[module_name] = module
        try:
            with patch.object(os, "name", "nt"), patch(
                "builtins.__import__", side_effect=import_without_fcntl
            ):
                spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_name, None)

        self.assertTrue(hasattr(module, "WeeklyAttemptLock"))

    def test_windows_weekly_lock_calls_msvcrt_lock_and_unlock(self):
        fake_msvcrt = SimpleNamespace(
            LK_NBLCK=10,
            LK_UNLCK=20,
            locking=MagicMock(),
        )
        imported = []

        def import_module(name):
            imported.append(name)
            if name == "msvcrt":
                return fake_msvcrt
            raise AssertionError(f"unexpected platform import: {name}")

        backend = scheduler_module._create_file_lock_backend(
            platform_name="nt", import_module=import_module
        )
        with TemporaryDirectory() as data_dir:
            lock = scheduler_module.WeeklyAttemptLock(
                data_dir, "2026-08-10", backend=backend
            )
            self.assertTrue(lock.acquire())
            handle_fd = lock._handle.fileno()
            lock.release()

        self.assertEqual(imported, ["msvcrt"])
        self.assertEqual(
            fake_msvcrt.locking.call_args_list,
            [
                unittest.mock.call(handle_fd, fake_msvcrt.LK_NBLCK, 1),
                unittest.mock.call(handle_fd, fake_msvcrt.LK_UNLCK, 1),
            ],
        )

    def test_windows_weekly_lock_treats_lock_violation_as_contention(self):
        fake_msvcrt = SimpleNamespace(
            LK_NBLCK=10,
            LK_UNLCK=20,
            locking=MagicMock(
                side_effect=OSError(errno.EACCES, "lock violation")
            ),
        )
        backend = scheduler_module._create_file_lock_backend(
            platform_name="nt", import_module=lambda name: fake_msvcrt
        )
        with TemporaryDirectory() as data_dir:
            lock = scheduler_module.WeeklyAttemptLock(
                data_dir, "2026-08-10", backend=backend
            )
            self.assertFalse(lock.acquire())
            self.assertIsNone(lock._handle)

    def test_posix_weekly_lock_treats_eacces_as_contention(self):
        fake_fcntl = SimpleNamespace(
            LOCK_EX=1,
            LOCK_NB=2,
            LOCK_UN=4,
            flock=MagicMock(
                side_effect=OSError(errno.EACCES, "lock contention")
            ),
        )
        backend = scheduler_module._create_file_lock_backend(
            platform_name="posix", import_module=lambda name: fake_fcntl
        )
        with TemporaryDirectory() as data_dir:
            lock = scheduler_module.WeeklyAttemptLock(
                data_dir, "2026-08-10", backend=backend
            )
            self.assertFalse(lock.acquire())
            self.assertIsNone(lock._handle)

    def test_weekly_attempt_lock_is_nonblocking_and_scoped_by_window(self):
        with TemporaryDirectory() as data_dir:
            first = scheduler_module.WeeklyAttemptLock(data_dir, "2026-08-10")
            overlapping = scheduler_module.WeeklyAttemptLock(
                data_dir, "2026-08-10"
            )
            next_week = scheduler_module.WeeklyAttemptLock(
                data_dir, "2026-08-17"
            )

            self.assertTrue(first.acquire())
            self.assertFalse(overlapping.acquire())
            self.assertTrue(next_week.acquire())

            context = multiprocessing.get_context("spawn")
            result_queue = context.Queue()
            process = context.Process(
                target=acquire_weekly_lock_in_child,
                args=(data_dir, "2026-08-10", result_queue),
            )
            process.start()
            self.assertFalse(result_queue.get(timeout=10))
            process.join(timeout=10)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)

            first.release()
            self.assertTrue(overlapping.acquire())

            overlapping.release()
            next_week.release()

    def test_overlapping_weekly_run_stops_before_checkpoint_or_network(self):
        with TemporaryDirectory() as data_dir:
            held = scheduler_module.WeeklyAttemptLock(data_dir, "2026-08-10")
            self.assertTrue(held.acquire())
            analyzer = self.make_analyzer()
            analyzer.storage_manager = SimpleNamespace(data_dir=data_dir)
            analyzer._create_weekly_attempt_lock = (
                NewsAnalyzer._create_weekly_attempt_lock.__get__(
                    analyzer, NewsAnalyzer
                )
            )
            scheduler = analyzer.ctx.create_scheduler.return_value

            try:
                self.assertTrue(analyzer.run())
            finally:
                held.release()

            scheduler.already_executed.assert_not_called()
            scheduler.record_execution.assert_not_called()
            analyzer._fetch_agro_weather.assert_not_called()
            analyzer._crawl_data.assert_not_called()
            analyzer._execute_mode_strategy.assert_not_called()

    def test_failed_weekly_run_releases_attempt_lock_for_retry(self):
        with TemporaryDirectory() as data_dir:
            analyzer = self.make_analyzer()
            analyzer.storage_manager = SimpleNamespace(data_dir=data_dir)
            analyzer._create_weekly_attempt_lock = (
                NewsAnalyzer._create_weekly_attempt_lock.__get__(
                    analyzer, NewsAnalyzer
                )
            )
            analyzer._fetch_agro_weather.return_value = None

            self.assertFalse(analyzer.run())

            retry = scheduler_module.WeeklyAttemptLock(data_dir, "2026-08-10")
            self.assertTrue(retry.acquire())
            retry.release()

    def test_monday_attempt_window_and_other_days_silent_collect(self):
        for hour, minute in [(10, 0), (10, 30), (11, 0), (11, 30), (12, 0)]:
            with self.subTest(hour=hour, minute=minute):
                monday = self.resolve(at(2026, 8, 10, hour, minute))
                self.assertEqual(monday.report_mode, "weekly")
                self.assertTrue(monday.collect and monday.analyze and monday.push)

        monday_late = self.resolve(at(2026, 8, 10, 12, 30))
        self.assertFalse(monday_late.collect)

        for day in (11, 16):
            with self.subTest(day=day):
                collect = self.resolve(at(2026, 8, day, 10, 0))
                self.assertTrue(collect.collect)
                self.assertFalse(collect.analyze)
                self.assertFalse(collect.push)
                late = self.resolve(at(2026, 8, day, 10, 30))
                self.assertFalse(late.collect)

    def test_missing_current_weather_aborts_before_ordinary_crawl(self):
        analyzer = self.make_analyzer()
        analyzer._fetch_agro_weather.return_value = None

        self.assertFalse(analyzer.run())

        analyzer._crawl_data.assert_not_called()
        analyzer._crawl_rss_data.assert_not_called()
        analyzer._execute_mode_strategy.assert_not_called()

    def test_weather_error_aborts_before_ordinary_crawl(self):
        analyzer = self.make_analyzer()
        analyzer._fetch_agro_weather.side_effect = RuntimeError("气象结构错误")

        self.assertFalse(analyzer.run())

        analyzer._crawl_data.assert_not_called()
        analyzer._crawl_rss_data.assert_not_called()

    def test_success_checkpoint_skips_retry_before_network(self):
        analyzer = self.make_analyzer()
        scheduler = analyzer.ctx.create_scheduler.return_value
        scheduler.already_executed.return_value = True

        self.assertTrue(analyzer.run())

        analyzer._fetch_agro_weather.assert_not_called()
        analyzer._crawl_data.assert_not_called()
        scheduler.already_executed.assert_called_once_with(
            "monday_weekly", "push", "2026-08-10"
        )

    def test_force_weekly_uses_weekly_period_outside_window(self):
        scheduler = Scheduler(
            {"enabled": True, "preset": "custom"}, TIMELINE, MagicMock(),
            lambda: at(2026, 8, 12, 15, 0),
        )

        forced = scheduler.resolve(force_period_key="monday_weekly")

        self.assertEqual(forced.period_key, "monday_weekly")
        self.assertEqual(forced.day_plan, "forced")
        self.assertTrue(forced.collect and forced.analyze and forced.push)
        self.assertEqual(forced.report_mode, "weekly")

    def test_force_weekly_overrides_disabled_scheduler_fallback(self):
        scheduler = Scheduler(
            {"enabled": False, "preset": "custom"}, TIMELINE, MagicMock(),
            lambda: at(2026, 8, 12, 15, 0),
        )

        forced = scheduler.resolve(force_period_key="monday_weekly")

        self.assertEqual(forced.period_key, "monday_weekly")
        self.assertEqual(forced.day_plan, "forced")
        self.assertTrue(forced.once_analyze)
        self.assertTrue(forced.once_push)
        self.assertEqual(forced.report_mode, "weekly")

    def test_analyzer_force_weekly_requests_the_weekly_period(self):
        run_at = at(2026, 8, 12, 15, 0)
        scheduler = MagicMock()
        scheduler.resolve.return_value = schedule()
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            create_scheduler=MagicMock(return_value=scheduler),
            filter_method="ai",
        )
        analyzer._run_at = run_at
        analyzer.force_weekly = True

        resolved = analyzer._resolve_and_apply_schedule()

        self.assertEqual(resolved.report_mode, "weekly")
        scheduler.resolve.assert_called_once_with(
            run_at, force_period_key="monday_weekly"
        )

    def test_force_weekly_still_respects_same_week_checkpoint(self):
        analyzer = self.make_analyzer(run_at=at(2026, 8, 12, 15, 0))
        scheduler = analyzer.ctx.create_scheduler.return_value
        scheduler.already_executed.return_value = True

        self.assertTrue(analyzer.run())

        analyzer._fetch_agro_weather.assert_not_called()
        scheduler.already_executed.assert_called_once_with(
            "monday_weekly", "push", "2026-08-10"
        )

    def test_manual_weekly_delivery_records_window_end_checkpoint(self):
        analyzer = self.make_analyzer(run_at=at(2026, 8, 12, 15, 0))
        scheduler = analyzer.ctx.create_scheduler.return_value

        self.assertTrue(analyzer._record_delivery_checkpoint(schedule()))

        scheduler.record_execution.assert_called_once_with(
            "monday_weekly", "push", "2026-08-10"
        )

    def test_manual_weather_fetch_uses_weekly_window_end_anchor(self):
        run_at = at(2026, 8, 12, 15, 0)
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config={"AGRO_WEATHER": {}},
            timezone="Asia/Shanghai",
        )
        analyzer.proxy_url = None
        analyzer._run_at = run_at

        with patch("trendradar.__main__.AgroWeatherClient") as client_class:
            client_class.return_value.fetch_latest.return_value = CURRENT_WEATHER
            report = analyzer._fetch_agro_weather()

        self.assertIs(report, CURRENT_WEATHER)
        client_class.return_value.fetch_latest.assert_called_once_with(
            run_at,
            expected_delivery_anchor=at(2026, 8, 10, 0, 0),
        )

    def test_active_timelines_use_the_weekly_collection_plan(self):
        for relative in ("config/timeline.yaml", "config/timeline.en.yaml"):
            with self.subTest(relative=relative):
                custom = yaml.safe_load(
                    (ROOT / relative).read_text(encoding="utf-8")
                )["custom"]
                self.assertFalse(custom["default"]["collect"])
                self.assertEqual(
                    custom["day_plans"]["monday"]["periods"],
                    ["monday_weekly"],
                )
                self.assertEqual(
                    custom["day_plans"]["collect_only"]["periods"],
                    ["daily_collect"],
                )
                self.assertEqual(
                    custom["periods"]["monday_weekly"]["end"], "12:01"
                )

    def test_monday_collects_analyzes_and_pushes_weekly_once(self):
        resolved = self.resolve(
            pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            )
        )
        self.assertTrue(resolved.collect)
        self.assertTrue(resolved.analyze)
        self.assertTrue(resolved.push)
        self.assertEqual(resolved.report_mode, "weekly")
        self.assertEqual(resolved.ai_mode, "weekly")
        self.assertTrue(resolved.once_analyze)
        self.assertTrue(resolved.once_push)

    def test_tuesday_only_collects(self):
        resolved = self.resolve(
            pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 11, 10, 0)
            )
        )
        self.assertTrue(resolved.collect)
        self.assertFalse(resolved.analyze)
        self.assertFalse(resolved.push)

    def test_silent_run_never_enters_analysis_pipeline(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            cleanup=MagicMock(),
            config={"DEBUG": False},
            timezone="Asia/Shanghai",
            get_time=MagicMock(return_value=RUN_AT),
            create_scheduler=MagicMock(return_value=MagicMock(
                already_executed=MagicMock(return_value=False)
            )),
        )
        analyzer.report_mode = "current"
        analyzer._initialize_and_check_config = MagicMock(return_value=True)
        analyzer._resolve_and_apply_schedule = MagicMock(
            return_value=schedule(
                report_mode="current", ai_mode="current",
                analyze=False, push=False,
            )
        )
        analyzer._crawl_data = MagicMock(return_value=({}, {}, []))
        analyzer._crawl_rss_data = MagicMock(
            return_value=(None, None, [], set())
        )
        analyzer._execute_mode_strategy = MagicMock()

        self.assertTrue(analyzer.run())

        analyzer._crawl_data.assert_called_once()
        analyzer._crawl_rss_data.assert_called_once()
        analyzer._execute_mode_strategy.assert_not_called()

    def test_weekly_snapshot_scope_flows_from_aggregator_to_both_ai_steps(self):
        window = SimpleNamespace(label="唯一自然周")
        allowed_ids = {101, 202}
        snapshot = SimpleNamespace(
            window=window,
            allowed_rss_ids=allowed_ids,
            data=RSSData(
                date="2026-08-10", crawl_time="10-00",
                items={"journal": [RSSItem(
                    title="Weekly item", feed_id="journal",
                    url="https://example.org/weekly",
                    published_at="2026-08-05T10:00:00+08:00",
                )]},
                id_to_name={"journal": "Journal"},
            ),
            total_read=8,
            filtered_out=2,
            duplicate_count=1,
            missing_dates=["2026-08-04"],
            failed_sources={"journal": ["2026-08-04"]},
        )
        ai_result = AIFilterResult(success=True, tags=[])
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config={
                "DISPLAY": {"REGIONS": {"RSS": True}},
                "AI_ANALYSIS": {"ENABLED": False},
                "STORAGE": {"FORMATS": {"HTML": False}},
            },
            timezone="Asia/Shanghai",
            get_time=MagicMock(),
            load_frequency_words=MagicMock(return_value=([], [], [])),
            rss_config={"FRESHNESS_FILTER": {"ENABLED": True}},
            rss_feeds=[],
            run_ai_filter=MagicMock(return_value=ai_result),
            convert_ai_filter_to_report_data=MagicMock(return_value=([], [], [])),
            display_mode="keyword",
        )
        analyzer.storage_manager = MagicMock()
        analyzer.report_mode = "weekly"
        analyzer.frequency_file = None
        analyzer.filter_method = "ai"
        analyzer.interests_file = None
        analyzer.rank_threshold = 5

        with patch("trendradar.__main__.WeeklyRSSAggregator") as aggregator, \
             patch("builtins.print") as printed:
            aggregator.return_value.build.return_value = snapshot
            rss_items, rss_new_items, _, rss_new_urls = \
                analyzer._process_rss_data_by_mode(MagicMock())
            analyzer._run_analysis_pipeline(
                {}, "weekly", {}, {}, [], [], {},
                rss_items=rss_items,
                rss_new_items=rss_new_items,
                rss_new_urls=rss_new_urls,
                schedule=schedule(),
            )

        filter_kwargs = analyzer.ctx.run_ai_filter.call_args.kwargs
        conversion_kwargs = analyzer.ctx.convert_ai_filter_to_report_data.call_args.kwargs
        self.assertIs(filter_kwargs["rss_window"], window)
        self.assertIs(conversion_kwargs["rss_window"], window)
        self.assertIs(filter_kwargs["allowed_rss_ids"], allowed_ids)
        self.assertIs(conversion_kwargs["allowed_rss_ids"], allowed_ids)
        log_lines = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("total_read=8", log_lines)
        self.assertIn("filtered_out=2", log_lines)
        self.assertIn("duplicate_count=1", log_lines)
        self.assertIn("retained=1", log_lines)
        self.assertIn("missing_dates=['2026-08-04']", log_lines)
        self.assertIn("failed_sources={'journal': ['2026-08-04']}", log_lines)

    def test_schedule_is_resolved_once_before_initialization_and_crawl(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        events = []
        resolved = schedule(report_mode="weekly")
        analyzer.ctx = SimpleNamespace(
            cleanup=MagicMock(),
            config={"DEBUG": False},
            timezone="Asia/Shanghai",
            get_time=MagicMock(return_value=RUN_AT),
            create_scheduler=MagicMock(return_value=MagicMock(
                already_executed=MagicMock(return_value=False)
            )),
        )
        analyzer.report_mode = "weekly"
        analyzer._rss_window = object()
        analyzer._resolve_and_apply_schedule = MagicMock(
            side_effect=lambda: events.append("schedule") or resolved
        )
        analyzer._initialize_and_check_config = MagicMock(
            side_effect=lambda: events.append("initialize") or True
        )
        analyzer._fetch_agro_weather = MagicMock(
            side_effect=lambda: events.append("weather") or CURRENT_WEATHER
        )
        attempt_lock = MagicMock()
        attempt_lock.acquire.return_value = True
        analyzer._create_weekly_attempt_lock = MagicMock(
            return_value=attempt_lock
        )
        analyzer._crawl_data = MagicMock(
            side_effect=lambda: events.append("crawl") or ({}, {}, [])
        )
        analyzer._crawl_rss_data = MagicMock(
            side_effect=lambda: events.append("rss") or (None, None, [], set())
        )
        analyzer._execute_mode_strategy = MagicMock(
            side_effect=lambda *args, **kwargs: events.append("strategy") or True
        )

        self.assertTrue(analyzer.run())

        self.assertEqual(
            events,
            ["schedule", "weather", "initialize", "crawl", "rss", "strategy"],
        )
        analyzer._resolve_and_apply_schedule.assert_called_once_with()
        self.assertIs(
            analyzer._execute_mode_strategy.call_args.kwargs["schedule"], resolved
        )

    def test_weekly_ai_scope_is_passed_identically_to_filter_and_conversion(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        tz = pytz.timezone("Asia/Shanghai")
        window = previous_natural_week(
            tz.localize(datetime(2026, 8, 10, 10, 0)), "Asia/Shanghai"
        )
        allowed_ids = {11, 12}
        ai_result = AIFilterResult(success=True, tags=[])
        analyzer.ctx = SimpleNamespace(
            run_ai_filter=MagicMock(return_value=ai_result),
            convert_ai_filter_to_report_data=MagicMock(return_value=([], [], [])),
            config={"AI_ANALYSIS": {"ENABLED": False}, "STORAGE": {"FORMATS": {"HTML": False}}},
            display_mode="keyword",
        )
        analyzer.filter_method = "ai"
        analyzer.interests_file = "weekly.txt"
        analyzer._rss_window = window
        analyzer._allowed_rss_ids = allowed_ids
        analyzer._rss_total_count = 0
        analyzer.rank_threshold = 5

        analyzer._run_analysis_pipeline(
            {}, "weekly", {}, {}, [], [], {}, schedule=schedule()
        )

        filter_kwargs = analyzer.ctx.run_ai_filter.call_args.kwargs
        conversion_kwargs = analyzer.ctx.convert_ai_filter_to_report_data.call_args.kwargs
        self.assertIs(filter_kwargs["rss_window"], window)
        self.assertIs(conversion_kwargs["rss_window"], window)
        self.assertIs(filter_kwargs["allowed_rss_ids"], allowed_ids)
        self.assertIs(conversion_kwargs["allowed_rss_ids"], allowed_ids)

    def test_weekly_ai_failure_raises_instead_of_keyword_fallback(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            run_ai_filter=MagicMock(return_value=AIFilterResult(success=False, error="boom")),
            count_frequency=MagicMock(),
        )
        analyzer.filter_method = "ai"
        analyzer.interests_file = None
        analyzer._rss_window = None
        analyzer._allowed_rss_ids = None

        with self.assertRaisesRegex(RuntimeError, "周报 AI 筛选失败"):
            analyzer._run_analysis_pipeline({}, "weekly", {}, {}, [], [], {})

        analyzer.ctx.count_frequency.assert_not_called()

    def test_weekly_empty_snapshot_short_circuits_strategy(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.report_mode = "weekly"
        analyzer.ctx = SimpleNamespace(
            config={"DISPLAY": {"REGIONS": {"RSS": True}}},
            timezone="Asia/Shanghai",
            get_time=MagicMock(),
            load_frequency_words=MagicMock(return_value=([], [], [])),
        )
        analyzer.storage_manager = MagicMock()
        analyzer.frequency_file = None
        analyzer.rank_threshold = 5

        with patch("trendradar.__main__.WeeklyRSSAggregator") as aggregator:
            aggregator.return_value.build.return_value = SimpleNamespace(data=None)
            result = analyzer._process_rss_data_by_mode(MagicMock())

        self.assertEqual(result, (None, None, None, set()))

    def test_weekly_failures_do_not_fall_back_or_mark_partial_delivery(self):
        self.assertFalse(NewsAnalyzer._should_fallback_ai_filter("weekly"))
        self.assertTrue(NewsAnalyzer._should_fallback_ai_filter("daily"))
        self.assertFalse(NewsAnalyzer._notification_delivery_succeeded(
            "weekly", {"wework": True, "email": False}
        ))
        self.assertTrue(NewsAnalyzer._notification_delivery_succeeded(
            "daily", {"wework": True, "email": False}
        ))
        self.assertTrue(NewsAnalyzer._notification_delivery_succeeded(
            "weekly", {"wework": True, "email": True}
        ))

    def test_weekly_notification_failure_is_not_recorded_for_once_delivery(self):
        scheduler = MagicMock()
        scheduler.already_executed.return_value = False
        dispatcher = MagicMock(dispatch_all=MagicMock(
            return_value={"wework": True, "email": False}
        ))
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config={"ENABLE_NOTIFICATION": True, "SHOW_VERSION_UPDATE": False},
            platform_ids=[],
            create_notification_dispatcher=MagicMock(return_value=dispatcher),
            create_scheduler=MagicMock(return_value=scheduler),
            prepare_report=MagicMock(return_value={}),
            format_date=MagicMock(return_value="2026-08-10"),
        )
        analyzer.report_mode = "weekly"
        analyzer._report_period_label = "2026-08-03—2026-08-09"
        analyzer.frequency_file = None
        analyzer._hotlist_total_count = 0
        analyzer._rss_matched_count = 1
        analyzer._rss_total_count = 1
        analyzer._rss_source_total = 0
        analyzer._rss_source_failed = 0
        analyzer.proxy_url = None
        analyzer.update_info = None
        analyzer._has_notification_configured = MagicMock(return_value=True)
        analyzer._has_valid_content = MagicMock(return_value=False)

        sent = analyzer._send_notification_if_needed(
            [], "自然周周报", "weekly", rss_items=[{"count": 1}],
            schedule=schedule(),
        )

        self.assertFalse(sent)
        scheduler.record_execution.assert_not_called()

    def test_weekly_retry_reanalyzes_until_once_push_is_recorded(self):
        executed = set()
        scheduler = MagicMock()
        scheduler.already_executed.side_effect = (
            lambda period_key, action, date_str: action in executed
        )

        def record_execution(period_key, action, date_str):
            executed.add(action)
            return True

        scheduler.record_execution.side_effect = record_execution
        dispatcher = MagicMock(dispatch_all=MagicMock(side_effect=[
            {"wework": True, "email": False},
            {"wework": True, "email": True},
        ]))
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config={
                "ENABLE_NOTIFICATION": True,
                "SHOW_VERSION_UPDATE": False,
                "AI": {},
                "AI_ANALYSIS": {"ENABLED": True, "MODE": "follow_report"},
                "DEBUG": False,
            },
            platform_ids=[],
            create_notification_dispatcher=MagicMock(return_value=dispatcher),
            create_scheduler=MagicMock(return_value=scheduler),
            prepare_report=MagicMock(return_value={}),
            format_date=MagicMock(return_value="2026-08-10"),
            get_time=MagicMock(),
        )
        analyzer.report_mode = "weekly"
        analyzer._report_period_label = "2026-08-03—2026-08-09"
        analyzer.frequency_file = None
        analyzer._hotlist_total_count = 0
        analyzer._rss_matched_count = 1
        analyzer._rss_total_count = 1
        analyzer._rss_source_total = 1
        analyzer._rss_source_failed = 0
        analyzer.proxy_url = None
        analyzer.update_info = None
        analyzer._has_notification_configured = MagicMock(return_value=True)
        analyzer._has_valid_content = MagicMock(return_value=False)

        def run_attempt():
            ai_result = analyzer._run_ai_analysis(
                [{"word": "育种"}], [{"count": 1}],
                "weekly", "自然周周报", {}, schedule=schedule(),
            )
            return analyzer._send_notification_if_needed(
                [], "自然周周报", "weekly",
                rss_items=[{"count": 1}], ai_result=ai_result,
                schedule=schedule(),
            )

        with patch("trendradar.__main__.AIAnalyzer") as analyzer_class:
            analyzer_class.return_value.analyze.side_effect = [
                AIAnalysisResult(success=True),
                AIAnalysisResult(success=True),
            ]

            self.assertFalse(run_attempt())
            self.assertIn("analyze", executed)
            self.assertNotIn("push", executed)
            self.assertEqual(dispatcher.dispatch_all.call_count, 1)

            self.assertTrue(run_attempt())
            self.assertIn("push", executed)
            self.assertEqual(analyzer_class.return_value.analyze.call_count, 2)
            self.assertEqual(dispatcher.dispatch_all.call_count, 2)

            self.assertFalse(run_attempt())
            self.assertEqual(analyzer_class.return_value.analyze.call_count, 2)
            self.assertEqual(dispatcher.dispatch_all.call_count, 2)

        push_records = [
            call for call in scheduler.record_execution.call_args_list
            if call.args[1] == "push"
        ]
        self.assertEqual(len(push_records), 1)

    def test_daily_once_analysis_still_skips_when_push_is_pending(self):
        scheduler = MagicMock()
        scheduler.already_executed.side_effect = (
            lambda period_key, action, date_str: action == "analyze"
        )
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config={
                "AI": {},
                "AI_ANALYSIS": {"ENABLED": True, "MODE": "follow_report"},
                "DEBUG": False,
            },
            create_scheduler=MagicMock(return_value=scheduler),
            format_date=MagicMock(return_value="2026-08-10"),
            get_time=MagicMock(),
        )

        with patch("trendradar.__main__.AIAnalyzer") as analyzer_class:
            result = analyzer._run_ai_analysis(
                [{"word": "育种"}], None, "daily", "当日汇总", {},
                schedule=schedule(report_mode="daily", ai_mode="daily"),
            )

        self.assertIsNone(result)
        analyzer_class.assert_not_called()
        scheduler.already_executed.assert_called_once_with(
            "monday_weekly", "analyze", "2026-08-10"
        )

    def test_multi_account_aggregation_requires_every_weekly_target(self):
        dispatcher = NotificationDispatcher(
            {"MAX_ACCOUNTS_PER_CHANNEL": 3},
            lambda: None,
            MagicMock(),
        )
        parameters = inspect.signature(
            dispatcher._send_to_multi_accounts
        ).parameters
        if "require_all_targets" not in parameters:
            self.fail("多账号聚合缺少 require_all_targets 参数")

        sender = MagicMock(side_effect=[True, False])
        weekly_result = dispatcher._send_to_multi_accounts(
            "测试渠道",
            "first;second",
            sender,
            require_all_targets=True,
        )
        sender.side_effect = [True, False]
        daily_result = dispatcher._send_to_multi_accounts(
            "测试渠道",
            "first;second",
            sender,
        )

        self.assertFalse(weekly_result)
        self.assertTrue(daily_result)

    def test_once_push_storage_failure_makes_weekly_delivery_fail(self):
        scheduler = MagicMock()
        scheduler.already_executed.return_value = False
        scheduler.record_execution.return_value = False
        dispatcher = MagicMock(dispatch_all=MagicMock(
            return_value={"wework": True}
        ))
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config={
                "ENABLE_NOTIFICATION": True,
                "SHOW_VERSION_UPDATE": False,
                "AI_ANALYSIS": {"ENABLED": False},
            },
            platform_ids=[],
            create_notification_dispatcher=MagicMock(return_value=dispatcher),
            create_scheduler=MagicMock(return_value=scheduler),
            prepare_report=MagicMock(return_value={}),
            format_date=MagicMock(return_value="2026-08-10"),
        )
        analyzer.report_mode = "weekly"
        analyzer._report_period_label = "2026-08-03—2026-08-09"
        analyzer.frequency_file = None
        analyzer._hotlist_total_count = 0
        analyzer._rss_matched_count = 1
        analyzer._rss_total_count = 1
        analyzer._rss_source_total = 0
        analyzer._rss_source_failed = 0
        analyzer.proxy_url = None
        analyzer.update_info = None
        analyzer._has_notification_configured = MagicMock(return_value=True)
        analyzer._has_valid_content = MagicMock(return_value=False)

        sent = analyzer._send_notification_if_needed(
            [], "自然周周报", "weekly", rss_items=[{"count": 1}],
            schedule=schedule(),
        )

        self.assertFalse(sent)
        self.assertTrue(
            dispatcher.dispatch_all.call_args.kwargs["require_all_targets"]
        )
        scheduler.record_execution.assert_called_once_with(
            "monday_weekly", "push", "2026-08-10"
        )

    def test_once_analyze_storage_failure_returns_failed_summary(self):
        scheduler = MagicMock()
        scheduler.already_executed.return_value = False
        scheduler.record_execution.return_value = False
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config={
                "AI": {},
                "AI_ANALYSIS": {"ENABLED": True, "MODE": "follow_report"},
                "DEBUG": False,
            },
            create_scheduler=MagicMock(return_value=scheduler),
            format_date=MagicMock(return_value="2026-08-10"),
            get_time=MagicMock(),
        )

        with patch("trendradar.__main__.AIAnalyzer") as analyzer_class:
            analyzer_class.return_value.analyze.return_value = AIAnalysisResult(
                success=True
            )
            result = analyzer._run_ai_analysis(
                [{"word": "育种"}], None, "weekly", "自然周周报", {},
                schedule=schedule(),
            )

        self.assertFalse(result.success)
        self.assertIn("once_analyze", result.error)

    def test_scheduler_record_execution_returns_storage_result(self):
        storage = MagicMock()
        storage.record_period_execution_strict.return_value = False
        scheduler = Scheduler(
            {"enabled": True, "preset": "custom"},
            TIMELINE,
            storage,
            lambda: pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            ),
        )

        self.assertIs(
            scheduler.record_execution(
                "monday_weekly", "push", "2026-08-10"
            ),
            False,
        )

    def test_weekly_summary_failure_aborts_before_notification_or_once(self):
        scheduler = MagicMock()
        scheduler.already_executed.return_value = False
        dispatcher = MagicMock()
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config={
                "ENABLE_NOTIFICATION": True,
                "SHOW_VERSION_UPDATE": False,
                "AI_ANALYSIS": {"ENABLED": True},
            },
            platform_ids=[],
            create_notification_dispatcher=MagicMock(return_value=dispatcher),
            create_scheduler=MagicMock(return_value=scheduler),
            prepare_report=MagicMock(return_value={}),
            format_date=MagicMock(return_value="2026-08-10"),
        )
        analyzer.report_mode = "weekly"
        analyzer._report_period_label = "2026-08-03—2026-08-09"
        analyzer.frequency_file = None
        analyzer._hotlist_total_count = 0
        analyzer._rss_matched_count = 1
        analyzer._rss_total_count = 1
        analyzer._rss_source_total = 0
        analyzer._rss_source_failed = 0
        analyzer.proxy_url = None
        analyzer.update_info = None
        analyzer._has_notification_configured = MagicMock(return_value=True)
        analyzer._has_valid_content = MagicMock(return_value=False)

        sent = analyzer._send_notification_if_needed(
            [], "自然周周报", "weekly", rss_items=[{"count": 1}],
            ai_result=AIAnalysisResult(success=False, error="摘要失败"),
            schedule=schedule(),
        )

        self.assertFalse(sent)
        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_main_exits_one_when_run_reports_failure(self):
        analyzer = MagicMock()
        analyzer.is_github_actions = False
        analyzer.run.return_value = False
        analyzer.ctx.config = {"DEBUG": False}
        with patch("sys.argv", ["trendradar"]), \
             patch.object(main_module, "load_config", return_value={}), \
             patch.object(main_module, "NewsAnalyzer", return_value=analyzer):
            with self.assertRaises(SystemExit) as raised:
                main_module.main()

        self.assertEqual(raised.exception.code, 1)

    def test_main_passes_force_weekly_to_analyzer(self):
        analyzer = MagicMock()
        analyzer.is_github_actions = False
        analyzer.run.return_value = True
        analyzer.ctx.config = {"DEBUG": False}
        with patch("sys.argv", ["trendradar", "--force-weekly"]), \
             patch.object(main_module, "load_config", return_value={}), \
             patch.object(main_module, "NewsAnalyzer", return_value=analyzer) as cls:
            main_module.main()

        cls.assert_called_once_with(config={}, force_weekly=True)

    def test_run_returns_false_on_failure_and_true_for_normal_completion(self):
        successful = NewsAnalyzer.__new__(NewsAnalyzer)
        successful.ctx = SimpleNamespace(
            cleanup=MagicMock(),
            config={"DEBUG": False},
            get_time=MagicMock(return_value=RUN_AT),
        )
        successful.report_mode = "daily"
        successful._resolve_and_apply_schedule = MagicMock(
            return_value=schedule(report_mode="daily", ai_mode="daily")
        )
        successful._initialize_and_check_config = MagicMock(return_value=True)
        successful._crawl_data = MagicMock(return_value=({}, {}, []))
        successful._crawl_rss_data = MagicMock(return_value=(None, None, [], set()))
        successful._execute_mode_strategy = MagicMock()
        self.assertTrue(successful.run())

        failed = NewsAnalyzer.__new__(NewsAnalyzer)
        failed.ctx = SimpleNamespace(
            cleanup=MagicMock(),
            config={"DEBUG": False},
            get_time=MagicMock(return_value=RUN_AT),
        )
        failed._resolve_and_apply_schedule = MagicMock(side_effect=RuntimeError("boom"))
        self.assertFalse(failed.run())

    def test_weekly_snapshot_exception_makes_run_fail(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            cleanup=MagicMock(),
            config={"DEBUG": False},
            timezone="Asia/Shanghai",
            get_time=MagicMock(return_value=RUN_AT),
            create_scheduler=MagicMock(return_value=MagicMock(
                already_executed=MagicMock(return_value=False)
            )),
        )
        analyzer.report_mode = "weekly"
        analyzer._resolve_and_apply_schedule = MagicMock(return_value=schedule())
        analyzer._initialize_and_check_config = MagicMock(return_value=True)
        analyzer._fetch_agro_weather = MagicMock(return_value=CURRENT_WEATHER)
        attempt_lock = MagicMock()
        attempt_lock.acquire.return_value = True
        analyzer._create_weekly_attempt_lock = MagicMock(
            return_value=attempt_lock
        )
        analyzer._crawl_data = MagicMock(return_value=({}, {}, []))
        analyzer._crawl_rss_data = MagicMock(
            side_effect=RuntimeError("八个日库全部缺失")
        )
        analyzer._execute_mode_strategy = MagicMock()

        self.assertFalse(analyzer.run())
        analyzer._execute_mode_strategy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
