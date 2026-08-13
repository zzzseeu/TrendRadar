# coding=utf-8
"""将没有 RSS 的科研、监管和种业官网新闻列表转换为通用 RSS 条目。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Pattern, Union
from urllib.parse import urljoin, urlsplit, urlunsplit

from .parser import ParsedRSSItem


_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
_GENERIC_LINK_TEXT = {
    "", "read more", "learn more", "view more", "more", "details",
    "查看详情", "详情", "更多", "查看更多",
}
_CONTAINER_HINTS = (
    "article", "card", "content-block", "item", "list", "media",
    "result", "row", "teaser", "tile", "view",
)
_STRONG_CONTAINER_HINTS = (
    "article", "card", "content-block", "row", "slide", "teaser", "tile",
)
_TITLE_HINTS = ("headline", "title")
_SUMMARY_HINTS = ("body", "description", "summary", "text")
_NANFAN_RICE_TERMS = (
    "水稻", "稻米", "稻谷", "稻作", "南繁",
    "种业", "育种", "制种", "种质", "品种",
)


@dataclass
class _Node:
    tag: str
    attrs: Dict[str, str]
    parent: Optional["_Node"] = None
    children: List[Union["_Node", str]] = field(default_factory=list)


class _DOMParser(HTMLParser):
    """任务所需的轻量 HTML 树，不依赖 BeautifulSoup/lxml。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs_list) -> None:
        node = _Node(tag.lower(), {k: v or "" for k, v in attrs_list}, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag.lower() not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs_list) -> None:
        self.handle_starttag(tag, attrs_list)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


@dataclass(frozen=True)
class _WebNewsProfile:
    author: str
    patterns: tuple[Pattern[str], ...]
    require_date: bool = False
    required_terms: tuple[str, ...] = ()

    def accepts(self, url: str) -> bool:
        return any(pattern.search(url) for pattern in self.patterns)


def _patterns(*values: str) -> tuple[Pattern[str], ...]:
    return tuple(re.compile(value, re.IGNORECASE) for value in values)


