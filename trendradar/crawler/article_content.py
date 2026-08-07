# coding=utf-8
"""为 AI 筛选获取新闻正文，并按正文、摘要、标题顺序安全降级。"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Dict, List, Optional
from urllib.parse import urlsplit

import requests

from trendradar.crawler.elsevier import ElsevierFullTextClient, extract_sciencedirect_pii
from trendradar.crawler.http import DirectFirstSession


_BLOCKED_TAGS = {
    "aside", "button", "footer", "form", "header", "menu", "nav",
    "noscript", "select", "style", "svg", "template",
}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
_POSITIVE_HINTS = (
    "article", "body", "content", "entry", "main", "news", "post",
    "story", "text",
)
_NEGATIVE_HINTS = (
    "advert", "breadcrumb", "cookie", "footer", "header", "menu", "nav",
    "newsletter", "related", "share", "sidebar", "social", "subscribe",
)
_PAYWALL_MARKERS = (
    "subscribe to read", "subscription required", "purchase access",
    "institutional access", "sign in to access", "register to read",
    "this article is available to subscribers", "付费阅读", "订阅后阅读",
)


@dataclass(frozen=True)
class ArticleContent:
    """传给 AI 的内容及其证据等级。"""

    text: str
    level: str
    risk_warning: str
    fetch_status: str


@dataclass
class _Frame:
    tag: str
    attrs: Dict[str, str]


class _ArticleHTMLParser(HTMLParser):
    """提取正文段落、页面摘要和 JSON-LD articleBody。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: List[_Frame] = []
        self.paragraph_stack: List[Dict] = []
        self.paragraphs: List[tuple[int, str]] = []
        self.meta_descriptions: List[str] = []
        self.json_ld_scripts: List[List[str]] = []
        self._json_ld_depth = 0

    def handle_starttag(self, tag: str, attrs_list) -> None:
        tag = tag.lower()
        attrs = {key.lower(): value or "" for key, value in attrs_list}

        if tag == "meta":
            key = (attrs.get("name") or attrs.get("property") or "").lower()
            if key in {"description", "og:description", "twitter:description"}:
                value = _clean_text(attrs.get("content", ""))
                if value:
                    self.meta_descriptions.append(value)

        if tag not in _VOID_TAGS:
            self.stack.append(_Frame(tag, attrs))

        if tag == "script" and "ld+json" in attrs.get("type", "").lower():
            self._json_ld_depth = len(self.stack)
            self.json_ld_scripts.append([])

        if tag == "p":
            self.paragraph_stack.append({
                "depth": len(self.stack),
                "parts": [],
                "score": self._context_score(),
                "blocked": self._is_blocked_context(),
            })

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "p" and self.paragraph_stack:
            current = self.paragraph_stack.pop()
            value = _clean_text(" ".join(current["parts"]))
            if not current["blocked"] and _is_meaningful_paragraph(value):
                self.paragraphs.append((current["score"], value))

        if self._json_ld_depth and len(self.stack) == self._json_ld_depth:
            self._json_ld_depth = 0

        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._json_ld_depth:
            self.json_ld_scripts[-1].append(data)
        if self.paragraph_stack and data.strip():
            self.paragraph_stack[-1]["parts"].append(data)

    def _is_blocked_context(self) -> bool:
        return any(frame.tag in _BLOCKED_TAGS for frame in self.stack)

    def _context_score(self) -> int:
        score = 0
        for frame in self.stack:
            context = " ".join((
                frame.attrs.get("class", ""),
                frame.attrs.get("id", ""),
                frame.attrs.get("role", ""),
            )).lower()
            if frame.tag in {"article", "main"}:
                score += 4
            if any(hint in context for hint in _POSITIVE_HINTS):
                score += 2
            if any(hint in context for hint in _NEGATIVE_HINTS):
                score -= 5
        return score

    def article_body_from_json_ld(self) -> str:
        bodies: List[str] = []

        def visit(value) -> None:
            if isinstance(value, dict):
                body = value.get("articleBody")
                if isinstance(body, str):
                    cleaned = _clean_text(body)
                    if cleaned:
                        bodies.append(cleaned)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        for chunks in self.json_ld_scripts:
            raw = "".join(chunks).strip()
            if not raw:
                continue
            try:
                visit(json.loads(html.unescape(raw)))
            except (json.JSONDecodeError, TypeError):
                continue
        return max(bodies, key=len, default="")


