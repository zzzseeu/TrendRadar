import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytz

from trendradar import __main__ as main_module
from trendradar.__main__ import NewsAnalyzer
from trendradar.ai.analyzer import AIAnalysisResult
from trendradar.core.scheduler import Scheduler


TIMELINE = {"custom": {
    "default": {
        "collect": False,
        "analyze": False,
        "push": False,
        "report_mode": "current",
        "ai_mode": "follow_report",
        "once": {"analyze": False, "push": False},
    },
    "periods": {
        "daily_collect": {
            "name": "每日静默采集",
            "start": "10:00",
            "end": "10:01",
            "collect": True,
            "analyze": False,
            "push": False,
            "report_mode": "current",
        },
        "monday_weekly": {
            "name": "自然周周报",
            "start": "10:00",
            "end": "12:01",
            "collect": True,
            "analyze": True,
            "push": True,
            "report_mode": "weekly",
            "ai_mode": "weekly",
            "once": {"analyze": True, "push": True},
        },
    },
    "day_plans": {
        "monday": {"periods": ["monday_weekly"]},
        "collect_only": {"periods": ["daily_collect"]},
    },
    "week_map": {
        1: "monday",
        2: "collect_only",
        3: "collect_only",
        4: "collect_only",
        5: "collect_only",
        6: "collect_only",
        7: "collect_only",
    },
}}


def at(year, month, day, hour, minute):
    return pytz.timezone("Asia/Shanghai").localize(
        datetime(year, month, day, hour, minute)
    )


def schedule(**overrides):
    values = {
        "period_key": "monday_weekly",
        "period_name": "自然周周报",
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


class WeeklyCompatibilityTests(unittest.TestCase):
    def test_daily_once_analysis_still_skips_when_push_is_pending(self):
        scheduler = MagicMock()
        scheduler.already_executed.side_effect = (
            lambda _period, action, _date: action == "analyze"
        )
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config={
                "AI": {},
                "AI_ANALYSIS": {
                    "ENABLED": True,
                    "MODE": "follow_report",
                },
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

    def test_main_exits_one_when_run_reports_failure(self):
        analyzer = MagicMock()
        analyzer.is_github_actions = False
        analyzer.run.return_value = False
        analyzer.ctx.config = {"DEBUG": False}
        with patch("sys.argv", ["trendradar"]), patch.object(
            main_module, "load_config", return_value={}
        ), patch.object(
            main_module, "NewsAnalyzer", return_value=analyzer
        ):
            with self.assertRaises(SystemExit) as raised:
                main_module.main()

        self.assertEqual(raised.exception.code, 1)

    def test_main_passes_force_weekly_to_analyzer(self):
        analyzer = MagicMock()
        analyzer.is_github_actions = False
        analyzer.run.return_value = True
        analyzer.ctx.config = {"DEBUG": False}
        with patch("sys.argv", ["trendradar", "--force-weekly"]), patch.object(
            main_module, "load_config", return_value={}
        ), patch.object(
            main_module, "NewsAnalyzer", return_value=analyzer
        ) as analyzer_class:
            main_module.main()

        analyzer_class.assert_called_once_with(config={}, force_weekly=True)

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

    def test_manual_force_reanalyzes_despite_existing_analyze_checkpoint(self):
        scheduler = MagicMock()
        scheduler.already_executed.return_value = True
        expected = AIAnalysisResult(
            success=True,
            current_events_trends="时事动态 [current_events:1]",
            research_trends="科研进展 [research:1]",
            weather_risks="气象影响 [weather:official]",
        )
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.force_weekly = True
        analyzer.ctx = SimpleNamespace(
            config={
                "AI": {},
                "AI_ANALYSIS": {
                    "ENABLED": True,
                    "MODE": "follow_report",
                },
                "DEBUG": False,
            },
            create_scheduler=MagicMock(return_value=scheduler),
        )
        analyzer._delivery_checkpoint_date = MagicMock(
            return_value="2026-08-10"
        )
        analyzer._operation_run_at = MagicMock(return_value=at(
            2026, 8, 12, 15, 0
        ))
        analyzer._agro_weather_report = SimpleNamespace(title="气象周报")

        with patch("trendradar.__main__.AIAnalyzer") as analyzer_class:
            analyzer_class.return_value.analyze.return_value = expected
            result = analyzer._run_ai_analysis(
                [{"word": "育种"}],
                [{"title": "水稻动态"}],
                "weekly",
                "自然周周报",
                {},
                schedule=schedule(),
            )

        self.assertIs(result, expected)
        analyzer_class.return_value.analyze.assert_called_once()

    def test_automatic_retry_crontab_never_uses_manual_force_flag(self):
        crontab = (
            Path(__file__).resolve().parents[1]
            / "config" / "daily.crontab"
        ).read_text(encoding="utf-8")

        self.assertNotIn("--force-weekly", crontab)

    def test_scheduler_preset_resolves_weekly_and_collect_only_periods(self):
        for run_at, expected in (
            (at(2026, 8, 10, 10, 0), ("weekly", True, True)),
            (at(2026, 8, 11, 10, 0), ("current", False, False)),
        ):
            with self.subTest(run_at=run_at):
                resolved = Scheduler(
                    {"enabled": True, "preset": "custom"},
                    TIMELINE,
                    MagicMock(),
                    lambda value=run_at: value,
                ).resolve()

                self.assertEqual(resolved.report_mode, expected[0])
                self.assertTrue(resolved.collect)
                self.assertEqual(resolved.analyze, expected[1])
                self.assertEqual(resolved.push, expected[2])

    def test_force_weekly_resolves_outside_window(self):
        scheduler = Scheduler(
            {"enabled": True, "preset": "custom"},
            TIMELINE,
            MagicMock(),
            lambda: at(2026, 8, 12, 15, 0),
        )

        forced = scheduler.resolve(force_period_key="monday_weekly")

        self.assertEqual(forced.period_key, "monday_weekly")
        self.assertEqual(forced.day_plan, "forced")
        self.assertTrue(forced.collect and forced.analyze and forced.push)
        self.assertEqual(forced.report_mode, "weekly")

    def test_force_weekly_overrides_disabled_scheduler_fallback(self):
        scheduler = Scheduler(
            {"enabled": False, "preset": "custom"},
            TIMELINE,
            MagicMock(),
            lambda: at(2026, 8, 12, 15, 0),
        )

        forced = scheduler.resolve(force_period_key="monday_weekly")

        self.assertEqual(forced.period_key, "monday_weekly")
        self.assertEqual(forced.day_plan, "forced")
        self.assertTrue(forced.once_analyze)
        self.assertTrue(forced.once_push)
        self.assertEqual(forced.report_mode, "weekly")


if __name__ == "__main__":
    unittest.main()