_PROFILES: Dict[str, _WebNewsProfile] = {
    "cnrri-research": _WebNewsProfile(
        "中国水稻研究所",
        _patterns(r"^https://cnrri\.caas\.cn/kyjz/[0-9a-f]{32}\.htm$"),
    ),
    "hzau-crop-lab": _WebNewsProfile(
        "华中农业大学作物遗传改良全国重点实验室",
        _patterns(r"^https://croplab\.hzau\.edu\.cn/info/1039/\d+\.htm$"),
        require_date=True,
    ),
    "cemps-research": _WebNewsProfile(
        "中国科学院分子植物科学卓越创新中心",
        _patterns(r"^https://cemps\.cas\.cn/kyjz/kyjz20\d{2}/20\d{4}/t20\d+_\d+\.html$"),
    ),
    "caas-crop-research": _WebNewsProfile(
        "中国农业科学院作物科学研究所",
        _patterns(r"^https://ics\.caas\.cn/xwdt/kyjz1/[0-9a-f]{32}\.htm$"),
    ),
    "moa-seed-news": _WebNewsProfile(
        "农业农村部种业管理司",
        _patterns(
            r"^https?://zys\.moa\.gov\.cn/gzdt/20\d{4}/t20\d+_\d+\.htm$",
            r"^https?://www\.moa\.gov\.cn/xw/zwdt/20\d{4}/t20\d+_\d+\.htm$",
        ),
    ),
    "moa-seed-notices": _WebNewsProfile(
        "农业农村部种业管理司",
        _patterns(r"^https?://zys\.moa\.gov\.cn/gsgg/20\d{4}/t20\d+_\d+\.htm$"),
    ),
    "moa-seed-policy": _WebNewsProfile(
        "农业农村部种业管理司",
        _patterns(
            r"^https?://zys\.moa\.gov\.cn/(?:flfg|zcwj|pzqltgl)/20\d{4}/t20\d+_\d+\.htm$"
        ),
    ),
    "aphis-biotech": _WebNewsProfile(
        "USDA APHIS Biotechnology Regulatory Services",
        _patterns(
            r"^https://(?:www|direct)\.aphis\.usda\.gov/news/(?:program-update|agency-announcements)/"
        ),
    ),
    "eu-plants-policy": _WebNewsProfile(
        "European Commission - Food Safety",
        _patterns(
            r"^https://food\.ec\.europa\.eu/food-safety-news/",
            r"^https://ec\.europa\.eu/commission/presscorner/detail/",
            r"^https://ec\.europa\.eu/newsroom/sante/newsletter-archives/",
            r"^https://food\.ec\.europa\.eu/plants/genetically-modified-organisms/public-consultations_en$",
        ),
        require_date=True,
    ),
    "cgiar-news": _WebNewsProfile(
        "CGIAR",
        _patterns(r"^https://www\.cgiar\.org/news-events/news/"),
    ),
    "vietnam-ppd": _WebNewsProfile(
        "越南种植与植物保护局",
        _patterns(r"^https://(?:www\.)?ppd\.gov\.vn/.+\.html$"),
        require_date=True,
    ),
    "ndrc-rice": _WebNewsProfile(
        "国家发展改革委价格司",
        _patterns(
            r"^https://www\.ndrc\.gov\.cn/fzggw/jgsj/jgs/sjdt/20\d{4}/t20\d+_\d+\.html$"
        ),
        require_date=True,
    ),
    "stats-grain": _WebNewsProfile(
        "国家统计局",
        _patterns(r"^https://www\.stats\.gov\.cn/sj/zxfb/20\d{4}/t20\d+_\d+\.html$"),
        require_date=True,
    ),
    "heilongjiang-rice": _WebNewsProfile(
        "黑龙江省农业农村厅",
        _patterns(r"^https://nynct\.hlj\.gov\.cn/nynct/c\d+/20\d{4}/c00_\d+\.shtml$"),
        require_date=True,
        required_terms=("水稻", "稻米", "稻谷", "稻作"),
    ),
    "hunan-rice": _WebNewsProfile(
        "湖南省农业农村厅",
        _patterns(r"^https://agri\.hunan\.gov\.cn/agri/.+/20\d{4}/t20\d+_\d+\.html$"),
        require_date=True,
        required_terms=("水稻", "稻米", "稻谷", "稻作"),
    ),
    "hubei-rice": _WebNewsProfile(
        "湖北省农业农村厅",
        _patterns(r"^https://nyt\.hubei\.gov\.cn/.+/20\d{4}/t20\d+_\d+\.shtml$"),
        require_date=True,
        required_terms=("水稻", "稻米", "稻谷", "稻作"),
    ),
    "jiangsu-rice": _WebNewsProfile(
        "江苏省农业农村厅",
        _patterns(
            r"^https://nynct\.jiangsu\.gov\.cn/art/20\d{2}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html$"
        ),
        require_date=True,
        required_terms=("水稻", "稻米", "稻谷", "稻作"),
    ),
    "hainan-nanfan-news": _WebNewsProfile(
        "海南省农业农村厅中国南繁",
        _patterns(r"^https?://"),
        require_date=True,
    ),
    "sanya-agri-documents": _WebNewsProfile(
        "三亚市农业农村局",
        _patterns(r"^https://ny\.sanya\.gov\.cn/nyjsite/bmwjxx/20\d{4}/[0-9a-f]+\.shtml$"),
        require_date=True,
        required_terms=_NANFAN_RICE_TERMS,
    ),
    "sanya-agri-news": _WebNewsProfile(
        "三亚市农业农村局",
        _patterns(r"^https://ny\.sanya\.gov\.cn/nyjsite/gzdt/20\d{4}/[0-9a-f]+\.shtml$"),
        require_date=True,
        required_terms=_NANFAN_RICE_TERMS,
    ),
    "philrice-news": _WebNewsProfile(
        "PhilRice",
        _patterns(r"^https://www\.philrice\.gov\.ph/(?!news/?$)[^/?#]+/$"),
        require_date=True,
    ),
    "winall-news": _WebNewsProfile(
        "安徽荃银高科种业股份有限公司",
        _patterns(r"^https?://www\.winallseed\.com/article/\d+/52\.html$"),
    ),
    "syngenta-news": _WebNewsProfile(
        "Syngenta Group",
        _patterns(r"^https://www\.syngentagroup\.com/newsroom/20\d{2}/[^/?#]+$"),
    ),
}

_DOCUMENT_PROFILES: Dict[str, _WebNewsProfile] = {
    "amis-rice": _WebNewsProfile(
        "AMIS Market Monitor",
        _patterns(
            r"^https://(?:legacy\.)?amis-outlook\.org/.+\.pdf$"
        ),
        require_date=True,
    ),
    "japan-maff-rice": _WebNewsProfile(
        "日本农林水产省",
        _patterns(r"^https://www\.maff\.go\.jp/.+\.pdf$"),
        require_date=True,
    ),
}


