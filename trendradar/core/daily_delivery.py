"""每日交付时间窗口和 RSS 快照聚合。"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Iterator, Optional

import pytz

from trendradar.core.rss_snapshot import (
    item_identity,
    item_richness,
    search_providers,
    stable_title_guid,
)
from trendradar.crawler.news_search import canonicalize_url
from trendradar.storage.base import RSSData, RSSItem


def normalize_delivery_datetime(value: datetime, timezone: str) -> datetime:
    tz = pytz.timezone(timezone)
    if value.tzinfo is None:
        return tz.localize(value)
    return value.astimezone(tz)


def parse_discovered_at(
    value: str,
    storage_date: str,
    timezone: str,
) -> Optional[datetime]:
    text = (value or "").strip()
    if not text:
        return None
    tz = pytz.timezone(timezone)
    for time_format in ("%H-%M", "%H:%M"):
        try:
            clock = datetime.strptime(text, time_format).time()
            day = datetime.strptime(storage_date, "%Y-%m-%d").date()
            return tz.localize(datetime.combine(day, clock))
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return normalize_delivery_datetime(parsed, timezone)


@dataclass(frozen=True)
class DailyDeliveryWindow:
    start: datetime
    end: datetime
    timezone: str

    @property
    def label(self) -> str:
        return f"{self.start:%Y-%m-%d %H:%M}—{self.end:%Y-%m-%d %H:%M}"

    @property
    def storage_dates(self) -> list[str]:
        day_count = (self.end.date() - self.start.date()).days
        return [
            (self.start.date() + timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(day_count + 1)
        ]

    def contains(self, value: datetime) -> bool:
        return self.start < value <= self.end

    def contains_discovered(self, value: str, storage_date: str) -> bool:
        """按记录精度判断首次发现时间是否与窗口相交。"""
        discovered_at = parse_discovered_at(
            value, storage_date, self.timezone
        )
        if discovered_at is None:
            return False
        text = (value or "").strip()
        if len(text) == 5 and text[2] in {":", "-"}:
            minute_end = discovered_at + timedelta(minutes=1)
            return discovered_at <= self.end and minute_end > self.start
        return self.contains(discovered_at)


def daily_delivery_window(
    now: datetime,
    checkpoint: Optional[str],
    timezone: str,
) -> DailyDeliveryWindow:
    end = normalize_delivery_datetime(now, timezone)
    start = (
        parse_discovered_at(checkpoint, end.strftime("%Y-%m-%d"), timezone)
        if checkpoint
        else pytz.timezone(timezone).normalize(end - timedelta(hours=24))
    )
    if start is None or start >= end:
        raise RuntimeError("每日交付检查点无效")
    return DailyDeliveryWindow(start=start, end=end, timezone=timezone)


@dataclass
class DailyDeliverySnapshot:
    window: DailyDeliveryWindow
    data: RSSData | None
    allowed_rss_ids: set[int] = field(default_factory=set)
    missing_dates: list[str] = field(default_factory=list)
    total_read: int = 0
    filtered_out: int = 0
    duplicate_count: int = 0

    def iter_items(self) -> Iterator[RSSItem]:
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


class DailyDeliveryAggregator:
    """从检查点后的日库构建并持久化每日 RSS 交付快照。"""

    def __init__(self, storage, timezone: str):
        self.storage = storage
        self.timezone = timezone

    def build(
        self,
        now: datetime,
        checkpoint: Optional[str],
    ) -> DailyDeliverySnapshot:
        window = daily_delivery_window(now, checkpoint, self.timezone)
        missing_dates: list[str] = []
        feed_names: dict[str, str] = {}
        deduplicated: dict[tuple, RSSItem] = {}
        provider_sets: dict[tuple, set[str]] = {}
        canonical_anchors: dict[str, tuple[str, str]] = {}
        total_read = 0
        filtered_out = 0
        duplicate_count = 0
        latest_feed_statuses: dict[str, str] = {}

        for storage_date in window.storage_dates:
            get_rss_data = getattr(
                self.storage, "get_rss_data_strict", None
            )
            if not callable(get_rss_data):
                get_rss_data = self.storage.get_rss_data
            daily_data = get_rss_data(storage_date)
            if daily_data is None:
                missing_dates.append(storage_date)
                continue

            get_statuses = getattr(
                self.storage, "get_rss_feed_statuses_strict", None
            )
            if not callable(get_statuses):
                get_statuses = getattr(
                    self.storage, "get_rss_feed_statuses", None
                )
            daily_statuses = (
                get_statuses(storage_date) if callable(get_statuses) else None
            )
            if not isinstance(daily_statuses, dict):
                daily_statuses = {
                    feed_id: "success" for feed_id in daily_data.items
                }
                daily_statuses.update({
                    feed_id: "failed" for feed_id in daily_data.failed_ids
                })
            latest_feed_statuses.update(daily_statuses)

            for feed_id, name in daily_data.id_to_name.items():
                if name:
                    feed_names[feed_id] = name

            for items in daily_data.items.values():
                for item in items:
                    total_read += 1
                    discovered_value = item.first_time or item.crawl_time
                    if not window.contains_discovered(
                        discovered_value, storage_date
                    ):
                        filtered_out += 1
                        continue

                    identity = item_identity(item)
                    if not identity:
                        filtered_out += 1
                        continue

                    candidate = replace(item)
                    canonical_url = canonicalize_url(candidate.url)
                    if canonical_url:
                        anchor = (candidate.feed_id, candidate.feed_name)
                        current_anchor = canonical_anchors.get(canonical_url)
                        if current_anchor is None or anchor[0] < current_anchor[0]:
                            canonical_anchors[canonical_url] = anchor
                    if candidate.feed_name:
                        feed_names[candidate.feed_id] = candidate.feed_name

                    existing = deduplicated.get(identity)
                    if existing is None:
                        providers = search_providers(candidate)
                        candidate.search_providers = ",".join(sorted(providers))
                        deduplicated[identity] = candidate
                        provider_sets[identity] = providers
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

        if len(missing_dates) == len(window.storage_dates):
            raise RuntimeError("每日交付快照构建失败：窗口内日库全部缺失")

        failed_feeds = sorted(
            feed_id
            for feed_id, status in latest_feed_statuses.items()
            if status == "failed"
        )
        if failed_feeds:
            failed = ", ".join(failed_feeds)
            raise RuntimeError(
                f"每日交付快照构建失败：窗口内最终失败源 {failed}"
            )

        if deduplicated:
            earliest = self.storage.get_earliest_rss_discoveries_strict(
                set(deduplicated), window.end.strftime("%Y-%m-%d")
            )
            missing_identities = set(deduplicated) - set(earliest)
            if missing_identities:
                raise RuntimeError(
                    "每日交付快照构建失败：候选首次发现历史不完整"
                )
            for identity, (discovered, storage_date) in earliest.items():
                if not window.contains_discovered(discovered, storage_date):
                    deduplicated.pop(identity, None)
                    provider_sets.pop(identity, None)
                    filtered_out += 1

        snapshot = DailyDeliverySnapshot(
            window=window,
            data=None,
            missing_dates=missing_dates,
            total_read=total_read,
            filtered_out=filtered_out,
            duplicate_count=duplicate_count,
        )
        ordered_items = sorted(
            deduplicated.values(),
            key=lambda item: (item.published_at, item.feed_id, item.title),
        )
        grouped_items: dict[str, list[RSSItem]] = {}
        id_to_name: dict[str, str] = {}
        snapshot_date = window.end.strftime("%Y-%m-%d")
        existing_by_canonical: dict[str, list[dict]] = {}
        for row in self.storage.get_all_rss_ids_strict(snapshot_date):
            canonical_url = canonicalize_url(row.get("url", ""))
            if not canonical_url:
                continue
            existing_by_canonical.setdefault(canonical_url, []).append(row)

        for item in ordered_items:
            canonical_url = canonicalize_url(item.url)
            if not canonical_url and not item.guid:
                item.guid = stable_title_guid(item, namespace="daily-delivery")
            existing_rows = existing_by_canonical.get(canonical_url, [])
            exact_rows = []
            if item.guid:
                exact_rows = [
                    row for row in existing_rows
                    if row.get("source_id", "") == item.feed_id
                    and row.get("guid", "") == item.guid
                ]
            if not exact_rows:
                exact_rows = [
                    row for row in existing_rows
                    if row.get("source_id", "") == item.feed_id
                    and row.get("url", "") == item.url
                ]
            existing_row = None
            if len(exact_rows) == 1:
                existing_row = exact_rows[0]
            elif existing_rows:
                existing_row = min(
                    existing_rows,
                    key=lambda row: (row.get("source_id", ""), row["id"]),
                )
            if existing_row:
                item.feed_id = existing_row.get("source_id", "")
                item.feed_name = existing_row.get("source_name", "")
                item.url = existing_row.get("url", "")
                item.guid = existing_row.get("guid", "")
            else:
                if canonical_url:
                    item.url = canonical_url
                anchor = canonical_anchors.get(canonical_url)
                if anchor:
                    item.feed_id, item.feed_name = anchor
            grouped_items.setdefault(item.feed_id, []).append(item)
            id_to_name[item.feed_id] = item.feed_name or feed_names.get(
                item.feed_id, item.feed_id
            )

        data = RSSData(
            date=window.end.strftime("%Y-%m-%d"),
            crawl_time=window.end.strftime("%Y-%m-%d %H:%M:%S"),
            items=grouped_items,
            id_to_name=id_to_name,
            failed_ids=[],
        )
        if not self.storage.save_rss_data(data):
            raise RuntimeError("每日交付快照保存失败")
        snapshot.data = data
        snapshot.allowed_rss_ids = self._resolve_allowed_ids(data)
        return snapshot

    def _resolve_allowed_ids(self, data: RSSData) -> set[int]:
        snapshot_items = [
            item
            for items in data.items.values()
            for item in items
        ]
        rows = self.storage.get_all_rss_ids_strict(data.date)
        resolved_ids: set[int] = set()
        for item in snapshot_items:
            if item.guid:
                matches = [
                    row for row in rows
                    if row.get("source_id", "") == item.feed_id
                    and row.get("guid", "") == item.guid
                ]
            else:
                matches = [
                    row for row in rows
                    if row.get("source_id", "") == item.feed_id
                    and row.get("url", "") == item.url
                ]
            if len(matches) != 1:
                raise RuntimeError(
                    "每日交付快照 ID 解析失败：存在未持久化条目"
                )
            resolved_ids.add(matches[0]["id"])

        if len(resolved_ids) != len(snapshot_items):
            raise RuntimeError("每日交付快照 ID 解析失败：存在未持久化条目")
        return resolved_ids
