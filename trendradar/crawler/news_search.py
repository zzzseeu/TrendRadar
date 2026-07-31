# coding=utf-8
"""Keyless clients for agricultural news search providers."""

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
from difflib import SequenceMatcher
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

from trendradar.crawler.http import DirectFirstSession


@dataclass
class SearchArticle:
    """A provider-neutral article returned by a news search."""

    title: str
    url: str
    published_at: str
    publisher: str
    language: str
    topic: str
    providers: set[str] = field(default_factory=set)
    related_publishers: set[str] = field(default_factory=set)
    summary: str = ""
    source_count: int = 1
    pre_hot_score: float = 0.0


@dataclass
class NewsSearchResult:
    """The articles retrieved by news search, plus provider failures."""

    items: list[SearchArticle]
    failed_providers: list[str] = field(default_factory=list)


TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "gclid", "fbclid"}


def _parse_timestamp(value: object) -> datetime | None:
    """Parse a timestamp with a concrete time component into an aware datetime."""
    if not isinstance(value, str) or not value.strip():
        return None

    raw = value.strip()
    if "T" not in raw and not re.search(r"\s\d{2}:\d{2}", raw):
        return None
    try:
        parsed = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
            except (TypeError, ValueError, IndexError, OverflowError):
                return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: object) -> str:
    """Return an ISO-8601 UTC timestamp, or an empty string when invalid."""
    parsed = _parse_timestamp(value)
    return parsed.isoformat() if parsed else ""


def _publisher_from_url(url: str) -> str:
    try:
        return urlsplit(url).netloc
    except ValueError:
        return ""


def canonicalize_url(url: str) -> str:
    """Remove fragments and known tracking parameters from a URL."""
    try:
        parts = urlsplit(url)
        query = urlencode([
            (key, value) for key, value in parse_qsl(parts.query)
            if key.lower() not in TRACKING_KEYS
        ])
        return urlunsplit((
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            query,
            "",
        ))
    except ValueError:
        return url


def normalize_title(title: str) -> str:
    """Normalize title casing, spacing, and punctuation for comparison."""
    return re.sub(r"[\W_]+", "", title.casefold())


def title_similarity(left: str, right: str) -> float:
    """Return a punctuation-insensitive title similarity ratio."""
    normalized_left = normalize_title(left)
    normalized_right = normalize_title(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


class AgriculturalNewsSearch:
    """Coordinate provider searches and rank recent cross-source reports."""

    def __init__(
        self,
        gdelt_client: "GDELTClient | None" = None,
        google_news_client: "GoogleNewsRSSClient | None" = None,
        topics: list[Mapping[str, str]] | None = None,
        max_results_per_provider: int = 50,
        similarity_threshold: float = 0.86,
        authority_domains: tuple[str, ...] | list[str] = (),
        now_func: Any | None = None,
    ):
        self.gdelt_client = gdelt_client or GDELTClient()
        self.google_news_client = google_news_client or GoogleNewsRSSClient()
        self.topics = topics or []
        self.max_results_per_provider = max_results_per_provider
        self.similarity_threshold = similarity_threshold
        self.authority_domains = {
            domain.lower().strip().lstrip(".")
            for domain in authority_domains
            if domain and domain.strip()
        }
        self.now_func = now_func or (lambda: datetime.now(timezone.utc))

    def _is_authority_url(self, url: str) -> bool:
        try:
            hostname = (urlsplit(url).hostname or "").lower()
        except ValueError:
            return False
        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in self.authority_domains
        )

    @staticmethod
    def _newer(left: SearchArticle, right: SearchArticle) -> bool:
        left_time = _parse_timestamp(left.published_at)
        right_time = _parse_timestamp(right.published_at)
        return bool(left_time and right_time and left_time > right_time)

    def _prefer_primary(self, current: SearchArticle, candidate: SearchArticle) -> bool:
        current_authority = self._is_authority_url(current.url)
        candidate_authority = self._is_authority_url(candidate.url)
        if candidate_authority != current_authority:
            return candidate_authority
        return self._newer(candidate, current)

    def _matches_group(self, group: dict[str, Any], article: SearchArticle) -> bool:
        if group["urls"] & {canonicalize_url(article.url)}:
            return True
        primary = group["primary"]
        return (
            primary.language.casefold() == article.language.casefold()
            and title_similarity(primary.title, article.title) >= self.similarity_threshold
        )

    def _current_time(self, now: datetime | str | None) -> datetime:
        if now is None:
            candidate = self.now_func()
            if not isinstance(candidate, datetime):
                raise ValueError("now_func must return an aware datetime")
            parsed = candidate
        elif isinstance(now, datetime):
            parsed = now
        else:
            parsed = _parse_timestamp(now)
            if parsed is None:
                raise ValueError("now must be an ISO-8601 timestamp with a time")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def aggregate(
        self, articles: list[SearchArticle], now: datetime | str | None = None
    ) -> list[SearchArticle]:
        """Strictly retain recent reports, then merge equivalent coverage."""
        current_time = self._current_time(now)
        groups: list[dict[str, Any]] = []
        for article in articles:
            published_at = _parse_timestamp(article.published_at)
            if published_at is None:
                continue
            age = current_time - published_at
            if age.total_seconds() < 0 or age.total_seconds() > 24 * 60 * 60:
                continue

            normalized = replace(
                article,
                url=canonicalize_url(article.url),
                providers=set(article.providers),
                related_publishers=set(article.related_publishers),
            )
            group = next(
                (group for group in groups if self._matches_group(group, normalized)),
                None,
            )
            if group is None:
                groups.append({
                    "primary": normalized,
                    "urls": {normalized.url},
                    "providers": set(normalized.providers),
                    "publishers": {normalized.publisher} if normalized.publisher else set(),
                })
                continue

            group["urls"].add(normalized.url)
            group["providers"].update(normalized.providers)
            if normalized.publisher:
                group["publishers"].add(normalized.publisher)
            if self._prefer_primary(group["primary"], normalized):
                group["primary"] = normalized

        results = []
        for group in groups:
            primary = group["primary"]
            publishers = group["publishers"]
            published_at = _parse_timestamp(primary.published_at)
            age_hours = (current_time - published_at).total_seconds() / 3600
            coverage = min(len(publishers) / 3, 1.0)
            authority = 1.0 if self._is_authority_url(primary.url) else 0.0
            recency = max(0.0, 1.0 - age_hours / 24.0)
            results.append(replace(
                primary,
                providers=group["providers"],
                related_publishers=publishers - {primary.publisher},
                source_count=len(publishers),
                pre_hot_score=round(0.5 * coverage + 0.3 * authority + 0.2 * recency, 4),
            ))
        return results

    def search(self) -> NewsSearchResult:
        """Fetch each configured query without letting one failure halt the rest."""
        articles: list[SearchArticle] = []
        failed_providers: list[str] = []

        def record_failure(provider: str) -> None:
            if provider not in failed_providers:
                failed_providers.append(provider)

        for topic in self.topics:
            topic_id = str(topic.get("id") or "")
            english_query = str(topic.get("en") or "").strip()
            chinese_query = str(topic.get("zh") or "").strip()
            if english_query:
                try:
                    articles.extend(self.gdelt_client.fetch(
                        english_query, topic_id, self.max_results_per_provider
                    ))
                except Exception:
                    record_failure("gdelt")
            for query, language in ((chinese_query, "zh"), (english_query, "en")):
                if not query:
                    continue
                try:
                    articles.extend(self.google_news_client.fetch(query, topic_id, language))
                except Exception:
                    record_failure("google_news")

        return NewsSearchResult(self.aggregate(articles), failed_providers)