class ArticleContentFetcher:
    """正文优先；无法获取时依次回退到摘要和标题。"""

    def __init__(
        self,
        *,
        timeout: int = 12,
        max_content_chars: int = 5000,
        min_body_chars: int = 300,
        use_proxy: bool = False,
        proxy_url: str = "",
        elsevier_api_key: str = "",
        elsevier_inst_token: str = "",
        elsevier_client: Optional[ElsevierFullTextClient] = None,
    ) -> None:
        self.timeout = max(1, int(timeout))
        self.max_content_chars = max(500, int(max_content_chars))
        self.min_body_chars = max(100, int(min_body_chars))
        self.session = DirectFirstSession(
            headers={
                "User-Agent": "TrendRadar/6.10 Article Reader",
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            use_proxy=use_proxy,
            proxy_url=proxy_url,
        )
        self.elsevier_client = elsevier_client
        if self.elsevier_client is None and elsevier_api_key and elsevier_inst_token:
            self.elsevier_client = ElsevierFullTextClient(
                elsevier_api_key,
                elsevier_inst_token,
                timeout=self.timeout,
            )

    def get(self, item: Dict) -> ArticleContent:
        title = _clean_text(str(item.get("title", "")))
        rss_summary = _clean_html_text(str(item.get("summary", "")))
        url = str(item.get("url") or item.get("mobile_url") or "").strip()

        page_summary = ""
        fetch_status = "missing_url"
        fetch_note = "未提供可访问的原文链接"

        if url and self._is_public_http_url(url):
            if self.elsevier_client and extract_sciencedirect_pii(url):
                try:
                    api_result = self.elsevier_client.fetch(url)
                    if len(api_result.text) >= self.min_body_chars:
                        return self._build_full_text_content(
                            api_result.text,
                            fetch_status="elsevier_full_text",
                            source_note="正文来自 Elsevier Article Retrieval API",
                        )
                except Exception:
                    # API 客户端异常不能中断既有 HTML → RSS → 标题降级链路。
                    pass
            try:
                request_url = _build_fetch_url(url)
                response = self.session.get(request_url, timeout=self.timeout)
                if response.status_code in {401, 402, 403}:
                    fetch_status = "paywalled_or_forbidden"
                    fetch_note = f"原文访问受限（HTTP {response.status_code}）"
                else:
                    response.raise_for_status()
                    content_type = response.headers.get("Content-Type", "").lower()
                    if "html" not in content_type and not response.text.lstrip().startswith("<"):
                        fetch_status = "unsupported_content"
                        fetch_note = f"原文不是可解析的 HTML（{content_type or '未知类型'}）"
                    else:
                        response.encoding = response.apparent_encoding or response.encoding
                        body, page_summary, is_paywalled = self._extract(response.text)
                        if body:
                            return self._build_full_text_content(body, fetch_status="full_text")
                        if is_paywalled:
                            fetch_status = "paywalled"
                            fetch_note = "页面存在付费墙或订阅限制，未取得正文"
                        else:
                            fetch_status = "body_unavailable"
                            fetch_note = "页面可访问，但未提取到足够正文"
            except requests.Timeout:
                fetch_status = "timeout"
                fetch_note = "原文请求超时"
            except requests.RequestException as exc:
                fetch_status = "request_failed"
                fetch_note = f"原文请求失败（{type(exc).__name__}）"
            except (UnicodeError, ValueError) as exc:
                fetch_status = "parse_failed"
                fetch_note = f"原文解析失败（{type(exc).__name__}）"
        elif url:
            fetch_status = "blocked_url"
            fetch_note = "原文链接不是允许访问的公网 HTTP(S) 地址"

        summary = rss_summary or page_summary
        if summary:
            summary = summary[:self.max_content_chars].rstrip()
            return ArticleContent(
                text=summary,
                level="summary",
                risk_warning=(
                    f"{fetch_note}；当前仅依据摘要判断，无法完整核实方法、样本量、"
                    "局限性及田间验证情况。"
                ),
                fetch_status=fetch_status,
            )

        return ArticleContent(
            text=title,
            level="title_only",
            risk_warning=(
                f"{fetch_note}，且无可用摘要；当前仅依据标题判断，可靠性较低，"
                "不得推断标题未明确说明的研究方法、结果、因果关系或证据阶段。"
            ),
            fetch_status=fetch_status,
        )

    def _build_full_text_content(
        self,
        body: str,
        *,
        fetch_status: str,
        source_note: str = "",
    ) -> ArticleContent:
        truncated = len(body) > self.max_content_chars
        body = body[:self.max_content_chars].rstrip()
        risk = (
            "正文已截断，仅能依据所提供正文片段判断；"
            "不得推断片段中未出现的方法、结果或验证阶段。"
            if truncated else
            "已获取正文，但仍应以原始论文、数据和补充材料作为最终证据。"
        )
        if source_note:
            risk = f"{source_note}；{risk}"
        return ArticleContent(
            text=body,
            level="full_text",
            risk_warning=risk,
            fetch_status=fetch_status,
        )

    def _extract(self, content: str) -> tuple[str, str, bool]:
        parser = _ArticleHTMLParser()
        parser.feed(content)
        parser.close()

        json_body = parser.article_body_from_json_ld()
        if len(json_body) >= self.min_body_chars:
            return json_body, _best_summary(parser.meta_descriptions), False

        positive = [text for score, text in parser.paragraphs if score > 0]
        paragraphs = positive if sum(map(len, positive)) >= self.min_body_chars else [
            text for score, text in parser.paragraphs if score >= 0
        ]
        paragraphs = _deduplicate_paragraphs(paragraphs)
        body = "\n\n".join(paragraphs)
        page_summary = _best_summary(parser.meta_descriptions)
        lowered = content.lower()
        is_paywalled = any(marker in lowered for marker in _PAYWALL_MARKERS)

        if len(body) < self.min_body_chars:
            body = ""
        return body, page_summary, is_paywalled

    @staticmethod
    def _is_public_http_url(url: str) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        try:
            address = ipaddress.ip_address(host)
            return not (
                address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved
            )
        except ValueError:
            pass

        # 明确阻止解析到本地地址；DNS 失败交由 requests 产生正常降级结果。
        try:
            for result in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM):
                address = ipaddress.ip_address(result[4][0])
                if (
                    address.is_private or address.is_loopback or address.is_link_local
                    or address.is_multicast or address.is_reserved
                ):
                    return False
        except (socket.gaierror, ValueError):
            return True
        return True


