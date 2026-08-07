import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytz

from trendradar import __main__ as main_module
from trendradar.__main__ import NewsAnalyzer
from trendradar.ai.filter import AIFilterResult
from trendradar.core.scheduler import ResolvedSchedule, Scheduler
from trendradar.core.weekly import previous_natural_week


TIMELINE = {"custom": {
    "default": {
        "collect": True, "analyze": False, "push": False,
        "report_mode": "current", "ai_mode": "follow_report",
        "once": {"analyze": False, "push": False},
    },
    "periods": {"monday_weekly": {
        "name": "自然周周报", "start": "00:00", "end": "23:59",
        "analyze": True, "push": True, "report_mode": "weekly",
        "ai_mode": "follow_report",
        "once": {"analyze": True, "push": True},
    }},
    "day_plans": {
        "monday": {"periods": ["monday_weekly"]},
        "silent": {"periods": []},
    },
    "week_map": {
        1: "monday", 2: "silent", 3: "silent", 4: "silent",
        5: "silent", 6: "silent", 7: "silent",
    },
}}


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
        analyzer.ctx = SimpleNamespace(cleanup=MagicMock(), config={"DEBUG": False})
        analyzer.report_mode = "weekly"
        analyzer._rss_window = None
        analyzer._initialize_and_check_config = MagicMock(return_value=True)
        analyzer._resolve_and_apply_schedule = MagicMock(
            return_value=schedule(analyze=False, push=False)
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

    def test_schedule_is_resolved_once_before_initialization_and_crawl(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        events = []
        resolved = schedule(report_mode="weekly")
        analyzer.ctx = SimpleNamespace(cleanup=MagicMock(), config={"DEBUG": False})
        analyzer.report_mode = "weekly"
        analyzer._rss_window = object()
        analyzer._resolve_and_apply_schedule = MagicMock(
            side_effect=lambda: events.append("schedule") or resolved
        )
        analyzer._initialize_and_check_config = MagicMock(
            side_effect=lambda: events.append("initialize") or True
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

        self.assertEqual(events, ["schedule", "initialize", "crawl", "rss", "strategy"])
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

    def test_run_returns_false_on_failure_and_true_for_normal_completion(self):
        successful = NewsAnalyzer.__new__(NewsAnalyzer)
        successful.ctx = SimpleNamespace(cleanup=MagicMock(), config={"DEBUG": False})
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
        failed.ctx = SimpleNamespace(cleanup=MagicMock(), config={"DEBUG": False})
        failed._resolve_and_apply_schedule = MagicMock(side_effect=RuntimeError("boom"))
        self.assertFalse(failed.run())


if __name__ == "__main__":
    unittest.main()
