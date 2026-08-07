import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.core.loader import _load_ai_filter_config
from trendradar.crawler.article_content import ArticleContent, ArticleContentFetcher
from trendradar.crawler.elsevier import (
    ElsevierFetchResult,
    ElsevierFullTextClient,
    extract_sciencedirect_pii,
    parse_full_text_xml,
)


FULL_TEXT_XML = b"""\
<full-text-retrieval-response xmlns:ce="urn:ce" xmlns:ja="urn:ja">
  <originalText>
    <ja:article><ja:body><ce:sections><ce:section>
      <ce:para>First <ce:bold>result</ce:bold> paragraph.</ce:para>
      <ce:para>Second result paragraph.</ce:para>
    </ce:section></ce:sections></ja:body></ja:article>
  </originalText>
</full-text-retrieval-response>
"""
METADATA_ONLY_XML = b"<full-text-retrieval-response><coredata /></full-text-retrieval-response>"
SCIENCEDIRECT_URL = "https://www.sciencedirect.com/science/article/pii/S1672630826000545"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_html_session(content: str):
    response = MagicMock(status_code=200, text=content)
    response.headers = {"Content-Type": "text/html"}
    response.apparent_encoding = "utf-8"
    return MagicMock(get=MagicMock(return_value=response))


class ElsevierFullTextClientTests(unittest.TestCase):
    def test_extracts_only_sciencedirect_pii_urls(self):
        self.assertEqual(extract_sciencedirect_pii(f"{SCIENCEDIRECT_URL}?dgcid=rss"), "S1672630826000545")
        self.assertIsNone(extract_sciencedirect_pii("https://example.com/science/article/pii/S123"))

    @patch("trendradar.crawler.elsevier.requests.Session")
    def test_requests_full_xml_with_server_side_headers_and_no_proxy(self, session_factory):
        response = MagicMock(status_code=200, content=FULL_TEXT_XML)
        session_factory.return_value.get.return_value = response

        client = ElsevierFullTextClient("test-api-key", "test-inst-token", timeout=12)
        result = client.fetch(SCIENCEDIRECT_URL)

        self.assertFalse(session_factory.return_value.trust_env)
        session_factory.return_value.headers.update.assert_called_once_with(
            {
                "X-ELS-APIKey": "test-api-key",
                "X-ELS-Insttoken": "test-inst-token",
                "Accept": "text/xml",
            }
        )
        session_factory.return_value.get.assert_called_once_with(
            "https://api.elsevier.com/content/article/pii/S1672630826000545",
            params={"view": "FULL"},
            timeout=12,
        )
        self.assertEqual(result.status, "full_text")
        self.assertIn("First result paragraph", result.text)

    def test_parse_uses_article_body_paragraphs_in_order_without_nested_duplicates(self):
        text = parse_full_text_xml(FULL_TEXT_XML)
        self.assertEqual(text.count("First result paragraph"), 1)
        self.assertLess(text.index("First result paragraph"), text.index("Second result paragraph"))

    @patch("trendradar.crawler.elsevier.requests.Session")
    def test_non_200_timeout_broken_xml_and_metadata_only_are_unavailable(self, session_factory):
        session_factory.return_value.get.side_effect = [
            MagicMock(status_code=401, content=b"auth failed"),
            requests.Timeout("slow"),
            MagicMock(status_code=200, content=b"<broken"),
            MagicMock(status_code=200, content=METADATA_ONLY_XML),
        ]
        client = ElsevierFullTextClient("test-api-key", "test-inst-token")

        statuses = [client.fetch(SCIENCEDIRECT_URL).status for _ in range(4)]

        self.assertEqual(statuses, ["http_401", "timeout", "invalid_xml", "body_unavailable"])


