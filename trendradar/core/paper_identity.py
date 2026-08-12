"""Exact publication identifiers used to collapse cross-source paper reports."""

from __future__ import annotations

import re
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from trendradar.crawler.news_search import normalize_title


_DOI_RE = re.compile(
    r"\b(10\.\d{4,9}/[^\s<>\"'，。；、]+)",
    re.IGNORECASE,
)
_LABELED_DOI_RE = re.compile(
    r"\bdoi\s*:\s*(?:https?://(?:dx\.)?doi\.org/)?"
    r"(10\.\d{4,9}/[^\s<>\"'，。；、]+)",
    re.IGNORECASE,
)
_PII_COMPACT_RE = re.compile(r"\b([SB][A-Z0-9]{16})\b", re.IGNORECASE)
_PII_PRINT_RE = re.compile(
    r"\b([SB]\d{4}-\d{4}\(\d{2}\)\d{5}-\d)\b",
    re.IGNORECASE,
)
_PII_LINK_RE = re.compile(
    r"/(?:pii|fulltext)/"
    r"([SB](?:[A-Z0-9]{16}|\d{4}-\d{4}\(\d{2}\)\d{5}-\d))"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_QUOTED_PAPER_TITLE_PATTERNS = (
    re.compile(
        r"(?:论文|文章|研究)\s*(?:题为|名为)\s*[《“\"]"
        r"([^》”\"]{8,300})[》”\"]",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:paper|article|study)\s+(?:entitled|titled)\s+[\"“]"
        r"([^\"”]{12,300})[\"”]",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bPlease\s+cite\s+this\s+article\s+as:\s*[\"“]"
        r"([^\"”]{12,300})[\"”]",
        re.IGNORECASE,
    ),
)
_PRIMARY_FIELDS = (
    "url",
    "guid",
    "reader_url",
)
_DESCRIPTIVE_FIELDS = (
    "title",
    "content_excerpt",
    "ai_summary",
    "summary",
)


def normalize_doi(value: object) -> str:
    """Return a lowercase bare DOI, or an empty string for invalid input."""
    text = str(value or "").strip()
    if re.match(r"^https?://", text, flags=re.IGNORECASE):
        try:
            parsed = urlsplit(text)
        except ValueError:
            return ""
        if (parsed.hostname or "").lower() in {"doi.org", "dx.doi.org"}:
            text = parsed.path.lstrip("/")
    text = re.sub(
        r"^(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    text = text.split("#", 1)[0].split("?", 1)[0]
    text = text.rstrip(".,;:，。；")
    while text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1].rstrip()
    if not re.fullmatch(r"10\.\d{4,9}/\S+", text, flags=re.IGNORECASE):
        return ""
    return text.lower()


def normalize_pii(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    if len(text) == 17 and text[:1] in {"S", "B"}:
        return text
    return ""


def _iter_fields(
    item: Mapping[str, object], fields: Iterable[str]
) -> Iterable[str]:
    for field in fields:
        value = str(item.get(field) or "").strip()
        if value:
            yield value


def extract_dois(item: Mapping[str, object]) -> set[str]:
    dois: set[str] = set()
    for text in _iter_fields(item, _PRIMARY_FIELDS):
        for match in _DOI_RE.finditer(text):
            value = normalize_doi(match.group(1))
            if value:
                dois.add(value)
    if dois:
        return dois
    for text in _iter_fields(item, _DESCRIPTIVE_FIELDS):
        for match in _LABELED_DOI_RE.finditer(text):
            value = normalize_doi(match.group(1))
            if value:
                dois.add(value)
    return dois


def extract_piis(item: Mapping[str, object]) -> set[str]:
    piis: set[str] = set()
    for text in _iter_fields(item, _PRIMARY_FIELDS):
        for pattern in (_PII_COMPACT_RE, _PII_PRINT_RE):
            for match in pattern.finditer(text):
                value = normalize_pii(match.group(1))
                if value:
                    piis.add(value)
    if piis:
        return piis
    for text in _iter_fields(item, _DESCRIPTIVE_FIELDS):
        for match in _PII_LINK_RE.finditer(text):
            value = normalize_pii(match.group(1))
            if value:
                piis.add(value)
    return piis


def extract_explicit_paper_titles(
    item: Mapping[str, object],
    *,
    include_item_title: bool = False,
) -> dict[str, str]:
    """Map exact normalized paper titles to their original display form."""
    titles: dict[str, str] = {}
    for text in _iter_fields(item, _DESCRIPTIVE_FIELDS):
        for pattern in _QUOTED_PAPER_TITLE_PATTERNS:
            for match in pattern.finditer(text):
                display = " ".join(match.group(1).split()).strip(" .，。；;")
                normalized = normalize_title(display)
                if normalized:
                    titles.setdefault(normalized, display)

    primary_text = " ".join(
        str(item.get(field) or "") for field in ("url", "guid")
    )
    if include_item_title or (
        extract_piis({"url": primary_text})
        or extract_dois({"url": primary_text})
    ):
        display = " ".join(str(item.get("title") or "").split())
        normalized = normalize_title(display)
        if normalized:
            titles.setdefault(normalized, display)
    return titles


def extract_paper_identifiers(
    item: Mapping[str, object],
    *,
    include_item_title: bool = False,
) -> set[tuple[str, str]]:
    dois = extract_dois(item)
    piis = extract_piis(item)
    titles = extract_explicit_paper_titles(
        item, include_item_title=include_item_title
    )
    identifiers = (
        {("doi", next(iter(dois)))} if len(dois) == 1 else set()
    )
    if len(piis) == 1:
        identifiers.add(("pii", next(iter(piis))))
    if len(titles) == 1:
        identifiers.add(("paper_title", next(iter(titles))))
    return identifiers


def has_primary_publication_link(item: Mapping[str, object]) -> bool:
    primary = {
        "url": str(item.get("url") or ""),
        "guid": str(item.get("guid") or ""),
    }
    return bool(extract_dois(primary) or extract_piis(primary))
