# coding=utf-8
"""
AI 筛选流水线

从 context.py 提取的完整 AI 筛选业务流程：
标签管理 → 待分类新闻收集 → 批量 AI 分类 → 结果保存 → 报告数据转换
"""

from concurrent.futures import ThreadPoolExecutor
from threading import local
from typing import Any, Callable, Dict, List, Optional

from trendradar.ai.filter import AIFilter, AIFilterResult
from trendradar.ai.source_evidence import classify_source_evidence
from trendradar.crawler.article_content import ArticleContentFetcher
from trendradar.crawler.news_search import NEWS_SEARCH_PROVIDERS, canonicalize_url
from trendradar.core.weekly import NaturalWeekWindow
from trendradar.utils.article_links import build_reader_url
from trendradar.utils.time import (
    DEFAULT_TIMEZONE,
    convert_time_for_display,
    format_iso_time_friendly,
)

SEARCH_FEED_ID = "agri-breeding-search"


def is_agricultural_news_search_item(item: Dict[str, Any]) -> bool:
    """Identify synthetic search items from persisted provenance metadata."""
    if item.get("source_id") != SEARCH_FEED_ID:
        return False
    topic = str(item.get("search_topic") or "").strip()
    provider_value = str(item.get("search_providers") or "")
    providers = {
        provider.strip()
        for provider in provider_value.split(",")
        if provider.strip()
    }
    return bool(topic and providers and providers <= NEWS_SEARCH_PROVIDERS)


