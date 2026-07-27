# coding=utf-8
"""IRRI 官方新闻列表解析。"""

from datetime import datetime
from html.parser import HTMLParser
from typing import Dict, List, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

from .parser import ParsedRSSItem


class _IRRINewsCardParser(HTMLParser):
    """从 IRRI 新闻页的 All News 区域提取卡片。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards: List[Dict[str, str]] = []
        self._scope_depth = 0
        self._current: Optional[Dict[str, str]] = None
        self._capture_tag: Optional[str] = None
        self._capture_field: Optional[str] = None
        self._capture_parts: List[str] = []

    @staticmethod
    def _classes(attrs: Dict[str, Optional[str]]) -> set:
        return set((attrs.get("class") or "").split())

    def handle_starttag(self, tag: str, attrs_list) -> None:
        attrs = dict(attrs_list)
        classes = self._classes(attrs)

        if not self._scope_depth:
            if tag == "div" and "related-news-content" in classes:
                self._scope_depth = 1
            return

        if tag == "div":
            self._scope_depth += 1

        if tag == "a" and "card-wrapper" in classes:
            self._current = {"href": attrs.get("href") or ""}
            return

        if not self._current:
            return

        field = None
        if tag == "h3" and "card-title" in classes:
            field = "title"
        elif tag == "div" and "card-text" in classes:
            field = "summary"
        elif tag == "span" and "date" in classes:
            field = "date"

        if field:
            self._capture_tag = tag
            self._capture_field = field
            self._capture_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_field:
            self._capture_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture_field and tag == self._capture_tag:
            value = " ".join("".join(self._capture_parts).split())
            if self._current is not None:
                self._current[self._capture_field] = value
            self._capture_tag = None
            self._capture_field = None
            self._capture_parts = []

        if tag == "a" and self._current is not None:
            if self._current.get("href") and self._current.get("title"):
                self.cards.append(self._current)
            self._current = None

        if tag == "div" and self._scope_depth:
            self._scope_depth -= 1


class _HTMLTitleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._capturing = False
        self._parts: List[str] = []

    def handle_starttag(self, tag: str, attrs_list) -> None:
        if tag == "title":
            self._capturing = True

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._capturing = False

    @property
    def title(self) -> str:
        return " ".join("".join(self._parts).split())


def _canonical_irri_url(url: str) -> str:
    """将 Google Translate 代理链接还原为 IRRI 官方链接。"""
    parsed = urlsplit(url)
    if not parsed.path.startswith("/news-and-events/news/"):
        return ""
    return urlunsplit(("https", "www.irri.org", parsed.path, "", ""))


def build_irri_translate_url(url: str) -> str:
    """构造由 Google Translate 代理读取的 IRRI URL。"""
    parsed = urlsplit(url)
    query = urlencode({"_x_tr_sl": "auto", "_x_tr_tl": "en", "_x_tr_hl": "en"})
    return urlunsplit(("https", "www-irri-org.translate.goog", parsed.path, query, ""))


def parse_irri_article_title_html(content: str) -> str:
    """从 IRRI 文章页的 HTML title 中提取完整新闻标题。"""
    parser = _HTMLTitleParser()
    parser.feed(content)
    parser.close()
    suffix = " | International Rice Research Institute"
    title = parser.title
    if title.endswith(suffix):
        title = title[:-len(suffix)]
    return title.strip()


def _parse_date(value: str) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def parse_irri_news_html(content: str) -> List[ParsedRSSItem]:
    """将 IRRI 新闻列表 HTML 转换为项目通用的 RSS 条目。"""
    parser = _IRRINewsCardParser()
    parser.feed(content)
    parser.close()

    items: List[ParsedRSSItem] = []
    seen_urls = set()
    for card in parser.cards:
        url = _canonical_irri_url(card.get("href", ""))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        items.append(
            ParsedRSSItem(
                title=card["title"],
                url=url,
                published_at=_parse_date(card.get("date", "")),
                summary=card.get("summary") or None,
                author="International Rice Research Institute",
                guid=url,
            )
        )

    if not items:
        raise ValueError("IRRI 新闻页中未找到新闻卡片，页面结构可能已变更")

    return items
