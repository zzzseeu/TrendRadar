"""自然周时间窗口和 RSS 周快照聚合。"""

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import re
from typing import Iterator

import pytz

from trendradar.crawler.news_search import canonicalize_url, normalize_title
from trendradar.core.rss_snapshot import (
    item_identity,
    item_richness,
    search_providers,
    stable_title_guid,
)
from trendradar.storage.base import RSSData, RSSItem
from trendradar.utils.time import parse_iso_datetime, parse_storage_datetime


DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_week_published_at(value: str, timezone_name: str) -> datetime | None:
    """Parse an explicit weekly publication time without accepting naive time."""
    text = str(value or "").strip()
    tz = pytz.timezone(timezone_name)
    if DATE_ONLY.fullmatch(text):
        try:
            return tz.localize(datetime.strptime(text, "%Y-%m-%d"))
        except ValueError:
            return None
    try:
        raw = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if raw.tzinfo is None:
        return None
    return parse_iso_datetime(text, timezone_name)


@dataclass(frozen=True)
class NaturalWeekWindow:
    start: datetime
    end: datetime
    timezone: str

    @property
    def label(self) -> str:
        return f"{self.start:%Y-%m-%d}—{self.end - timedelta(days=1):%Y-%m-%d}"

    @property
    def storage_dates(self) -> list[str]:
        return [
            (self.start + timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(8)
        ]

    def contains(self, published_at: str) -> bool:
        parsed = parse_week_published_at(published_at, self.timezone)
        return parsed is not None and self.start <= parsed < self.end


def previous_natural_week(now: datetime, timezone: str) -> NaturalWeekWindow:
    current = current_natural_week(now, timezone)
    return NaturalWeekWindow(
        current.start - timedelta(days=7), current.start, timezone
    )


def current_natural_week(now: datetime, timezone_name: str) -> NaturalWeekWindow:
    """Return the local Monday-to-Monday window containing ``now``."""
    tz = pytz.timezone(timezone_name)
    local_now = now.astimezone(tz)
    monday = (local_now - timedelta(days=local_now.weekday())).date()
    start = tz.localize(datetime.combine(monday, datetime.min.time()))
    return NaturalWeekWindow(start, start + timedelta(days=7), timezone_name)


def report_item_identity(item: dict) -> tuple[str, str]:
    url = canonicalize_url(str(item.get("url") or ""))
    if url:
        return ("url", url)
    return ("title", normalize_title(str(item.get("title") or "")))


def primary_weekly_topic(item: dict) -> str:
    topics = item.get("weekly_topics") or []
    if isinstance(topics, str):
        topics = [topics]
    normalized = sorted({str(topic).strip() for topic in topics if str(topic).strip()})
    return normalized[0] if normalized else "其他"


def weekly_value_sort_key(item: dict) -> tuple:
    try:
        highlight = int(item.get("highlight_rank") or 10**9)
    except (TypeError, ValueError):
        highlight = 10**9
    try:
        score = float(item.get("ai_score") or item.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    published = parse_week_published_at(
        str(item.get("published_at") or ""), "Asia/Shanghai"
    )
    published_epoch = published.timestamp() if published else 0.0
    return (
        highlight,
        -score,
        -published_epoch,
        str(item.get("source_name") or ""),
        str(item.get("title") or ""),
    )


def deduplicate_report_items(items: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for raw in items:
        item = dict(raw)
        key = report_item_identity(item)
        if not key[1]:
            continue
        existing = merged.get(key)
        if existing is None or weekly_value_sort_key(item) < weekly_value_sort_key(existing):
            merged[key] = item
    return list(merged.values())


def select_weekly_news(
    items: list[dict], *, limit: int = 20, highlight_count: int = 5,
) -> list[dict]:
    """Select a stable, topic-balanced weekly news list without padding."""
    unique = deduplicate_report_items(items)
    ranked = sorted(unique, key=weekly_value_sort_key)
    selected = ranked[:min(highlight_count, limit)]
    selected_keys = {report_item_identity(item) for item in selected}
    buckets: dict[str, deque[dict]] = {}
    for item in ranked:
        if report_item_identity(item) in selected_keys:
            continue
        topic = primary_weekly_topic(item) or "其他"
        buckets.setdefault(topic, deque()).append(item)
    topics = sorted(buckets)
    while len(selected) < limit and topics:
        next_topics = []
        for topic in topics:
            bucket = buckets[topic]
            if bucket and len(selected) < limit:
                item = bucket.popleft()
                selected.append(item)
                selected_keys.add(report_item_identity(item))
            if bucket:
                next_topics.append(topic)
        topics = next_topics
    for rank, item in enumerate(selected[:highlight_count], start=1):
        item["highlight_rank"] = rank
    return selected


@dataclass
class WeeklyRSSSnapshot:
    """已写入本周一库的上个自然周 RSS 快照。"""

    window: NaturalWeekWindow
    data: RSSData | None
    allowed_rss_ids: set[int] = field(default_factory=set)
    missing_dates: list[str] = field(default_factory=list)
    failed_sources: dict[str, list[str]] = field(default_factory=dict)
    failed_ids: list[str] = field(default_factory=list)
    total_read: int = 0
    filtered_out: int = 0
    duplicate_count: int = 0

    def iter_items(self) -> Iterator[RSSItem]:
        """按快照固定顺序迭代条目。"""
        if self.data is None:
            return iter(())
        return iter(sorted(
            (
                item
                for items in self.data.items.values()
                for item in items
            ),
            key=lambda item: (item.published_at, item.feed_id, item.title),
        ))


class WeeklyRSSAggregator:
    """从八个日库构建并持久化一个自然周 RSS 快照。"""

    def __init__(self, storage, timezone: str):
        self.storage = storage
        self.timezone = timezone

    def build(self, now: datetime) -> WeeklyRSSSnapshot:
        window = previous_natural_week(now, self.timezone)
        missing_dates: list[str] = []
        failed_sources: dict[str, list[str]] = {}
        feed_names: dict[str, str] = {}
        deduplicated: dict[tuple[str, ...], RSSItem] = {}
        provider_sets: dict[tuple[str, ...], set[str]] = {}
        canonical_anchors: dict[str, tuple[str, str]] = {}
        total_read = 0
        filtered_out = 0
        duplicate_count = 0

        for date in window.storage_dates:
            daily_data = self.storage.get_rss_data_strict(date)
            if daily_data is None:
                raise RuntimeError(
                    f"周快照构建失败：RSS 日库缺失或没有抓取记录: {date}"
                )

            if daily_data.failed_ids:
                failed = ", ".join(sorted(daily_data.failed_ids))
                raise RuntimeError(
                    f"周快照构建失败：{date} RSS 来源失败: {failed}"
                )

            for feed_id, name in daily_data.id_to_name.items():
                if name:
                    feed_names[feed_id] = name

            for items in daily_data.items.values():
                for item in items:
                    total_read += 1
                    if not window.contains(item.published_at):
                        filtered_out += 1
                        continue

                    identity = item_identity(item)
                    if not identity:
                        filtered_out += 1
                        continue

                    candidate = replace(item)
                    # 周快照会把上一周条目写入本周一日库。把源日库中的
                    # 分钟格式首次抓取时间先固定为完整时间，避免账本误把
                    # 它解释为周快照日期；老数据缺少条目时间时使用该日
                    # crawl record 作为首次观察时间。
                    first_seen = parse_storage_datetime(
                        candidate.first_time
                        or candidate.crawl_time
                        or daily_data.crawl_time,
                        date,
                        self.timezone,
                    )
                    if first_seen is not None:
                        candidate.first_time = first_seen.isoformat()
                    canonical_url = canonicalize_url(candidate.url)
                    if canonical_url:
                        candidate.url = canonical_url
                        anchor = (candidate.feed_id, candidate.feed_name)
                        current_anchor = canonical_anchors.get(canonical_url)
                        if current_anchor is None or anchor[0] < current_anchor[0]:
                            canonical_anchors[canonical_url] = anchor
                    if candidate.feed_name:
                        feed_names[candidate.feed_id] = candidate.feed_name

                    existing = deduplicated.get(identity)
                    if existing is None:
                        candidate.search_providers = ",".join(
                            sorted(search_providers(candidate))
                        )
                        deduplicated[identity] = candidate
                        provider_sets[identity] = search_providers(candidate)
                        continue

                    duplicate_count += 1
                    providers = provider_sets[identity]
                    providers.update(search_providers(candidate))
                    if item_richness(candidate) > item_richness(existing):
                        deduplicated[identity] = candidate

                    merged = deduplicated[identity]
                    merged.source_count = max(
                        existing.source_count or 0,
                        candidate.source_count or 0,
                    )
                    merged.pre_hot_score = max(
                        existing.pre_hot_score or 0.0,
                        candidate.pre_hot_score or 0.0,
                    )
                    merged.search_providers = ",".join(sorted(providers))

        snapshot = WeeklyRSSSnapshot(
            window=window,
            data=None,
            missing_dates=missing_dates,
            failed_sources=failed_sources,
            failed_ids=sorted({
                failed_id
                for failed in failed_sources.values()
                for failed_id in failed
            }),
            total_read=total_read,
            filtered_out=filtered_out,
            duplicate_count=duplicate_count,
        )
        if not deduplicated:
            return snapshot

        ordered_items = sorted(
            deduplicated.values(),
            key=lambda item: (item.published_at, item.feed_id, item.title),
        )
        grouped_items: dict[str, list[RSSItem]] = {}
        id_to_name: dict[str, str] = {}
        existing_rows = self.storage.get_all_rss_ids_strict(
            window.end.strftime("%Y-%m-%d")
        )
        existing_anchors = {
            canonicalize_url(row.get("url", "")): (
                row.get("source_id", ""), row.get("source_name", ""),
            )
            for row in existing_rows
            if canonicalize_url(row.get("url", ""))
        }
        for item in ordered_items:
            canonical_url = canonicalize_url(item.url)
            if not canonical_url and not item.guid:
                item.guid = stable_title_guid(item, namespace="weekly")
            anchor = existing_anchors.get(canonical_url) or canonical_anchors.get(
                canonical_url
            )
            if anchor:
                item.feed_id, item.feed_name = anchor
            grouped_items.setdefault(item.feed_id, []).append(item)
            id_to_name[item.feed_id] = item.feed_name or feed_names.get(
                item.feed_id, item.feed_id
            )

        data = RSSData(
            date=window.end.strftime("%Y-%m-%d"),
            crawl_time=now.astimezone(pytz.timezone(self.timezone)).isoformat(),
            items=grouped_items,
            id_to_name=id_to_name,
            failed_ids=[],
        )
        if not self.storage.save_rss_data(data):
            raise RuntimeError("周快照保存失败")

        snapshot.data = data
        snapshot.allowed_rss_ids = self._resolve_allowed_ids(data)
        return snapshot

    def _resolve_allowed_ids(self, data: RSSData) -> set[int]:
        identities = {
            (item.feed_id, canonicalize_url(item.url), normalize_title(item.title))
            for items in data.items.values()
            for item in items
        }
        resolved_ids: set[int] = set()
        resolved_identities: set[tuple[str, str, str]] = set()
        for row in self.storage.get_all_rss_ids_strict(data.date):
            identity = (
                row.get("source_id", ""),
                canonicalize_url(row.get("url", "")),
                normalize_title(row.get("title", "")),
            )
            if identity in identities:
                resolved_ids.add(row["id"])
                resolved_identities.add(identity)

        if resolved_identities != identities:
            raise RuntimeError("周快照 ID 解析失败：存在未持久化条目")
        return resolved_ids
