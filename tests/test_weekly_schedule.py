import builtins
import errno
import importlib.util
import multiprocessing
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytz

from trendradar.__main__ import NewsAnalyzer
from trendradar.core import scheduler as scheduler_module


RUN_AT = pytz.timezone("Asia/Shanghai").localize(
    datetime(2026, 8, 10, 10, 0)
)
CURRENT_WEATHER = SimpleNamespace(
    report_date="2026-08-10",
    review_start="2026-08-02",
    review_end="2026-08-08",
)
ROOT = Path(__file__).resolve().parents[1]


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


def acquire_weekly_lock_in_child(data_dir, checkpoint_date, result_queue):
    lock = scheduler_module.WeeklyAttemptLock(data_dir, checkpoint_date)
    acquired = lock.acquire()
    result_queue.put(acquired)
    if acquired:
        lock.release()


class WeeklyScheduleTests(unittest.TestCase):
    def make_analyzer(self, *, run_at=RUN_AT):
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
        analyzer._resolve_and_apply_schedule = MagicMock(
            return_value=schedule()
        )
        lock = MagicMock()
        lock.acquire.return_value = True
        analyzer._create_weekly_attempt_lock = MagicMock(return_value=lock)
        analyzer._resume_weekly_pdf_delivery = MagicMock(return_value=None)
        analyzer._fetch_agro_weather = MagicMock(return_value=CURRENT_WEATHER)
        analyzer._initialize_and_check_config = MagicMock(return_value=True)
        analyzer._crawl_data = MagicMock(return_value=({}, {}, []))
        analyzer._crawl_rss_data = MagicMock(
            return_value=(None, None, [], set())
        )
        analyzer._execute_mode_strategy = MagicMock(return_value=True)
        return analyzer, scheduler, lock

    def test_run_returns_false_on_failure_and_true_for_normal_completion(self):
        successful = NewsAnalyzer.__new__(NewsAnalyzer)
        successful.ctx = SimpleNamespace(
            cleanup=MagicMock(),
            config={"DEBUG": False},
            get_time=MagicMock(return_value=RUN_AT),
        )
        successful.report_mode = "daily"
        successful._resolve_and_apply_schedule = MagicMock(
            return_value=schedule(
                report_mode="daily", ai_mode="daily",
                once_analyze=False, once_push=False,
            )
        )
        successful._initialize_and_check_config = MagicMock(return_value=True)
        successful._crawl_data = MagicMock(return_value=({}, {}, []))
        successful._crawl_rss_data = MagicMock(
            return_value=(None, None, [], set())
        )
        successful._execute_mode_strategy = MagicMock(return_value=True)
        self.assertTrue(successful.run())

        failed = NewsAnalyzer.__new__(NewsAnalyzer)
        failed.ctx = SimpleNamespace(
            cleanup=MagicMock(),
            config={"DEBUG": False},
            get_time=MagicMock(return_value=RUN_AT),
        )
        failed._resolve_and_apply_schedule = MagicMock(
            side_effect=RuntimeError("boom")
        )
        self.assertFalse(failed.run())

    def test_scheduler_import_on_windows_does_not_require_fcntl(self):
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

    def test_windows_weekly_lock_calls_lock_and_unlock(self):
        fake_msvcrt = SimpleNamespace(
            LK_NBLCK=10,
            LK_UNLCK=20,
            locking=MagicMock(),
        )
        backend = scheduler_module._create_file_lock_backend(
            platform_name="nt", import_module=lambda _name: fake_msvcrt
        )
        with TemporaryDirectory() as data_dir:
            lock = scheduler_module.WeeklyAttemptLock(
                data_dir, "2026-08-10", backend=backend
            )
            self.assertTrue(lock.acquire())
            handle_fd = lock._handle.fileno()
            lock.release()

        self.assertEqual(
            fake_msvcrt.locking.call_args_list,
            [
                call(handle_fd, fake_msvcrt.LK_NBLCK, 1),
                call(handle_fd, fake_msvcrt.LK_UNLCK, 1),
            ],
        )

    def test_lock_contention_is_nonfatal_on_windows_and_posix(self):
        backends = {
            "nt": SimpleNamespace(
                LK_NBLCK=10,
                LK_UNLCK=20,
                locking=MagicMock(
                    side_effect=OSError(errno.EACCES, "lock violation")
                ),
            ),
            "posix": SimpleNamespace(
                LOCK_EX=1,
                LOCK_NB=2,
                LOCK_UN=4,
                flock=MagicMock(
                    side_effect=OSError(errno.EACCES, "lock contention")
                ),
            ),
        }
        for platform_name, module in backends.items():
            with self.subTest(platform_name=platform_name), TemporaryDirectory() as data_dir:
                backend = scheduler_module._create_file_lock_backend(
                    platform_name=platform_name,
                    import_module=lambda _name, value=module: value,
                )
                lock = scheduler_module.WeeklyAttemptLock(
                    data_dir, "2026-08-10", backend=backend
                )
                self.assertFalse(lock.acquire())
                self.assertIsNone(lock._handle)

    def test_weekly_attempt_lock_is_nonblocking_and_scoped_by_window(self):
        with TemporaryDirectory() as data_dir:
            first = scheduler_module.WeeklyAttemptLock(
                data_dir, "2026-08-10"
            )
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

    def test_overlapping_run_stops_before_checkpoint_weather_or_network(self):
        with TemporaryDirectory() as data_dir:
            held = scheduler_module.WeeklyAttemptLock(
                data_dir, "2026-08-10"
            )
            self.assertTrue(held.acquire())
            analyzer, scheduler, _ = self.make_analyzer()
            analyzer.storage_manager = SimpleNamespace(data_dir=data_dir)
            analyzer._create_weekly_attempt_lock = (
                NewsAnalyzer._create_weekly_attempt_lock.__get__(
                    analyzer, NewsAnalyzer
                )
            )
            try:
                self.assertTrue(analyzer.run())
            finally:
                held.release()

        scheduler.already_executed.assert_not_called()
        scheduler.record_execution.assert_not_called()
        analyzer._fetch_agro_weather.assert_not_called()
        analyzer._crawl_data.assert_not_called()
        analyzer._execute_mode_strategy.assert_not_called()

    def test_weekly_lock_covers_the_complete_delivery_transaction(self):
        analyzer, scheduler, lock = self.make_analyzer()
        events = []
        lock.acquire.side_effect = lambda: events.append("lock") or True
        scheduler.already_executed.side_effect = (
            lambda *_args: events.append("checkpoint") or False
        )
        analyzer._fetch_agro_weather.side_effect = (
            lambda: events.append("weather") or CURRENT_WEATHER
        )
        analyzer._initialize_and_check_config.side_effect = (
            lambda: events.append("initialize") or True
        )
        analyzer._crawl_data.side_effect = (
            lambda: events.append("crawl") or ({}, {}, [])
        )
        analyzer._crawl_rss_data.side_effect = (
            lambda: events.append("rss") or (None, None, [], set())
        )
        analyzer._execute_mode_strategy.side_effect = (
            lambda *_args, **_kwargs: events.append("delivery") or True
        )
        lock.release.side_effect = lambda: events.append("release")

        self.assertTrue(analyzer.run())

        self.assertEqual(
            events,
            [
                "lock", "checkpoint", "weather", "initialize",
                "crawl", "rss", "delivery", "release",
            ],
        )

    def test_failed_weekly_run_releases_lock_for_retry(self):
        analyzer, _, lock = self.make_analyzer()
        analyzer._fetch_agro_weather.return_value = None

        self.assertFalse(analyzer.run())

        lock.release.assert_called_once_with()

    def test_missing_or_invalid_weather_aborts_before_ordinary_crawl(self):
        failures = [None, RuntimeError("气象结构错误")]
        for failure in failures:
            with self.subTest(failure=failure):
                analyzer, scheduler, _ = self.make_analyzer()
                if isinstance(failure, Exception):
                    analyzer._fetch_agro_weather.side_effect = failure
                else:
                    analyzer._fetch_agro_weather.return_value = failure

                self.assertFalse(analyzer.run())

                analyzer._crawl_data.assert_not_called()
                analyzer._crawl_rss_data.assert_not_called()
                analyzer._execute_mode_strategy.assert_not_called()
                scheduler.record_execution.assert_not_called()

    def test_success_checkpoint_skips_retry_before_weather_or_network(self):
        analyzer, scheduler, _ = self.make_analyzer()
        scheduler.already_executed.return_value = True

        self.assertTrue(analyzer.run())

        scheduler.already_executed.assert_called_once_with(
            "monday_weekly", "push", "2026-08-10"
        )
        analyzer._fetch_agro_weather.assert_not_called()
        analyzer._crawl_data.assert_not_called()

    def test_manual_weather_fetch_uses_weekly_window_end_anchor(self):
        run_at = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 12, 15, 0)
        )
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config={"AGRO_WEATHER": {}}, timezone="Asia/Shanghai"
        )
        analyzer.proxy_url = None
        analyzer._run_at = run_at

        with patch("trendradar.__main__.AgroWeatherClient") as client_class:
            client_class.return_value.fetch_latest.return_value = CURRENT_WEATHER
            report = analyzer._fetch_agro_weather()

        self.assertIs(report, CURRENT_WEATHER)
        client_class.return_value.fetch_latest.assert_called_once_with(
            run_at,
            expected_delivery_anchor=pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 0, 0)
            ),
        )

if __name__ == "__main__":
    unittest.main()