class GDELTClient:
    """Search GDELT's public DOC 2.0 article-list API."""

    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"

    def __init__(self, session: DirectFirstSession | None = None, timeout: int = 15):
        self.session = session or DirectFirstSession(
            headers={"User-Agent": "TrendRadar/2.0 News Search"}
        )
        self.timeout = timeout

    def build_params(self, query: str, max_results: int) -> dict:
        return {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "timespan": "24h",
            "sort": "datedesc",
            "maxrecords": max_results,
        }

    def fetch(
        self, query: str, topic: str, max_results: int
    ) -> list[SearchArticle]:
        response = self.session.get(
            self.endpoint,
            params=self.build_params(query, max_results),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self.parse(response.json(), topic)

    def parse(self, payload: Mapping[str, Any], topic: str) -> list[SearchArticle]:
        articles = payload.get("articles", [])
        if not isinstance(articles, list):
            return []

        parsed_articles = []
        for item in articles:
            if not isinstance(item, Mapping):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            published_at = _format_timestamp(item.get("seendate"))
            if not title or not url or not published_at:
                continue

            publisher = str(item.get("domain") or "").strip()
            parsed_articles.append(
                SearchArticle(
                    title=title,
                    url=url,
                    published_at=published_at,
                    publisher=publisher or _publisher_from_url(url),
                    language=str(item.get("language") or "").strip(),
                    topic=topic,
                    providers={"gdelt"},
                )
            )
        return parsed_articles


class GoogleNewsRSSClient:
    """Search Google News RSS without attempting lossy link decoding."""

    endpoint = "https://news.google.com/rss/search"

    def __init__(self, session: DirectFirstSession | None = None, timeout: int = 15):
        self.session = session or DirectFirstSession(
            headers={"User-Agent": "TrendRadar/2.0 News Search"}
        )
        self.timeout = timeout

    def build_params(self, query: str, language: str) -> dict:
        locale = (
            {"hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}
            if language == "zh"
            else {"hl": "en-US", "gl": "US", "ceid": "US:en"}
        )
        return {"q": f"{query} when:1d", **locale}

    def fetch(self, query: str, topic: str, language: str) -> list[SearchArticle]:
        response = self.session.get(
            self.endpoint,
            params=self.build_params(query, language),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self.parse(response.text, topic, language)

    def parse(self, content: str, topic: str, language: str) -> list[SearchArticle]:
        feed = feedparser.parse(content)
        parsed_articles = []
        for entry in feed.entries:
            title = str(entry.get("title") or "")
            url = str(entry.get("link") or "").strip()
            published_at = _format_timestamp(entry.get("published"))
            if not title or not url or not published_at:
                continue

            source = entry.get("source") or {}
            publisher = str(source.get("title") or "").strip()
            suffix = f" - {publisher}"
            if publisher and title.endswith(suffix):
                title = title[: -len(suffix)]
            elif publisher and title.strip() == f"- {publisher}":
                # feedparser normalizes a leading space in " - <source>".
                title = ""
            title = title.strip()
            if not title:
                continue

            parsed_articles.append(
                SearchArticle(
                    title=title,
                    url=url,
                    published_at=published_at,
                    publisher=publisher or _publisher_from_url(url),
                    language=language,
                    topic=topic,
                    providers={"google_news"},
                )
            )
        return parsed_articles
