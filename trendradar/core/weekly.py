"""自然周时间窗口和 RSS 周快照聚合。"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Iterator

import pytz

from trendradar.crawler.news_search import canonicalize_url, normalize_title
from trendradar.storage.base import RSSData, RSSItem
from trendradar.utils.time import parse_iso_datetime


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
        parsed = parse_iso_datetime(published_at, self.timezone)
        return parsed is not None and self.start <= parsed < self.end


def previous_natural_week(now: datetime, timezone: str) -> NaturalWeekWindow:
    tz = pytz.timezone(timezone)
    local_now = now.astimezone(tz)
    monday_date = (local_now - timedelta(days=local_now.weekday())).date()
    end = tz.localize(datetime.combine(monday_date, datetime.min.time()))
    return NaturalWeekWindow(end - timedelta(days=7), end, timezone)


@dataclass
class WeeklyRSSSnapshot:
    """已写入本周一库的上个自然周 RSS 快照。"""

    window: NaturalWeekWindow
    data: RSSData | None
    allowed_rss_ids: set[int] = field(default_factory=set)
    missing_dates: list[str] = field(default_factory=list)
    failed_sources: dict[str, list[str]] = field(default_factory=dict)
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


def _item_identity(item: RSSItem) -> tuple[str, ...]:
    """返回用于跨日去重的稳定条目身份。"""
    canonical = canonicalize_url(item.url)
    if canonical:
        return ("url", canonical)
    normalized = normalize_title(item.title)
    return ("title", item.feed_id, normalized) if normalized else ()


def _richness(item: RSSItem) -> tuple[int, int, float, bool]:
    """用于稳定选择同一新闻中信息更完整的版本。"""
    return (
        len(item.summary or ""),
        item.source_count or 0,
        item.pre_hot_score or 0.0,
        bool(item.author),
    )


def _providers(item: RSSItem) -> set[str]:
    return {
        provider.strip()
        for provider in (item.search_providers or "").split(",")
        if provider.strip()
    }


class WeeklyRSSAggregator:
    """从八个日库构建并持久化一个自然周 RSS 快照。"""

    def __init__(self, storage, timezone: str):
        self.storage = storage
        self.timezone = timezone

    def build(self, now: datetime) -> WeeklyRSSSnapshot:
        window = previous_natural_week(now, self.timezone)
        missing_dates: list[str] = []
        failed_sources: dict[str, list[str]] = {}
        failed_ids: list[str] = []
        feed_names: dict[str, str] = {}
        deduplicated: dict[tuple[str, ...], RSSItem] = {}
        provider_sets: dict[tuple[str, ...], set[str]] = {}
        total_read = 0
        filtered_out = 0
        duplicate_count = 0

        for date in window.storage_dates:
            daily_data = self.storage.get_rss_data(date)
            if daily_data is None:
                missing_dates.append(date)
                continue

            for failed_id in daily_data.failed_ids:
                if failed_id not in failed_ids:
                    failed_ids.append(failed_id)
            if daily_data.failed_ids:
                failed_sources[date] = list(daily_data.failed_ids)

            for feed_id, name in daily_data.id_to_name.items():
                if name:
                    feed_names[feed_id] = name

            for items in daily_data.items.values():
                for item in items:
                    total_read += 1
                    if not window.contains(item.published_at):
                        filtered_out += 1
                        continue

                    identity = _item_identity(item)
                    if not identity:
                        filtered_out += 1
                        continue

                    candidate = replace(item)
                    if candidate.feed_name:
                        feed_names[candidate.feed_id] = candidate.feed_name

                    existing = deduplicated.get(identity)
                    if existing is None:
                        candidate.search_providers = ",".join(
                            sorted(_providers(candidate))
                        )
                        deduplicated[identity] = candidate
                        provider_sets[identity] = _providers(candidate)
                        continue

                    duplicate_count += 1
                    providers = provider_sets[identity]
                    providers.update(_providers(candidate))
                    if _richness(candidate) > _richness(existing):
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
        for item in ordered_items:
            grouped_items.setdefault(item.feed_id, []).append(item)
            id_to_name[item.feed_id] = item.feed_name or feed_names.get(
                item.feed_id, item.feed_id
            )

        data = RSSData(
            date=window.end.strftime("%Y-%m-%d"),
            crawl_time="weekly",
            items=grouped_items,
            id_to_name=id_to_name,
            failed_ids=failed_ids,
        )
        if not self.storage.save_rss_data(data):
            raise RuntimeError("周快照保存失败")

        snapshot.data = data
        snapshot.allowed_rss_ids = self._resolve_allowed_ids(data)
        if not snapshot.allowed_rss_ids:
            raise RuntimeError("周快照 ID 解析失败")
        return snapshot

    def _resolve_allowed_ids(self, data: RSSData) -> set[int]:
        identities = {
            (item.feed_id, canonicalize_url(item.url), normalize_title(item.title))
            for items in data.items.values()
            for item in items
        }
        return {
            row["id"]
            for row in self.storage.get_all_rss_ids(data.date)
            if (
                row.get("source_id", ""),
                canonicalize_url(row.get("url", "")),
                normalize_title(row.get("title", "")),
            ) in identities
        }
