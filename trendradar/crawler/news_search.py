# coding=utf-8
"""Keyless clients for agricultural news search providers."""

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
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
    publisher_domain: str = ""


@dataclass
class NewsSearchResult:
    """The articles retrieved by news search, plus provider failures."""

    items: list[SearchArticle]
    failed_providers: list[str] = field(default_factory=list)


TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "gclid", "fbclid"}
LANGUAGE_ALIASES = {
    "en": "en",
    "english": "en",
    "en-us": "en",
    "zh": "zh",
    "chinese": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
}
MAX_URL_LENGTH = 4096
SAFE_URL_SCHEMES = {"http", "https"}
NEWS_SEARCH_PROVIDERS = {"gdelt", "google_news"}


def _parse_explicit_bool(value: object, default: bool, label: str) -> bool:
    """Parse booleans without treating arbitrary non-empty strings as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    print(
        f"[新闻搜索] {label} 布尔值格式错误 ({value!r})，"
        f"使用默认值 {default}"
    )
    return default


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


def _is_safe_http_url(url: object) -> bool:
    """Return whether a provider URL is a bounded absolute HTTP(S) URL."""
    if not isinstance(url, str):
        return False
    if not url or len(url) > MAX_URL_LENGTH or re.search(r"\s|[\x00-\x1f\x7f]", url):
        return False
    try:
        parts = urlsplit(url)
        if parts.scheme.casefold() not in SAFE_URL_SCHEMES or not parts.hostname:
            return False
        # Accessing ``port`` also rejects malformed/non-numeric port syntax.
        _ = parts.port
    except (ValueError, UnicodeError):
        return False
    return True


def _normalize_language(language: str) -> str:
    """Map provider-specific language labels to a shared language code."""
    normalized = language.strip().casefold().replace("_", "-")
    return LANGUAGE_ALIASES.get(normalized, normalized)


def _normalize_publisher_domain(value: str) -> str:
    """Return a lowercase publisher hostname without a leading ``www``."""
    raw = value.strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw if "://" in raw else f"//{raw}")
        hostname = (parts.hostname or "").lower()
    except ValueError:
        return ""
    return hostname.removeprefix("www.")


def canonicalize_url(url: str) -> str:
    """Remove fragments and known tracking parameters from a URL."""
    if not _is_safe_http_url(url):
        return ""
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
    except (ValueError, UnicodeError):
        return ""


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
        providers: Mapping[str, bool] | None = None,
    ):
        self.gdelt_client = gdelt_client or GDELTClient()
        self.google_news_client = google_news_client or GoogleNewsRSSClient()
        self.topics = [
            topic for topic in (topics or []) if isinstance(topic, Mapping)
        ]
        self.max_results_per_provider = max_results_per_provider
        self.similarity_threshold = similarity_threshold
        provider_config = providers if isinstance(providers, Mapping) else {}
        self.providers = {
            "gdelt": _parse_explicit_bool(
                provider_config.get("gdelt", True), True, "providers.gdelt"
            ),
            "google_news": _parse_explicit_bool(
                provider_config.get("google_news", True),
                True,
                "providers.google_news",
            ),
        }
        authority_values = (
            authority_domains
            if isinstance(authority_domains, (tuple, list, set))
            else ()
        )
        self.authority_domains = {
            _normalize_publisher_domain(domain)
            for domain in authority_values
            if isinstance(domain, str)
            and domain
            and _normalize_publisher_domain(domain)
        }
        self.now_func = now_func or (lambda: datetime.now(timezone.utc))

    def _is_authority_domain(self, domain: str) -> bool:
        hostname = _normalize_publisher_domain(domain)
        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in self.authority_domains
        )

    @staticmethod
    def _article_publisher_domain(article: SearchArticle) -> str:
        domain = _normalize_publisher_domain(article.publisher_domain)
        if domain:
            return domain
        if "google_news" in article.providers:
            return ""
        return _normalize_publisher_domain(article.url)

    @staticmethod
    def _newer(left: SearchArticle, right: SearchArticle) -> bool:
        left_time = _parse_timestamp(left.published_at)
        right_time = _parse_timestamp(right.published_at)
        return bool(left_time and right_time and left_time > right_time)

    def _prefer_primary(self, current: SearchArticle, candidate: SearchArticle) -> bool:
        current_authority = self._is_authority_domain(current.publisher_domain)
        candidate_authority = self._is_authority_domain(candidate.publisher_domain)
        if candidate_authority != current_authority:
            return candidate_authority
        return self._newer(candidate, current)

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
        normalized_articles: list[SearchArticle] = []
        for article in articles:
            published_at = _parse_timestamp(article.published_at)
            if published_at is None:
                continue
            age = current_time - published_at
            if age.total_seconds() < 0 or age.total_seconds() > 24 * 60 * 60:
                continue

            normalized_url = canonicalize_url(article.url)
            if not normalized_url:
                continue
            normalized = replace(
                article,
                url=normalized_url,
                language=_normalize_language(article.language),
                publisher_domain=self._article_publisher_domain(article),
                providers=set(article.providers),
                related_publishers=set(article.related_publishers),
            )
            normalized_articles.append(normalized)

        parents = list(range(len(normalized_articles)))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root == right_root:
                return
            # Preserve stable component order by always retaining the earlier root.
            if left_root > right_root:
                left_root, right_root = right_root, left_root
            parents[right_root] = left_root

        for left_index, left in enumerate(normalized_articles):
            for right_index in range(left_index):
                right = normalized_articles[right_index]
                same_url = left.url == right.url
                similar_title = (
                    left.language == right.language
                    and title_similarity(left.title, right.title)
                    >= self.similarity_threshold
                )
                if same_url or similar_title:
                    union(left_index, right_index)

        components: dict[int, list[SearchArticle]] = {}
        for index, article in enumerate(normalized_articles):
            components.setdefault(find(index), []).append(article)

        results = []
        for component_index in sorted(components):
            component = components[component_index]
            primary = component[0]
            providers: set[str] = set()
            publishers: set[str] = set()
            source_keys: set[str] = set()
            publisher_domains: dict[str, set[str]] = {}
            domain_labels: dict[str, set[str]] = {}
            for candidate in component:
                if not candidate.publisher_domain:
                    continue
                publisher_key = normalize_title(candidate.publisher)
                if publisher_key:
                    publisher_domains.setdefault(publisher_key, set()).add(
                        candidate.publisher_domain
                    )
                labels = candidate.publisher_domain.split(".")
                if len(labels) >= 2:
                    domain_key = normalize_title(labels[-2])
                    if domain_key:
                        domain_labels.setdefault(domain_key, set()).add(
                            candidate.publisher_domain
                        )
            for candidate in component:
                providers.update(candidate.providers)
                if candidate.publisher:
                    publishers.add(candidate.publisher)
                if candidate.publisher_domain:
                    source_keys.add(f"domain:{candidate.publisher_domain}")
                elif candidate.publisher:
                    publisher_key = normalize_title(candidate.publisher)
                    matched_domains = publisher_domains.get(
                        publisher_key, set()
                    ) or domain_labels.get(publisher_key, set())
                    if len(matched_domains) == 1:
                        source_keys.add(f"domain:{next(iter(matched_domains))}")
                    else:
                        source_keys.add(f"publisher:{publisher_key}")
                if self._prefer_primary(primary, candidate):
                    primary = candidate
            published_at = _parse_timestamp(primary.published_at)
            age_hours = (current_time - published_at).total_seconds() / 3600
            source_count = max(1, len(source_keys))
            coverage = min(source_count / 3, 1.0)
            authority = 1.0 if self._is_authority_domain(primary.publisher_domain) else 0.0
            recency = max(0.0, 1.0 - age_hours / 24.0)
            results.append(replace(
                primary,
                providers=providers,
                related_publishers=publishers - {primary.publisher},
                source_count=source_count,
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

        try:
            provider_budget = max(1, int(self.max_results_per_provider))
        except (TypeError, ValueError):
            provider_budget = 50

        gdelt_first_round: list[tuple[str, str, str]] = []
        google_first_round: list[tuple[str, str, str]] = []
        google_second_round: list[tuple[str, str, str]] = []
        for topic in self.topics:
            topic_id = str(topic.get("id") or "")
            english_query = str(topic.get("en") or "").strip()
            chinese_query = str(topic.get("zh") or "").strip()
            if english_query:
                gdelt_first_round.append((english_query, topic_id, "en"))
            if chinese_query:
                google_first_round.append((chinese_query, topic_id, "zh"))
                if english_query:
                    google_second_round.append((english_query, topic_id, "en"))
            elif english_query:
                google_first_round.append((english_query, topic_id, "en"))

        def spread_topics(
            queries: list[tuple[str, str, str]],
        ) -> list[tuple[str, str, str]]:
            """Put an evenly-spaced topic sample before the remaining queries."""
            count = len(queries)
            if count <= provider_budget:
                return queries
            sample_size = min(provider_budget, count)
            if sample_size == 1:
                selected_indices = [count // 2]
            else:
                selected_indices = [
                    round(index * (count - 1) / (sample_size - 1))
                    for index in range(sample_size)
                ]
            selected = set(selected_indices)
            return (
                [queries[index] for index in selected_indices]
                + [query for index, query in enumerate(queries) if index not in selected]
            )

        gdelt_queries = spread_topics(gdelt_first_round)
        google_queries = (
            spread_topics(google_first_round)
            + spread_topics(google_second_round)
        )

        def run_queries(
            provider: str,
            queries: list[tuple[str, str, str]],
        ) -> None:
            remaining = provider_budget
            for index, (query, topic_id, language) in enumerate(queries):
                if remaining <= 0:
                    break
                queries_left = len(queries) - index
                allocation = max(1, math.ceil(remaining / queries_left))
                try:
                    if provider == "gdelt":
                        fetched = self.gdelt_client.fetch(
                            query, topic_id, allocation
                        )
                    else:
                        fetched = self.google_news_client.fetch(
                            query, topic_id, language
                        )
                    accepted = list(fetched)[:allocation]
                    articles.extend(accepted)
                    remaining -= len(accepted)
                except Exception as exc:
                    record_failure(provider)
                    print(
                        f"[新闻搜索] {provider} 请求失败: "
                        f"topic={topic_id}, language={language}, "
                        f"query_index={index + 1}/{len(queries)}, "
                        f"error={type(exc).__name__}"
                    )

        if self.providers["gdelt"]:
            run_queries("gdelt", gdelt_queries)
        if self.providers["google_news"]:
            run_queries("google_news", google_queries)

        return NewsSearchResult(self.aggregate(articles), failed_providers)


class GDELTClient:
    """Search GDELT's public DOC 2.0 article-list API."""

    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"

    def __init__(
        self,
        session: DirectFirstSession | None = None,
        timeout: int = 15,
        use_proxy: bool = False,
        proxy_url: str = "",
    ):
        self.session = session or DirectFirstSession(
            headers={"User-Agent": "TrendRadar/2.0 News Search"},
            use_proxy=use_proxy,
            proxy_url=proxy_url,
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
            if not title or not _is_safe_http_url(url) or not published_at:
                continue

            publisher = str(item.get("domain") or "").strip()
            publisher_domain = _normalize_publisher_domain(publisher)
            parsed_articles.append(
                SearchArticle(
                    title=title,
                    url=url,
                    published_at=published_at,
                    publisher=publisher or _publisher_from_url(url),
                    language=_normalize_language(str(item.get("language") or "")),
                    topic=topic,
                    providers={"gdelt"},
                    publisher_domain=publisher_domain or _normalize_publisher_domain(url),
                )
            )
        return parsed_articles


class GoogleNewsRSSClient:
    """Search Google News RSS without attempting lossy link decoding."""

    endpoint = "https://news.google.com/rss/search"

    def __init__(
        self,
        session: DirectFirstSession | None = None,
        timeout: int = 15,
        use_proxy: bool = False,
        proxy_url: str = "",
    ):
        self.session = session or DirectFirstSession(
            headers={"User-Agent": "TrendRadar/2.0 News Search"},
            use_proxy=use_proxy,
            proxy_url=proxy_url,
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
            if not title or not _is_safe_http_url(url) or not published_at:
                continue

            source = entry.get("source") or {}
            publisher = str(source.get("title") or "").strip()
            source_url = str(source.get("href") or source.get("url") or "").strip()
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
                    publisher=publisher or _publisher_from_url(source_url),
                    language=_normalize_language(language),
                    topic=topic,
                    providers={"google_news"},
                    publisher_domain=_normalize_publisher_domain(source_url),
                )
            )
        return parsed_articles
