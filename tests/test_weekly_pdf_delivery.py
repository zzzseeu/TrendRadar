# coding=utf-8
"""Regression coverage for the strict weekly WeCom PDF-only delivery path."""

import tempfile
import unittest
import hashlib
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytz

from trendradar.__main__ import NewsAnalyzer
from trendradar.ai.analyzer import AIAnalysisResult
from trendradar.context import AppContext
from trendradar.core.weekly import previous_natural_week
from trendradar.notification.dispatcher import NotificationDispatcher
from trendradar.notification.wework_pdf import (
    MAX_WEWORK_FILE_BYTES,
    send_wework_pdf_file,
)


WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test"


def response(payload, status_code=200):
    result = MagicMock(status_code=status_code)
    result.json.return_value = payload
    return result


def schedule(**overrides):
    values = {
        "period_key": "monday_weekly",
        "period_name": "自然周周报",
        "collect": True,
        "analyze": True,
        "push": True,
        "report_mode": "weekly",
        "once_analyze": True,
        "once_push": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class WeeklyPdfMainlineTests(unittest.TestCase):
    def setUp(self):
        self.run_at = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 12, 15, 0)
        )
        self.window = previous_natural_week(
            self.run_at, "Asia/Shanghai"
        )
        self.weather = SimpleNamespace(
            title="全国农业气象周报",
            impact="影响",
            outlook="展望",
            recommendations="建议",
            source_url=(
                "https://www.nmc.cn/publish/agro/ten-week/index.html"
            ),
        )

    @staticmethod
    def _item(module_type):
        return {
            "title": f"{module_type} news",
            "url": f"https://example.org/{module_type}",
            "source_name": "Source",
            "published_at": "2026-08-06T08:00:00+08:00",
            "module_type": module_type,
            "relevance_score": 0.8,
            "importance_score": 0.8,
            "content_level": "summary",
        }

    def _analyzer(self, selected_items, *, weather=True, html=True):
        scheduler = MagicMock()
        scheduler.already_executed.return_value = False
        scheduler.record_execution.return_value = True
        dispatcher = MagicMock()
        dispatcher.dispatch_weekly_pdf.return_value = True
        ai_filter = SimpleNamespace(
            success=True, total_matched=len(selected_items), tags=[]
        )
        rss_groups = [
            {
                "word": "weekly",
                "count": len(selected_items),
                "titles": list(selected_items),
            }
        ] if selected_items else []
        ctx = SimpleNamespace(
            config={
                "ENABLE_NOTIFICATION": True,
                "SHOW_VERSION_UPDATE": False,
                "DEBUG": False,
                "AI_FILTER": {"MIN_SCORE": 0.5},
                "AI_ANALYSIS": {"ENABLED": True, "MODE": "weekly"},
                "AI_TRANSLATION": {"ENABLED": False},
                "STORAGE": {"FORMATS": {"HTML": html}},
                "DISPLAY": {"REGIONS": {}},
            },
            display_mode="keyword",
            platform_ids=[],
            run_ai_filter=MagicMock(return_value=ai_filter),
            convert_ai_filter_to_report_data=MagicMock(
                return_value=([], rss_groups, [])
            ),
            generate_html=MagicMock(return_value="output/html/weekly.html"),
            create_scheduler=MagicMock(return_value=scheduler),
            create_notification_dispatcher=MagicMock(
                return_value=dispatcher
            ),
            cleanup=MagicMock(),
            timezone="Asia/Shanghai",
            get_time=MagicMock(return_value=self.run_at),
            detect_new_titles=MagicMock(return_value={}),
            load_frequency_words=MagicMock(return_value=([], [], [])),
        )
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = ctx
        analyzer.report_mode = "weekly"
        analyzer.filter_method = "ai"
        analyzer.interests_file = None
        analyzer._rss_window = self.window
        analyzer._allowed_rss_ids = set(range(1, len(selected_items) + 1))
        analyzer._rss_ids_authoritative = True
        analyzer._agro_weather_report = self.weather if weather else None
        analyzer._rss_total_count = len(selected_items)
        analyzer._rss_source_total = 1
        analyzer._rss_source_failed = 0
        analyzer._report_period_label = self.window.label
        analyzer._run_at = self.run_at
        analyzer.proxy_url = ""
        analyzer._weekly_pdf_digest = MagicMock(return_value="a" * 64)
        analyzer.update_info = None
        analyzer.frequency_file = None
        analyzer._run_ai_analysis = MagicMock(return_value=AIAnalysisResult(
            success=True,
            policy_trends="政策暂无" if not any(
                item["module_type"] == "policy" for item in selected_items
            ) else "政策趋势 [policy:1]",
            research_trends="科研暂无" if not any(
                item["module_type"] == "research" for item in selected_items
            ) else "科研趋势 [research:1]",
            weather_risks="气象风险 [weather:official]",
        ))
        analyzer._has_notification_configured = MagicMock(return_value=True)
        return analyzer, scheduler, dispatcher

    def _prepare_run(self, analyzer, *, resolved_schedule=None):
        analyzer._resolve_and_apply_schedule = MagicMock(
            return_value=resolved_schedule or schedule()
        )
        lock = MagicMock()
        lock.acquire.return_value = True
        analyzer._create_weekly_attempt_lock = MagicMock(return_value=lock)
        analyzer._resume_weekly_pdf_delivery = MagicMock(return_value=None)
        analyzer._fetch_agro_weather = MagicMock(
            return_value=analyzer._agro_weather_report
        )
        analyzer._initialize_and_check_config = MagicMock(return_value=True)
        analyzer._crawl_data = MagicMock(return_value=({}, {}, []))
        analyzer._crawl_rss_data = MagicMock(
            return_value=(None, None, [], set())
        )
        analyzer._prepare_current_title_info = MagicMock(return_value={})
        analyzer._prepare_standalone_data = MagicMock(return_value={})
        analyzer._has_valid_content = MagicMock(return_value=False)
        analyzer._should_open_browser = MagicMock(return_value=False)
        analyzer.is_docker_container = False
        return lock

    def _run_pipeline(self, analyzer, *, once_analyze=False):
        return analyzer._run_analysis_pipeline(
            {}, "weekly", {}, {}, [], [], {},
            schedule=schedule(once_analyze=once_analyze),
        )

    def test_weekly_mainline_uses_only_one_dedicated_pdf_and_file_delivery(self):
        analyzer, _, dispatcher = self._analyzer([
            self._item("policy"), self._item("research")
        ])
        with patch(
            "trendradar.__main__.render_weekly_pdf_html",
            return_value="<html />",
        ) as render, patch(
            "trendradar.__main__.build_weekly_pdf",
            return_value="output/weekly.pdf",
        ) as build:
            _, html_file, _, _, _, _ = self._run_pipeline(analyzer)
            delivered = analyzer._send_notification_if_needed(
                [], "自然周周报", "weekly", schedule=schedule()
            )

        self.assertIsNone(html_file)
        analyzer.ctx.generate_html.assert_not_called()
        render.assert_called_once()
        self.assertEqual(len(render.call_args.kwargs["policy_items"]), 1)
        self.assertEqual(len(render.call_args.kwargs["research_items"]), 1)
        narrative_groups = analyzer._run_ai_analysis.call_args.args[1]
        narrative_items = [
            item for group in narrative_groups for item in group["titles"]
        ]
        self.assertIs(
            render.call_args.kwargs["policy_items"][0], narrative_items[0]
        )
        self.assertIs(
            render.call_args.kwargs["research_items"][0], narrative_items[1]
        )
        build.assert_called_once()
        self.assertTrue(delivered)
        dispatcher.dispatch_weekly_pdf.assert_called_once()
        dispatcher.dispatch_all.assert_not_called()

    def test_run_has_one_pdf_only_mainline_and_writes_no_generic_html(self):
        analyzer, _, dispatcher = self._analyzer([
            self._item("policy"), self._item("research")
        ])
        self._prepare_run(analyzer)
        with tempfile.TemporaryDirectory() as workdir:
            workdir_path = Path(workdir)

            def forbidden_html(*_args, **_kwargs):
                latest = workdir_path / "output/html/latest/weekly.html"
                latest.parent.mkdir(parents=True, exist_ok=True)
                latest.write_text("forbidden", encoding="utf-8")
                (workdir_path / "output/index.html").write_text(
                    "forbidden", encoding="utf-8"
                )
                (workdir_path / "index.html").write_text(
                    "forbidden", encoding="utf-8"
                )
                return str(latest)

            analyzer.ctx.generate_html.side_effect = forbidden_html

            def build_pdf(*_args):
                pdf_path = workdir_path / "output/weekly.pdf"
                pdf_path.parent.mkdir(parents=True, exist_ok=True)
                pdf_path.write_bytes(b"%PDF-1.4\nweekly")
                return str(pdf_path)

            previous_cwd = os.getcwd()
            os.chdir(workdir)
            try:
                with patch(
                    "trendradar.__main__.render_weekly_pdf_html",
                    return_value="<html />",
                ) as render, patch(
                    "trendradar.__main__.build_weekly_pdf",
                    side_effect=build_pdf,
                ) as build:
                    result = analyzer.run()
            finally:
                os.chdir(previous_cwd)

            self.assertTrue(result)
            self.assertFalse(
                (workdir_path / "output/html/latest/weekly.html").exists()
            )
            self.assertFalse((workdir_path / "output/index.html").exists())
            self.assertFalse((workdir_path / "index.html").exists())

        analyzer.ctx.generate_html.assert_not_called()
        render.assert_called_once()
        build.assert_called_once()
        dispatcher.dispatch_weekly_pdf.assert_called_once()
        dispatcher.dispatch_all.assert_not_called()

    def test_run_rebuilds_selection_and_pdf_when_no_account_succeeded(self):
        analyzer, scheduler, dispatcher = self._analyzer([
            self._item("policy")
        ])
        pending_schedule = schedule(once_push=False)
        lock = self._prepare_run(
            analyzer, resolved_schedule=pending_schedule
        )
        analyzer._run_ai_analysis = NewsAnalyzer._run_ai_analysis.__get__(
            analyzer, NewsAnalyzer
        )
        executed = set()
        scheduler.already_executed.side_effect = (
            lambda _period, action, _date: action in executed
        )
        scheduler.record_execution.side_effect = (
            lambda _period, action, _date: executed.add(action) or True
        )
        dispatcher.dispatch_weekly_pdf.side_effect = [False, True]
        built_paths = []

        def build_pdf(*_args):
            path = f"output/rebuilt-{len(built_paths) + 1}.pdf"
            built_paths.append(path)
            return path

        with patch("trendradar.__main__.AIAnalyzer") as analyzer_class, patch(
            "trendradar.__main__.render_weekly_pdf_html",
            return_value="<html />",
        ) as render, patch(
            "trendradar.__main__.build_weekly_pdf",
            side_effect=build_pdf,
        ) as build:
            analyzer_class.return_value.analyze.return_value = AIAnalysisResult(
                success=True,
                policy_trends="政策趋势 [policy:1]",
                research_trends="科研暂无 [research:none]",
                weather_risks="气象风险 [weather:official]",
            )
            self.assertFalse(analyzer.run())
            first_selection = analyzer._weekly_news_modules
            self.assertTrue(analyzer.run())
            second_selection = analyzer._weekly_news_modules

        self.assertIsNot(first_selection, second_selection)
        self.assertEqual(build.call_count, 2)
        self.assertEqual(render.call_count, 2)
        self.assertEqual(built_paths, [
            "output/rebuilt-1.pdf", "output/rebuilt-2.pdf"
        ])
        self.assertEqual(dispatcher.dispatch_weekly_pdf.call_count, 2)
        self.assertEqual(analyzer_class.return_value.analyze.call_count, 2)
        self.assertEqual(lock.release.call_count, 2)
        push_calls = [
            item for item in scheduler.record_execution.call_args_list
            if item.args[1] == "push"
        ]
        self.assertEqual(len(push_calls), 1)
        self.assertIn("analyze", executed)
        self.assertIn("push", executed)

    def test_contract_change_rejects_partial_artifact_before_rebuilding(self):
        analyzer, scheduler, dispatcher = self._analyzer([
            self._item("policy")
        ])
        self._prepare_run(analyzer)
        analyzer._resume_weekly_pdf_delivery = (
            NewsAnalyzer._resume_weekly_pdf_delivery.__get__(
                analyzer, NewsAnalyzer
            )
        )
        dispatcher.weekly_pdf_account_hashes.return_value = [
            "account-a", "account-b"
        ]
        with tempfile.TemporaryDirectory() as tmp:
            old_pdf = Path(tmp) / (
                "农业育种新闻周报_三模块_2026-08-03至2026-08-09.pdf"
            )
            old_pdf.write_bytes(b"%PDF-1.4\nold-contract")
            old_digest = hashlib.sha256(old_pdf.read_bytes()).hexdigest()
            old_contract = analyzer._weekly_artifact_contract_hash()
            delivered_actions = {
                NewsAnalyzer._weekly_account_delivery_action(
                    old_contract, old_digest, "account-a"
                )
            }
            scheduler.already_executed.side_effect = (
                lambda _period, action, _date: action in delivered_actions
            )
            scheduler.record_execution.side_effect = (
                lambda _period, action, _date:
                delivered_actions.add(action) or True
            )

            analyzer.ctx.config["AI_FILTER"]["MIN_SCORE"] = 0.6
            new_pdf = Path(tmp) / "rebuilt.pdf"
            new_pdf.write_bytes(b"%PDF-1.4\nnew-contract")
            with patch(
                "trendradar.__main__.weekly_pdf_output_path",
                return_value=old_pdf,
            ), patch(
                "trendradar.__main__.render_weekly_pdf_html",
                return_value="<html />",
            ), patch(
                "trendradar.__main__.build_weekly_pdf",
                return_value=str(new_pdf),
            ) as build:
                self.assertTrue(analyzer.run())

        build.assert_called_once()
        dispatcher.dispatch_weekly_pdf.assert_called_once()
        self.assertEqual(
            dispatcher.dispatch_weekly_pdf.call_args.args[0], str(new_pdf)
        )
        queried_actions = {
            call.args[1] for call in scheduler.already_executed.call_args_list
        }
        self.assertNotIn(
            NewsAnalyzer._weekly_account_delivery_action(
                old_contract, old_digest, "account-a"
            ),
            queried_actions,
        )

    def test_fresh_pdf_contract_change_after_build_writes_no_ledger(self):
        analyzer, scheduler, dispatcher = self._analyzer([
            self._item("policy")
        ])
        self._prepare_run(analyzer)
        analyzer._weekly_artifact_contract_hash = MagicMock(
            side_effect=["a" * 64, "b" * 64]
        )

        with patch(
            "trendradar.__main__.render_weekly_pdf_html",
            return_value="<html />",
        ), patch(
            "trendradar.__main__.build_weekly_pdf",
            return_value="output/weekly.pdf",
        ) as build:
            self.assertFalse(analyzer.run())

        build.assert_called_once()
        self.assertEqual(
            analyzer._weekly_artifact_contract_hash.call_count, 2
        )
        dispatcher.dispatch_weekly_pdf.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_weekly_mainline_allows_either_news_module_to_be_empty(self):
        for module_type in ("policy", "research"):
            with self.subTest(module_type=module_type):
                analyzer, _, _ = self._analyzer([
                    self._item(module_type)
                ])
                self._prepare_run(analyzer)
                with patch(
                    "trendradar.__main__.render_weekly_pdf_html",
                    return_value="<html />",
                ) as render, patch(
                    "trendradar.__main__.build_weekly_pdf",
                    return_value="output/weekly.pdf",
                ):
                    self.assertTrue(analyzer.run())

                expected = render.call_args.kwargs[f"{module_type}_items"]
                other = "research" if module_type == "policy" else "policy"
                self.assertEqual(len(expected), 1)
                self.assertEqual(render.call_args.kwargs[f"{other}_items"], [])

    def test_weekly_mainline_allows_weather_only_content(self):
        analyzer, scheduler, dispatcher = self._analyzer([])
        self._prepare_run(analyzer)
        with patch(
            "trendradar.__main__.render_weekly_pdf_html",
            return_value="<html />",
        ) as render, patch(
            "trendradar.__main__.build_weekly_pdf",
            return_value="output/weekly.pdf",
        ):
            self.assertTrue(analyzer.run())

        self.assertEqual(render.call_args.kwargs["policy_items"], [])
        self.assertEqual(render.call_args.kwargs["research_items"], [])
        self.assertIs(render.call_args.kwargs["agro_weather"], self.weather)
        analyzer.ctx.run_ai_filter.assert_not_called()
        analyzer.ctx.convert_ai_filter_to_report_data.assert_not_called()
        analyzer._run_ai_analysis.assert_called_once()
        self.assertEqual(analyzer._weekly_news_modules.policy, [])
        self.assertEqual(analyzer._weekly_news_modules.research, [])
        self.assertFalse(analyzer._weekly_ai_filter_succeeded)
        dispatcher.dispatch_weekly_pdf.assert_called_once()
        push_calls = [
            item for item in scheduler.record_execution.call_args_list
            if item.args[1] == "push"
        ]
        self.assertEqual(len(push_calls), 1)

    def test_weekly_ai_analysis_disabled_fails_before_pdf_or_delivery(self):
        analyzer, scheduler, dispatcher = self._analyzer([])
        analyzer.ctx.config["AI_ANALYSIS"]["ENABLED"] = False
        self._prepare_run(analyzer)
        with patch("trendradar.__main__.render_weekly_pdf_html") as render, patch(
            "trendradar.__main__.build_weekly_pdf"
        ) as build:
            self.assertFalse(analyzer.run())

        analyzer._run_ai_analysis.assert_not_called()
        render.assert_not_called()
        build.assert_not_called()
        dispatcher.dispatch_weekly_pdf.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_weekly_schedule_without_analysis_fails_before_pdf_or_delivery(self):
        analyzer, scheduler, dispatcher = self._analyzer([])
        self._prepare_run(
            analyzer, resolved_schedule=schedule(analyze=False)
        )
        with patch("trendradar.__main__.render_weekly_pdf_html") as render, patch(
            "trendradar.__main__.build_weekly_pdf"
        ) as build:
            self.assertFalse(analyzer.run())

        analyzer._run_ai_analysis.assert_not_called()
        render.assert_not_called()
        build.assert_not_called()
        dispatcher.dispatch_weekly_pdf.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_weekly_incomplete_three_part_narrative_fails_closed(self):
        analyzer, scheduler, dispatcher = self._analyzer([])
        analyzer._run_ai_analysis.return_value = AIAnalysisResult(
            success=True,
            policy_trends="政策暂无 [policy:none]",
            research_trends="科研暂无 [research:none]",
            weather_risks="",
        )
        self._prepare_run(analyzer)
        with patch("trendradar.__main__.render_weekly_pdf_html") as render, patch(
            "trendradar.__main__.build_weekly_pdf"
        ) as build:
            self.assertFalse(analyzer.run())

        render.assert_not_called()
        build.assert_not_called()
        dispatcher.dispatch_weekly_pdf.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_weekly_mainline_rejects_three_empty_modules(self):
        analyzer, scheduler, dispatcher = self._analyzer([], weather=False)
        lock = self._prepare_run(analyzer)
        with patch("trendradar.__main__.build_weekly_pdf") as build:
            self.assertFalse(analyzer.run())

        analyzer.ctx.generate_html.assert_not_called()
        analyzer.ctx.run_ai_filter.assert_not_called()
        build.assert_not_called()
        dispatcher.dispatch_weekly_pdf.assert_not_called()
        scheduler.record_execution.assert_not_called()
        lock.release.assert_called_once_with()

    def test_weekly_pdf_failure_does_not_write_analyze_or_push_checkpoint(self):
        analyzer, scheduler, dispatcher = self._analyzer([])
        self._prepare_run(analyzer)
        analyzer._run_ai_analysis = NewsAnalyzer._run_ai_analysis.__get__(
            analyzer, NewsAnalyzer
        )
        with patch("trendradar.__main__.AIAnalyzer") as analyzer_class, patch(
            "trendradar.__main__.render_weekly_pdf_html",
            return_value="<html />",
        ), patch(
            "trendradar.__main__.build_weekly_pdf",
            side_effect=RuntimeError("PDF build failed"),
        ):
            analyzer_class.return_value.analyze.return_value = AIAnalysisResult(
                success=True,
                policy_trends="政策暂无 [policy:none]",
                research_trends="科研暂无 [research:none]",
                weather_risks="气象风险 [weather:official]",
            )
            self.assertFalse(analyzer.run())

        scheduler.record_execution.assert_not_called()
        dispatcher.dispatch_weekly_pdf.assert_not_called()

    def test_weekly_writes_analyze_checkpoint_only_after_pdf_is_durable(self):
        analyzer, scheduler, _ = self._analyzer([])
        analyzer._run_ai_analysis = NewsAnalyzer._run_ai_analysis.__get__(
            analyzer, NewsAnalyzer
        )
        events = []
        scheduler.record_execution.side_effect = (
            lambda *_args: events.append("analyze") or True
        )
        with patch("trendradar.__main__.AIAnalyzer") as analyzer_class, patch(
            "trendradar.__main__.render_weekly_pdf_html",
            return_value="<html />",
        ), patch(
            "trendradar.__main__.build_weekly_pdf",
            side_effect=lambda *_args: events.append("pdf")
            or "output/weekly.pdf",
        ):
            analyzer_class.return_value.analyze.return_value = AIAnalysisResult(
                success=True,
                policy_trends="政策暂无 [policy:none]",
                research_trends="科研暂无 [research:none]",
                weather_risks="气象风险 [weather:official]",
            )
            self._run_pipeline(analyzer, once_analyze=True)

        self.assertEqual(events, ["pdf", "analyze"])
        scheduler.record_execution.assert_called_once_with(
            "monday_weekly", "analyze", "2026-08-10"
        )

    def test_weekly_classification_failure_writes_no_checkpoint(self):
        analyzer, scheduler, dispatcher = self._analyzer([
            self._item("policy")
        ])
        self._prepare_run(analyzer)
        analyzer.ctx.run_ai_filter.return_value = SimpleNamespace(
            success=False, error="classification failed"
        )
        with patch("trendradar.__main__.build_weekly_pdf") as build:
            self.assertFalse(analyzer.run())

        build.assert_not_called()
        scheduler.record_execution.assert_not_called()
        dispatcher.dispatch_weekly_pdf.assert_not_called()

    def test_weekly_narrative_failure_writes_no_checkpoint(self):
        analyzer, scheduler, dispatcher = self._analyzer([])
        self._prepare_run(analyzer)
        analyzer._run_ai_analysis = NewsAnalyzer._run_ai_analysis.__get__(
            analyzer, NewsAnalyzer
        )
        with patch("trendradar.__main__.AIAnalyzer") as analyzer_class, patch(
            "trendradar.__main__.build_weekly_pdf"
        ) as build:
            analyzer_class.return_value.analyze.return_value = AIAnalysisResult(
                success=False, error="narrative failed"
            )
            self.assertFalse(analyzer.run())

        build.assert_not_called()
        scheduler.record_execution.assert_not_called()
        dispatcher.dispatch_weekly_pdf.assert_not_called()

    def test_weekly_analyze_storage_failure_stops_before_delivery(self):
        analyzer, scheduler, dispatcher = self._analyzer([])
        self._prepare_run(analyzer)
        analyzer._run_ai_analysis = NewsAnalyzer._run_ai_analysis.__get__(
            analyzer, NewsAnalyzer
        )
        scheduler.record_execution.return_value = False
        with patch("trendradar.__main__.AIAnalyzer") as analyzer_class, patch(
            "trendradar.__main__.render_weekly_pdf_html",
            return_value="<html />",
        ), patch(
            "trendradar.__main__.build_weekly_pdf",
            return_value="output/weekly.pdf",
        ) as build:
            analyzer_class.return_value.analyze.return_value = AIAnalysisResult(
                success=True,
                policy_trends="政策暂无 [policy:none]",
                research_trends="科研暂无 [research:none]",
                weather_risks="气象风险 [weather:official]",
            )
            self.assertFalse(analyzer.run())

        build.assert_called_once()
        scheduler.record_execution.assert_called_once_with(
            "monday_weekly", "analyze", "2026-08-10"
        )
        dispatcher.dispatch_weekly_pdf.assert_not_called()

    def test_weekly_strategy_keeps_explicit_report_type(self):
        self.assertEqual(
            NewsAnalyzer.MODE_STRATEGIES["weekly"]["report_type"],
            "上周周报",
        )

    def test_context_refuses_generic_weekly_html_generation(self):
        ctx = AppContext.__new__(AppContext)
        ctx.config = {}
        with patch("trendradar.context.generate_html_report") as generate:
            with self.assertRaisesRegex(RuntimeError, "weekly"):
                ctx.generate_html([], 0, mode="weekly")

        generate.assert_not_called()

    def test_context_still_generates_generic_html_for_ordinary_modes(self):
        ctx = AppContext.__new__(AppContext)
        ctx.config = {}
        with patch(
            "trendradar.context.generate_html_report",
            return_value="output/html/daily.html",
        ) as generate:
            result = ctx.generate_html([], 0, mode="daily")

        self.assertEqual(result, "output/html/daily.html")
        generate.assert_called_once()


class WeeklyPdfDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.schedule = schedule()

    def tearDown(self):
        self.tempdir.cleanup()

    def _pdf(self, name="weekly.pdf"):
        path = Path(self.tempdir.name) / name
        path.write_bytes(b"%PDF-1.4\n" + b"x" * 128)
        return path

    def _dispatcher(self, webhook=WEBHOOK):
        return NotificationDispatcher(
            {"WEWORK_WEBHOOK_URL": webhook, "MAX_ACCOUNTS_PER_CHANNEL": 3},
            lambda: None,
            MagicMock(),
        )

    def _weekly_execution_analyzer(
        self,
        *,
        notification_enabled=True,
        dispatcher=None,
    ):
        run_at = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 12, 15, 0)
        )
        scheduler = MagicMock()
        scheduler.already_executed.return_value = False
        scheduler.record_execution.return_value = True
        notification_config = {
            "ENABLE_NOTIFICATION": notification_enabled,
            "FEISHU_WEBHOOK_URL": "",
            "DINGTALK_WEBHOOK_URL": "",
            "WEWORK_WEBHOOK_URL": "",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "EMAIL_FROM": "",
            "EMAIL_PASSWORD": "",
            "EMAIL_TO": "",
            "NTFY_SERVER_URL": "",
            "NTFY_TOPIC": "",
            "BARK_URL": "",
            "SLACK_WEBHOOK_URL": "",
            "GENERIC_WEBHOOK_URL": "",
        }
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config=notification_config,
            platform_ids=[],
            detect_new_titles=MagicMock(return_value={}),
            load_frequency_words=MagicMock(return_value=([], [], [])),
            create_scheduler=MagicMock(return_value=scheduler),
            create_notification_dispatcher=MagicMock(return_value=dispatcher),
        )
        analyzer.report_mode = "weekly"
        analyzer.frequency_file = None
        analyzer.proxy_url = ""
        analyzer._run_at = run_at
        analyzer._run_time_filename = "15-00"
        analyzer._rss_window = previous_natural_week(run_at, "Asia/Shanghai")
        analyzer._weekly_pdf_path = str(self._pdf())
        expected_contract = "a" * 64
        analyzer._weekly_artifact_contract_expected = expected_contract
        analyzer._weekly_artifact_contract_hash = MagicMock(
            return_value=expected_contract
        )
        analyzer._prepare_current_title_info = MagicMock(return_value={})
        analyzer._prepare_standalone_data = MagicMock(return_value={})
        analyzer._run_analysis_pipeline = MagicMock(
            return_value=([], None, None, None, {}, None)
        )
        analyzer._has_valid_content = MagicMock(return_value=False)
        analyzer._should_open_browser = MagicMock(return_value=False)
        analyzer.is_docker_container = False
        return analyzer, scheduler

    def _execute_weather_only_weekly(self, analyzer):
        return analyzer._execute_mode_strategy(
            NewsAnalyzer.MODE_STRATEGIES["weekly"], {}, {}, [],
            rss_items=None,
            rss_new_items=None,
            raw_rss_items=None,
            rss_new_urls=set(),
            schedule=self.schedule,
        )

    def test_weekly_delivery_uploads_and_sends_only_one_file_message(self):
        pdf_path = self._pdf()
        responses = [
            response({"errcode": 0, "media_id": "media-1"}),
            response({"errcode": 0}),
        ]
        with patch(
            "trendradar.notification.wework_pdf.requests.post",
            side_effect=responses,
        ) as post:
            ok = send_wework_pdf_file(WEBHOOK, str(pdf_path))

        self.assertTrue(ok)
        self.assertEqual(post.call_count, 2)
        self.assertIn("upload_media", post.call_args_list[0].args[0])
        self.assertEqual(
            post.call_args_list[1].kwargs["json"],
            {"msgtype": "file", "file": {"media_id": "media-1"}},
        )
        payloads = [call.kwargs.get("json") for call in post.call_args_list]
        self.assertFalse(any(
            payload and payload.get("msgtype") in {"text", "markdown"}
            for payload in payloads
        ))

    def test_upload_failure_does_not_send_a_file_message(self):
        with patch(
            "trendradar.notification.wework_pdf.requests.post",
            return_value=response({"errcode": 1}),
        ) as post:
            with self.assertRaisesRegex(RuntimeError, "上传失败"):
                send_wework_pdf_file(WEBHOOK, str(self._pdf()))

        self.assertEqual(post.call_count, 1)

    def test_file_send_failure_returns_false_without_text_fallback(self):
        with patch(
            "trendradar.notification.wework_pdf.requests.post",
            side_effect=[
                response({"errcode": 0, "media_id": "media-1"}),
                response({"errcode": 1}),
            ],
        ) as post:
            self.assertFalse(send_wework_pdf_file(WEBHOOK, str(self._pdf())))

        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[1].kwargs["json"]["msgtype"], "file")

    def test_dispatcher_requires_every_configured_wework_account(self):
        dispatcher = self._dispatcher(f"{WEBHOOK};{WEBHOOK}2")
        with patch(
            "trendradar.notification.dispatcher.send_wework_pdf_file",
            side_effect=[True, False],
        ) as send:
            self.assertFalse(dispatcher.dispatch_weekly_pdf(str(self._pdf())))

        self.assertEqual(send.call_count, 2)

    def test_dispatcher_rejects_missing_wework_webhook(self):
        dispatcher = self._dispatcher("")
        with patch(
            "trendradar.notification.dispatcher.send_wework_pdf_file",
        ) as send:
            self.assertFalse(dispatcher.dispatch_weekly_pdf(str(self._pdf())))

        send.assert_not_called()

    def test_over_limit_pdf_never_reaches_wework(self):
        pdf_path = Path(self.tempdir.name) / "too-large.pdf"
        with pdf_path.open("wb") as file:
            file.truncate(MAX_WEWORK_FILE_BYTES + 1)
        with patch("trendradar.notification.wework_pdf.requests.post") as post:
            with self.assertRaisesRegex(ValueError, "20MB"):
                send_wework_pdf_file(WEBHOOK, str(pdf_path))
        post.assert_not_called()

    def test_invalid_pdf_never_reaches_wework(self):
        invalid_pdf = Path(self.tempdir.name) / "invalid.pdf"
        invalid_pdf.write_bytes(b"not a PDF")
        with patch("trendradar.notification.wework_pdf.requests.post") as post:
            with self.assertRaisesRegex(ValueError, "无效"):
                send_wework_pdf_file(WEBHOOK, str(invalid_pdf))
        post.assert_not_called()

    def test_pdf_failure_never_calls_text_sender_or_records_checkpoint(self):
        scheduler = MagicMock()
        dispatcher = MagicMock()
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            create_notification_dispatcher=MagicMock(return_value=dispatcher),
            create_scheduler=MagicMock(return_value=scheduler),
        )
        analyzer._weekly_pdf_path = None

        self.assertFalse(
            analyzer._deliver_weekly_pdf(self.schedule, "a" * 64)
        )
        dispatcher.dispatch_weekly_pdf.assert_not_called()
        dispatcher.dispatch_all.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_successful_pdf_delivery_records_window_end_checkpoint(self):
        scheduler = MagicMock()
        scheduler.already_executed.return_value = False
        scheduler.record_execution.return_value = True
        dispatcher = MagicMock()
        dispatcher.dispatch_weekly_pdf.return_value = True
        run_at = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 12, 15, 0)
        )
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            create_notification_dispatcher=MagicMock(return_value=dispatcher),
            create_scheduler=MagicMock(return_value=scheduler),
            timezone="Asia/Shanghai",
            config={},
        )
        analyzer.proxy_url = ""
        analyzer._run_at = run_at
        analyzer._rss_window = previous_natural_week(run_at, "Asia/Shanghai")
        analyzer._weekly_pdf_path = str(self._pdf())
        expected_contract = analyzer._weekly_artifact_contract_hash()

        self.assertTrue(
            analyzer._deliver_weekly_pdf(
                self.schedule, expected_contract
            )
        )
        dispatcher.dispatch_weekly_pdf.assert_called_once()
        dispatch_call = dispatcher.dispatch_weekly_pdf.call_args
        self.assertEqual(dispatch_call.args, (str(self._pdf()), ""))
        self.assertTrue(callable(dispatch_call.kwargs["is_delivered"]))
        self.assertTrue(callable(dispatch_call.kwargs["record_delivery"]))
        scheduler.record_execution.assert_called_once_with(
            "monday_weekly", "push", "2026-08-10"
        )

    def test_partial_account_retry_sends_only_the_failed_account(self):
        first_webhook = f"{WEBHOOK}-account-a"
        second_webhook = f"{WEBHOOK}-account-b"
        dispatcher = self._dispatcher(f"{first_webhook};{second_webhook}")
        scheduler = MagicMock()
        recorded_actions = set()

        def already_executed(_period_key, action, _date_str):
            return action in recorded_actions

        def record_execution(_period_key, action, _date_str):
            recorded_actions.add(action)
            return True

        scheduler.already_executed.side_effect = already_executed
        scheduler.record_execution.side_effect = record_execution
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            create_scheduler=MagicMock(return_value=scheduler),
            create_notification_dispatcher=MagicMock(return_value=dispatcher),
            timezone="Asia/Shanghai",
            config={},
        )
        analyzer.proxy_url = ""
        analyzer._run_at = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 12, 15, 0)
        )
        analyzer._rss_window = previous_natural_week(
            analyzer._run_at, "Asia/Shanghai"
        )
        analyzer._weekly_pdf_path = str(self._pdf())

        artifact_contract = analyzer._weekly_artifact_contract_hash()

        with patch(
            "trendradar.notification.dispatcher.send_wework_pdf_file",
            side_effect=[True, False, True],
        ) as send:
            self.assertFalse(analyzer._deliver_weekly_pdf(
                self.schedule, artifact_contract
            ))
            self.assertTrue(analyzer._deliver_weekly_pdf(
                self.schedule, artifact_contract
            ))

        self.assertEqual(
            [call.args[0] for call in send.call_args_list],
            [first_webhook, second_webhook, second_webhook],
        )
        account_actions = recorded_actions - {"push"}
        self.assertEqual(len(account_actions), 2)
        pdf_digest = hashlib.sha256(
            Path(analyzer._weekly_pdf_path).read_bytes()
        ).hexdigest()
        self.assertTrue(all(pdf_digest in action for action in account_actions))
        self.assertTrue(all(
            artifact_contract in action for action in account_actions
        ))
        self.assertTrue(all("webhook" not in action for action in account_actions))
        self.assertTrue(all("key=" not in action for action in account_actions))

    def test_global_checkpoint_failure_retries_only_checkpoint_aggregation(self):
        dispatcher = self._dispatcher(f"{WEBHOOK}-a;{WEBHOOK}-b")
        scheduler = MagicMock()
        recorded_actions = set()
        global_attempts = 0

        def already_executed(_period_key, action, _date_str):
            return action in recorded_actions

        def record_execution(_period_key, action, _date_str):
            nonlocal global_attempts
            if action == "push":
                global_attempts += 1
                if global_attempts == 1:
                    return False
            recorded_actions.add(action)
            return True

        scheduler.already_executed.side_effect = already_executed
        scheduler.record_execution.side_effect = record_execution
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            create_scheduler=MagicMock(return_value=scheduler),
            create_notification_dispatcher=MagicMock(return_value=dispatcher),
            timezone="Asia/Shanghai",
            config={},
        )
        analyzer.proxy_url = ""
        analyzer._run_at = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 12, 15, 0)
        )
        analyzer._rss_window = previous_natural_week(
            analyzer._run_at, "Asia/Shanghai"
        )
        analyzer._weekly_pdf_path = str(self._pdf())
        expected_contract = analyzer._weekly_artifact_contract_hash()

        with patch(
            "trendradar.notification.dispatcher.send_wework_pdf_file",
            return_value=True,
        ) as send:
            self.assertFalse(analyzer._deliver_weekly_pdf(
                self.schedule, expected_contract
            ))
            self.assertTrue(analyzer._deliver_weekly_pdf(
                self.schedule, expected_contract
            ))

        self.assertEqual(send.call_count, 2)
        self.assertEqual(global_attempts, 2)
        self.assertIn("push", recorded_actions)

    def test_account_ledger_read_error_fails_before_any_external_send(self):
        dispatcher = self._dispatcher(f"{WEBHOOK}-a;{WEBHOOK}-b")
        with patch(
            "trendradar.notification.dispatcher.send_wework_pdf_file"
        ) as send:
            result = dispatcher.dispatch_weekly_pdf(
                str(self._pdf()),
                is_delivered=MagicMock(side_effect=RuntimeError("ledger bad")),
                record_delivery=MagicMock(),
            )

        self.assertFalse(result)
        send.assert_not_called()

    def test_run_resumes_existing_partial_pdf_before_weather_or_crawl(self):
        first_webhook = f"{WEBHOOK}-account-a"
        second_webhook = f"{WEBHOOK}-account-b"
        dispatcher = self._dispatcher(f"{first_webhook};{second_webhook}")
        account_hashes = dispatcher.weekly_pdf_account_hashes()
        pdf_path = self._pdf(
            "农业育种新闻周报_三模块_2026-08-03至2026-08-09.pdf"
        )
        original_pdf = pdf_path.read_bytes()
        pdf_digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        recorded_actions = set()
        scheduler = MagicMock()
        scheduler.already_executed.side_effect = (
            lambda _period, action, _date: action in recorded_actions
        )

        def record_execution(_period, action, _date):
            recorded_actions.add(action)
            return True

        scheduler.record_execution.side_effect = record_execution
        run_at = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 12, 15, 0)
        )
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            cleanup=MagicMock(),
            config={
                "DEBUG": False,
                "AI_ANALYSIS": {"ENABLED": True},
            },
            timezone="Asia/Shanghai",
            get_time=MagicMock(return_value=run_at),
            create_scheduler=MagicMock(return_value=scheduler),
            create_notification_dispatcher=MagicMock(return_value=dispatcher),
        )
        artifact_contract = analyzer._weekly_artifact_contract_hash()
        recorded_actions.add(NewsAnalyzer._weekly_account_delivery_action(
            artifact_contract, pdf_digest, account_hashes[0]
        ))
        analyzer.proxy_url = ""
        analyzer._resolve_and_apply_schedule = MagicMock(
            return_value=self.schedule
        )
        lock = MagicMock()
        lock.acquire.return_value = True
        analyzer._create_weekly_attempt_lock = MagicMock(return_value=lock)
        analyzer._fetch_agro_weather = MagicMock()
        analyzer._initialize_and_check_config = MagicMock()
        analyzer._crawl_data = MagicMock()
        analyzer._crawl_rss_data = MagicMock()

        with patch(
            "trendradar.__main__.weekly_pdf_output_path",
            return_value=pdf_path,
        ) as output_path, patch(
            "trendradar.notification.dispatcher.send_wework_pdf_file",
            return_value=True,
        ) as send, patch(
            "trendradar.__main__.build_weekly_pdf"
        ) as build, patch(
            "trendradar.__main__.render_weekly_pdf_html"
        ) as render:
            self.assertTrue(analyzer.run())

        output_path.assert_called_once()
        self.assertIn("三模块", pdf_path.name)
        self.assertEqual(pdf_path.read_bytes(), original_pdf)
        build.assert_not_called()
        render.assert_not_called()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], second_webhook)
        analyzer._fetch_agro_weather.assert_not_called()
        analyzer._initialize_and_check_config.assert_not_called()
        analyzer._crawl_data.assert_not_called()
        analyzer._crawl_rss_data.assert_not_called()
        self.assertIn("push", recorded_actions)

    def test_resume_refuses_partial_pdf_when_schedule_disables_analysis(self):
        pdf_path = self._pdf(
            "农业育种新闻周报_三模块_2026-08-03至2026-08-09.pdf"
        )
        dispatcher = MagicMock()
        dispatcher.weekly_pdf_account_hashes.return_value = [
            "account-a", "account-b"
        ]
        scheduler = MagicMock()
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        run_at = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 12, 15, 0)
        )
        analyzer.ctx = SimpleNamespace(
            config={"AI_ANALYSIS": {"ENABLED": True}},
            timezone="Asia/Shanghai",
            get_time=MagicMock(return_value=run_at),
            create_scheduler=MagicMock(return_value=scheduler),
            create_notification_dispatcher=MagicMock(return_value=dispatcher),
        )
        analyzer._run_at = run_at
        analyzer.interests_file = None
        analyzer.proxy_url = ""
        contract = analyzer._weekly_artifact_contract_hash()
        digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        delivered_action = NewsAnalyzer._weekly_account_delivery_action(
            contract, digest, "account-a"
        )
        scheduler.already_executed.side_effect = (
            lambda _period, action, _date: action == delivered_action
        )

        with patch(
            "trendradar.__main__.weekly_pdf_output_path",
            return_value=pdf_path,
        ):
            result = analyzer._resume_weekly_pdf_delivery(
                schedule(analyze=False), contract
            )

        self.assertIsNone(result)
        dispatcher.dispatch_weekly_pdf.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_resume_refuses_partial_pdf_when_ai_analysis_is_disabled(self):
        pdf_path = self._pdf(
            "农业育种新闻周报_三模块_2026-08-03至2026-08-09.pdf"
        )
        create_dispatcher = MagicMock()
        run_at = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 12, 15, 0)
        )
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            config={"AI_ANALYSIS": {"ENABLED": False}},
            timezone="Asia/Shanghai",
            get_time=MagicMock(return_value=run_at),
            create_notification_dispatcher=create_dispatcher,
            create_scheduler=MagicMock(),
        )
        analyzer._run_at = run_at

        with patch(
            "trendradar.__main__.weekly_pdf_output_path",
            return_value=pdf_path,
        ):
            result = analyzer._resume_weekly_pdf_delivery(
                schedule(), "a" * 64
            )

        self.assertIsNone(result)
        create_dispatcher.assert_not_called()

    def test_resume_contract_change_before_delivery_writes_no_new_ledger(self):
        dispatcher = self._dispatcher(f"{WEBHOOK}-a;{WEBHOOK}-b")
        account_hashes = dispatcher.weekly_pdf_account_hashes()
        pdf_path = self._pdf(
            "农业育种新闻周报_三模块_2026-08-03至2026-08-09.pdf"
        )
        digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        expected_contract = "a" * 64
        changed_contract = "b" * 64
        original_action = NewsAnalyzer._weekly_account_delivery_action(
            expected_contract, digest, account_hashes[0]
        )
        recorded_actions = {original_action}
        original_actions = set(recorded_actions)
        scheduler = MagicMock()
        scheduler.already_executed.side_effect = (
            lambda _period, action, _date: action in recorded_actions
        )
        scheduler.record_execution.side_effect = (
            lambda _period, action, _date:
            recorded_actions.add(action) or True
        )
        run_at = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 12, 15, 0)
        )
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            cleanup=MagicMock(),
            config={
                "DEBUG": False,
                "AI_ANALYSIS": {"ENABLED": True},
            },
            timezone="Asia/Shanghai",
            get_time=MagicMock(return_value=run_at),
            create_scheduler=MagicMock(return_value=scheduler),
            create_notification_dispatcher=MagicMock(
                return_value=dispatcher
            ),
        )
        analyzer.proxy_url = ""
        analyzer._weekly_artifact_contract_hash = MagicMock(
            side_effect=[expected_contract, changed_contract]
        )
        analyzer._resolve_and_apply_schedule = MagicMock(
            return_value=self.schedule
        )
        lock = MagicMock()
        lock.acquire.return_value = True
        analyzer._create_weekly_attempt_lock = MagicMock(return_value=lock)
        analyzer._fetch_agro_weather = MagicMock()
        analyzer._initialize_and_check_config = MagicMock()
        analyzer._crawl_data = MagicMock()
        analyzer._crawl_rss_data = MagicMock()

        with patch(
            "trendradar.__main__.weekly_pdf_output_path",
            return_value=pdf_path,
        ), patch(
            "trendradar.notification.dispatcher.send_wework_pdf_file",
            return_value=True,
        ) as send:
            self.assertFalse(analyzer.run())

        send.assert_not_called()
        self.assertEqual(recorded_actions, original_actions)
        self.assertEqual(
            analyzer._weekly_artifact_contract_hash.call_count, 2
        )
        analyzer._fetch_agro_weather.assert_not_called()
        analyzer._crawl_data.assert_not_called()

    def test_same_week_checkpoint_prevents_a_second_dispatch(self):
        scheduler = MagicMock()
        scheduler.already_executed.return_value = True
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            cleanup=MagicMock(),
            config={"DEBUG": False},
            timezone="Asia/Shanghai",
            get_time=MagicMock(return_value=pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 12, 15, 0)
            )),
            create_scheduler=MagicMock(return_value=scheduler),
            create_notification_dispatcher=MagicMock(),
        )
        analyzer._resolve_and_apply_schedule = MagicMock(return_value=self.schedule)
        lock = MagicMock()
        lock.acquire.return_value = True
        analyzer._create_weekly_attempt_lock = MagicMock(return_value=lock)
        analyzer._fetch_agro_weather = MagicMock()

        self.assertTrue(analyzer.run())
        analyzer._fetch_agro_weather.assert_not_called()
        analyzer.ctx.create_notification_dispatcher.assert_not_called()

    def test_weather_only_weekly_with_no_wework_webhook_fails_the_run(self):
        dispatcher = self._dispatcher("")
        analyzer, scheduler = self._weekly_execution_analyzer(
            dispatcher=dispatcher
        )

        self.assertFalse(self._execute_weather_only_weekly(analyzer))
        scheduler.record_execution.assert_not_called()

    def test_weather_only_weekly_file_delivery_failure_fails_the_run(self):
        dispatcher = MagicMock()
        dispatcher.dispatch_weekly_pdf.return_value = False
        analyzer, scheduler = self._weekly_execution_analyzer(
            dispatcher=dispatcher
        )

        self.assertFalse(self._execute_weather_only_weekly(analyzer))
        scheduler.record_execution.assert_not_called()

    def test_disabled_notifications_fail_a_scheduled_weekly_delivery(self):
        dispatcher = MagicMock()
        analyzer, scheduler = self._weekly_execution_analyzer(
            notification_enabled=False,
            dispatcher=dispatcher,
        )

        self.assertFalse(self._execute_weather_only_weekly(analyzer))
        dispatcher.dispatch_weekly_pdf.assert_not_called()
        scheduler.record_execution.assert_not_called()

    def test_weather_only_weekly_file_delivery_records_checkpoint_on_success(self):
        dispatcher = MagicMock()
        dispatcher.dispatch_weekly_pdf.return_value = True
        analyzer, scheduler = self._weekly_execution_analyzer(
            dispatcher=dispatcher
        )

        self.assertTrue(self._execute_weather_only_weekly(analyzer))
        scheduler.record_execution.assert_called_once_with(
            "monday_weekly", "push", "2026-08-10"
        )


if __name__ == "__main__":
    unittest.main()