def _build_fetch_url(url: str) -> str:
    """对当前环境无法直连的三个官网使用只读代理。"""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host == "www.irri.org":
        return (
            "https://www-irri-org.translate.goog"
            f"{parsed.path or '/'}"
            f"?_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en"
        )
    if host == "www.cgiar.org":
        return (
            "https://www-cgiar-org.translate.goog"
            f"{parsed.path or '/'}"
            f"?_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en"
        )
    if host == "www.aphis.usda.gov":
        return (
            "https://www-aphis-usda-gov.translate.goog"
            f"{parsed.path or '/'}"
            f"?_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en"
        )
    return url


def _clean_text(value: str) -> str:
    return " ".join(html.unescape(value).replace("\xa0", " ").split())


def _clean_html_text(value: str) -> str:
    value = re.sub(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return _clean_text(value)


def _is_meaningful_paragraph(value: str) -> bool:
    if len(value) < 40:
        return False
    lowered = value.lower()
    if any(hint in lowered for hint in (
        "accept all cookies", "all rights reserved", "privacy policy",
        "terms and conditions", "follow us on", "sign up for",
    )):
        return False
    letters = sum(character.isalpha() for character in value)
    return letters >= max(12, len(value) // 5)


def _deduplicate_paragraphs(values: List[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        key = re.sub(r"\W+", "", value).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _best_summary(values: List[str]) -> str:
    useful = [value for value in values if 40 <= len(value) <= 2000]
    return max(useful, key=len, default="")
