# coding=utf-8
"""
TrendRadar 主程序

热点新闻聚合与分析工具
支持: python -m trendradar
"""

import argparse
import hashlib
import json
import os
import webbrowser
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from trendradar.context import AppContext
from trendradar import __version__
from trendradar.core import load_config
from trendradar.core.analyzer import convert_keyword_stats_to_platform_stats
from trendradar.crawler import AgroWeatherClient, DataFetcher
from trendradar.crawler.news_search import (
    AgriculturalNewsSearch,
    GDELTClient,
    GoogleNewsRSSClient,
    NEWS_SEARCH_PROVIDERS,
    NewsSearchBounds,
    canonicalize_url,
)
from trendradar.storage import convert_crawl_results_to_news_data
from trendradar.storage.base import RSSItem
from trendradar.utils.article_links import build_reader_url
from trendradar.utils.time import DEFAULT_TIMEZONE
from trendradar.ai import AIAnalyzer, AIAnalysisResult, AIFilter
from trendradar.ai.analyzer import has_required_narrative
from trendradar.ai.module_contract import (
    CLASSIFICATION_MODULE_TYPES,
    MODULE_CONTRACT_VERSION,
    MODULE_STORAGE_SCHEMA_VERSION,
    PERSISTED_MODULE_TYPES,
)
from trendradar.ai.prompt_loader import load_prompt_template
from trendradar.core.daily_delivery import DailyDeliveryAggregator
from trendradar.core.scheduler import ResolvedSchedule, WeeklyAttemptLock
from trendradar.core.weekly import (
    WeeklyNewsSelection,
    WeeklyRSSAggregator,
    current_natural_week,
    previous_week_candidates,
    previous_natural_week,
    primary_weekly_topic,
    select_weekly_modules,
)
from trendradar.report.weekly_pdf import (
    build_weekly_pdf,
    render_weekly_pdf_html,
    weekly_pdf_output_path,
)
from trendradar.commands import check_all_versions, run_doctor, run_test_notification, handle_status_commands
from trendradar.commands.version import _fetch_remote_version, _parse_version



SEARCH_FEED_ID = "agri-breeding-search"
SEARCH_FEED_NAME = "农业育种热点搜索"
WEEKLY_ARTIFACT_CONTRACT_VERSION = 1


def _mark_news_search_failed(rss_data) -> None:
    """Persist one opaque synthetic-source failure without changing item IDs."""
    rss_data.id_to_name.setdefault(SEARCH_FEED_ID, SEARCH_FEED_NAME)
    if SEARCH_FEED_ID not in rss_data.failed_ids:
        rss_data.failed_ids.append(SEARCH_FEED_ID)


def merge_news_search_into_rss(rss_data, search_result) -> None:
    """Merge discovered breeding hotspots into an isolated synthetic RSS feed."""
    if (
        SEARCH_FEED_ID in rss_data.items
        or SEARCH_FEED_ID in rss_data.id_to_name
    ):
        print(
            f"[新闻搜索] 合成源 ID 冲突，保留现有固定 RSS: {SEARCH_FEED_ID}"
        )
        _mark_news_search_failed(rss_data)
        return

    mapped_items = []
    for item in search_result.items:
        canonical_url = canonicalize_url(item.url)
        providers = {
            str(provider)
            for provider in item.providers
            if str(provider) in NEWS_SEARCH_PROVIDERS
        }
        if not canonical_url or not item.topic or not providers:
            print(f"[新闻搜索] 丢弃元数据或 URL 无效的搜索结果: {item.title}")
            continue
        mapped_items.append(RSSItem(
            title=item.title,
            feed_id=SEARCH_FEED_ID,
            feed_name=SEARCH_FEED_NAME,
            url=item.url,
            guid=canonical_url,
            published_at=item.published_at,
            summary=item.summary,
            author=item.publisher,
            source_count=item.source_count,
            pre_hot_score=item.pre_hot_score,
            search_topic=item.topic,
            search_providers=",".join(sorted(providers)),
            crawl_time=rss_data.crawl_time,
            first_time=rss_data.crawl_time,
            last_time=rss_data.crawl_time,
        ))
    rss_data.id_to_name[SEARCH_FEED_ID] = SEARCH_FEED_NAME
    rss_data.items[SEARCH_FEED_ID] = mapped_items


