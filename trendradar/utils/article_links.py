from urllib.parse import quote, urlsplit


RICE_SCIENCE_FEED_ID = "rice-science"
SCIENCEDIRECT_HOST = "www.sciencedirect.com"
PII_PATH_PREFIX = "/science/article/pii/"
SEMANTIC_SCHOLAR_SEARCH_PREFIX = "https://www.semanticscholar.org/search?q="


def build_reader_url(source_id: str, url: str, title: str) -> str:
    if source_id != RICE_SCIENCE_FEED_ID or not url or not isinstance(title, str):
        return ""
    search_title = title.strip()
    if not search_title:
        return ""
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    if (parsed.hostname or "").lower() != SCIENCEDIRECT_HOST:
        return ""
    if not parsed.path.startswith(PII_PATH_PREFIX):
        return ""
    pii = parsed.path[len(PII_PATH_PREFIX):].strip("/")
    if not pii or "/" in pii or not pii.isalnum():
        return ""
    return f"{SEMANTIC_SCHOLAR_SEARCH_PREFIX}{quote(search_title, safe='')}"