class AIFilterPipeline:
    """AI 筛选流水线，编排标签提取、批量分类、结果存储的完整流程"""

    def __init__(
        self,
        config: Dict[str, Any],
        storage_manager: Any,
        get_time_func: Callable,
        rss_window: Optional[NaturalWeekWindow] = None,
        allowed_rss_ids: Optional[set[int]] = None,
        rss_ids_authoritative: bool = False,
        strict: bool = False,
        operation_date: Optional[str] = None,
    ):
        self.config = config
        self.storage = storage_manager
        self.get_time = get_time_func

        self._ai_config = config.get("AI", {})
        self._filter_config = config.get("AI_FILTER", {})
        self._debug = config.get("DEBUG", False)

        rss_config = config.get("RSS", {})
        self._rss_enabled = rss_config.get("ENABLED", False)
        self._rss_feeds = rss_config.get("FEEDS", [])
        self._rss_use_proxy = rss_config.get("USE_PROXY", False)
        self._rss_proxy_url = rss_config.get("PROXY_URL", "")
        self._content_config = self._filter_config.get("CONTENT_ENRICHMENT", {})

        news_search_config = rss_config.get("NEWS_SEARCH", {})
        if not isinstance(news_search_config, dict):
            news_search_config = {}
        try:
            self._max_search_hotspots = max(
                1,
                int(news_search_config.get("MAX_HOTSPOTS", 5)),
            )
        except (TypeError, ValueError):
            self._max_search_hotspots = 5

        self._timezone = config.get("TIMEZONE", DEFAULT_TIMEZONE)
        self._rss_window = rss_window
        self._allowed_rss_ids = (
            frozenset(allowed_rss_ids) if allowed_rss_ids is not None else None
        )
        self._rss_ids_authoritative = rss_ids_authoritative
        self._strict = strict
        self._operation_date = operation_date
        if self._strict and self._operation_date is None:
            self._operation_date = self.get_time().strftime("%Y-%m-%d")

        self._priority_sort_enabled = config.get("FILTER", {}).get("PRIORITY_SORT_ENABLED", False)
        self._rank_threshold = config.get("RANK_THRESHOLD", 50)
        self._max_news = config.get("MAX_NEWS_PER_KEYWORD", 0)

    def _is_rss_item_in_scope(self, item: dict) -> bool:
        if self._allowed_rss_ids is not None:
            item_id = item.get("news_item_id")
            if item_id is None:
                item_id = item.get("id")
            return item_id in self._allowed_rss_ids
        if self._rss_window is not None:
            return self._rss_window.contains(str(item.get("published_at") or ""))
        return True

    def run(self, interests_file: Optional[str] = None) -> Optional[AIFilterResult]:
        """
        执行 AI 智能筛选完整流程

        1. 读取兴趣描述文件，计算 hash
        2. 对比数据库 prompt_hash，决定是否重新提取标签
        3. 收集待分类新闻（去重）
        4. 按 batch_size 分组调用 AI 分类
        5. 保存结果
        6. 查询 active 结果，按标签分组返回
        """
        filter_config = self._filter_config

        ai_filter = AIFilter(self._ai_config, filter_config, self.get_time, self._debug)

        configured_interests = interests_file or filter_config.get("INTERESTS_FILE")
        effective_interests_file = configured_interests or "ai_interests.txt"

        if self._debug:
            print(f"[AI筛选][DEBUG] === 配置信息 ===")
            print(f"[AI筛选][DEBUG] 存储后端: {self.storage.backend_name}")
            print(f"[AI筛选][DEBUG] batch_size={filter_config.get('BATCH_SIZE', 200)}, "
                  f"batch_interval={filter_config.get('BATCH_INTERVAL', 5)}")
            print(f"[AI筛选][DEBUG] interests_file={effective_interests_file}")
            print(f"[AI筛选][DEBUG] prompt_file={filter_config.get('PROMPT_FILE', 'prompt.txt')}")
            print(f"[AI筛选][DEBUG] extract_prompt_file={filter_config.get('EXTRACT_PROMPT_FILE', 'extract_prompt.txt')}")

        # 1. 读取兴趣描述
        interests_content = ai_filter.load_interests_content(configured_interests)
        if not interests_content:
            return AIFilterResult(success=False, error="兴趣描述文件为空或不存在")

        current_hash = ai_filter.compute_interests_hash(interests_content, effective_interests_file)

        if self._debug:
            print(f"[AI筛选][DEBUG] 兴趣描述 hash: {current_hash}")
            print(f"[AI筛选][DEBUG] 兴趣描述内容 ({len(interests_content)} 字符):\n{interests_content}")

        # 2. 开启批量模式
        self._batch_commit_attempted = False
        self.storage.begin_batch()

        # 3. 检查提示词是否变更。严格模式使用同一事务标签快照，
        # hash 变化时只允许全量原子替换，禁止复用 fail-soft 增量路径。
        if self._strict:
            try:
                tag_snapshot = self.storage.get_ai_filter_tag_snapshot_strict(
                    date=self._operation_date,
                    interests_file=effective_interests_file
                )
                stored_hash = tag_snapshot.get("prompt_hash")
                if stored_hash != current_hash:
                    tags_data = ai_filter.extract_tags(interests_content)
                    if not tags_data:
                        raise RuntimeError("严格 AI 标签提取未返回标签")
                    tags_data = _with_ordered_priorities(
                        tags_data, start_priority=1
                    )
                    new_version = int(
                        tag_snapshot.get("latest_version", 0)
                    ) + 1
                    tag_snapshot = self.storage.replace_ai_filter_tags_strict(
                        tags_data,
                        new_version,
                        current_hash,
                        date=self._operation_date,
                        interests_file=effective_interests_file,
                    )
                    active_tags = self._validate_strict_tag_snapshot(
                        tag_snapshot,
                        current_hash,
                        expected_tags=tags_data,
                        expected_version=new_version,
                    )
                else:
                    active_tags = self._validate_strict_tag_snapshot(
                        tag_snapshot, current_hash
                    )
            except Exception as exc:
                cleanup_exc = self._end_batch_after_storage_error()
                error = (
                    "严格 AI 标签生命周期失败: "
                    f"{type(exc).__name__}: {exc}"
                )
                error = self._append_batch_cleanup_error(
                    error, cleanup_exc
                )
                return AIFilterResult(
                    success=False,
                    error=error,
                )
        else:
            stored_hash = self.storage.get_latest_prompt_hash(
                interests_file=effective_interests_file
            )

        if self._debug:
            print(f"[AI筛选][DEBUG] 数据库存储 hash: {stored_hash}")
            print(f"[AI筛选][DEBUG] hash 对比: stored={stored_hash} vs current={current_hash} → {'匹配' if stored_hash == current_hash else '不匹配'}")

        if not self._strict and stored_hash != current_hash:
            self._handle_tag_update(
                ai_filter, interests_content, current_hash, stored_hash,
                effective_interests_file, filter_config,
            )

        # 获取当前 active 标签
        if not self._strict:
            active_tags = self.storage.get_active_ai_filter_tags(
                interests_file=effective_interests_file
            )
        if self._debug:
            print(f"[AI筛选][DEBUG] 从数据库获取 active 标签: {len(active_tags)} 个")
            for t in active_tags:
                print(f"[AI筛选][DEBUG]   id={t['id']} tag={t['tag']} priority={t.get('priority', 9999)} version={t.get('version')} hash={t.get('prompt_hash', '')[:8]}...")

        if not active_tags:
            self.storage.end_batch()
            return AIFilterResult(success=False, error="没有可用的标签")

        print(f"[AI筛选] 使用 {len(active_tags)} 个标签")

        # 4. 收集待分类新闻。严格模式下存储读取失败不能降级成空集合。
        try:
            pending_news, pending_rss, all_news, analyzed_hotlist, all_rss, analyzed_rss, scope_filtered_rss = self._collect_pending_news(effective_interests_file)
        except Exception as exc:
            cleanup_exc = self._end_batch_after_storage_error()
            error = (
                f"严格 AI 存储读取失败: {type(exc).__name__}: {exc}"
            )
            error = self._append_batch_cleanup_error(error, cleanup_exc)
            return AIFilterResult(
                success=False,
                error=error,
            )

        self._print_pending_stats(
            all_news, analyzed_hotlist, pending_news,
            all_rss, analyzed_rss, pending_rss, scope_filtered_rss,
        )

        total_pending = len(pending_news) + len(pending_rss)
        if total_pending == 0:
            print("[AI筛选] 没有新增新闻需要分类")

        # 5. 批量分类
        total_results, succeeded_news_ids, succeeded_rss_ids = self._classify_batches(
            ai_filter, pending_news, pending_rss, active_tags, interests_content, filter_config,
        )
        scoped_batch_failed = (
            (self._rss_window is not None or self._allowed_rss_ids is not None)
            and len(succeeded_news_ids) + len(succeeded_rss_ids) < total_pending
        )

        if scoped_batch_failed:
            try:
                if self._strict:
                    self.storage.abort_batch()
                else:
                    self._end_batch()
            except Exception as exc:
                return AIFilterResult(
                    success=False,
                    error=(
                        "范围内 AI 分类批次失败，且批次回滚失败: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
            return AIFilterResult(
                success=False,
                error="范围内 AI 分类批次失败，已拒绝使用部分结果",
            )

        # 6. 保存结果并结束批量模式。严格模式要求事务写入和持久化均成功。
        try:
            self._save_results(
                total_results, succeeded_news_ids, succeeded_rss_ids,
                effective_interests_file, current_hash,
            )
            batch_result = self._end_batch()
            # 第三方旧后端可能仍返回 None；仅明确 False 代表延迟
            # CAS/PUT 已失败，不能把本地 rowcount 当成持久化成功。
            if batch_result is False:
                raise RuntimeError("AI 批次最终持久化失败")
        except Exception as exc:
            cleanup_exc = self._end_batch_after_storage_error()
            error = (
                f"{'严格 ' if self._strict else ''}AI 存储持久化失败: "
                f"{type(exc).__name__}: {exc}"
            )
            error = self._append_batch_cleanup_error(error, cleanup_exc)
            return AIFilterResult(
                success=False,
                error=error,
            )

        # 8. 查询并组装返回结果
        try:
            if self._strict:
                all_results = self.storage.get_active_ai_filter_results_strict(
                    date=self._operation_date,
                    interests_file=effective_interests_file
                )
            else:
                all_results = self.storage.get_active_ai_filter_results(
                    interests_file=effective_interests_file
                )
        except Exception as exc:
            return AIFilterResult(
                success=False,
                error=f"严格 AI 存储读取失败: {type(exc).__name__}: {exc}",
            )

        if self._strict:
            expected_result_keys = {
                (
                    result["news_item_id"],
                    result.get("source_type", "hotlist"),
                    result["tag_id"],
                    result["module_type"],
                    result["species_scope"],
                )
                for result in total_results
            }
            current_ids = {
                (news_item_id, "hotlist")
                for news_item_id in succeeded_news_ids
            } | {
                (news_item_id, "rss")
                for news_item_id in succeeded_rss_ids
            }
            all_results = [
                result for result in all_results
                if (result.get("news_item_id"), result.get("source_type"))
                in current_ids
            ]
            actual_result_keys = [
                (
                    result.get("news_item_id"),
                    result.get("source_type"),
                    result.get("tag_id"),
                    result.get("module_type"),
                    result.get("species_scope"),
                )
                for result in all_results
            ]
            if (
                len(actual_result_keys) != len(set(actual_result_keys))
                or set(actual_result_keys) != expected_result_keys
            ):
                return AIFilterResult(
                    success=False,
                    error="严格 AI 存储读回结果与本轮匹配集合不一致",
                )
        all_results = [
            result
            for result in all_results
            if (
                not self._rss_ids_authoritative
                or result.get("source_type") == "rss"
            )
            and (
                result.get("source_type") != "rss"
                or self._is_rss_item_in_scope(result)
            )
        ]

        if self._debug:
            print(f"[AI筛选][DEBUG] === 最终汇总 ===")
            print(f"[AI筛选][DEBUG] 数据库 active 分类结果: {len(all_results)} 条")
            tag_counts: dict = {}
            for r in all_results:
                tag_name = r.get("tag", "?")
                src_type = r.get("source_type", "?")
                key = f"{tag_name}({src_type})"
                tag_counts[key] = tag_counts.get(key, 0) + 1
            for key, count in sorted(tag_counts.items()):
                print(f"[AI筛选][DEBUG]   {key}: {count} 条")

        return self._build_filter_result(all_results, active_tags, total_pending)

    @staticmethod
    def _validate_strict_tag_snapshot(
        snapshot: Dict,
        current_hash: str,
        expected_tags: Optional[List[Dict]] = None,
        expected_version: Optional[int] = None,
    ) -> List[Dict]:
        if not isinstance(snapshot, dict):
            raise RuntimeError("严格 AI 标签读回不是快照对象")
        tags = snapshot.get("tags")
        if not isinstance(tags, list) or not tags:
            raise RuntimeError("严格 AI 标签保存/读回数量为 0")
        if snapshot.get("prompt_hash") != current_hash:
            raise RuntimeError("严格 AI 标签读回 prompt_hash 不一致")
        version = snapshot.get("version")
        if expected_version is not None and version != expected_version:
            raise RuntimeError("严格 AI 标签读回 version 不一致")

        normalized = []
        seen_names = set()
        seen_priorities = set()
        for tag in tags:
            if not isinstance(tag, dict):
                raise RuntimeError("严格 AI 标签读回包含非法项")
            name = str(tag.get("tag", "")).strip()
            description = str(tag.get("description", "")).strip()
            try:
                priority = int(tag["priority"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("严格 AI 标签读回 priority 无效") from exc
            if (
                not name
                or name in seen_names
                or priority in seen_priorities
                or tag.get("prompt_hash") != current_hash
                or tag.get("version") != version
            ):
                raise RuntimeError("严格 AI 标签读回 active 集合不一致")
            seen_names.add(name)
            seen_priorities.add(priority)
            normalized.append((name, description, priority))
        if [item[2] for item in normalized] != sorted(seen_priorities):
            raise RuntimeError("严格 AI 标签读回顺序不一致")
        if expected_tags is not None:
            expected = [
                (
                    str(tag.get("tag", "")).strip(),
                    str(tag.get("description", "")).strip(),
                    int(tag.get("priority", index)),
                )
                for index, tag in enumerate(expected_tags, start=1)
            ]
            if normalized != expected:
                raise RuntimeError("严格 AI 标签读回集合/描述/priority 不一致")
        return tags

    def _end_batch(self) -> Optional[bool]:
        self._batch_commit_attempted = True
        if self._strict:
            return self.storage.end_batch_strict()
        return self.storage.end_batch()

    def _end_batch_after_storage_error(self) -> Optional[Exception]:
        """错误时严格回滚；普通模式保留既有 fail-soft 关闭语义。"""
        try:
            if self._strict:
                self.storage.abort_batch()
            elif not getattr(self, "_batch_commit_attempted", False):
                self._end_batch()
        except Exception as exc:
            return exc
        return None

    @staticmethod
    def _append_batch_cleanup_error(
        error: str, cleanup_exc: Optional[Exception]
    ) -> str:
        if cleanup_exc is None:
            return error
        return (
            f"{error}; 批次回滚/关闭失败: "
            f"{type(cleanup_exc).__name__}: {cleanup_exc}"
        )

    def _handle_tag_update(
        self,
        ai_filter: AIFilter,
        interests_content: str,
        current_hash: str,
        stored_hash: Optional[str],
        effective_interests_file: str,
        filter_config: Dict,
    ) -> None:
        new_version = self.storage.get_latest_ai_filter_tag_version() + 1
        threshold = filter_config.get("RECLASSIFY_THRESHOLD", 0.6)

        if stored_hash is None:
            print(f"[AI筛选] 首次运行 ({effective_interests_file})，提取标签...")
            tags_data = ai_filter.extract_tags(interests_content)
            if not tags_data:
                self.storage.end_batch()
                raise _TagExtractionError()
            tags_data = _with_ordered_priorities(tags_data, start_priority=1)
            saved_count = self.storage.save_ai_filter_tags(tags_data, new_version, current_hash, interests_file=effective_interests_file)
            print(f"[AI筛选] 已保存 {saved_count} 个标签 (版本 {new_version})")
            return

        old_tags = self.storage.get_active_ai_filter_tags(interests_file=effective_interests_file)
        update_result = ai_filter.update_tags(old_tags, interests_content)

        if update_result is None:
            print(f"[AI筛选] AI 标签更新失败，回退到重新提取")
            tags_data = ai_filter.extract_tags(interests_content)
            if not tags_data:
                self.storage.end_batch()
                raise _TagExtractionError()
            tags_data = _with_ordered_priorities(tags_data, start_priority=1)
            deprecated_count = self.storage.deprecate_all_ai_filter_tags(interests_file=effective_interests_file)
            self.storage.clear_analyzed_news(interests_file=effective_interests_file)
            saved_count = self.storage.save_ai_filter_tags(tags_data, new_version, current_hash, interests_file=effective_interests_file)
            print(f"[AI筛选] 废弃 {deprecated_count} 个旧标签, 保存 {saved_count} 个新标签 (版本 {new_version})")
            return

        change_ratio = update_result["change_ratio"]
        keep_tags = update_result["keep"]
        add_tags = update_result["add"]
        remove_tags = update_result["remove"]

        if self._debug:
            print(f"[AI筛选][DEBUG] AI 标签更新: keep={len(keep_tags)}, add={len(add_tags)}, remove={len(remove_tags)}, change_ratio={change_ratio:.2f}, threshold={threshold:.2f}")

        if change_ratio >= threshold:
            print(f"[AI筛选] 兴趣文件变更: {effective_interests_file} (AI change_ratio={change_ratio:.2f} >= threshold={threshold:.2f} → 全量重分类)")
            tags_data = ai_filter.extract_tags(interests_content)
            if not tags_data:
                self.storage.end_batch()
                raise _TagExtractionError()
            tags_data = _with_ordered_priorities(tags_data, start_priority=1)
            deprecated_count = self.storage.deprecate_all_ai_filter_tags(interests_file=effective_interests_file)
            self.storage.clear_analyzed_news(interests_file=effective_interests_file)
            saved_count = self.storage.save_ai_filter_tags(tags_data, new_version, current_hash, interests_file=effective_interests_file)
            print(f"[AI筛选] 废弃 {deprecated_count} 个旧标签, 保存 {saved_count} 个新标签 (版本 {new_version})")
        else:
            self._apply_incremental_update(
                old_tags, keep_tags, add_tags, remove_tags,
                change_ratio, threshold, new_version, current_hash,
                effective_interests_file,
            )

    def _apply_incremental_update(
        self,
        old_tags, keep_tags, add_tags, remove_tags,
        change_ratio, threshold, new_version, current_hash,
        effective_interests_file,
    ) -> None:
        print(f"[AI筛选] 兴趣文件变更: {effective_interests_file} (AI change_ratio={change_ratio:.2f} < threshold={threshold:.2f} → 增量更新)")
        print(f"[AI筛选]   保留 {len(keep_tags)} 个标签, 新增 {len(add_tags)} 个, 废弃 {len(remove_tags)} 个")

        if remove_tags:
            remove_set = set(remove_tags)
            removed_ids = [t["id"] for t in old_tags if t["tag"] in remove_set]
            if removed_ids:
                self.storage.deprecate_specific_ai_filter_tags(removed_ids)
                if self._debug:
                    print(f"[AI筛选][DEBUG] 废弃标签 IDs: {removed_ids}")

        keep_with_priority = []
        if keep_tags:
            self.storage.update_ai_filter_tag_descriptions(keep_tags, interests_file=effective_interests_file)
            keep_with_priority = _with_ordered_priorities(keep_tags, start_priority=1)
            self.storage.update_ai_filter_tag_priorities(keep_with_priority, interests_file=effective_interests_file)

        if add_tags:
            add_start = keep_with_priority[-1]["priority"] + 1 if keep_with_priority else 1
            add_with_priority = _with_ordered_priorities(add_tags, start_priority=add_start)
            saved_count = self.storage.save_ai_filter_tags(add_with_priority, new_version, current_hash, interests_file=effective_interests_file)
            if self._debug:
                print(f"[AI筛选][DEBUG] 新增保存 {saved_count} 个标签")

        self.storage.update_ai_filter_tags_hash(effective_interests_file, current_hash)

        cleared = self.storage.clear_analyzed_news(
            interests_file=effective_interests_file
        )
        if cleared > 0:
            print(
                f"[AI筛选]   清除 {cleared} 条旧规则下的分析记录，"
                "将在新规则下重新分析"
            )

    def _collect_pending_news(self, effective_interests_file: str):
        if self._rss_ids_authoritative:
            all_news = []
            analyzed_hotlist = set()
            pending_news = []
        else:
            all_news = self.storage.get_all_news_ids()
            analyzed_hotlist = self.storage.get_analyzed_news_ids(
                "hotlist", interests_file=effective_interests_file
            )
            pending_news = [
                n for n in all_news if n["id"] not in analyzed_hotlist
            ]

        pending_rss = []
        scope_filtered_rss = 0
        all_rss = []
        analyzed_rss = set()

        if self._rss_enabled:
            if self._rss_ids_authoritative:
                all_rss = self.storage.get_all_rss_ids_strict(
                    date=self._operation_date
                )
            else:
                all_rss = self.storage.get_all_rss_ids()

            scoped_rss = []
            for n in all_rss:
                if not self._is_rss_item_in_scope(n):
                    scope_filtered_rss += 1
                    continue
                scoped_rss.append(n)

            if self._strict and self._rss_ids_authoritative:
                # 即使严格模式不复用缓存，也必须证明 analyzed 表可读。
                self.storage.get_analyzed_news_ids_strict(
                    "rss",
                    date=self._operation_date,
                    interests_file=effective_interests_file,
                )
                pending_rss = scoped_rss
                analyzed_rss = set()
            else:
                analyzed_rss = self.storage.get_analyzed_news_ids(
                    "rss", interests_file=effective_interests_file
                )
                pending_rss = [
                    n for n in scoped_rss if n["id"] not in analyzed_rss
                ]

        return pending_news, pending_rss, all_news, analyzed_hotlist, all_rss, analyzed_rss, scope_filtered_rss

    def _print_pending_stats(self, all_news, analyzed_hotlist, pending_news, all_rss, analyzed_rss, pending_rss, scope_filtered_rss):
        hotlist_total = len(all_news)
        hotlist_skipped = len(analyzed_hotlist)
        hotlist_pending = len(pending_news)
        print(f"[AI筛选] 热榜: 总计 {hotlist_total} 条, 已分析跳过 {hotlist_skipped} 条, 本次发送AI分析 {hotlist_pending} 条")
        if self._rss_enabled:
            rss_total = len(all_rss)
            rss_skipped = len(analyzed_rss)
            rss_pending = len(pending_rss)
            scope_info = f", 范围过滤 {scope_filtered_rss} 条" if scope_filtered_rss > 0 else ""
            print(f"[AI筛选] RSS: 总计 {rss_total} 条{scope_info}, 已分析跳过 {rss_skipped} 条, 本次发送AI分析 {rss_pending} 条")

    def _classify_batches(self, ai_filter, pending_news, pending_rss, active_tags, interests_content, filter_config):
        batch_size = filter_config.get("BATCH_SIZE", 200)
        batch_interval = filter_config.get("BATCH_INTERVAL", 5)
        total_results = []
        batch_count = 0

        pending_news = self._enrich_pending_items(pending_news, "热榜")
        pending_rss = self._enrich_pending_items(pending_rss, "RSS")
        pending_news = self._assign_module_evidence(pending_news)
        pending_rss = self._assign_module_evidence(pending_rss)

        def classify(titles_for_ai):
            result = ai_filter.classify_batch(
                titles_for_ai,
                active_tags,
                interests_content,
                strict=getattr(self, "_strict", False),
            )
            if result is None and getattr(self, "_strict", False):
                print("[AI筛选] 严格批次首次失败，立即重试本批次一次")
                result = ai_filter.classify_batch(
                    titles_for_ai,
                    active_tags,
                    interests_content,
                    strict=True,
                )
            return result

        succeeded_news_ids = []
        for i in range(0, len(pending_news), batch_size):
            if batch_count > 0 and batch_interval > 0:
                import time
                print(f"[AI筛选] 批次间隔等待 {batch_interval} 秒...")
                time.sleep(batch_interval)
            batch = pending_news[i:i + batch_size]
            titles_for_ai = [
                {
                    "id": n["id"],
                    "title": n["title"],
                    "source": n.get("source_name", ""),
                    "url": n.get("url", ""),
                    "content": n.get("content", n["title"]),
                    "content_level": n.get("content_level", "title_only"),
                    "risk_warning": n.get("risk_warning", ""),
                    "module_type": n["module_type"],
                    "module_reason": n["module_reason"],
                }
                for n in batch
            ]
            batch_results = classify(titles_for_ai)
            batch_count += 1
            if batch_results is None:
                print(f"[AI筛选] 热榜批次 {i // batch_size + 1}: {len(batch)} 条 → 分类失败，将在下次运行重试")
                continue
            for r in batch_results:
                r["source_type"] = "hotlist"
            total_results.extend(batch_results)
            succeeded_news_ids.extend(n["id"] for n in batch)
            print(f"[AI筛选] 热榜批次 {i // batch_size + 1}: {len(batch)} 条 → {len(batch_results)} 条匹配")

        succeeded_rss_ids = []
        for i in range(0, len(pending_rss), batch_size):
            if batch_count > 0 and batch_interval > 0:
                import time
                print(f"[AI筛选] 批次间隔等待 {batch_interval} 秒...")
                time.sleep(batch_interval)
            batch = pending_rss[i:i + batch_size]
            titles_for_ai = [
                {
                    "id": n["id"],
                    "title": n["title"],
                    "source": n.get("source_name", ""),
                    "url": n.get("url", ""),
                    "content": n.get("content", n.get("summary") or n["title"]),
                    "content_level": n.get("content_level", "title_only"),
                    "risk_warning": n.get("risk_warning", ""),
                    "module_type": n["module_type"],
                    "module_reason": n["module_reason"],
                }
                for n in batch
            ]
            batch_results = classify(titles_for_ai)
            batch_count += 1
            if batch_results is None:
                print(f"[AI筛选] RSS 批次 {i // batch_size + 1}: {len(batch)} 条 → 分类失败，将在下次运行重试")
                continue
            for r in batch_results:
                r["source_type"] = "rss"
            total_results.extend(batch_results)
            succeeded_rss_ids.extend(n["id"] for n in batch)
            print(f"[AI筛选] RSS 批次 {i // batch_size + 1}: {len(batch)} 条 → {len(batch_results)} 条匹配")

        return total_results, succeeded_news_ids, succeeded_rss_ids

    def _assign_module_evidence(self, items: List[Dict]) -> List[Dict]:
        """Freeze report modules before any AI relevance decision."""
        source_categories = {
            str(feed.get("id") or "").strip(): str(
                feed.get("content_category") or ""
            ).strip()
            for feed in self._rss_feeds
            if isinstance(feed, dict) and str(feed.get("id") or "").strip()
        }
        assigned = []
        for item in items:
            enriched = dict(item)
            evidence = classify_source_evidence(enriched, source_categories)
            enriched["module_type"] = evidence.module_type
            enriched["module_reason"] = evidence.reason
            assigned.append(enriched)
        return assigned

    def _enrich_pending_items(self, items: List[Dict], label: str) -> List[Dict]:
        """仅为本次尚未分析的记录抓正文，并携带明确的降级风险。"""
        if not items:
            return []

        config = self._content_config
        enabled = config.get("ENABLED", True)
        fetch_full_text = config.get("FETCH_FULL_TEXT", True)
        concurrency = max(1, min(16, int(config.get("CONCURRENCY", 4))))
        thread_state = local()

        def get_fetcher() -> ArticleContentFetcher:
            fetcher = getattr(thread_state, "fetcher", None)
            if fetcher is None:
                fetcher = ArticleContentFetcher(
                    timeout=config.get("TIMEOUT", 12),
                    max_content_chars=config.get("MAX_CONTENT_CHARS", 5000),
                    min_body_chars=config.get("MIN_BODY_CHARS", 300),
                    use_proxy=self._rss_use_proxy,
                    proxy_url=self._rss_proxy_url,
                    elsevier_api_key=config.get("ELSEVIER_API_KEY", ""),
                    elsevier_inst_token=config.get("ELSEVIER_INST_TOKEN", ""),
                )
                thread_state.fetcher = fetcher
            return fetcher

        def enrich(item: Dict) -> Dict:
            enriched = dict(item)
            request_item = enriched
            if not enabled or not fetch_full_text:
                request_item = dict(enriched)
                request_item["url"] = ""
                request_item["mobile_url"] = ""
            try:
                result = get_fetcher().get(request_item)
                enriched.update({
                    "content": result.text,
                    "content_level": result.level,
                    "risk_warning": result.risk_warning,
                    "content_fetch_status": result.fetch_status,
                })
            except Exception as exc:
                enriched.update({
                    "content": enriched.get("summary") or enriched.get("title", ""),
                    "content_level": "summary" if enriched.get("summary") else "title_only",
                    "risk_warning": (
                        f"内容提取异常（{type(exc).__name__}）；当前依据"
                        f"{'摘要' if enriched.get('summary') else '标题'}判断，可靠性受限。"
                    ),
                    "content_fetch_status": "unexpected_error",
                })
            return enriched

        if concurrency == 1:
            enriched_items = [enrich(item) for item in items]
        else:
            with ThreadPoolExecutor(
                max_workers=concurrency,
                thread_name_prefix="article-content",
            ) as executor:
                enriched_items = list(executor.map(enrich, items))

        counts: Dict[str, int] = {}
        for item in enriched_items:
            level = item.get("content_level", "title_only")
            counts[level] = counts.get(level, 0) + 1
        print(
            f"[AI筛选] {label}内容层级: 正文 {counts.get('full_text', 0)}，"
            f"摘要 {counts.get('summary', 0)}，仅标题 {counts.get('title_only', 0)}"
        )
        return enriched_items

    def _save_results(self, total_results, succeeded_news_ids, succeeded_rss_ids, effective_interests_file, current_hash):
        if self._strict:
            status = self.storage.replace_ai_filter_batch_strict(
                total_results,
                succeeded_news_ids,
                succeeded_rss_ids,
                effective_interests_file,
                current_hash,
                date=self._operation_date,
            )
            expected_analyzed = len(set(succeeded_news_ids)) + len(
                set(succeeded_rss_ids)
            )
            if (
                not isinstance(status, dict)
                or status.get("results") != len(total_results)
                or status.get("analyzed") != expected_analyzed
            ):
                raise RuntimeError(
                    "严格 AI 批次保存数量不一致: "
                    f"expected=({len(total_results)}, {expected_analyzed}), "
                    f"actual={status!r}"
                )
            print(
                f"[AI筛选] 严格保存 {status['results']} 条分类结果、"
                f"{status['analyzed']} 条分析状态"
            )
            return

        if total_results:
            saved = self.storage.save_ai_filter_results(total_results)
            print(f"[AI筛选] 保存 {saved} 条分类结果")
            if saved != len(total_results):
                raise RuntimeError(
                    "普通 AI 分类结果保存数量不一致: "
                    f"expected={len(total_results)}, actual={saved}"
                )

        matched_hotlist_ids = {r["news_item_id"] for r in total_results if r.get("source_type") == "hotlist"}
        matched_rss_ids = {r["news_item_id"] for r in total_results if r.get("source_type") == "rss"}

        if succeeded_news_ids:
            self.storage.save_analyzed_news(
                succeeded_news_ids, "hotlist", effective_interests_file,
                current_hash, matched_hotlist_ids
            )

        if succeeded_rss_ids:
            self.storage.save_analyzed_news(
                succeeded_rss_ids, "rss", effective_interests_file,
                current_hash, matched_rss_ids
            )

        if succeeded_news_ids or succeeded_rss_ids:
            total_analyzed = len(succeeded_news_ids) + len(succeeded_rss_ids)
            total_matched = len(matched_hotlist_ids) + len(matched_rss_ids)
            print(f"[AI筛选] 已记录 {total_analyzed} 条新闻分析状态 (匹配 {total_matched}, 不匹配 {total_analyzed - total_matched})")

    def _build_filter_result(
        self,
        raw_results: List[Dict],
        tags: List[Dict],
        total_processed: int,
    ) -> AIFilterResult:
        tag_priority_map = {}
        for idx, t in enumerate(tags, start=1):
            tag_name = str(t.get("tag", "")).strip() if isinstance(t, dict) else ""
            if not tag_name:
                continue
            try:
                tag_priority_map[tag_name] = int(t.get("priority", idx))
            except (TypeError, ValueError):
                tag_priority_map[tag_name] = idx

        tag_groups: Dict[str, Dict] = {}
        seen_titles: Dict[str, set] = {}
        deduplicate_titles = not (
            self._rss_window is not None and self._rss_ids_authoritative
        )
        min_score = self._score_value(
            self._filter_config.get("MIN_SCORE", 0)
        )

        for r in raw_results:
            if self._score_value(r.get("relevance_score")) < min_score:
                continue
            tag_name = r["tag"]
            if tag_name not in tag_groups:
                raw_priority = r.get("tag_priority", tag_priority_map.get(tag_name, 9999))
                try:
                    tag_position = int(raw_priority)
                except (TypeError, ValueError):
                    tag_position = 9999
                tag_groups[tag_name] = {
                    "tag": tag_name,
                    "description": r.get("tag_description", ""),
                    "position": tag_position,
                    "count": 0,
                    "items": [],
                }
                seen_titles[tag_name] = set()

            title = r["title"]
            if deduplicate_titles and title in seen_titles[tag_name]:
                continue
            seen_titles[tag_name].add(title)

            tag_groups[tag_name]["items"].append({
                "id": r.get("news_item_id"),
                "news_item_id": r.get("news_item_id"),
                "module_type": r["module_type"],
                "species_scope": r.get("species_scope"),
                "title": title,
                "source_id": r.get("source_id", ""),
                "source_name": r.get("source_name", ""),
                "url": r.get("url", ""),
                "guid": r.get("guid", ""),
                "reader_url": build_reader_url(
                    r.get("source_id", ""),
                    r.get("url", ""),
                    title,
                ),
                "mobile_url": r.get("mobile_url", ""),
                "rank": r.get("rank", 0),
                "ranks": r.get("ranks", []),
                "first_time": r.get("first_time", ""),
                "last_time": r.get("last_time", ""),
                "published_at": r.get("published_at", ""),
                "count": r.get("count", 1),
                "relevance_score": r.get("relevance_score", 0),
                "source_type": r.get("source_type", "hotlist"),
                "content_level": r.get("content_level", "title_only"),
                "risk_warning": r.get("risk_warning", ""),
                "content_excerpt": r.get("content_excerpt", ""),
                "importance_score": r.get("importance_score", 0),
                "ai_summary": r.get("ai_summary", ""),
                "source_count": r.get("source_count", 1),
                "pre_hot_score": r.get("pre_hot_score", 0),
                "search_topic": r.get("search_topic", ""),
                "search_providers": r.get("search_providers", ""),
            })
            tag_groups[tag_name]["count"] += 1

        # The authoritative natural-week snapshot must reach the module
        # selector intact. Ordinary reports still use the display hotspot cap.
        if not (
            self._rss_window is not None and self._rss_ids_authoritative
        ):
            self._limit_search_hotspots(tag_groups)

        # 跨标签、跨来源统一选择重点新闻。importance_score 是科研/育种价值，
        # relevance_score 是与用户兴趣的相关性；证据层级仅用于同分时优先。
        evidence_weight = {"full_text": 2, "summary": 1, "title_only": 0}
        all_items = []
        for group in tag_groups.values():
            for item in group.get("items", []):
                if item.get("source_type") == "rss":
                    if not self._is_rss_item_in_scope(item):
                        continue
                all_items.append(item)
        ranked_items = sorted(
            all_items,
            key=lambda item: (
                float(item.get("importance_score", 0) or 0),
                float(item.get("relevance_score", 0) or 0),
                evidence_weight.get(item.get("content_level", "title_only"), 0),
                item.get("first_time", ""),
                item.get("title", ""),
            ),
            reverse=True,
        )
        highlight_top_n = max(
            0, int(self._filter_config.get("HIGHLIGHT_TOP_N", 5))
        )
        highlights = ranked_items[:highlight_top_n]
        for rank, item in enumerate(highlights, start=1):
            item["highlight_rank"] = rank

        # 每个标签内优先显示已入选的重点新闻，其余按重要性和相关度排序。
        for group in tag_groups.values():
            legacy_order = sorted(
                group["items"],
                key=lambda item: (
                    0 if item.get("highlight_rank") else 1,
                    item.get("highlight_rank", 9999),
                    -float(item.get("importance_score", 0) or 0),
                    -float(item.get("relevance_score", 0) or 0),
                )
            )
            ordered_search = sorted(
                (
                    item
                    for item in legacy_order
                    if is_agricultural_news_search_item(item)
                ),
                key=lambda item: item.get("search_hotspot_rank", 9999),
            )
            ordered_search_items = iter(ordered_search)
            group["items"] = [
                next(ordered_search_items)
                if is_agricultural_news_search_item(item)
                else item
                for item in legacy_order
            ]

        if self._priority_sort_enabled:
            sorted_tags = sorted(
                tag_groups.values(),
                key=lambda x: (x.get("position", 9999), -x["count"], x["tag"]),
            )
        else:
            sorted_tags = sorted(
                tag_groups.values(),
                key=lambda x: (-x["count"], x.get("position", 9999), x["tag"]),
            )

        total_matched = sum(t["count"] for t in sorted_tags)

        return AIFilterResult(
            tags=sorted_tags,
            highlights=highlights,
            total_matched=total_matched,
            total_processed=total_processed,
            success=True,
        )

    @staticmethod
    def _score_value(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _limit_search_hotspots(self, tag_groups: Dict[str, Dict]) -> None:
        """跨标签选择搜索热点，同时不改变普通 RSS 条目。"""
        candidates = []
        min_score = self._score_value(self._filter_config.get("MIN_SCORE", 0))

        for group in tag_groups.values():
            for item in group.get("items", []):
                if not is_agricultural_news_search_item(item):
                    continue
                if self._score_value(item.get("relevance_score")) < min_score:
                    continue
                if not self._is_rss_item_in_scope(item):
                    continue

                final_hot_score = round(
                    0.45 * self._score_value(item.get("pre_hot_score"))
                    + 0.35 * self._score_value(item.get("relevance_score"))
                    + 0.20 * self._score_value(item.get("importance_score")),
                    4,
                )
                item["final_hot_score"] = final_hot_score
                news_item_id = item.get("news_item_id")
                id_key = (
                    str(news_item_id)
                    if news_item_id not in (None, "")
                    else ""
                )
                url_key = canonicalize_url(str(item.get("url", "")).strip())
                candidates.append(
                    {
                        "score": final_hot_score,
                        "sequence": len(candidates),
                        "item": item,
                        "id_key": id_key,
                        "url_key": url_key,
                    }
                )

        parents = list(range(len(candidates)))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        id_owners: Dict[str, int] = {}
        url_owners: Dict[str, int] = {}
        for index, candidate in enumerate(candidates):
            for key, owners in (
                (candidate["id_key"], id_owners),
                (candidate["url_key"], url_owners),
            ):
                if not key:
                    continue
                owner = owners.get(key)
                if owner is None:
                    owners[key] = index
                else:
                    union(index, owner)

        best_by_event: Dict[int, Dict] = {}
        for index, candidate in enumerate(candidates):
            root = find(index)
            current = best_by_event.get(root)
            if current is None or candidate["score"] > current["score"]:
                best_by_event[root] = candidate

        ranked = sorted(
            best_by_event.values(),
            key=lambda candidate: (-candidate["score"], candidate["sequence"]),
        )
        selected = ranked[:self._max_search_hotspots]
        selected_objects = {id(candidate["item"]) for candidate in selected}
        for rank, candidate in enumerate(selected, start=1):
            candidate["item"]["search_hotspot_rank"] = rank

        for tag_name, group in list(tag_groups.items()):
            group["items"] = [
                item
                for item in group.get("items", [])
                if not is_agricultural_news_search_item(item)
                or id(item) in selected_objects
            ]
            group["count"] = len(group["items"])
            if not group["items"]:
                del tag_groups[tag_name]

    def convert_to_report_data(
        self,
        ai_filter_result: AIFilterResult,
        mode: str = "daily",
        new_titles: Optional[Dict] = None,
        rss_new_urls: Optional[set] = None,
    ) -> tuple:
        """
        将 AI 筛选结果转换为与关键词匹配相同的数据结构

        Returns:
            (hotlist_stats, rss_stats, rss_new_stats)
        """
        hotlist_stats = []
        rss_stats = []
        rss_new_stats = []
        min_score = self._score_value(
            self._filter_config.get("MIN_SCORE", 0)
        )

        latest_time = None
        if mode == "current":
            for tag_data in ai_filter_result.tags:
                for item in tag_data.get("items", []):
                    if item.get("source_type", "hotlist") == "hotlist":
                        last_time = item.get("last_time", "")
                        if last_time and (latest_time is None or last_time > latest_time):
                            latest_time = last_time
            if latest_time:
                print(f"[AI筛选] current 模式：最新时间 {latest_time}，过滤已下榜新闻")

        filtered_count = 0
        for tag_data in ai_filter_result.tags:
            tag_name = tag_data.get("tag", "")
            items = tag_data.get("items", [])
            if not items:
                continue

            hotlist_titles = []
            rss_titles = []

            for item in items:
                source_type = item.get("source_type", "hotlist")

                if self._rss_ids_authoritative and source_type != "rss":
                    continue

                if mode == "current" and latest_time and source_type == "hotlist":
                    if item.get("last_time", "") != latest_time:
                        filtered_count += 1
                        continue

                if self._score_value(item.get("relevance_score")) < min_score:
                    continue

                first_time = item.get("first_time", "")
                last_time = item.get("last_time", "")
                if source_type == "rss":
                    if not self._is_rss_item_in_scope(item):
                        continue
                    time_display = format_iso_time_friendly(first_time, self._timezone, include_date=True) if first_time else ""
                else:
                    if first_time and last_time and first_time != last_time:
                        first_display = convert_time_for_display(first_time)
                        last_display = convert_time_for_display(last_time)
                        time_display = f"[{first_display} ~ {last_display}]"
                    elif first_time:
                        time_display = convert_time_for_display(first_time)
                    else:
                        time_display = ""

                if source_type == "rss":
                    is_new = False
                    if rss_new_urls:
                        item_url = item.get("url", "")
                        is_new = item_url in rss_new_urls if item_url else False
                else:
                    is_new = False
                    if new_titles:
                        item_source_id = item.get("source_id", "")
                        item_title = item.get("title", "")
                        if item_source_id in new_titles:
                            is_new = item_title in new_titles[item_source_id]

                if mode == "incremental" and not is_new:
                    continue

                title_entry = {
                    "news_item_id": item.get("news_item_id"),
                    "module_type": item["module_type"],
                    "species_scope": item.get("species_scope"),
                    "title": item.get("title", ""),
                    "source_id": item.get("source_id", ""),
                    "source_name": item.get("source_name", ""),
                    "url": item.get("url", ""),
                    "guid": item.get("guid", ""),
                    "reader_url": item.get("reader_url", ""),
                    "mobile_url": item.get("mobile_url", ""),
                    "ranks": item.get("ranks", []),
                    "rank_threshold": self._rank_threshold,
                    "count": item.get("count", 1),
                    "is_new": is_new,
                    "time_display": time_display,
                    "published_at": item.get("published_at", ""),
                    "matched_keyword": tag_name,
                    "content_level": item.get("content_level", "title_only"),
                    "relevance_score": item.get("relevance_score", 0),
                    "risk_warning": item.get("risk_warning", ""),
                    "content_excerpt": item.get("content_excerpt", ""),
                    "importance_score": item.get("importance_score", 0),
                    "ai_summary": item.get("ai_summary", ""),
                    "highlight_rank": item.get("highlight_rank"),
                    "source_count": item.get("source_count", 1),
                    "final_hot_score": item.get("final_hot_score"),
                    "search_hotspot_rank": item.get("search_hotspot_rank"),
                }

                if source_type == "rss":
                    rss_titles.append(title_entry)
                else:
                    hotlist_titles.append(title_entry)

            if hotlist_titles:
                if self._max_news > 0 and mode != "weekly":
                    hotlist_titles = hotlist_titles[:self._max_news]
                hotlist_stats.append({
                    "word": tag_name,
                    "count": len(hotlist_titles),
                    "position": tag_data.get("position", 9999),
                    "titles": hotlist_titles,
                })

            if rss_titles:
                if self._max_news > 0 and mode != "weekly":
                    rss_titles = rss_titles[:self._max_news]
                rss_stats.append({
                    "word": tag_name,
                    "count": len(rss_titles),
                    "position": tag_data.get("position", 9999),
                    "titles": rss_titles,
                })
                new_rss_titles = [t for t in rss_titles if t.get("is_new")]
                if new_rss_titles:
                    rss_new_stats.append({
                        "word": tag_name,
                        "count": len(new_rss_titles),
                        "position": tag_data.get("position", 9999),
                        "titles": new_rss_titles,
                    })

        if mode == "current" and filtered_count > 0:
            total_kept = sum(s["count"] for s in hotlist_stats)
            print(f"[AI筛选] current 模式：过滤 {filtered_count} 条已下榜新闻，保留 {total_kept} 条当前在榜")

        if min_score > 0:
            hotlist_kept = sum(s["count"] for s in hotlist_stats)
            rss_kept = sum(s["count"] for s in rss_stats)
            total_kept = hotlist_kept + rss_kept
            parts = [f"热榜 {hotlist_kept} 条"]
            if rss_kept > 0:
                parts.append(f"RSS {rss_kept} 条")
            print(f"[AI筛选] 分数过滤：min_score={min_score}，保留 {total_kept} 条 score≥{min_score} ({', '.join(parts)})")

        sort_key_priority = lambda x: (x.get("position", 9999), -x["count"], x["word"])
        sort_key_count = lambda x: (-x["count"], x.get("position", 9999), x["word"])
        sort_key = sort_key_priority if self._priority_sort_enabled else sort_key_count
        hotlist_stats.sort(key=sort_key)
        rss_stats.sort(key=sort_key)
        rss_new_stats.sort(key=sort_key)

        return hotlist_stats, rss_stats, rss_new_stats


class _TagExtractionError(Exception):
    pass


def _with_ordered_priorities(tags: List[Dict], start_priority: int = 1) -> List[Dict]:
    normalized: List[Dict] = []
    priority = start_priority
    for tag_data in tags:
        if not isinstance(tag_data, dict):
            continue
        tag_name = str(tag_data.get("tag", "")).strip()
        if not tag_name:
            continue
        item = dict(tag_data)
        item["tag"] = tag_name
        item["priority"] = priority
        normalized.append(item)
        priority += 1
    return normalized