class ArticleContentElsevierIntegrationTests(unittest.TestCase):
    def test_sciencedirect_api_full_text_is_used_before_html(self):
        api_client = MagicMock()
        api_client.fetch.return_value = ElsevierFetchResult("A" * 800, "full_text")
        fetcher = ArticleContentFetcher(
            min_body_chars=300,
            elsevier_client=api_client,
        )
        fetcher.session = MagicMock()
        fetcher._is_public_http_url = MagicMock(return_value=True)

        result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

        self.assertEqual(result.level, "full_text")
        self.assertEqual(result.fetch_status, "elsevier_full_text")
        fetcher.session.get.assert_not_called()

    def test_metadata_only_api_response_falls_back_to_existing_html(self):
        api_client = MagicMock()
        api_client.fetch.return_value = ElsevierFetchResult("", "body_unavailable")
        fetcher = ArticleContentFetcher(elsevier_client=api_client)
        fetcher.session = build_html_session("<article><p>" + "B" * 400 + "</p></article>")
        fetcher._is_public_http_url = MagicMock(return_value=True)

        result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

        self.assertEqual(result.level, "full_text")
        self.assertEqual(result.fetch_status, "full_text")

    def test_short_api_response_falls_back_to_existing_html(self):
        api_client = MagicMock()
        api_client.fetch.return_value = ElsevierFetchResult("A" * 299, "full_text")
        fetcher = ArticleContentFetcher(min_body_chars=300, elsevier_client=api_client)
        fetcher.session = build_html_session("<article><p>" + "B" * 400 + "</p></article>")
        fetcher._is_public_http_url = MagicMock(return_value=True)

        result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

        self.assertEqual(result.level, "full_text")
        self.assertEqual(result.fetch_status, "full_text")

    def test_api_exception_falls_back_to_existing_html(self):
        api_client = MagicMock()
        api_client.fetch.side_effect = requests.RequestException("API unavailable")
        fetcher = ArticleContentFetcher(elsevier_client=api_client)
        fetcher.session = build_html_session("<article><p>" + "B" * 400 + "</p></article>")
        fetcher._is_public_http_url = MagicMock(return_value=True)

        result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

        self.assertEqual(result.level, "full_text")
        self.assertEqual(result.fetch_status, "full_text")

    def test_api_runtime_error_falls_back_to_existing_html(self):
        api_client = MagicMock()
        api_client.fetch.side_effect = RuntimeError("API client failed")
        fetcher = ArticleContentFetcher(elsevier_client=api_client)
        fetcher.session = build_html_session("<article><p>" + "B" * 400 + "</p></article>")
        fetcher._is_public_http_url = MagicMock(return_value=True)

        result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

        self.assertEqual(result.level, "full_text")
        self.assertEqual(result.fetch_status, "full_text")

    def test_missing_credentials_preserves_existing_html_summary_title_behavior(self):
        fetcher = ArticleContentFetcher(elsevier_api_key="", elsevier_inst_token="")

        self.assertIsNone(fetcher.elsevier_client)

    def test_missing_credentials_uses_existing_html_full_text_behavior(self):
        fetcher = ArticleContentFetcher(elsevier_api_key="", elsevier_inst_token="")
        fetcher.session = build_html_session("<article><p>" + "B" * 400 + "</p></article>")
        fetcher._is_public_http_url = MagicMock(return_value=True)

        result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

        self.assertEqual(result.level, "full_text")
        self.assertEqual(result.fetch_status, "full_text")

    def test_missing_credentials_uses_existing_rss_summary_behavior(self):
        fetcher = ArticleContentFetcher(elsevier_api_key="", elsevier_inst_token="")
        fetcher.session = build_html_session("<html><body>Unavailable</body></html>")
        fetcher._is_public_http_url = MagicMock(return_value=True)

        result = fetcher.get(
            {"title": "Paper", "summary": "RSS summary", "url": SCIENCEDIRECT_URL}
        )

        self.assertEqual(result.level, "summary")
        self.assertEqual(result.text, "RSS summary")

    def test_missing_credentials_uses_existing_title_only_behavior(self):
        fetcher = ArticleContentFetcher(elsevier_api_key="", elsevier_inst_token="")
        fetcher.session = build_html_session("<html><body>Unavailable</body></html>")
        fetcher._is_public_http_url = MagicMock(return_value=True)

        result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

        self.assertEqual(result.level, "title_only")
        self.assertEqual(result.text, "Paper")

    def test_sciencedirect_api_full_text_uses_existing_truncation_warning(self):
        api_client = MagicMock()
        api_client.fetch.return_value = ElsevierFetchResult("C" * 800, "full_text")
        fetcher = ArticleContentFetcher(
            max_content_chars=500,
            elsevier_client=api_client,
        )
        fetcher._is_public_http_url = MagicMock(return_value=True)

        result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

        self.assertEqual(result.text, "C" * 500)
        self.assertIn("正文已截断", result.risk_warning)


