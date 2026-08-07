"""Client for retrieving ScienceDirect full text through Elsevier's API."""

from dataclasses import dataclass
import re
from typing import Optional
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import requests


@dataclass(frozen=True)
class ElsevierFetchResult:
    text: str
    status: str


def extract_sciencedirect_pii(url: str) -> Optional[str]:
    """Return a PII only from a canonical ScienceDirect article URL."""
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or (parsed.hostname or "").lower()
        not in {"sciencedirect.com", "www.sciencedirect.com"}
    ):
        return None
    match = re.fullmatch(
        r"/science/article/pii/([SB][A-Z0-9]{16})",
        parsed.path.rstrip("/"),
    )
    return match.group(1) if match else None


def parse_full_text_xml(xml_content: bytes) -> str:
    """Extract ordered paragraphs from an Elsevier full-text XML article body."""
    root = ET.fromstring(xml_content)
    parents = {
        child: parent
        for parent in root.iter()
        for child in parent
    }
    bodies = [
        element
        for element in root.iter()
        if _local_name(element.tag) == "body"
        and _is_article_body(element, parents)
    ]
    if len(bodies) != 1:
        return ""

    body = bodies[0]
    paragraph_tags = {"para", "simple-para"}
    paragraphs = [
        _normalise_text(element.itertext())
        for element in body.iter()
        if _local_name(element.tag) in paragraph_tags
        and not _has_paragraph_ancestor(element, body, parents, paragraph_tags)
    ]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


class ElsevierFullTextClient:
    def __init__(self, api_key: str, inst_token: str, *, timeout: int = 12) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "X-ELS-APIKey": api_key,
                "X-ELS-Insttoken": inst_token,
                "Accept": "text/xml",
            }
        )

    def fetch(self, url: str) -> ElsevierFetchResult:
        pii = extract_sciencedirect_pii(url)
        if not pii:
            return ElsevierFetchResult("", "unsupported_url")
        try:
            response = self.session.get(
                f"https://api.elsevier.com/content/article/pii/{pii}",
                params={"view": "FULL"},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.Timeout:
            return ElsevierFetchResult("", "timeout")
        except requests.RequestException:
            return ElsevierFetchResult("", "request_failed")
        if response.status_code != 200:
            return ElsevierFetchResult("", f"http_{response.status_code}")
        try:
            text = parse_full_text_xml(response.content)
        except (ET.ParseError, UnicodeError, ValueError):
            return ElsevierFetchResult("", "invalid_xml")
        return ElsevierFetchResult(text, "full_text" if text else "body_unavailable")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _is_article_body(body, parents) -> bool:
    article_seen = False
    ancestor = parents.get(body)
    while ancestor is not None:
        name = _local_name(ancestor.tag)
        if name == "article":
            article_seen = True
        elif name == "originalText" and article_seen:
            return True
        ancestor = parents.get(ancestor)
    return False


def _has_paragraph_ancestor(element, body, parents, paragraph_tags) -> bool:
    ancestor = parents.get(element)
    while ancestor is not None and ancestor is not body:
        if _local_name(ancestor.tag) in paragraph_tags:
            return True
        ancestor = parents.get(ancestor)
    return False


def _normalise_text(parts) -> str:
    return re.sub(r"\s+", " ", "".join(parts)).strip()