def _iter_nodes(node: _Node) -> Iterable[_Node]:
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _iter_nodes(child)


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _text(node: _Node) -> str:
    parts: List[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        else:
            parts.append(_text(child))
    return _clean_text(" ".join(parts))


def _classes(node: _Node) -> str:
    return node.attrs.get("class", "").lower()


def _nearest_container(anchor: _Node) -> _Node:
    node = anchor.parent
    fallback = anchor.parent or anchor
    weak_match: Optional[_Node] = None
    depth = 0
    while node is not None and depth < 8:
        classes = _classes(node)
        text = _text(node)
        if 12 <= len(text) <= 3000:
            is_title_wrapper = any(hint in classes for hint in _TITLE_HINTS)
            if node.tag in {"article", "li"} or (
                not is_title_wrapper
                and any(hint in classes for hint in _STRONG_CONTAINER_HINTS)
            ):
                return node
            if weak_match is None and any(hint in classes for hint in _CONTAINER_HINTS):
                weak_match = node
        fallback = node
        node = node.parent
        depth += 1
    return weak_match or fallback


def _is_title(value: str) -> bool:
    value = _clean_text(value)
    return (
        8 <= len(value) <= 300
        and value.casefold() not in _GENERIC_LINK_TEXT
        and not re.fullmatch(r"20\d{2}", value)
    )


def _best_title(anchor: _Node, container: _Node) -> str:
    candidates: List[tuple[int, int, str]] = []

    for node in _iter_nodes(container):
        value = _text(node)
        if not _is_title(value):
            continue
        classes = _classes(node)
        if node.tag in {"h1", "h2", "h3", "h4"}:
            candidates.append((0, len(value), value))
        elif any(hint in classes for hint in _TITLE_HINTS) or "tit" in classes.split():
            candidates.append((1, len(value), value))

    anchor_text = _text(anchor)
    if _is_title(anchor_text):
        candidates.append((2, len(anchor_text), anchor_text))

    attr_title = _clean_text(anchor.attrs.get("title", ""))
    if _is_title(attr_title):
        candidates.append((3, len(attr_title), attr_title))

    if not candidates:
        return ""

    # 优先标题节点；同一优先级取较短文本，避免把标题、日期和摘要拼成一条。
    candidates.sort(key=lambda item: (item[0], item[1]))
    title = candidates[0][2]
    # 部分中文站把发布日期放在链接文本首尾，展示时去掉重复日期。
    title = re.sub(r"^20\d{2}[-./]\d{1,2}[-./]\d{1,2}\s+", "", title)
    title = re.sub(r"\s+20\d{2}[-./]\d{1,2}[-./]\d{1,2}$", "", title)
    return title.strip()


def _best_summary(container: _Node, title: str) -> Optional[str]:
    candidates: List[str] = []
    for node in _iter_nodes(container):
        classes = _classes(node)
        if not any(hint in classes for hint in _SUMMARY_HINTS):
            continue
        value = _text(node)
        if value and value != title and title not in value and 20 <= len(value) <= 1200:
            candidates.append(value)
    if not candidates:
        return None
    summary = min(candidates, key=len)
    return summary[:500] + ("..." if len(summary) > 500 else "")


def _parse_date_value(value: str) -> Optional[str]:
    value = _clean_text(value).strip("[]()")
    formats = (
        "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d",
        "%m/%d/%y", "%m/%d/%Y",
        "%B %d, %Y", "%b %d, %Y", "%d %B %Y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _extract_date(container: _Node, url: str) -> Optional[str]:
    for node in _iter_nodes(container):
        if node.tag == "time":
            date = _parse_date_value(node.attrs.get("datetime", "")) or _parse_date_value(_text(node))
            if date:
                return date

    text = _text(container)
    date_patterns = (
        r"20\d{2}[-./]\d{1,2}[-./]\d{1,2}",
        r"\d{1,2}/\d{1,2}/(?:20)?\d{2}",
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+\d{1,2},\s+20\d{2}",
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"\s+\d{1,2},\s+20\d{2}",
        r"\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+20\d{2}",
    )
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            date = _parse_date_value(match.group(0))
            if date:
                return date

    # 中科院、农业农村部等站点将发布日期编码在 URL 中。
    match = re.search(r"/(?:t)?(20\d{2})(\d{2})(\d{2})(?:_|/)", url)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


def _canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.netloc.lower()
    path = parsed.path

    if host == "www-cgiar-org.translate.goog":
        host = "www.cgiar.org"
    elif host == "www-aphis-usda-gov.translate.goog":
        host = "www.aphis.usda.gov"
    elif host == "www.winallseed.com":
        host = "www.winallseed.com"

    scheme = "https" if host else parsed.scheme
    return urlunsplit((scheme, host, path, "", ""))


def parse_web_news_html(
    content: str,
    feed_id: str,
    page_url: str,
) -> List[ParsedRSSItem]:
    """解析已注册官网的新闻列表页。"""
    profile = _PROFILES.get(feed_id)
    if not profile:
        raise ValueError(f"未注册的网页新闻源: {feed_id}")

    parser = _DOMParser()
    parser.feed(content)
    parser.close()

    items: List[ParsedRSSItem] = []
    seen_urls = set()
    for anchor in (node for node in _iter_nodes(parser.root) if node.tag == "a"):
        href = anchor.attrs.get("href", "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        # 首页轮播图常复用新闻 URL，但空图片链接不含标题，会误取到整块栏目标题。
        if not _text(anchor) and "pic" in _classes(anchor).split():
            continue

        url = _canonical_url(urljoin(page_url, href))
        if url in seen_urls or not profile.accepts(url):
            continue

        container = _nearest_container(anchor)
        title = _best_title(anchor, container)
        if not title:
            continue

        published_at = _extract_date(container, url)
        if profile.require_date and not published_at:
            continue

        summary = _best_summary(container, title)
        if profile.required_terms:
            evidence = _text(container).casefold()
            if not any(term.casefold() in evidence for term in profile.required_terms):
                continue

        seen_urls.add(url)
        items.append(
            ParsedRSSItem(
                title=title,
                url=url,
                published_at=published_at,
                summary=summary,
                author=profile.author,
                guid=url,
            )
        )

    if not items:
        raise ValueError(f"{profile.author} 页面中未找到新闻条目，页面结构可能已变更")
    return items


def parse_official_document_html(
    content: str,
    feed_id: str,
    page_url: str,
) -> List[ParsedRSSItem]:
    """解析官方专题页中的带日期 PDF 文档链接。"""
    profile = _DOCUMENT_PROFILES.get(feed_id)
    if not profile:
        raise ValueError(f"未注册的官方文档源: {feed_id}")

    parser = _DOMParser()
    parser.feed(content)
    parser.close()

    items: List[ParsedRSSItem] = []
    seen_urls = set()
    for anchor in (node for node in _iter_nodes(parser.root) if node.tag == "a"):
        href = anchor.attrs.get("href", "").strip()
        if not href:
            continue
        url = _canonical_url(urljoin(page_url, href))
        if url in seen_urls or not profile.accepts(url):
            continue
        container = _nearest_container(anchor)
        title = _best_title(anchor, container)
        published_at = _extract_date(container, url)
        if not title or not published_at:
            continue
        seen_urls.add(url)
        items.append(ParsedRSSItem(
            title=title,
            url=url,
            published_at=published_at,
            summary=_best_summary(container, title),
            author=profile.author,
            guid=url,
        ))

    if not items:
        raise ValueError(f"{profile.author} 页面中未找到官方文档")
    return items


def parse_corteva_news_json(content: str) -> List[ParsedRSSItem]:
    """解析 Corteva 官网公开的 Media Center JSON 数据。"""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Corteva 新闻 JSON 解析失败: {exc}") from exc

    items: List[ParsedRSSItem] = []
    seen_urls = set()
    for record in payload.get("results", []):
        title = _clean_text(record.get("articleTitle", ""))
        path = record.get("articlePagePath", "")
        if not title or not path:
            continue
        url = _canonical_url(urljoin("https://www.corteva.com/", path))
        if url in seen_urls:
            continue
        seen_urls.add(url)

        published_at = _parse_date_value(record.get("customDisplayDate", ""))
        summary = _clean_text(record.get("shortDescription", "")) or None
        if summary and len(summary) > 500:
            summary = summary[:500] + "..."

        items.append(
            ParsedRSSItem(
                title=title,
                url=url,
                published_at=published_at,
                summary=summary,
                author="Corteva Agriscience",
                guid=url,
            )
        )

    if not items:
        raise ValueError("Corteva Media Center JSON 中未找到新闻条目")
    return items