class ElsevierConfigurationTests(unittest.TestCase):
    def test_loader_reads_elsevier_credentials_from_environment(self):
        with patch.dict(os.environ, {
            "ELSEVIER_API_KEY": "api-key",
            "ELSEVIER_INST_TOKEN": "inst-token",
        }, clear=False):
            config = _load_ai_filter_config({"ai_filter": {}})

        content = config["CONTENT_ENRICHMENT"]
        self.assertEqual(content["ELSEVIER_API_KEY"], "api-key")
        self.assertEqual(content["ELSEVIER_INST_TOKEN"], "inst-token")

    @patch("trendradar.ai.filter_pipeline.ArticleContentFetcher")
    def test_pipeline_passes_elsevier_credentials_to_fetcher(self, fetcher_class):
        fetcher_class.return_value.get.return_value = ArticleContent(
            text="summary",
            level="summary",
            risk_warning="limited",
            fetch_status="body_unavailable",
        )
        pipeline = AIFilterPipeline(
            {
                "RSS": {"ENABLED": True},
                "AI_FILTER": {"CONTENT_ENRICHMENT": {
                    "ENABLED": True,
                    "FETCH_FULL_TEXT": True,
                    "TIMEOUT": 12,
                    "MAX_CONTENT_CHARS": 5000,
                    "MIN_BODY_CHARS": 300,
                    "CONCURRENCY": 1,
                    "ELSEVIER_API_KEY": "api-key",
                    "ELSEVIER_INST_TOKEN": "inst-token",
                }},
            },
            MagicMock(),
            lambda: None,
        )

        pipeline._enrich_pending_items(
            [{"id": 1, "title": "Paper", "url": SCIENCEDIRECT_URL}],
            "RSS",
        )

        fetcher_class.assert_called_once_with(
            timeout=12,
            max_content_chars=5000,
            min_body_chars=300,
            use_proxy=False,
            proxy_url="",
            elsevier_api_key="api-key",
            elsevier_inst_token="inst-token",
        )

    def test_compose_and_example_declare_empty_server_side_credentials(self):
        compose = (PROJECT_ROOT / "docker/docker-compose.yml").read_text()
        example = (PROJECT_ROOT / "docker/.env.example").read_text()
        trendradar_service, mcp_service = compose.split("\n  trendradar-mcp:", 1)

        self.assertIn("- ELSEVIER_API_KEY=${ELSEVIER_API_KEY:-}", trendradar_service)
        self.assertIn("- ELSEVIER_INST_TOKEN=${ELSEVIER_INST_TOKEN:-}", trendradar_service)
        self.assertNotIn("ELSEVIER_API_KEY", mcp_service)
        self.assertNotIn("ELSEVIER_INST_TOKEN", mcp_service)
        self.assertIn("仅服务端", example)

        example_values = {
            key: value
            for key, value in (
                line.split("=", 1)
                for line in example.splitlines()
                if line.startswith("ELSEVIER_")
            )
        }
        self.assertEqual(example_values["ELSEVIER_API_KEY"], "")
        self.assertEqual(example_values["ELSEVIER_INST_TOKEN"], "")
