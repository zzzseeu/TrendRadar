"""RSS 快照构建共用的稳定身份和合并规则。"""

import hashlib

from trendradar.crawler.news_search import canonicalize_url, normalize_title
from trendradar.storage.base import RSSItem


def item_identity(item: RSSItem) -> tuple:
    canonical = canonicalize_url(item.url)
    if canonical:
        return ("url", canonical)
    normalized = normalize_title(item.title)
    if not normalized:
        return ()
    return ("title", item.feed_id, normalized)


def stable_title_guid(item: RSSItem, namespace: str) -> str:
    identity = f"{item.feed_id}\0{normalize_title(item.title)}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{namespace}-title:{digest}"


def item_richness(item: RSSItem) -> tuple[int, int, float, bool]:
    return (
        len(item.summary or ""),
        item.source_count or 0,
        item.pre_hot_score or 0.0,
        bool(item.author),
    )


def search_providers(item: RSSItem) -> set[str]:
    return {
        provider.strip()
        for provider in (item.search_providers or "").split(",")
        if provider.strip()
    }