# === 主分析器 ===
class NewsAnalyzer:
    """新闻分析器"""

    # 模式策略定义
    MODE_STRATEGIES = {
        "incremental": {
            "mode_name": "增量模式",
            "description": "增量模式（只关注新增新闻，无新增时不推送）",
            "report_type": "增量分析",
            "should_send_notification": True,
        },
        "current": {
            "mode_name": "当前榜单模式",
            "description": "当前榜单模式（当前榜单匹配新闻 + 新增新闻区域 + 按时推送）",
            "report_type": "当前榜单",
            "should_send_notification": True,
        },
        "daily": {
            "mode_name": "全天汇总模式",
            "description": "全天汇总模式（所有匹配新闻 + 新增新闻区域 + 按时推送）",
            "report_type": "全天汇总",
            "should_send_notification": True,
        },
        "daily_delivery": {
            "mode_name": "每日新增模式",
            "description": "每日新增模式（上次成功后首次发现的内容）",
            "report_type": "每日新增",
            "should_send_notification": True,
        },
        "weekly": {
            "mode_name": "自然周周报模式",
            "description": "自然周周报模式（上一周一至周日）",
            "report_type": "上周周报",
            "should_send_notification": True,
        },
    }

    def __init__(
        self, config: Optional[Dict] = None, *, force_weekly: bool = False
    ):
        # 使用传入的配置或加载新配置
        if config is None:
            print("正在加载配置...")
            config = load_config()
        print(f"TrendRadar v{__version__} 配置加载完成")
        print(f"监控平台数量: {len(config['PLATFORMS'])}")
        print(f"时区: {config.get('TIMEZONE', DEFAULT_TIMEZONE)}")

        # 创建应用上下文
        self.ctx = AppContext(config)

        self.request_interval = self.ctx.config["REQUEST_INTERVAL"]
        self.report_mode = self.ctx.config["REPORT_MODE"]
        self.frequency_file = None
        self.filter_method = None  # None=使用全局配置 ctx.filter_method
        self.interests_file = None  # None=使用全局配置 ai_filter.interests_file
        self.force_weekly = force_weekly
        self.rank_threshold = self.ctx.rank_threshold
        self.is_github_actions = os.environ.get("GITHUB_ACTIONS") == "true"
        self.is_docker_container = self._detect_docker_environment()
        self.update_info = None
        self.proxy_url = None
        self._setup_proxy()
        self.data_fetcher = DataFetcher(
            self.proxy_url,
            api_url=self.ctx.config.get("PLATFORMS_API_URL") or None,
        )

        # RSS/平台元数据（用于报告头部展示）
        self._rss_source_total = 0
        self._rss_source_failed = 0
        self._rss_total_count = 0
        self._rss_matched_count = 0
        self._hotlist_total_count = 0
        self._rss_window = None
        self._allowed_rss_ids = None
        self._rss_ids_authoritative = False
        self._daily_delivery_rss_data = None
        self._report_period_label = ""
        self._agro_weather_report = None
        self._weekly_pdf_path = None
        self._weekly_artifact_contract_expected = None
        self._weekly_ai_filter_succeeded = False
        self._weekly_news_modules = WeeklyNewsSelection()
        self._weekly_source_status = {"missing_dates": [], "failed_sources": {}}
        # A single run owns one immutable clock snapshot.  Strict storage keys,
        # delivery windows and scheduler decisions must never straddle midnight.
        self._run_at = None
        self._run_date = None
        self._run_time_filename = None

        # 初始化存储管理器（使用 AppContext）
        self._init_storage_manager()
        # 注意：update_info 由 main() 函数设置，避免重复请求远程版本

    def _init_storage_manager(self) -> None:
        """初始化存储管理器（使用 AppContext）"""
        # 获取数据保留天数（支持环境变量覆盖）
        env_retention = os.environ.get("STORAGE_RETENTION_DAYS", "").strip()
        if env_retention:
            # 环境变量覆盖配置
            self.ctx.config["STORAGE"]["RETENTION_DAYS"] = int(env_retention)

        self.storage_manager = self.ctx.get_storage_manager()
        print(f"存储后端: {self.storage_manager.backend_name}")

        retention_days = self.ctx.config.get("STORAGE", {}).get("RETENTION_DAYS", 0)
        if retention_days > 0:
            print(f"数据保留天数: {retention_days} 天")

    def _detect_docker_environment(self) -> bool:
        """检测是否运行在 Docker 容器中"""
        try:
            if os.environ.get("DOCKER_CONTAINER") == "true":
                return True

            if os.path.exists("/.dockerenv"):
                return True

            return False
        except Exception:
            return False

    def _should_open_browser(self) -> bool:
        """判断是否应该打开浏览器"""
        return not self.is_github_actions and not self.is_docker_container

    def _setup_proxy(self) -> None:
        """设置代理配置"""
        if not self.is_github_actions and self.ctx.config["USE_PROXY"]:
            self.proxy_url = self.ctx.config["DEFAULT_PROXY"]
            print("本地环境，使用代理")
        elif not self.is_github_actions and not self.ctx.config["USE_PROXY"]:
            print("本地环境，未启用代理")
        else:
            print("GitHub Actions环境，不使用代理")

    def _set_update_info_from_config(self) -> None:
        """从已缓存的远程版本设置更新信息（不再重复请求）"""
        try:
            version_url = self.ctx.config.get("VERSION_CHECK_URL", "")
            if not version_url:
                return

            remote_version = _fetch_remote_version(version_url, self.proxy_url)
            if remote_version:
                need_update = _parse_version(__version__) < _parse_version(remote_version)
                if need_update:
                    self.update_info = {
                        "current_version": __version__,
                        "remote_version": remote_version,
                    }
        except Exception as e:
            print(f"版本检查出错: {e}")

    def _get_mode_strategy(self) -> Dict:
        """获取当前模式的策略配置"""
        return self.MODE_STRATEGIES.get(self.report_mode, self.MODE_STRATEGIES["daily"])

    def _operation_run_at(self):
        """Return the immutable clock snapshot for this run.

        The fallback keeps directly-invoked helpers backwards compatible in
        tests and integrations that do not enter through ``run()``.
        """
        return getattr(self, "_run_at", None) or self.ctx.get_time()

    def _operation_date(self) -> str:
        frozen = getattr(self, "_run_date", None)
        return frozen or self.ctx.format_date()

    def _operation_time_filename(self) -> str:
        frozen = getattr(self, "_run_time_filename", None)
        return frozen or self.ctx.format_time()

    def _news_search_bounds(self, report_mode: str) -> NewsSearchBounds:
        """Derive all provider recall from the run's one natural-week window."""
        run_at = self._operation_run_at()
        if report_mode == "weekly":
            window = previous_natural_week(run_at, self.ctx.timezone)
        else:
            window = current_natural_week(run_at, self.ctx.timezone)
        return NewsSearchBounds(window.start, window.end)

    def _resolve_and_apply_schedule(self) -> ResolvedSchedule:
        """在采集前解析一次调度，并应用本次运行的覆盖配置。"""
        force_period_key = (
            "monday_weekly" if getattr(self, "force_weekly", False) else None
        )
        scheduler = self.ctx.create_scheduler()
        if force_period_key:
            schedule = scheduler.resolve(
                self._operation_run_at(), force_period_key=force_period_key
            )
        else:
            schedule = scheduler.resolve(self._operation_run_at())
        self.report_mode = schedule.report_mode
        self.frequency_file = schedule.frequency_file
        self.filter_method = schedule.filter_method or self.ctx.filter_method
        self.interests_file = schedule.interests_file
        return schedule

    def _is_manual_weekly_rerun(
        self, schedule: Optional[ResolvedSchedule] = None
    ) -> bool:
        """Return whether this run is an explicit full weekly rerun."""
        if not getattr(self, "force_weekly", False):
            return False
        return schedule is None or schedule.report_mode == "weekly"

    def _delivery_checkpoint_date(self, report_mode: str) -> str:
        """返回交付周期稳定的幂等键日期。"""
        if report_mode == "weekly":
            timezone = getattr(self.ctx, "timezone", None) or self.ctx.config.get(
                "TIMEZONE", DEFAULT_TIMEZONE
            )
            run_at = getattr(self, "_run_at", None)
            if not isinstance(run_at, datetime):
                get_time = getattr(self.ctx, "get_time", None)
                run_at = get_time() if callable(get_time) else None
            if not isinstance(run_at, datetime):
                run_date = self._operation_date()
                import pytz

                run_at = pytz.timezone(timezone).localize(
                    datetime.strptime(run_date, "%Y-%m-%d")
                )
            window = previous_natural_week(run_at, timezone)
            return window.end.strftime("%Y-%m-%d")
        return self._operation_date()

    def _fetch_agro_weather(self):
        """每次周报尝试都重新抓取官方农业气象周报。"""
        config = self.ctx.config.get("AGRO_WEATHER", {})
        if not config.get("ENABLED", True):
            return None
        client = AgroWeatherClient(
            source_url=config.get(
                "URL", "https://www.nmc.cn/publish/agro/ten-week/index.html"
            ),
            timeout=config.get("TIMEOUT", 30),
            timezone_name=self.ctx.timezone,
            use_proxy=bool(self.proxy_url),
            proxy_url=self.proxy_url or "",
        )
        run_at = self._operation_run_at()
        delivery_anchor = previous_natural_week(
            run_at, self.ctx.timezone
        ).end
        return client.fetch_latest(
            run_at,
            expected_delivery_anchor=delivery_anchor,
        )

    def _create_weekly_attempt_lock(
        self, checkpoint_date: str
    ) -> WeeklyAttemptLock:
        return WeeklyAttemptLock(
            self.storage_manager.data_dir, checkpoint_date
        )

    @staticmethod
    def _should_fallback_ai_filter(mode: str) -> bool:
        """严格交付必须由 AI 筛选成功，不允许降级到关键词结果。"""
        return not NewsAnalyzer._is_strict_delivery_mode(mode)

    @staticmethod
    def _is_strict_delivery_mode(mode: str) -> bool:
        return mode in {"weekly", "daily_delivery"}

    @staticmethod
    def _notification_delivery_succeeded(
        mode: str, results: Dict[str, bool]
    ) -> bool:
        """严格交付需所有配置渠道成功；既有模式保留任一成功。"""
        return bool(results) and (
            all(results.values())
            if NewsAnalyzer._is_strict_delivery_mode(mode)
            else any(results.values())
        )

    def _record_delivery_checkpoint(
        self, schedule: ResolvedSchedule
    ) -> bool:
        if not schedule.period_key:
            return False
        return self.ctx.create_scheduler().record_execution(
            schedule.period_key,
            "push",
            self._delivery_checkpoint_date(schedule.report_mode),
        )

    @staticmethod
    def _weekly_pdf_digest(pdf_file_path: str) -> str:
        digest = hashlib.sha256()
        with Path(pdf_file_path).open("rb") as pdf_file:
            for chunk in iter(lambda: pdf_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _weekly_account_delivery_action(
        artifact_contract_hash: str,
        pdf_digest: str,
        account_hash: str,
    ) -> str:
        return (
            f"weekly-pdf:{artifact_contract_hash}:"
            f"{pdf_digest}:{account_hash}"
        )

    def _weekly_artifact_contract_hash(self) -> str:
        """Bind partial delivery to the complete current weekly artifact contract."""
        config = self.ctx.config
        filter_config = {
            "PROMPT_FILE": "prompt.txt",
            "EXTRACT_PROMPT_FILE": "extract_prompt.txt",
            "UPDATE_TAGS_PROMPT_FILE": "update_tags_prompt.txt",
            **config.get("AI_FILTER", {}),
        }
        ai_filter = AIFilter(
            config.get("AI", {}),
            filter_config,
            self._operation_run_at,
        )
        interests_file = (
            getattr(self, "interests_file", None)
            or filter_config.get("INTERESTS_FILE")
        )
        interests_content = ai_filter.load_interests_content(interests_file)
        if not interests_content:
            raise RuntimeError("周报 artifact contract 缺少兴趣内容")
        if not ai_filter.classify_system or not ai_filter.classify_user:
            raise RuntimeError("周报 artifact contract 缺少分类 Prompt")

        analysis_config = config.get("AI_ANALYSIS", {})
        analysis_prompt_file = analysis_config.get(
            "PROMPT_FILE", "ai_analysis_prompt.txt"
        )
        analysis_system, analysis_user = load_prompt_template(
            analysis_prompt_file, label="AI"
        )
        if not analysis_system or not analysis_user:
            raise RuntimeError("周报 artifact contract 缺少分析 Prompt")

        payload = {
            "artifact_contract_version": WEEKLY_ARTIFACT_CONTRACT_VERSION,
            "classification_prompt": {
                "file": filter_config.get("PROMPT_FILE", "prompt.txt"),
                "system": ai_filter.classify_system,
                "user": ai_filter.classify_user,
            },
            "interests": {
                "file": interests_file or "ai_interests.txt",
                "content": interests_content,
            },
            "analysis_prompt": {
                "file": analysis_prompt_file,
                "system": analysis_system,
                "user": analysis_user,
            },
            "min_score": filter_config.get("MIN_SCORE", 0.5),
            "module_contract": {
                "version": MODULE_CONTRACT_VERSION,
                "classification_types": sorted(
                    CLASSIFICATION_MODULE_TYPES
                ),
                "persisted_types": sorted(PERSISTED_MODULE_TYPES),
                "storage_schema_version": MODULE_STORAGE_SCHEMA_VERSION,
                "weekly_selection_fields": [
                    "module_type", "module_rank", "highlight_rank",
                    "weekly_topics",
                ],
            },
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _freeze_weekly_artifact_contract(self) -> str:
        """Freeze one immutable artifact contract for the current run."""
        expected = getattr(
            self, "_weekly_artifact_contract_expected", None
        )
        if expected is None:
            expected = self._weekly_artifact_contract_hash()
            if not isinstance(expected, str) or len(expected) != 64:
                raise RuntimeError("周报 artifact contract hash 无效")
            self._weekly_artifact_contract_expected = expected
        return expected

    def _deliver_weekly_pdf(
        self,
        schedule: ResolvedSchedule,
        expected_contract_hash: str,
    ) -> bool:
        """Send the dedicated weekly PDF to WeCom and then persist once-push."""
        if not getattr(self, "_weekly_pdf_path", None):
            print("[周报] 未生成有效 PDF，取消投递")
            return False
        if not schedule.period_key:
            print("[周报] 调度周期缺失，取消投递")
            return False
        if (
            not isinstance(expected_contract_hash, str)
            or len(expected_contract_hash) != 64
        ):
            print("[周报] 缺少本轮冻结的 artifact contract，取消投递")
            return False
        window = getattr(self, "_rss_window", None)
        checkpoint_date = (
            window.end.strftime("%Y-%m-%d")
            if window is not None
            else self._delivery_checkpoint_date("weekly")
        )
        scheduler = self.ctx.create_scheduler()
        if (
            not self._is_manual_weekly_rerun(schedule)
            and schedule.once_push
            and schedule.period_key
            and scheduler.already_executed(
                schedule.period_key, "push", checkpoint_date
            )
        ):
            print("[周报] 本周 PDF 已成功发送，跳过重试")
            return False
        try:
            pdf_digest = self._weekly_pdf_digest(self._weekly_pdf_path)
            current_contract_hash = self._weekly_artifact_contract_hash()
        except Exception as exc:
            print(f"[周报] PDF 交付契约计算失败: {type(exc).__name__}")
            return False
        if current_contract_hash != expected_contract_hash:
            print("[周报] artifact contract 在投递前发生变化，拒绝发送")
            return False
        dispatcher = self.ctx.create_notification_dispatcher(
            operation_at=self._operation_run_at()
        )

        def account_action(account_hash: str) -> str:
            return self._weekly_account_delivery_action(
                expected_contract_hash, pdf_digest, account_hash
            )

        if self._is_manual_weekly_rerun(schedule):
            delivered = dispatcher.dispatch_weekly_pdf(
                self._weekly_pdf_path, self.proxy_url
            )
        else:
            delivered = dispatcher.dispatch_weekly_pdf(
                self._weekly_pdf_path,
                self.proxy_url,
                is_delivered=lambda account_hash: scheduler.already_executed(
                    schedule.period_key,
                    account_action(account_hash),
                    checkpoint_date,
                ),
                record_delivery=lambda account_hash: scheduler.record_execution(
                    schedule.period_key,
                    account_action(account_hash),
                    checkpoint_date,
                ),
            )
        if not delivered:
            return False
        return scheduler.record_execution(
            schedule.period_key, "push", checkpoint_date
        )

    def _resume_weekly_pdf_delivery(
        self,
        schedule: ResolvedSchedule,
        expected_contract_hash: str,
    ) -> Optional[bool]:
        """Resume a durable partial delivery without regenerating its PDF."""
        if self._is_manual_weekly_rerun(schedule):
            return None
        if not schedule.period_key:
            return None
        if (
            not schedule.analyze
            or not self.ctx.config.get("AI_ANALYSIS", {}).get(
                "ENABLED", False
            )
        ):
            return None
        window = previous_natural_week(
            self._operation_run_at(), self.ctx.timezone
        )
        pdf_path = weekly_pdf_output_path(
            "output", window.start.date(), window.end.date()
        )
        if not pdf_path.is_file():
            return None

        dispatcher = self.ctx.create_notification_dispatcher(
            operation_at=self._operation_run_at()
        )
        account_hashes = dispatcher.weekly_pdf_account_hashes()
        if not account_hashes:
            return None
        try:
            pdf_digest = self._weekly_pdf_digest(str(pdf_path))
        except Exception as exc:
            print(
                f"[周报] 旧 PDF 交付契约校验失败，执行完整重建: "
                f"{type(exc).__name__}"
            )
            return None
        checkpoint_date = window.end.strftime("%Y-%m-%d")
        scheduler = self.ctx.create_scheduler()
        delivered = [
            scheduler.already_executed(
                schedule.period_key,
                self._weekly_account_delivery_action(
                    expected_contract_hash, pdf_digest, account_hash
                ),
                checkpoint_date,
            )
            for account_hash in account_hashes
        ]
        if not any(delivered):
            return None

        self._rss_window = window
        self._weekly_pdf_path = str(pdf_path)
        print("[周报] 检测到部分账号成功账本，复用现有 PDF 继续投递")
        return self._deliver_weekly_pdf(
            schedule, expected_contract_hash
        )

    def _has_notification_configured(self) -> bool:
        """检查是否配置了任何通知渠道"""
        cfg = self.ctx.config
        return any(
            [
                cfg["FEISHU_WEBHOOK_URL"],
                cfg["DINGTALK_WEBHOOK_URL"],
                cfg["WEWORK_WEBHOOK_URL"],
                (cfg["TELEGRAM_BOT_TOKEN"] and cfg["TELEGRAM_CHAT_ID"]),
                (
                    cfg["EMAIL_FROM"]
                    and cfg["EMAIL_PASSWORD"]
                    and cfg["EMAIL_TO"]
                ),
                (cfg["NTFY_SERVER_URL"] and cfg["NTFY_TOPIC"]),
                cfg["BARK_URL"],
                cfg["SLACK_WEBHOOK_URL"],
                cfg["GENERIC_WEBHOOK_URL"],
            ]
        )

    def _has_valid_content(
        self, stats: List[Dict], new_titles: Optional[Dict] = None
    ) -> bool:
        """检查是否有有效的新闻内容"""
        if self.report_mode == "incremental":
            # 增量模式：只要有匹配的新闻就推送
            # count_word_frequency 已经确保只处理新增的新闻（包括当天第一次爬取的情况）
            has_matched_news = any(stat["count"] > 0 for stat in stats)
            return has_matched_news
        elif self.report_mode == "current":
            # current模式：只要stats有内容就说明有匹配的新闻
            return any(stat["count"] > 0 for stat in stats)
        else:
            # 当日汇总模式下，检查是否有匹配的频率词新闻或新增新闻
            has_matched_news = any(stat["count"] > 0 for stat in stats)
            has_new_news = bool(
                new_titles and any(len(titles) > 0 for titles in new_titles.values())
            )
            return has_matched_news or has_new_news

    def _prepare_ai_analysis_data(
        self,
        ai_mode: str,
        current_results: Optional[Dict] = None,
        current_id_to_name: Optional[Dict] = None,
    ) -> Tuple[List[Dict], Optional[Dict]]:
        """
        为 AI 分析准备指定模式的数据

        Args:
            ai_mode: AI 分析模式 (daily/current/incremental)
            current_results: 当前抓取的结果（用于 incremental 模式）
            current_id_to_name: 当前的平台映射（用于 incremental 模式）

        Returns:
            Tuple[stats, id_to_name]: 统计数据和平台映射
        """
        try:
            word_groups, filter_words, global_filters = self.ctx.load_frequency_words(self.frequency_file)

            if ai_mode == "incremental":
                # incremental 模式：使用当前抓取的数据
                if not current_results or not current_id_to_name:
                    print("[AI] incremental 模式需要当前抓取数据，但未提供")
                    return [], None

                # 准备当前时间信息
                time_info = self._operation_time_filename()
                title_info = self._prepare_current_title_info(current_results, time_info)

                # 检测新增标题
                new_titles = self.ctx.detect_new_titles(list(current_results.keys()))

                # 统计计算
                stats, _ = self.ctx.count_frequency(
                    current_results,
                    word_groups,
                    filter_words,
                    current_id_to_name,
                    title_info,
                    new_titles,
                    mode="incremental",
                    global_filters=global_filters,
                    quiet=True,
                )

                # 如果是 platform 模式，转换数据结构
                if self.ctx.display_mode == "platform" and stats:
                    stats = convert_keyword_stats_to_platform_stats(
                        stats,
                        self.ctx.weight_config,
                        self.ctx.rank_threshold,
                    )

                return stats, current_id_to_name

            elif ai_mode in ["daily", "current"]:
                # 加载历史数据
                analysis_data = self._load_analysis_data(quiet=True)
                if not analysis_data:
                    print(f"[AI] 无法加载历史数据用于 {ai_mode} 模式分析")
                    return [], None

                (
                    all_results,
                    id_to_name,
                    title_info,
                    new_titles,
                    _,
                    _,
                    _,
                ) = analysis_data

                # 统计计算
                stats, _ = self.ctx.count_frequency(
                    all_results,
                    word_groups,
                    filter_words,
                    id_to_name,
                    title_info,
                    new_titles,
                    mode=ai_mode,
                    global_filters=global_filters,
                    quiet=True,
                )

                # 如果是 platform 模式，转换数据结构
                if self.ctx.display_mode == "platform" and stats:
                    stats = convert_keyword_stats_to_platform_stats(
                        stats,
                        self.ctx.weight_config,
                        self.ctx.rank_threshold,
                    )

                return stats, id_to_name
            else:
                print(f"[AI] 未知的 AI 模式: {ai_mode}")
                return [], None

        except Exception as e:
            print(f"[AI] 准备 {ai_mode} 模式数据时出错: {e}")
            if self.ctx.config.get("DEBUG", False):
                import traceback
                traceback.print_exc()
            return [], None

    def _run_ai_analysis(
        self,
        stats: List[Dict],
        rss_items: Optional[List[Dict]],
        mode: str,
        report_type: str,
        id_to_name: Optional[Dict],
        current_results: Optional[Dict] = None,
        schedule: ResolvedSchedule = None,
        standalone_data: Optional[Dict] = None,
    ) -> Optional[AIAnalysisResult]:
        """执行 AI 分析"""
        analysis_config = self.ctx.config.get("AI_ANALYSIS", {})
        if not analysis_config.get("ENABLED", False):
            return None

        # 调度系统决策
        if not schedule.analyze:
            print("[AI] 调度器: 当前时间段不执行 AI 分析")
            return None

        manual_weekly_rerun = self._is_manual_weekly_rerun(schedule)
        if manual_weekly_rerun:
            print("[AI] 手动强制周报：忽略已有分析检查点，重新分析")
        elif schedule.once_analyze and schedule.period_key:
            scheduler = self.ctx.create_scheduler()
            date_str = self._delivery_checkpoint_date(mode)
            if scheduler.already_executed(schedule.period_key, "analyze", date_str):
                if mode == "weekly":
                    strict_push_pending = not scheduler.already_executed(
                        schedule.period_key, "push", date_str
                    )
                else:
                    strict_push_pending = (
                        mode == "daily_delivery"
                        and schedule.once_push
                        and not scheduler.already_executed(
                            schedule.period_key, "push", date_str
                        )
                    )
                if not strict_push_pending:
                    print(f"[AI] 调度器: 时间段 {schedule.period_name or schedule.period_key} 今天已分析过，跳过")
                    return None
                print(
                    f"[AI] 调度器: 时间段 {schedule.period_name or schedule.period_key} "
                    "已分析但严格交付推送未完成，重新分析用于补跑"
                )
            else:
                print(f"[AI] 调度器: 时间段 {schedule.period_name or schedule.period_key} 今天首次分析")

        print("[AI] 正在进行 AI 分析...")
        try:
            ai_config = self.ctx.config.get("AI", {})
            debug_mode = self.ctx.config.get("DEBUG", False)
            analyzer = AIAnalyzer(
                ai_config,
                analysis_config,
                lambda: self._operation_run_at(),
                debug=debug_mode,
            )

            # 确定 AI 分析使用的模式
            ai_mode_config = analysis_config.get("MODE", "follow_report")
            if mode in {"daily_delivery", "weekly"}:
                # 严格交付的公开 AI_ANALYSIS.MODE 不能绕过权威 RSS
                # 快照去读取 daily/current/incremental 热榜历史。
                if ai_mode_config not in ("follow_report", mode):
                    label = "周报" if mode == "weekly" else "每日交付"
                    print(
                        f"[AI] {label}忽略独立分析模式 {ai_mode_config}，"
                        "强制使用权威 RSS 快照"
                    )
                ai_mode = mode
                ai_stats = stats
                ai_id_to_name = {}
            elif ai_mode_config == "follow_report":
                # 跟随推送报告模式
                ai_mode = mode
                ai_stats = stats
                ai_id_to_name = id_to_name
            elif ai_mode_config in ["daily", "current", "incremental"]:
                # 使用独立配置的模式，需要重新准备数据
                ai_mode = ai_mode_config
                if ai_mode != mode:
                    print(f"[AI] 使用独立分析模式: {ai_mode} (推送模式: {mode})")
                    print(f"[AI] 正在准备 {ai_mode} 模式的数据...")

                    # 根据 AI 模式重新准备数据
                    ai_stats, ai_id_to_name = self._prepare_ai_analysis_data(
                        ai_mode, current_results, id_to_name
                    )
                    if not ai_stats:
                        print(f"[AI] 警告: 无法准备 {ai_mode} 模式的数据，回退到推送模式数据")
                        ai_stats = stats
                        ai_id_to_name = id_to_name
                        ai_mode = mode
                else:
                    ai_stats = stats
                    ai_id_to_name = id_to_name
            else:
                # 配置错误，回退到跟随模式
                print(f"[AI] 警告: 无效的 ai_analysis.mode 配置 '{ai_mode_config}'，使用推送模式 '{mode}'")
                ai_mode = mode
                ai_stats = stats
                ai_id_to_name = id_to_name

            # 提取平台列表
            platforms = list(ai_id_to_name.values()) if ai_id_to_name else []

            # 提取关键词列表
            keywords = [s.get("word", "") for s in ai_stats if s.get("word")] if ai_stats else []

            # 确定报告类型
            if ai_mode != mode:
                # 根据 AI 模式确定报告类型
                ai_report_type = {
                    "daily": "当日汇总",
                    "current": "当前榜单",
                    "incremental": "增量更新"
                }.get(ai_mode, report_type)
            else:
                ai_report_type = report_type

            # 独立 AI 模式（ai_mode != 推送 mode）下，rss_items/standalone_data 仍是推送 mode 的数据，
            # 与 ai_mode 的热榜 ai_stats 不同源。为避免时间窗错配的数据误导分析，独立模式下不向 AI
            # 传入 RSS/独立展示区，使其专注于 ai_mode 的热榜分析（同 mode 时正常传入）。
            ai_rss_stats = rss_items if ai_mode == mode else None
            ai_standalone = standalone_data if ai_mode == mode else None
            if ai_mode != mode and (rss_items or standalone_data):
                print(f"[AI] 独立分析模式（{ai_mode}）：RSS/独立展示区与推送模式（{mode}）不同源，本次分析仅聚焦热榜")

            result = analyzer.analyze(
                stats=ai_stats,
                rss_stats=ai_rss_stats,
                report_mode=ai_mode,
                report_type=ai_report_type,
                platforms=platforms,
                keywords=keywords,
                standalone_data=ai_standalone,
                strict=mode in {"daily_delivery", "weekly"},
                weather_report=(
                    getattr(self, "_agro_weather_report", None)
                    if mode == "weekly"
                    else None
                ),
            )

            # 设置 AI 分析使用的模式
            if result.success:
                result.ai_mode = ai_mode
                if result.error:
                    # 成功但有警告（如 JSON 解析问题但使用了原始文本）
                    print(f"[AI] 分析完成（有警告: {result.error}）")
                else:
                    print("[AI] 分析完成")

                # 记录 AI 分析
                if (
                    mode != "weekly"
                    and schedule.once_analyze
                    and schedule.period_key
                ):
                    scheduler = self.ctx.create_scheduler()
                    date_str = self._delivery_checkpoint_date(mode)
                    if not scheduler.record_execution(
                        schedule.period_key, "analyze", date_str
                    ):
                        return AIAnalysisResult(
                            success=False,
                            error="AI 分析完成，但 once_analyze 持久化失败",
                        )
            elif result.skipped:
                print(f"[AI] {result.error}")
            else:
                print(f"[AI] 分析失败: {result.error}")

            return result
        except Exception as e:
            import traceback
            error_type = type(e).__name__
            error_msg = str(e)
            # 截断过长的错误消息
            if len(error_msg) > 200:
                error_msg = error_msg[:200] + "..."
            print(f"[AI] 分析出错 ({error_type}): {error_msg}")
            # 详细错误日志到 stderr
            import sys
            print(f"[AI] 详细错误堆栈:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return AIAnalysisResult(success=False, error=f"{error_type}: {error_msg}")

    def _load_analysis_data(
        self,
        quiet: bool = False,
    ) -> Optional[Tuple[Dict, Dict, Dict, Dict, List, List]]:
        """统一的数据加载和预处理，使用当前监控平台列表过滤历史数据"""
        try:
            # 获取当前配置的监控平台ID列表
            current_platform_ids = self.ctx.platform_ids
            if not quiet:
                print(f"当前监控平台: {current_platform_ids}")

            all_results, id_to_name, title_info = self.ctx.read_today_titles(
                current_platform_ids, quiet=quiet
            )

            if not all_results:
                print("没有找到当天的数据")
                return None

            total_titles = sum(len(titles) for titles in all_results.values())
            if not quiet:
                print(f"读取到 {total_titles} 个标题（已按当前监控平台过滤）")

            new_titles = self.ctx.detect_new_titles(current_platform_ids, quiet=quiet)
            word_groups, filter_words, global_filters = self.ctx.load_frequency_words(self.frequency_file)

            return (
                all_results,
                id_to_name,
                title_info,
                new_titles,
                word_groups,
                filter_words,
                global_filters,
            )
        except Exception as e:
            print(f"数据加载失败: {e}")
            return None

    def _prepare_current_title_info(self, results: Dict, time_info: str) -> Dict:
        """从当前抓取结果构建标题信息"""
        title_info = {}
        for source_id, titles_data in results.items():
            title_info[source_id] = {}
            for title, title_data in titles_data.items():
                ranks = title_data.get("ranks", [])
                url = title_data.get("url", "")
                mobile_url = title_data.get("mobileUrl", "")

                title_info[source_id][title] = {
                    "first_time": time_info,
                    "last_time": time_info,
                    "count": 1,
                    "ranks": ranks,
                    "url": url,
                    "mobileUrl": mobile_url,
                }
        return title_info

    def _prepare_standalone_data(
        self,
        results: Dict,
        id_to_name: Dict,
        title_info: Optional[Dict] = None,
        rss_items: Optional[List[Dict]] = None,
    ) -> Optional[Dict]:
        """
        从原始数据中提取独立展示区数据

        纯数据准备方法，不检查 display.regions.standalone 开关。
        各消费者自行决定是否使用：
        - AI 分析：由 ai.include_standalone 控制（在 _run_ai_analysis 层门控）
        - HTML 报告 / 邮件：由 display.regions.standalone 控制（在 HTML 生成前过滤）
        - Webhook 推送：由 display.regions.standalone 控制（在 dispatcher 层门控）

        Args:
            results: 原始爬取结果 {platform_id: {title: title_data}}
            id_to_name: 平台 ID 到名称的映射
            title_info: 标题元信息（含排名历史、时间等）
            rss_items: RSS 条目列表

        Returns:
            独立展示数据字典，如果未配置数据源返回 None
        """
        display_config = self.ctx.config.get("DISPLAY", {})
        standalone_config = display_config.get("STANDALONE", {})

        platform_ids = standalone_config.get("PLATFORMS", [])
        rss_feed_ids = standalone_config.get("RSS_FEEDS", [])
        max_items = standalone_config.get("MAX_ITEMS", 20)

        if not platform_ids and not rss_feed_ids:
            return None

        standalone_data = {
            "platforms": [],
            "rss_feeds": [],
        }

        # 找出最新批次时间（类似 current 模式的过滤逻辑）
        latest_time = None
        if title_info:
            for source_titles in title_info.values():
                for title_data in source_titles.values():
                    last_time = title_data.get("last_time", "")
                    if last_time:
                        if latest_time is None or last_time > latest_time:
                            latest_time = last_time

        # 提取热榜平台数据
        for platform_id in platform_ids:
            if platform_id not in results:
                continue

            platform_name = id_to_name.get(platform_id, platform_id)
            platform_titles = results[platform_id]

            items = []
            for title, title_data in platform_titles.items():
                # 获取元信息（如果有 title_info）
                meta = {}
                if title_info and platform_id in title_info and title in title_info[platform_id]:
                    meta = title_info[platform_id][title]

                # 只保留当前在榜的话题（last_time 等于最新时间）
                if latest_time and meta:
                    if meta.get("last_time") != latest_time:
                        continue

                # 使用当前热榜的排名数据（title_data）进行排序
                # title_data 包含的是爬虫返回的当前排名，用于保证独立展示区的顺序与热榜一致
                current_ranks = title_data.get("ranks", [])
                current_rank = current_ranks[-1] if current_ranks else 0

                # 用于显示的排名范围：合并历史排名和当前排名
                historical_ranks = meta.get("ranks", []) if meta else []
                # 合并去重，保持顺序
                all_ranks = historical_ranks.copy()
                for rank in current_ranks:
                    if rank not in all_ranks:
                        all_ranks.append(rank)
                display_ranks = all_ranks if all_ranks else current_ranks

                item = {
                    "title": title,
                    "url": title_data.get("url", ""),
                    "mobileUrl": title_data.get("mobileUrl", ""),
                    "rank": current_rank,  # 用于排序的当前排名
                    "ranks": display_ranks,  # 用于显示的排名范围（历史+当前）
                    "first_time": meta.get("first_time", ""),
                    "last_time": meta.get("last_time", ""),
                    "count": meta.get("count", 1),
                    "rank_timeline": meta.get("rank_timeline", []),
                }
                items.append(item)

            # 按当前排名排序
            items.sort(key=lambda x: x["rank"] if x["rank"] > 0 else 9999)

            # 限制条数
            if max_items > 0:
                items = items[:max_items]

            if items:
                standalone_data["platforms"].append({
                    "id": platform_id,
                    "name": platform_name,
                    "items": items,
                })

        # 提取 RSS 数据
        if rss_items and rss_feed_ids:
            # 按 feed_id 分组
            feed_items_map = {}
            for item in rss_items:
                feed_id = item.get("feed_id", "")
                if feed_id in rss_feed_ids:
                    if feed_id not in feed_items_map:
                        feed_items_map[feed_id] = {
                            "name": item.get("feed_name", feed_id),
                            "items": [],
                        }
                    feed_items_map[feed_id]["items"].append({
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "reader_url": item.get("reader_url", ""),
                        "published_at": item.get("published_at", ""),
                        "author": item.get("author", ""),
                    })

            # 限制条数并添加到结果
            for feed_id in rss_feed_ids:
                if feed_id in feed_items_map:
                    feed_data = feed_items_map[feed_id]
                    items = feed_data["items"]
                    if max_items > 0:
                        items = items[:max_items]
                    if items:
                        standalone_data["rss_feeds"].append({
                            "id": feed_id,
                            "name": feed_data["name"],
                            "items": items,
                        })

        # 如果没有任何数据，返回 None
        if not standalone_data["platforms"] and not standalone_data["rss_feeds"]:
            return None

        return standalone_data

    def _run_analysis_pipeline(
        self,
        data_source: Dict,
        mode: str,
        title_info: Dict,
        new_titles: Dict,
        word_groups: List[Dict],
        filter_words: List[str],
        id_to_name: Dict,
        failed_ids: Optional[List] = None,
        global_filters: Optional[List[str]] = None,
        quiet: bool = False,
        rss_items: Optional[List[Dict]] = None,
        rss_new_items: Optional[List[Dict]] = None,
        standalone_data: Optional[Dict] = None,
        schedule: ResolvedSchedule = None,
        rss_new_urls: Optional[set] = None,
    ) -> Tuple[List[Dict], Optional[str], Optional[AIAnalysisResult], Optional[List[Dict]], Optional[Dict], Optional[List[Dict]]]:
        """统一的分析流水线：数据处理 → 统计计算（关键词/AI筛选）→ AI分析 → HTML生成"""

        if mode == "weekly":
            expected_contract_hash = (
                self._freeze_weekly_artifact_contract()
            )
            self._weekly_ai_filter_succeeded = False
            self._weekly_news_modules = WeeklyNewsSelection()

        strict_operation_date = None
        if mode == "daily_delivery":
            strict_operation_date = self._operation_date()
        elif mode == "weekly" and self._rss_window is not None:
            strict_operation_date = self._rss_window.end.strftime("%Y-%m-%d")

        # 周报普通新闻只允许权威自然周快照经过严格 AI filter；不允许
        # keyword 或其他未筛选路径把普通新闻带入双模块选择/PDF。
        if mode == "weekly" and rss_items and self.filter_method != "ai":
            raise RuntimeError("周报普通新闻仅支持严格 AI 筛选")

        weekly_has_no_news_candidates = (
            mode == "weekly"
            and getattr(self, "_rss_ids_authoritative", False)
            and not getattr(self, "_allowed_rss_ids", None)
        )

        # 权威周快照为空时不启动新闻分类、标签或分类账本写入；气象叙事
        # 和专用 PDF 继续使用显式的空 WeeklyNewsSelection。
        if weekly_has_no_news_candidates:
            print("[周报] 权威周快照没有普通新闻，跳过新闻 AI 筛选")
            stats = []
            total_titles = 0
            rss_items = []
            rss_new_items = []
        # 根据筛选策略选择数据处理方式
        elif self.filter_method == "ai":
            # === AI 筛选策略 ===
            print("[筛选] 使用 AI 智能筛选策略")
            ai_filter_result = self.ctx.run_ai_filter(
                interests_file=self.interests_file,
                rss_window=self._rss_window,
                allowed_rss_ids=self._allowed_rss_ids,
                rss_ids_authoritative=getattr(
                    self, "_rss_ids_authoritative", False
                ),
                strict=mode in {"daily_delivery", "weekly"},
                operation_date=strict_operation_date,
            )

            if ai_filter_result and ai_filter_result.success:
                print(f"[筛选] AI 筛选完成: {ai_filter_result.total_matched} 条匹配, {len(ai_filter_result.tags)} 个标签")
                # 转换为与关键词匹配相同的数据结构
                stats, ai_rss_stats, ai_rss_new_stats = self.ctx.convert_ai_filter_to_report_data(
                    ai_filter_result, mode=mode,
                    new_titles=new_titles, rss_new_urls=rss_new_urls,
                    rss_window=self._rss_window,
                    allowed_rss_ids=self._allowed_rss_ids,
                    rss_ids_authoritative=getattr(
                        self, "_rss_ids_authoritative", False
                    ),
                    operation_date=strict_operation_date,
                )
                total_titles = (
                    0
                    if getattr(self, "_rss_ids_authoritative", False)
                    else sum(len(titles) for titles in data_source.values())
                )

                # AI 筛选成功：无条件用 AI 结果替换 RSS 主区与新增区（与热榜 stats 一致，
                # 不因 AI 命中为空而回退到关键词结果）
                rss_items = ai_rss_stats
                rss_new_items = ai_rss_new_stats
                if mode == "weekly":
                    self._weekly_ai_filter_succeeded = True
            else:
                error_msg = ai_filter_result.error if ai_filter_result else "未知错误"
                if not self._should_fallback_ai_filter(mode):
                    label = "周报" if mode == "weekly" else "每日交付"
                    raise RuntimeError(f"{label} AI 筛选失败")
                # AI 筛选失败，回退到关键词匹配
                print(f"[筛选] AI 筛选失败: {error_msg}，回退到关键词匹配")
                stats, total_titles = self.ctx.count_frequency(
                    data_source, word_groups, filter_words,
                    id_to_name, title_info, new_titles,
                    mode=mode, global_filters=global_filters, quiet=quiet,
                )
        else:
            # === 关键词匹配策略（默认）===
            stats, total_titles = self.ctx.count_frequency(
                data_source, word_groups, filter_words,
                id_to_name, title_info, new_titles,
                mode=mode, global_filters=global_filters, quiet=quiet,
            )

        # 周报普通新闻在这里完成唯一一次双模块选择；摘要和 PDF 复用结果。
        if mode == "weekly" and rss_items:
            rss_items = self._select_weekly_rss_items(rss_items)

        self._hotlist_total_count = total_titles

        # 如果是 platform 模式，转换数据结构
        if self.ctx.display_mode == "platform" and stats:
            stats = convert_keyword_stats_to_platform_stats(
                stats,
                self.ctx.weight_config,
                self.ctx.rank_threshold,
            )

        has_delivery_content = (
            any(stat.get("count", 0) > 0 for stat in stats)
            or any(stat.get("count", 0) > 0 for stat in (rss_items or []))
        )
        if mode == "daily_delivery" and not has_delivery_content:
            self._rss_matched_count = 0
            return (
                stats, None, None, rss_items, standalone_data, rss_new_items
            )

        # AI 分析（如果启用，用于 HTML 报告）
        ai_result = None
        ai_config = self.ctx.config.get("AI_ANALYSIS", {})
        if (
            ai_config.get("ENABLED", False)
            and (
                stats
                or rss_items
                or (
                    mode == "weekly"
                    and getattr(self, "_agro_weather_report", None) is not None
                )
            )
            and (schedule is None or schedule.analyze)
        ):
            # 获取模式策略来确定报告类型
            mode_strategy = self._get_mode_strategy()
            report_type = mode_strategy["report_type"]
            ai_result = self._run_ai_analysis(
                stats, rss_items, mode, report_type, id_to_name,
                current_results=data_source, schedule=schedule,
                standalone_data=standalone_data
            )
            if (
                self._is_strict_delivery_mode(mode)
                and (ai_result is None or not ai_result.success)
            ):
                label = "周报" if mode == "weekly" else "每日交付"
                error = ai_result.error if ai_result is not None else "无有效结果"
                raise RuntimeError(f"{label} AI 摘要失败: {error}")

        if mode == "weekly" and (
            ai_result is None
            or not ai_result.success
            or not has_required_narrative(ai_result, report_mode="weekly")
        ):
            error = ai_result.error if ai_result is not None else "未执行 AI 分析"
            raise RuntimeError(f"周报 AI 三段叙事失败: {error}")

        # 翻译 RSS 和独立展示区内容（如果启用）— 在 HTML 生成前执行，确保网页版也能展示翻译内容
        # standalone_data 在此翻译一次后贯穿到推送阶段复用，避免重复翻译并保证网页与推送译文一致
        # 热榜翻译在推送时由 dispatch_all 处理 report_data
        trans_config = self.ctx.config.get("AI_TRANSLATION", {})
        translate_report_func = None  # 供 HTML 翻译热榜 report_data（在过滤之后翻译）
        if trans_config.get("ENABLED", False):
            dispatcher = self.ctx.create_notification_dispatcher(
                operation_at=getattr(self, "_run_at", None)
            )
            display_regions = self.ctx.config.get("DISPLAY", {}).get("REGIONS", {})
            _, rss_items, rss_new_items, standalone_data = \
                dispatcher.translate_content(
                    report_data={"stats": [], "new_titles": []},
                    rss_items=rss_items,
                    rss_new_items=rss_new_items,
                    standalone_data=standalone_data,
                    display_regions=display_regions,
                    require_all=mode == "daily_delivery",
                )

            # 热榜 report_data 翻译回调：HTML 在 prepare_report_data 过滤之后调用，
            # 仅翻译热榜（skip_rss/skip_standalone 跳过已在上游翻译的 RSS/独立区），网页版热榜展示译文
            def translate_report_func(rd, _d=dispatcher, _r=display_regions):
                translated_rd, _, _, _ = _d.translate_content(
                    report_data=rd, display_regions=_r,
                    skip_rss=True, skip_standalone=True,
                    require_all=mode == "daily_delivery",
                )
                return translated_rd

        # 计算 RSS 匹配条数（供 HTML 和推送共用）
        self._rss_matched_count = sum(stat.get("count", 0) for stat in rss_items) if rss_items else 0

        # HTML生成（如果启用）— 使用翻译后的数据
        html_file = None
        if (
            mode != "weekly"
            and self.ctx.config["STORAGE"]["FORMATS"]["HTML"]
        ):
            display_regions = self.ctx.config.get("DISPLAY", {}).get("REGIONS", {})
            html_standalone = standalone_data if display_regions.get("STANDALONE", False) else None
            html_ai = ai_result if display_regions.get("AI_ANALYSIS", True) else None
            html_file = self.ctx.generate_html(
                stats,
                total_titles,
                failed_ids=failed_ids,
                new_titles=new_titles,
                id_to_name=id_to_name,
                mode=mode,
                update_info=self.update_info if self.ctx.config["SHOW_VERSION_UPDATE"] else None,
                rss_items=rss_items,
                rss_new_items=rss_new_items,
                ai_analysis=html_ai,
                standalone_data=html_standalone,
                frequency_file=self.frequency_file,
                report_metadata={
                    "hotlist_total": total_titles,
                    "platform_total": len(self.ctx.platform_ids),
                    "rss_matched_count": self._rss_matched_count,
                    "rss_total_count": self._rss_total_count,
                    "rss_source_total": self._rss_source_total,
                    "rss_source_failed": self._rss_source_failed,
                    "period_label": self._report_period_label,
                },
                translate_report_func=translate_report_func,
                operation_at=getattr(self, "_run_at", None),
            )
            if not html_file and mode == "daily_delivery":
                raise RuntimeError("每日交付 HTML 报告生成失败")

        # 专用 PDF 不使用现有 dashboard HTML。运行实例均设置此属性；hasattr
        # 使直接构造的旧分析单测保持其原有职责。
        if mode == "weekly" and hasattr(self, "_agro_weather_report"):
            self._generate_weekly_pdf_report(
                rss_items or [], ai_result, expected_contract_hash
            )
            if (
                ai_result is not None
                and ai_result.success
                and schedule is not None
                and schedule.once_analyze
                and schedule.period_key
            ):
                scheduler = self.ctx.create_scheduler()
                date_str = self._delivery_checkpoint_date(mode)
                if not scheduler.record_execution(
                    schedule.period_key, "analyze", date_str
                ):
                    raise RuntimeError(
                        "周报 PDF 已生成，但 once_analyze 持久化失败"
                    )

        return stats, html_file, ai_result, rss_items, standalone_data, rss_new_items

    def _select_weekly_rss_items(
        self, rss_stats: list[dict] | None
    ) -> list[dict]:
        """Select both weekly modules once and group them for the summary model."""
        candidates: list[dict] = []
        for stat in rss_stats or []:
            topic = str(stat.get("word") or "").strip()
            for raw_item in stat.get("titles") or []:
                item = dict(raw_item)
                topics = item.get("weekly_topics") or []
                if isinstance(topics, str):
                    topics = [topics]
                if topic:
                    topics = [*topics, topic]
                item["weekly_topics"] = sorted({
                    str(value).strip() for value in topics if str(value).strip()
                })
                candidates.append(item)

        scholarly_source_ids = {
            str(feed.get("id") or "").strip()
            for feed in self.ctx.config.get("RSS", {}).get("FEEDS", [])
            if isinstance(feed, dict)
            and str(feed.get("content_category") or "").strip() == "scholarly"
            and str(feed.get("id") or "").strip()
        }
        self._weekly_news_modules = select_weekly_modules(
            candidates,
            min_score=self.ctx.config["AI_FILTER"]["MIN_SCORE"],
            scholarly_source_ids=scholarly_source_ids,
        )
        grouped: dict[str, list[dict]] = {}
        for item in (
            self._weekly_news_modules.current_events
            + self._weekly_news_modules.research
        ):
            topic = primary_weekly_topic(item)
            grouped.setdefault(topic, []).append(item)
        return [
            {"word": topic, "count": len(items), "position": 9999, "titles": items}
            for topic, items in grouped.items()
        ]

    def _generate_weekly_pdf_report(
        self,
        rss_items: list[dict],
        ai_result: Optional[AIAnalysisResult],
        expected_contract_hash: str,
    ) -> str:
        """Build the offline A4 report from selected RSS news and official weather."""
        modules = getattr(self, "_weekly_news_modules", None)
        if not isinstance(modules, WeeklyNewsSelection):
            raise RuntimeError(
                "周报缺少显式 WeeklyNewsSelection，已拒绝生成 PDF"
            )
        selected_news = [
            *modules.current_events, *modules.research
        ]
        weather = self._agro_weather_report
        if not selected_news and weather is None:
            raise RuntimeError("周报没有入选新闻和农业气象报告，无法生成 PDF")
        if selected_news and not getattr(
            self, "_weekly_ai_filter_succeeded", False
        ):
            raise RuntimeError("周报普通新闻缺少成功的严格 AI 筛选，无法生成 PDF")
        if (
            ai_result is None
            or not ai_result.success
            or not has_required_narrative(ai_result, report_mode="weekly")
        ):
            raise RuntimeError("周报缺少成功且完整的 AI 三段叙事，无法生成 PDF")

        window = self._rss_window
        if window is None:
            window = previous_natural_week(
                self._operation_run_at(), self.ctx.timezone
            )
        html = render_weekly_pdf_html(
            current_events_items=modules.current_events,
            research_items=modules.research,
            ai_analysis=ai_result,
            agro_weather=weather,
            period_label=window.label,
            generated_at=self._operation_run_at(),
            missing_dates=getattr(
                self, "_weekly_source_status", {}
            ).get("missing_dates", []),
            failed_sources=getattr(
                self, "_weekly_source_status", {}
            ).get("failed_sources", {}),
        )
        pdf_path = build_weekly_pdf(
            "output", window.start.date(), window.end.date(), html
        )
        try:
            current_contract_hash = self._weekly_artifact_contract_hash()
        except Exception as exc:
            raise RuntimeError(
                "周报 PDF 生成后 artifact contract 读取失败"
            ) from exc
        if current_contract_hash != expected_contract_hash:
            raise RuntimeError(
                "周报 PDF 生成期间 artifact contract 发生变化"
            )
        self._weekly_pdf_path = pdf_path
        print(f"[周报] 专用 PDF 已生成: {self._weekly_pdf_path}")
        return self._weekly_pdf_path

    def _send_notification_if_needed(
        self,
        stats: List[Dict],
        report_type: str,
        mode: str,
        failed_ids: Optional[List] = None,
        new_titles: Optional[Dict] = None,
        id_to_name: Optional[Dict] = None,
        html_file_path: Optional[str] = None,
        rss_items: Optional[List[Dict]] = None,
        rss_new_items: Optional[List[Dict]] = None,
        standalone_data: Optional[Dict] = None,
        ai_result: Optional[AIAnalysisResult] = None,
        current_results: Optional[Dict] = None,
        schedule: ResolvedSchedule = None,
    ) -> bool:
        """统一的通知发送逻辑，包含所有判断条件，支持热榜+RSS合并推送+AI分析+独立展示区"""
        has_notification = self._has_notification_configured()
        cfg = self.ctx.config

        if mode == "weekly":
            if not cfg["ENABLE_NOTIFICATION"]:
                print(f"跳过{report_type}通知：通知功能已禁用")
                return False
            if not schedule or not schedule.push:
                print("[周报] 调度器: 当前时间段不执行推送")
                return False
            return self._deliver_weekly_pdf(
                schedule,
                getattr(
                    self, "_weekly_artifact_contract_expected", None
                ),
            )

        # 检查是否有有效内容（热榜或RSS）
        has_news_content = self._has_valid_content(stats, new_titles)
        has_rss_content = bool(rss_items and len(rss_items) > 0)
        has_any_content = has_news_content or has_rss_content

        # 计算热榜匹配条数
        news_count = sum(len(stat.get("titles", [])) for stat in stats) if stats else 0
        rss_count = sum(stat.get("count", 0) for stat in rss_items) if rss_items else 0

        if (
            cfg["ENABLE_NOTIFICATION"]
            and has_notification
            and has_any_content
        ):
            # 输出推送内容统计
            content_parts = []
            if news_count > 0:
                content_parts.append(f"热榜 {news_count} 条")
            if rss_count > 0:
                content_parts.append(f"RSS {rss_count} 条")
            total_count = news_count + rss_count
            print(f"[推送] 准备发送：{' + '.join(content_parts)}，合计 {total_count} 条")

            # 调度系统决策
            if not schedule.push:
                print("[推送] 调度器: 当前时间段不执行推送")
                return False

            if schedule.once_push and schedule.period_key:
                scheduler = self.ctx.create_scheduler()
                date_str = self._delivery_checkpoint_date(mode)
                if scheduler.already_executed(schedule.period_key, "push", date_str):
                    print(f"[推送] 调度器: 时间段 {schedule.period_name or schedule.period_key} 今天已推送过，跳过")
                    return False
                else:
                    print(f"[推送] 调度器: 时间段 {schedule.period_name or schedule.period_key} 今天首次推送")

            # AI 分析：优先使用传入的结果，避免重复分析
            if ai_result is None:
                ai_config = cfg.get("AI_ANALYSIS", {})
                if (
                    ai_config.get("ENABLED", False)
                    and (schedule is None or schedule.analyze)
                ):
                    ai_result = self._run_ai_analysis(
                        stats, rss_items, mode, report_type, id_to_name,
                        current_results=current_results, schedule=schedule,
                        standalone_data=standalone_data,
                    )

            if (
                self._is_strict_delivery_mode(mode)
                and cfg.get("AI_ANALYSIS", {}).get("ENABLED", False)
                and (schedule is None or schedule.analyze)
                and (ai_result is None or not ai_result.success)
            ):
                error = ai_result.error if ai_result is not None else "无有效结果"
                label = "周报" if mode == "weekly" else "每日交付"
                print(f"[推送] {label} AI 摘要失败，取消推送: {error}")
                return False

            # 准备报告数据
            report_data = self.ctx.prepare_report(stats, failed_ids, new_titles, id_to_name, mode, frequency_file=self.frequency_file)

            # 注入元数据（用于推送头部展示）
            report_data["hotlist_total"] = self._hotlist_total_count
            report_data["platform_total"] = len(self.ctx.platform_ids)
            report_data["rss_matched_count"] = self._rss_matched_count
            report_data["rss_total_count"] = self._rss_total_count
            report_data["rss_source_total"] = self._rss_source_total
            report_data["rss_source_failed"] = self._rss_source_failed
            report_data["period_label"] = self._report_period_label

            # 是否发送版本更新信息
            update_info_to_send = self.update_info if cfg["SHOW_VERSION_UPDATE"] else None

            # 使用 NotificationDispatcher 发送到所有渠道
            # RSS/独立展示区数据已在分析流水线中翻译过，跳过重复翻译（仅翻译热榜 report_data）
            dispatcher = self.ctx.create_notification_dispatcher(
                operation_at=getattr(self, "_run_at", None)
            )
            results = dispatcher.dispatch_all(
                report_data=report_data,
                report_type=report_type,
                update_info=update_info_to_send,
                proxy_url=self.proxy_url,
                mode=mode,
                html_file_path=html_file_path,
                rss_items=rss_items,
                rss_new_items=rss_new_items,
                ai_analysis=ai_result,
                standalone_data=standalone_data,
                skip_translation=True,
                require_all_targets=self._is_strict_delivery_mode(mode),
            )

            if not results:
                print("未配置任何通知渠道，跳过通知发送")
                return False

            delivery_succeeded = self._notification_delivery_succeeded(mode, results)
            if delivery_succeeded:
                if schedule.once_push and schedule.period_key:
                    if not self._record_delivery_checkpoint(schedule):
                        print("[推送] once_push 持久化失败，本次任务标记失败")
                        return False
            elif self._is_strict_delivery_mode(mode):
                label = "周报" if mode == "weekly" else "每日交付"
                print(f"[推送] {label}存在发送失败，未记录 once_push，可人工补跑")

            return delivery_succeeded

        elif cfg["ENABLE_NOTIFICATION"] and not has_notification:
            print("⚠️ 警告：通知功能已启用但未配置任何通知渠道，将跳过通知发送")
        elif not cfg["ENABLE_NOTIFICATION"]:
            print(f"跳过{report_type}通知：通知功能已禁用")
        elif (
            cfg["ENABLE_NOTIFICATION"]
            and has_notification
            and not has_any_content
        ):
            mode_strategy = self._get_mode_strategy()
            if self.report_mode == "incremental":
                if not has_rss_content:
                    print("跳过通知：增量模式下未检测到匹配的新闻和RSS")
                else:
                    print("跳过通知：增量模式下新闻未匹配到关键词")
            else:
                print(
                    f"跳过通知：{mode_strategy['mode_name']}下未检测到匹配的新闻"
                )

        return False

    def _initialize_and_check_config(self) -> bool:
        """通用初始化和配置检查。返回 True 表示可以继续执行。"""
        now = self._operation_run_at()
        print(f"当前北京时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")

        if not self.ctx.config["ENABLE_CRAWLER"]:
            print("爬虫功能已禁用（ENABLE_CRAWLER=False），程序退出")
            return False

        has_notification = self._has_notification_configured()
        if not self.ctx.config["ENABLE_NOTIFICATION"]:
            print("通知功能已禁用（ENABLE_NOTIFICATION=False），将只进行数据抓取")
        elif not has_notification:
            print("未配置任何通知渠道，将只进行数据抓取，不发送通知")
        else:
            print("通知功能已启用，将发送通知")

        mode_strategy = self._get_mode_strategy()
        print(f"报告模式: {self.report_mode}")
        print(f"运行模式: {mode_strategy['description']}")
        return True

    def _crawl_data(self) -> Tuple[Dict, Dict, List]:
        """执行数据爬取"""
        ids = []
        domain_rules = {}
        for platform in self.ctx.platforms:
            if "name" in platform:
                ids.append((platform["id"], platform["name"]))
            else:
                ids.append(platform["id"])
            expected_domain = platform.get("expected_domain", "")
            if expected_domain:
                domain_rules[platform["id"]] = expected_domain

        print(
            f"配置的监控平台: {[p.get('name', p['id']) for p in self.ctx.platforms]}"
        )
        print(f"开始爬取数据，请求间隔 {self.request_interval} 毫秒")
        Path("output").mkdir(parents=True, exist_ok=True)

        results, id_to_name, failed_ids = self.data_fetcher.crawl_websites(
            ids, self.request_interval, domain_rules=domain_rules
        )

        # 转换为 NewsData 格式并保存到存储后端
        crawl_time = self._operation_time_filename()
        crawl_date = self._operation_date()
        news_data = convert_crawl_results_to_news_data(
            results, id_to_name, failed_ids, crawl_time, crawl_date
        )

        # 保存到存储后端（SQLite）
        news_saved = self.storage_manager.save_news_data(news_data)
        if news_saved:
            print(f"数据已保存到存储后端: {self.storage_manager.backend_name}")
        elif self.report_mode == "daily_delivery":
            raise RuntimeError("每日交付热榜数据保存失败")

        # 保存 TXT 快照（如果启用）
        txt_file = self.storage_manager.save_txt_snapshot(news_data)
        if txt_file:
            print(f"TXT 快照已保存: {txt_file}")

        return results, id_to_name, failed_ids

    def _crawl_rss_data(self) -> Tuple[Optional[List[Dict]], Optional[List[Dict]], Optional[List[Dict]], set]:
        """
        执行 RSS 数据抓取

        Returns:
            (rss_items, rss_new_items, raw_rss_items, rss_new_urls) 元组：
            - rss_items: 统计条目列表（按模式处理，用于统计区块）
            - rss_new_items: 新增条目列表（用于新增区块）
            - raw_rss_items: 原始 RSS 条目列表（用于独立展示区）
            - rss_new_urls: 原始新增 RSS 条目的 URL 集合（用于 AI 模式 is_new 检测）
            如果未启用或失败返回 (None, None, None, set())
        """
        if self.report_mode == "daily_delivery":
            self._daily_delivery_rss_data = None
            self._rss_window = None
            self._allowed_rss_ids = None
            self._rss_ids_authoritative = False

        if not self.ctx.rss_enabled:
            return None, None, None, set()

        rss_feeds = self.ctx.rss_feeds
        if not rss_feeds:
            print("[RSS] 未配置任何 RSS 源")
            return None, None, None, set()

        try:
            from trendradar.crawler.rss import RSSFetcher, RSSFeedConfig

            # 构建 RSS 源配置
            feeds = []
            for feed_config in rss_feeds:
                feed = RSSFeedConfig(
                    id=feed_config.get("id", ""),
                    name=feed_config.get("name", ""),
                    url=feed_config.get("url", ""),
                    max_items=feed_config.get("max_items", 50),
                    enabled=feed_config.get("enabled", True),
                    source_type=feed_config.get("source_type", "rss"),
                    fetch_url=feed_config.get("fetch_url", ""),
                )
                if feed.id and feed.url and feed.enabled:
                    feeds.append(feed)

            if not feeds:
                print("[RSS] 没有启用的 RSS 源")
                return None, None, None, set()

            # 创建抓取器
            rss_config = self.ctx.rss_config
            # RSS 代理：优先使用 RSS 专属代理，否则使用爬虫默认代理
            rss_proxy_url = rss_config.get("PROXY_URL", "") or self.proxy_url or ""
            # 获取配置的时区
            timezone = self.ctx.config.get("TIMEZONE", DEFAULT_TIMEZONE)
            fetcher = RSSFetcher(
                feeds=feeds,
                request_interval=rss_config.get("REQUEST_INTERVAL", 2000),
                timeout=rss_config.get("TIMEOUT", 15),
                use_proxy=rss_config.get("USE_PROXY", False),
                proxy_url=rss_proxy_url,
                timezone=timezone,
                get_time_func=lambda: self._operation_run_at(),
            )

            # 抓取数据
            rss_data = fetcher.fetch_all()
            # RSSFetcher may finish after midnight.  The run owns the date and
            # discovery timestamp captured at its entry, so normalize the
            # returned snapshot (and only item timestamps originating from the
            # fetcher's snapshot clock) before any durable write.
            if getattr(self, "_run_at", None) is not None:
                source_crawl_time = rss_data.crawl_time
                operation_time = self._operation_run_at().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                rss_data.date = self._operation_date()
                rss_data.crawl_time = operation_time
                for fetched_items in rss_data.items.values():
                    for item in fetched_items:
                        for attribute in (
                            "crawl_time", "first_time", "last_time"
                        ):
                            value = getattr(item, attribute, "")
                            if not value or value == source_crawl_time:
                                setattr(item, attribute, operation_time)

            self._rss_source_total = len(feeds)
            self._rss_source_failed = len(rss_data.failed_ids)
            search_failure = None

            news_search_value = rss_config.get("NEWS_SEARCH", {})
            news_search_config = (
                news_search_value
                if isinstance(news_search_value, Mapping)
                else {}
            )
            if news_search_config.get("ENABLED", False):
                has_enabled_search_provider = False
                try:
                    providers_value = news_search_config.get("PROVIDERS", {})
                    providers = (
                        providers_value
                        if isinstance(providers_value, Mapping)
                        else {}
                    )
                    has_enabled_search_provider = any(
                        bool(providers.get(provider, False))
                        for provider in NEWS_SEARCH_PROVIDERS
                    )
                    topics_value = news_search_config.get("TOPICS", [])
                    topics = topics_value if isinstance(topics_value, list) else []
                    authority_value = news_search_config.get(
                        "AUTHORITY_DOMAINS", []
                    )
                    authority_domains = (
                        authority_value
                        if isinstance(authority_value, list)
                        else []
                    )
                    news_search = AgriculturalNewsSearch(
                        gdelt_client=GDELTClient(
                            use_proxy=rss_config.get("USE_PROXY", False),
                            proxy_url=rss_proxy_url,
                            timeout=rss_config.get("TIMEOUT", 15),
                        ),
                        google_news_client=GoogleNewsRSSClient(
                            use_proxy=rss_config.get("USE_PROXY", False),
                            proxy_url=rss_proxy_url,
                            timeout=rss_config.get("TIMEOUT", 15),
                        ),
                        topics=topics,
                        max_results_per_provider=news_search_config.get(
                            "MAX_RESULTS_PER_PROVIDER", 50
                        ),
                        similarity_threshold=news_search_config.get(
                            "SIMILARITY_THRESHOLD", 0.86
                        ),
                        authority_domains=authority_domains,
                        providers=providers,
                    )
                    search_result = news_search.search(
                        self._news_search_bounds(self.report_mode)
                    )
                    if (
                        has_enabled_search_provider
                        and search_result.failed_providers
                    ):
                        failed = ", ".join(search_result.failed_providers)
                        print(f"[新闻搜索] 部分来源失败: {failed}")
                        if self._is_strict_delivery_mode(self.report_mode):
                            label = (
                                "每日交付"
                                if self.report_mode == "daily_delivery"
                                else "周报"
                            )
                            search_failure = (
                                f"{label}新闻搜索来源失败: {failed}"
                            )
                    if has_enabled_search_provider:
                        merge_news_search_into_rss(rss_data, search_result)
                        if search_result.failed_providers:
                            _mark_news_search_failed(rss_data)
                except Exception as e:
                    print(f"[新闻搜索] 搜索失败，继续使用固定 RSS: {e}")
                    if has_enabled_search_provider:
                        _mark_news_search_failed(rss_data)
                    if self._is_strict_delivery_mode(self.report_mode):
                        label = (
                            "每日交付"
                            if self.report_mode == "daily_delivery"
                            else "周报"
                        )
                        search_failure = f"{label}新闻搜索失败: {e}"

            if self.report_mode == "weekly":
                before_count = sum(
                    len(items) for items in rss_data.items.values()
                )
                rss_data = previous_week_candidates(
                    rss_data,
                    self._operation_run_at(),
                    self.ctx.timezone,
                )
                retained_count = sum(
                    len(items) for items in rss_data.items.values()
                )
                print(
                    "[周报] 上一自然周候选过滤: "
                    f"保留 {retained_count}/{before_count} 条，"
                    "本周及窗口外内容不保存、不参与分析"
                )

            # 保存到存储后端
            if self.storage_manager.save_rss_data(rss_data):
                print(f"[RSS] 数据已保存到存储后端")

                if search_failure and self.report_mode == "daily_delivery":
                    raise RuntimeError(search_failure)
                if (
                    self.report_mode == "daily_delivery"
                    and rss_data.failed_ids
                ):
                    failed = ", ".join(rss_data.failed_ids)
                    label = (
                        "每日交付"
                        if self.report_mode == "daily_delivery"
                        else "周报"
                    )
                    raise RuntimeError(f"{label} RSS 来源失败: {failed}")

                if self.report_mode == "daily_delivery":
                    self._daily_delivery_rss_data = rss_data
                    return None, None, None, set()

                # 处理 RSS 数据（按模式过滤）并返回用于合并推送
                return self._process_rss_data_by_mode(rss_data)
            else:
                print(f"[RSS] 数据保存失败")
                if self._is_strict_delivery_mode(self.report_mode):
                    raise RuntimeError("RSS 数据保存失败")
                return None, None, None, set()

        except ImportError as e:
            print(f"[RSS] 缺少依赖: {e}")
            print("[RSS] 请安装 feedparser: pip install feedparser")
            if self._is_strict_delivery_mode(self.report_mode):
                raise
            return None, None, None, set()
        except Exception as e:
            print(f"[RSS] 抓取失败: {e}")
            if self._is_strict_delivery_mode(self.report_mode):
                raise
            return None, None, None, set()

    def _process_rss_data_by_mode(self, rss_data) -> Tuple[Optional[List[Dict]], Optional[List[Dict]], Optional[List[Dict]], set]:
        """
        按报告模式处理 RSS 数据，返回与热榜相同格式的统计结构

        四种模式：
        - daily: 当日汇总，统计=当天所有条目，新增=本次新增条目
        - current: 当前榜单，统计=当前榜单条目，新增=本次新增条目
        - incremental: 增量模式，统计=新增条目，新增=无
        - weekly: 上一自然周快照，统计=快照条目，新增=无

        Args:
            rss_data: 当前抓取的 RSSData 对象

        Returns:
            (rss_stats, rss_new_stats, raw_rss_items, rss_new_urls) 元组：
            - rss_stats: RSS 关键词统计列表（与热榜 stats 格式一致）
            - rss_new_stats: RSS 新增关键词统计列表（与热榜 stats 格式一致）
            - raw_rss_items: 原始 RSS 条目列表（用于独立展示区）
            - rss_new_urls: 原始新增 RSS 条目的 URL 集合（未经关键词过滤，用于 AI 模式 is_new 检测）
        """
        from trendradar.core.analyzer import count_rss_frequency

        # 从 display.regions.rss 统一控制 RSS 分析和展示
        rss_display_enabled = self.ctx.config.get("DISPLAY", {}).get("REGIONS", {}).get("RSS", True)

        # 加载关键词配置
        try:
            word_groups, filter_words, global_filters = self.ctx.load_frequency_words(self.frequency_file)
        except FileNotFoundError:
            word_groups, filter_words, global_filters = [], [], []

        timezone = self.ctx.timezone
        max_news_per_keyword = self.ctx.config.get("MAX_NEWS_PER_KEYWORD", 0)
        sort_by_position_first = self.ctx.config.get("SORT_BY_POSITION_FIRST", False)

        rss_stats = None
        rss_new_stats = None
        raw_rss_items = None  # 原始 RSS 条目列表（用于独立展示区）
        rss_new_urls = set()  # 原始新增 RSS URLs（未经关键词过滤）

        if self.report_mode == "daily_delivery":
            scheduler = self.ctx.create_scheduler()
            date_str = self._operation_date()
            checkpoint = scheduler.latest_execution(
                "daily_delivery", "push", date_str
            )
            snapshot = DailyDeliveryAggregator(
                self.storage_manager, self.ctx.timezone
            ).build(self._operation_run_at(), checkpoint)
            self._rss_window = None
            self._allowed_rss_ids = snapshot.allowed_rss_ids
            self._rss_ids_authoritative = True
            self._report_period_label = snapshot.window.label
            self._rss_total_count = (
                sum(len(items) for items in snapshot.data.items.values())
                if snapshot.data
                else 0
            )
            raw_rss_items = (
                self._convert_rss_items_to_list(
                    snapshot.data.items,
                    snapshot.data.id_to_name,
                )
                if snapshot.data
                else []
            )
            if not rss_display_enabled or not raw_rss_items:
                return None, None, raw_rss_items, set()

            rss_stats, _ = count_rss_frequency(
                rss_items=raw_rss_items,
                word_groups=word_groups,
                filter_words=filter_words,
                global_filters=global_filters,
                max_news_per_keyword=max_news_per_keyword,
                sort_by_position_first=sort_by_position_first,
                timezone=timezone,
                rank_threshold=self.rank_threshold,
                quiet=False,
            )
            return rss_stats, None, raw_rss_items, set()

        if self.report_mode == "weekly":
            snapshot = WeeklyRSSAggregator(
                self.storage_manager, self.ctx.timezone,
            ).build(
                self._operation_run_at(),
                current_data=rss_data,
            )
            self._weekly_source_status = {
                "missing_dates": list(snapshot.missing_dates),
                "failed_sources": dict(snapshot.failed_sources),
            }
            self._rss_window = snapshot.window
            self._allowed_rss_ids = snapshot.allowed_rss_ids
            self._rss_ids_authoritative = True
            self._report_period_label = snapshot.window.label
            if not snapshot.data:
                print("[周报] 上一自然周没有可用数据，不生成空周报")
                self._rss_total_count = 0
                return None, None, [], set()

            self._rss_total_count = sum(
                len(items) for items in snapshot.data.items.values()
            )
            print(
                "[周报] 快照汇总: "
                f"total_read={snapshot.total_read}, "
                f"filtered_out={snapshot.filtered_out}, "
                f"duplicate_count={snapshot.duplicate_count}, "
                f"retained={self._rss_total_count}, "
                f"missing_dates={snapshot.missing_dates}, "
                f"failed_sources={snapshot.failed_sources}"
            )
            raw_rss_items = self._convert_rss_items_to_list(
                snapshot.data.items,
                snapshot.data.id_to_name,
            )
            if not rss_display_enabled:
                return None, None, raw_rss_items, set()

            rss_stats, _ = count_rss_frequency(
                rss_items=raw_rss_items,
                word_groups=word_groups,
                filter_words=filter_words,
                global_filters=global_filters,
                max_news_per_keyword=max_news_per_keyword,
                sort_by_position_first=sort_by_position_first,
                timezone=timezone,
                rank_threshold=self.rank_threshold,
                quiet=False,
            )
            return rss_stats, None, raw_rss_items, set()

        # 1. 首先获取原始条目（用于独立展示区，不受 display.regions.rss 影响）
        # 根据模式获取原始条目
        if self.report_mode == "incremental":
            new_items_dict = self.storage_manager.detect_new_rss_items(rss_data)
            if new_items_dict:
                raw_rss_items = self._convert_rss_items_to_list(new_items_dict, rss_data.id_to_name)
        elif self.report_mode == "current":
            latest_data = self.storage_manager.get_latest_rss_data(rss_data.date)
            if latest_data:
                raw_rss_items = self._convert_rss_items_to_list(latest_data.items, latest_data.id_to_name)
        else:  # daily
            all_data = self.storage_manager.get_rss_data(rss_data.date)
            if all_data:
                raw_rss_items = self._convert_rss_items_to_list(all_data.items, all_data.id_to_name)

        # 如果 RSS 展示未启用，跳过关键词分析，只返回原始条目用于独立展示区
        if not rss_display_enabled:
            return None, None, raw_rss_items, rss_new_urls

        # 2. 获取新增条目（用于统计）
        new_items_dict = self.storage_manager.detect_new_rss_items(rss_data)
        new_items_list = None
        if new_items_dict:
            new_items_list = self._convert_rss_items_to_list(new_items_dict, rss_data.id_to_name)
            if new_items_list:
                print(f"[RSS] 检测到 {len(new_items_list)} 条新增")
                # 收集原始新增 URLs（未经关键词过滤，用于 AI 模式 is_new 检测）
                rss_new_urls = {item["url"] for item in new_items_list if item.get("url")}

        # 3. 根据模式获取统计条目
        if self.report_mode == "incremental":
            # 增量模式：统计条目就是新增条目
            if not new_items_list:
                print("[RSS] 增量模式：没有新增 RSS 条目")
                return None, None, raw_rss_items, rss_new_urls

            rss_stats, total = count_rss_frequency(
                rss_items=new_items_list,
                word_groups=word_groups,
                filter_words=filter_words,
                global_filters=global_filters,
                new_items=new_items_list,  # 增量模式所有都是新增
                max_news_per_keyword=max_news_per_keyword,
                sort_by_position_first=sort_by_position_first,
                timezone=timezone,
                rank_threshold=self.rank_threshold,
                quiet=False,
            )
            if not rss_stats:
                print("[RSS] 增量模式：关键词匹配后没有内容")
                # 即使关键词匹配为空，也返回原始条目用于独立展示区
                return None, None, raw_rss_items, rss_new_urls

        elif self.report_mode == "current":
            # 当前榜单模式：统计=当前榜单所有条目
            # raw_rss_items 已在前面获取
            if not raw_rss_items:
                print("[RSS] 当前榜单模式：没有 RSS 数据")
                return None, None, None, rss_new_urls

            rss_stats, total = count_rss_frequency(
                rss_items=raw_rss_items,
                word_groups=word_groups,
                filter_words=filter_words,
                global_filters=global_filters,
                new_items=new_items_list,  # 标记新增
                max_news_per_keyword=max_news_per_keyword,
                sort_by_position_first=sort_by_position_first,
                timezone=timezone,
                rank_threshold=self.rank_threshold,
                quiet=False,
            )
            if not rss_stats:
                print("[RSS] 当前榜单模式：关键词匹配后没有内容")
                # 即使关键词匹配为空，也返回原始条目用于独立展示区
                return None, None, raw_rss_items, rss_new_urls

            # 生成新增统计
            if new_items_list:
                rss_new_stats, _ = count_rss_frequency(
                    rss_items=new_items_list,
                    word_groups=word_groups,
                    filter_words=filter_words,
                    global_filters=global_filters,
                    new_items=new_items_list,
                    max_news_per_keyword=max_news_per_keyword,
                    sort_by_position_first=sort_by_position_first,
                    timezone=timezone,
                    rank_threshold=self.rank_threshold,
                    quiet=True,
                )

        else:
            # daily 模式：统计=当天所有条目
            # raw_rss_items 已在前面获取
            if not raw_rss_items:
                print("[RSS] 当日汇总模式：没有 RSS 数据")
                return None, None, None, rss_new_urls

            rss_stats, total = count_rss_frequency(
                rss_items=raw_rss_items,
                word_groups=word_groups,
                filter_words=filter_words,
                global_filters=global_filters,
                new_items=new_items_list,  # 标记新增
                max_news_per_keyword=max_news_per_keyword,
                sort_by_position_first=sort_by_position_first,
                timezone=timezone,
                rank_threshold=self.rank_threshold,
                quiet=False,
            )
            if not rss_stats:
                print("[RSS] 当日汇总模式：关键词匹配后没有内容")
                # 即使关键词匹配为空，也返回原始条目用于独立展示区
                return None, None, raw_rss_items, rss_new_urls

            # 生成新增统计
            if new_items_list:
                rss_new_stats, _ = count_rss_frequency(
                    rss_items=new_items_list,
                    word_groups=word_groups,
                    filter_words=filter_words,
                    global_filters=global_filters,
                    new_items=new_items_list,
                    max_news_per_keyword=max_news_per_keyword,
                    sort_by_position_first=sort_by_position_first,
                    timezone=timezone,
                    rank_threshold=self.rank_threshold,
                    quiet=True,
                )

        self._rss_total_count = total
        return rss_stats, rss_new_stats, raw_rss_items, rss_new_urls

    def _convert_rss_items_to_list(
        self, items_dict: Dict, id_to_name: Dict,
    ) -> List[Dict]:
        """将 RSS 条目字典转换为推送列表格式。"""
        rss_items = []

        for feed_id, items in items_dict.items():
            for item in items:
                rss_items.append({
                    "title": item.title,
                    "feed_id": feed_id,
                    "feed_name": id_to_name.get(feed_id, feed_id),
                    "url": item.url,
                    "reader_url": build_reader_url(feed_id, item.url, item.title),
                    "published_at": item.published_at,
                    "summary": item.summary,
                    "author": item.author,
                })

        return rss_items

    def _filter_rss_by_keywords(self, rss_items: List[Dict]) -> List[Dict]:
        """使用关键词文件过滤 RSS 条目"""
        try:
            word_groups, filter_words, global_filters = self.ctx.load_frequency_words(self.frequency_file)
            if word_groups or filter_words or global_filters:
                from trendradar.core.frequency import matches_word_groups
                filtered_items = []
                for item in rss_items:
                    title = item.get("title", "")
                    if matches_word_groups(title, word_groups, filter_words, global_filters):
                        filtered_items.append(item)

                original_count = len(rss_items)
                rss_items = filtered_items
                print(f"[RSS] 关键词过滤后剩余 {len(rss_items)}/{original_count} 条")

                if not rss_items:
                    print("[RSS] 关键词过滤后没有匹配内容")
                    return []
        except FileNotFoundError:
            # 关键词文件不存在时跳过过滤
            pass
        return rss_items

    def _generate_rss_html_report(self, rss_items: list, feeds_info: dict) -> str:
        """生成 RSS HTML 报告"""
        try:
            from trendradar.report.rss_html import render_rss_html_content
            from pathlib import Path

            html_content = render_rss_html_content(
                rss_items=rss_items,
                total_count=len(rss_items),
                feeds_info=feeds_info,
                get_time_func=lambda: self._operation_run_at(),
            )

            # 保存 HTML 文件（扁平化结构：output/html/日期/）
            date_folder = self._operation_date()
            time_filename = self._operation_time_filename()
            output_dir = Path("output") / "html" / date_folder
            output_dir.mkdir(parents=True, exist_ok=True)

            file_path = output_dir / f"rss_{time_filename}.html"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            print(f"[RSS] HTML 报告已生成: {file_path}")
            return str(file_path)

        except Exception as e:
            print(f"[RSS] 生成 HTML 报告失败: {e}")
            return None

    def _execute_mode_strategy(
        self, mode_strategy: Dict, results: Dict, id_to_name: Dict, failed_ids: List,
        rss_items: Optional[List[Dict]] = None,
        rss_new_items: Optional[List[Dict]] = None,
        raw_rss_items: Optional[List[Dict]] = None,
        rss_new_urls: Optional[set] = None,
        schedule: ResolvedSchedule = None,
    ) -> bool:
        """执行模式特定逻辑，支持热榜+RSS合并推送

        普通模式生成 HTML 报告并按模式发送通知；weekly 只生成和投递
        专用 PDF，不写通用 latest/index。
        """
        # 调度已在采集前解析并应用，避免 RSS 处理和运行策略得到不同结果。
        mode_strategy = self._get_mode_strategy()
        # 获取当前监控平台ID列表
        current_platform_ids = self.ctx.platform_ids

        new_titles = self.ctx.detect_new_titles(current_platform_ids)
        time_info = self._operation_time_filename()
        word_groups, filter_words, global_filters = self.ctx.load_frequency_words(self.frequency_file)

        html_file = None
        stats = []
        ai_result = None
        title_info = None
        standalone_data = None

        # 每日交付只使用上次成功检查点后的 RSS 快照；热榜仍采集保存，
        # 但不能进入交付报告，避免和当前榜单内容重复。
        if self.report_mode == "daily_delivery":
            new_titles = {}
            title_info = {}
            results = {}
            id_to_name = {}
            failed_ids = []
            standalone_data = self._prepare_standalone_data(
                {}, {}, title_info, raw_rss_items
            )
            (
                stats,
                html_file,
                ai_result,
                rss_items,
                standalone_data,
                rss_new_items,
            ) = self._run_analysis_pipeline(
                {},
                self.report_mode,
                title_info,
                new_titles,
                word_groups,
                filter_words,
                id_to_name,
                failed_ids=[],
                global_filters=global_filters,
                rss_items=rss_items,
                rss_new_items=None,
                standalone_data=standalone_data,
                schedule=schedule,
                rss_new_urls=set(),
            )
        # weekly 模式使用本轮热榜和已聚合的自然周 RSS，不读取当日历史热榜。
        elif self.report_mode == "weekly":
            title_info = self._prepare_current_title_info(results, time_info)
            standalone_data = self._prepare_standalone_data(
                results, id_to_name, title_info, raw_rss_items
            )
            stats, html_file, ai_result, rss_items, standalone_data, rss_new_items = self._run_analysis_pipeline(
                results, self.report_mode, title_info, new_titles,
                word_groups, filter_words, id_to_name,
                failed_ids=failed_ids, global_filters=global_filters,
                rss_items=rss_items, rss_new_items=rss_new_items,
                standalone_data=standalone_data, schedule=schedule,
                rss_new_urls=rss_new_urls,
            )
        # current 模式需要使用完整的历史数据
        elif self.report_mode == "current":
            analysis_data = self._load_analysis_data()
            if analysis_data:
                (
                    all_results,
                    historical_id_to_name,
                    historical_title_info,
                    historical_new_titles,
                    _,
                    _,
                    _,
                ) = analysis_data

                print(
                    f"current模式：使用过滤后的历史数据，包含平台：{list(all_results.keys())}"
                )

                # 使用历史数据准备独立展示区数据（包含完整的 title_info）
                standalone_data = self._prepare_standalone_data(
                    all_results, historical_id_to_name, historical_title_info, raw_rss_items
                )

                stats, html_file, ai_result, rss_items, standalone_data, rss_new_items = self._run_analysis_pipeline(
                    all_results,
                    self.report_mode,
                    historical_title_info,
                    historical_new_titles,
                    word_groups,
                    filter_words,
                    historical_id_to_name,
                    failed_ids=failed_ids,
                    global_filters=global_filters,
                    rss_items=rss_items,
                    rss_new_items=rss_new_items,
                    standalone_data=standalone_data,
                    schedule=schedule,
                    rss_new_urls=rss_new_urls,
                )

                combined_id_to_name = {**historical_id_to_name, **id_to_name}
                new_titles = historical_new_titles
                id_to_name = combined_id_to_name
                title_info = historical_title_info
                results = all_results
            else:
                if results:
                    print("❌ 严重错误：无法读取刚保存的数据文件")
                    raise RuntimeError("数据一致性检查失败：保存后立即读取失败")

                # RSS-only 模式下热榜数据库为空是正常状态，直接使用本轮
                # RSS 数据继续 AI 筛选、报告生成和通知发送。
                print("current模式：热榜已关闭，使用本轮 RSS 数据继续分析")
                title_info = self._prepare_current_title_info(results, time_info)
                standalone_data = self._prepare_standalone_data(
                    results, id_to_name, title_info, raw_rss_items
                )
                stats, html_file, ai_result, rss_items, standalone_data, rss_new_items = self._run_analysis_pipeline(
                    results,
                    self.report_mode,
                    title_info,
                    new_titles,
                    word_groups,
                    filter_words,
                    id_to_name,
                    failed_ids=failed_ids,
                    global_filters=global_filters,
                    rss_items=rss_items,
                    rss_new_items=rss_new_items,
                    standalone_data=standalone_data,
                    schedule=schedule,
                    rss_new_urls=rss_new_urls,
                )
        elif self.report_mode == "daily":
            # daily 模式：使用全天累计数据
            analysis_data = self._load_analysis_data()
            if analysis_data:
                (
                    all_results,
                    historical_id_to_name,
                    historical_title_info,
                    historical_new_titles,
                    _,
                    _,
                    _,
                ) = analysis_data

                # 使用历史数据准备独立展示区数据（包含完整的 title_info）
                standalone_data = self._prepare_standalone_data(
                    all_results, historical_id_to_name, historical_title_info, raw_rss_items
                )

                stats, html_file, ai_result, rss_items, standalone_data, rss_new_items = self._run_analysis_pipeline(
                    all_results,
                    self.report_mode,
                    historical_title_info,
                    historical_new_titles,
                    word_groups,
                    filter_words,
                    historical_id_to_name,
                    failed_ids=failed_ids,
                    global_filters=global_filters,
                    rss_items=rss_items,
                    rss_new_items=rss_new_items,
                    standalone_data=standalone_data,
                    schedule=schedule,
                    rss_new_urls=rss_new_urls,
                )

                combined_id_to_name = {**historical_id_to_name, **id_to_name}
                new_titles = historical_new_titles
                id_to_name = combined_id_to_name
                title_info = historical_title_info
                results = all_results
            else:
                # 没有历史数据时使用当前数据
                title_info = self._prepare_current_title_info(results, time_info)
                standalone_data = self._prepare_standalone_data(
                    results, id_to_name, title_info, raw_rss_items
                )
                stats, html_file, ai_result, rss_items, standalone_data, rss_new_items = self._run_analysis_pipeline(
                    results,
                    self.report_mode,
                    title_info,
                    new_titles,
                    word_groups,
                    filter_words,
                    id_to_name,
                    failed_ids=failed_ids,
                    global_filters=global_filters,
                    rss_items=rss_items,
                    rss_new_items=rss_new_items,
                    standalone_data=standalone_data,
                    schedule=schedule,
                    rss_new_urls=rss_new_urls,
                )
        else:
            # incremental 模式：只使用当前抓取的数据
            title_info = self._prepare_current_title_info(results, time_info)
            standalone_data = self._prepare_standalone_data(
                results, id_to_name, title_info, raw_rss_items
            )
            stats, html_file, ai_result, rss_items, standalone_data, rss_new_items = self._run_analysis_pipeline(
                results,
                self.report_mode,
                title_info,
                new_titles,
                word_groups,
                filter_words,
                id_to_name,
                failed_ids=failed_ids,
                global_filters=global_filters,
                rss_items=rss_items,
                rss_new_items=rss_new_items,
                standalone_data=standalone_data,
                schedule=schedule,
                rss_new_urls=rss_new_urls,
            )

        if html_file:
            print(f"HTML报告已生成: {html_file}")
            print(f"最新报告已更新: output/html/latest/{self.report_mode}.html")

        has_content = self._has_valid_content(stats, new_titles) or bool(
            rss_items
        )
        if self.report_mode == "daily_delivery" and not has_content:
            print("[每日交付] 快照没有匹配内容，跳过摘要、报告和通知")
            if not schedule.push:
                print("[每日交付] 调度器未启用推送，不记录 push 检查点")
                return True
            if not self._record_delivery_checkpoint(schedule):
                print("[每日交付] 空周期 push 检查点持久化失败")
                return False
            return True

        if self.report_mode == "daily_delivery" and not schedule.push:
            print("[每日交付] 分析完成，调度器未启用推送")
            return True

        # 发送通知
        if mode_strategy["should_send_notification"]:
            # standalone_data 已在分析流水线中翻译，直接复用（不再重新 prepare 原文，
            # 避免覆盖译文、避免重复翻译，并保证网页报告与推送译文一致）
            delivered = self._send_notification_if_needed(
                stats,
                mode_strategy["report_type"],
                self.report_mode,
                failed_ids=failed_ids,
                new_titles=new_titles,
                id_to_name=id_to_name,
                html_file_path=html_file,
                rss_items=rss_items,
                rss_new_items=rss_new_items,
                standalone_data=standalone_data,
                ai_result=ai_result,
                current_results=results,
                schedule=schedule,
            )
            if self.report_mode == "daily_delivery" and not delivered:
                return False
            if (
                self.report_mode == "weekly"
                and schedule
                and schedule.push
                and not delivered
            ):
                return False

        # 打开浏览器（仅在非容器环境）
        if self._should_open_browser() and html_file:
            file_url = "file://" + str(Path(html_file).resolve())
            print(f"正在打开HTML报告: {file_url}")
            webbrowser.open(file_url)
        elif self.is_docker_container and html_file:
            print(f"HTML报告已生成（Docker环境）: {html_file}")

        return True

    def run(self) -> bool:
        """执行分析流程"""
        weekly_attempt_lock = None
        try:
            self._weekly_artifact_contract_expected = None
            self._weekly_pdf_path = None
            self._run_at = self.ctx.get_time()
            self._run_date = self._run_at.strftime("%Y-%m-%d")
            self._run_time_filename = self._run_at.strftime("%H-%M")
            schedule = self._resolve_and_apply_schedule()

            if schedule.report_mode == "weekly":
                manual_weekly_rerun = self._is_manual_weekly_rerun(schedule)
                checkpoint_date = self._delivery_checkpoint_date("weekly")
                attempt_lock = self._create_weekly_attempt_lock(checkpoint_date)
                if not attempt_lock.acquire():
                    print("[周报] 同一自然周已有任务执行中，跳过本次重叠触发")
                    return True
                weekly_attempt_lock = attempt_lock
                scheduler = self.ctx.create_scheduler()
                if (
                    not manual_weekly_rerun
                    and schedule.once_push
                    and schedule.period_key
                    and scheduler.already_executed(
                        schedule.period_key, "push", checkpoint_date
                    )
                ):
                    print("[周报] 本周 PDF 已成功发送，跳过重试")
                    return True
                if manual_weekly_rerun:
                    print(
                        "[周报] 手动强制执行：重新在线采集上一完整自然周、"
                        "重建 PDF 并向全部账号发送"
                    )
                expected_contract_hash = (
                    self._freeze_weekly_artifact_contract()
                )
                if not manual_weekly_rerun:
                    resumed = self._resume_weekly_pdf_delivery(
                        schedule, expected_contract_hash
                    )
                    if resumed is not None:
                        return resumed
                self._agro_weather_report = self._fetch_agro_weather()
                if self._agro_weather_report is None:
                    raise RuntimeError("本期全国农业气象周报尚未发布")

            if not self._initialize_and_check_config():
                return True
            if not schedule.collect:
                print("[调度] 当前不执行采集")
                return True

            # 抓取热榜数据
            results, id_to_name, failed_ids = self._crawl_data()

            # 抓取 RSS 数据（如果启用），返回统计条目、新增条目和原始条目
            rss_items, rss_new_items, raw_rss_items, rss_new_urls = self._crawl_rss_data()

            if (
                schedule.report_mode == "daily_delivery"
                and getattr(self, "_daily_delivery_rss_data", None) is None
                and not getattr(self, "_rss_ids_authoritative", False)
            ):
                raise RuntimeError("每日交付未保存当前 RSS 数据")

            if schedule.report_mode == "daily_delivery" and failed_ids:
                failed = ", ".join(str(source_id) for source_id in failed_ids)
                raise RuntimeError(f"每日交付热榜来源失败: {failed}")

            if (
                schedule.report_mode == "daily_delivery"
                and schedule.once_push
                and schedule.period_key
                and self.ctx.create_scheduler().already_executed(
                    schedule.period_key, "push", self._operation_date()
                )
            ):
                print("[每日交付] 今天已成功交付，保留采集结果并跳过分析和通知")
                return True

            if not schedule.analyze and not schedule.push:
                print("[调度] 静默采集完成，本次不执行 AI 和推送")
                return True

            if (
                schedule.report_mode == "daily_delivery"
                and getattr(self, "_daily_delivery_rss_data", None) is not None
            ):
                (
                    rss_items,
                    rss_new_items,
                    raw_rss_items,
                    rss_new_urls,
                ) = self._process_rss_data_by_mode(
                    self._daily_delivery_rss_data
                )

            if (
                schedule.report_mode == "daily_delivery"
                and not getattr(self, "_rss_ids_authoritative", False)
            ):
                raise RuntimeError("每日交付未生成权威 RSS 快照")

            if (
                schedule.report_mode == "daily_delivery"
                and raw_rss_items == []
            ):
                print("[每日交付] 快照为空，跳过分析、报告和通知")
                if not schedule.push:
                    print("[每日交付] 调度器未启用推送，不记录 push 检查点")
                    return True
                if not self._record_delivery_checkpoint(schedule):
                    print("[每日交付] 空快照 push 检查点持久化失败")
                    return False
                return True

            if (
                schedule.report_mode == "weekly"
                and getattr(self, "_rss_window", None) is None
            ):
                if self._agro_weather_report is None:
                    raise RuntimeError("周报没有入选新闻和农业气象报告，无法生成 PDF")
                print("[周报] 上一自然周没有普通新闻，生成仅含气象专栏的 PDF")

            # 执行模式策略，传递 RSS 数据用于合并推送
            return self._execute_mode_strategy(
                self._get_mode_strategy(), results, id_to_name, failed_ids,
                rss_items=rss_items, rss_new_items=rss_new_items,
                raw_rss_items=raw_rss_items, rss_new_urls=rss_new_urls,
                schedule=schedule,
            )

        except Exception as e:
            print(f"分析流程执行出错: {e}")
            if self.ctx.config.get("DEBUG", False):
                raise
            return False
        finally:
            if weekly_attempt_lock is not None:
                weekly_attempt_lock.release()
            # 清理资源（包括过期数据清理和数据库连接关闭）
            self.ctx.cleanup()


def main():
    """主程序入口"""
    parser = argparse.ArgumentParser(
        description="TrendRadar - 热点新闻聚合与分析工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
调度状态命令:
  --show-schedule        显示当前调度状态（时间段、行为开关）
  --force-weekly         忽略本周完成状态，重新采集上一自然周并再次推送
诊断命令:
  --doctor               运行环境与配置体检
  --test-notification    发送测试通知到已配置渠道

示例:
  python -m trendradar                    # 正常运行
  python -m trendradar --show-schedule    # 查看当前调度状态
  python -m trendradar --force-weekly     # 重新采集上一自然周、重建 PDF 并再次发送
  python -m trendradar --doctor           # 运行一键体检
  python -m trendradar --test-notification # 测试通知渠道连通性
"""
    )
    parser.add_argument("--show-schedule", action="store_true", help="显示当前调度状态")
    parser.add_argument("--doctor", action="store_true", help="运行环境与配置体检")
    parser.add_argument("--test-notification", action="store_true", help="发送测试通知到已配置渠道")
    parser.add_argument(
        "--force-weekly",
        action="store_true",
        help="忽略已有周期结果，重新采集上一自然周并再次推送农业周报",
    )

    args = parser.parse_args()

    debug_mode = False
    try:
        if args.doctor:
            ok = run_doctor()
            if not ok:
                raise SystemExit(1)
            return

        config = load_config()

        if args.show_schedule:
            handle_status_commands(config)
            return

        if args.test_notification:
            ok = run_test_notification(config)
            if not ok:
                raise SystemExit(1)
            return

        version_url = config.get("VERSION_CHECK_URL", "")
        configs_version_url = config.get("CONFIGS_VERSION_CHECK_URL", "")

        need_update = False
        remote_version = None
        if version_url:
            need_update, remote_version = check_all_versions(version_url, configs_version_url)

        analyzer = NewsAnalyzer(config=config, force_weekly=args.force_weekly)

        if analyzer.is_github_actions and need_update and remote_version:
            analyzer.update_info = {
                "current_version": __version__,
                "remote_version": remote_version,
            }

        debug_mode = analyzer.ctx.config.get("DEBUG", False)
        if not analyzer.run():
            raise SystemExit(1)
    except FileNotFoundError as e:
        print(f"❌ 配置文件错误: {e}")
        print("\n请确保以下文件存在:")
        print("  • config/config.yaml")
        print("  • config/frequency_words.txt")
        print("\n参考项目文档进行正确配置")
    except Exception as e:
        print(f"❌ 程序运行错误: {e}")
        if debug_mode:
            raise


if __name__ == "__main__":
    main()
