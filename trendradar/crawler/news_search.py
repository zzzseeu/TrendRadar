# coding=utf-8
"""Keyless clients for agricultural news search providers."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

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


def _format_timestamp(value: object) -> str:
    """Return an ISO-8601 UTC timestamp, or an empty string when invalid."""
    if not isinstance(value, str) or not value.strip():
        return ""

    raw = value.strip()
    if "T" not in raw and not re.search(r"\s\d{2}:\d{2}", raw):
        return ""
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
                return ""

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _publisher_from_url(url: str) -> str:
    try:
        return urlsplit(url).netloc
    except ValueError:
        return ""


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
