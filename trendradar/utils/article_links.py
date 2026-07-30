from urllib.parse import urlsplit


RICE_SCIENCE_FEED_ID = "rice-science"
SCIENCEDIRECT_HOST = "www.sciencedirect.com"
PII_PATH_PREFIX = "/science/article/pii/"
JINA_READER_PREFIX = "https://r.jina.ai/http://www.sciencedirect.com"


def build_reader_url(source_id: str, url: str) -> str:
    if source_id != RICE_SCIENCE_FEED_ID or not url:
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
    return f"{JINA_READER_PREFIX}{PII_PATH_PREFIX}{pii}"
