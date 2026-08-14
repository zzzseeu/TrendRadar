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
TEST_ARTICLE_PII = "S0000000000000000"
TEST_BOOK_PII = "B1234567890123456"
TEST_X_PII = "S123456789012345X"
INVALID_TEST_PIIS = (
    "S123",
    "S12345678901234567",
    "A1234567890123456",
)
SCIENCEDIRECT_URL = (
    f"https://www.sciencedirect.com/science/article/pii/{TEST_ARTICLE_PII}"
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_html_session(content: str):
    response = MagicMock(status_code=200, text=content)
    response.headers = {"Content-Type": "text/html"}
    response.apparent_encoding = "utf-8"
    return MagicMock(get=MagicMock(return_value=response))


class ElsevierFullTextClientTests(unittest.TestCase):
    def test_extracts_article_book_and_terminal_x_piis(self):
        cases = {
            f"{SCIENCEDIRECT_URL}?dgcid=rss": TEST_ARTICLE_PII,
            (
                "https://www.sciencedirect.com:443/science/article/pii/"
                f"{TEST_BOOK_PII}"
            ): TEST_BOOK_PII,
            (
                "https://www.sciencedirect.com/science/article/pii/"
                f"{TEST_X_PII}"
            ): TEST_X_PII,
        }

        for url, expected in cases.items():
            with self.subTest(pii=expected):
                self.assertEqual(extract_sciencedirect_pii(url), expected)

    def test_rejects_invalid_pii_lengths_and_prefixes(self):
        for pii in INVALID_TEST_PIIS:
            with self.subTest(pii=pii):
                self.assertIsNone(
                    extract_sciencedirect_pii(
                        f"https://www.sciencedirect.com/science/article/pii/{pii}"
                    )
                )

        self.assertIsNone(
            extract_sciencedirect_pii(
                f"https://example.com/science/article/pii/{TEST_ARTICLE_PII}"
            )
        )

    def test_rejects_noncanonical_sciencedirect_urls(self):
        urls = [
            f"ftp://www.sciencedirect.com/science/article/pii/{TEST_ARTICLE_PII}",
            f"https://user@www.sciencedirect.com/science/article/pii/{TEST_ARTICLE_PII}",
            (
                "https://user:password@www.sciencedirect.com/science/article/pii/"
                f"{TEST_ARTICLE_PII}"
            ),
            (
                "https://www.sciencedirect.com:8443/science/article/pii/"
                f"{TEST_ARTICLE_PII}"
            ),
        ]

        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(extract_sciencedirect_pii(url))

    @patch("trendradar.crawler.elsevier.requests.Session")
    def test_invalid_piis_never_request_the_api(self, session_factory):
        client = ElsevierFullTextClient("test-api-key", "test-inst-token")

        statuses = [
            client.fetch(
                f"https://www.sciencedirect.com/science/article/pii/{pii}"
            ).status
            for pii in INVALID_TEST_PIIS
        ]

        self.assertEqual(statuses, ["unsupported_url"] * len(INVALID_TEST_PIIS))
        session_factory.return_value.get.assert_not_called()

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
            f"https://api.elsevier.com/content/article/pii/{TEST_ARTICLE_PII}",
            params={"view": "FULL"},
            timeout=12,
            allow_redirects=False,
        )
        self.assertEqual(result.status, "full_text")
        self.assertIn("First result paragraph", result.text)

    def test_parse_uses_article_body_paragraphs_in_order_without_nested_duplicates(self):
        text = parse_full_text_xml(FULL_TEXT_XML)
        self.assertEqual(text.count("First result paragraph"), 1)
        self.assertLess(text.index("First result paragraph"), text.index("Second result paragraph"))

    def test_parse_uses_doc_rawtext_when_supported_body_is_absent(self):
        xml_content = b"""\
<full-text-retrieval-response xmlns:xocs="urn:xocs" xmlns:ja="urn:ja">
  <originalText><xocs:doc>
    <xocs:meta />
    <xocs:rawtext>Abstract\n\n  Rice result.\tConclusion.</xocs:rawtext>
    <xocs:serial-item><ja:simple-article /></xocs:serial-item>
  </xocs:doc></originalText>
</full-text-retrieval-response>
"""

        self.assertEqual(
            parse_full_text_xml(xml_content),
            "Abstract Rice result. Conclusion.",
        )

    def test_parse_accepts_simple_and_converted_article_bodies(self):
        for article_tag in ("simple-article", "converted-article"):
            with self.subTest(article_tag=article_tag):
                xml_content = f"""\
<full-text-retrieval-response xmlns:ce="urn:ce" xmlns:ja="urn:ja">
  <originalText><doc><serial-item><ja:{article_tag}><ja:body>
    <ce:para>{article_tag} paragraph.</ce:para>
  </ja:body></ja:{article_tag}></serial-item></doc></originalText>
</full-text-retrieval-response>
""".encode()

                self.assertEqual(
                    parse_full_text_xml(xml_content),
                    f"{article_tag} paragraph.",
                )

    def test_parse_prefers_structured_body_over_doc_rawtext(self):
        xml_content = b"""\
<full-text-retrieval-response>
  <originalText><doc>
    <rawtext>Stale raw representation.</rawtext>
    <serial-item><article><body>
      <para>Structured article paragraph.</para>
    </body></article></serial-item>
  </doc></originalText>
</full-text-retrieval-response>
"""

        self.assertEqual(
            parse_full_text_xml(xml_content),
            "Structured article paragraph.",
        )

    def test_parse_rejects_ambiguous_doc_rawtext(self):
        xml_content = b"""\
<full-text-retrieval-response>
  <originalText><doc>
    <rawtext>First representation.</rawtext>
    <rawtext>Second representation.</rawtext>
    <serial-item><simple-article /></serial-item>
  </doc></originalText>
</full-text-retrieval-response>
"""

        self.assertEqual(parse_full_text_xml(xml_content), "")

    def test_parse_ignores_body_like_metadata_outside_original_text_article(self):
        xml_content = b"""\
<full-text-retrieval-response>
  <metadata><body><para>Metadata paragraph.</para></body></metadata>
  <originalText><article><body>
    <para>Article paragraph.</para>
  </body></article></originalText>
</full-text-retrieval-response>
"""

        self.assertEqual(parse_full_text_xml(xml_content), "Article paragraph.")

    def test_parse_rejects_multiple_article_bodies(self):
        xml_content = b"""\
<full-text-retrieval-response>
  <originalText><article>
    <body><para>First body.</para></body>
    <body><para>Second body.</para></body>
  </article></originalText>
</full-text-retrieval-response>
"""

        self.assertEqual(parse_full_text_xml(xml_content), "")

    def test_parse_mixed_outermost_paragraphs_in_order_without_duplicates(self):
        xml_content = b"""\
<full-text-retrieval-response>
  <originalText><article><body>
    <simple-para>Simple first.</simple-para>
    <para>Outer start <list-item><para>Nested paragraph.</para></list-item> Outer end.</para>
    <simple-para>Simple last.</simple-para>
  </body></article></originalText>
</full-text-retrieval-response>
"""

        self.assertEqual(
            parse_full_text_xml(xml_content),
            "Simple first.\n\nOuter start Nested paragraph. Outer end.\n\nSimple last.",
        )

    @patch("trendradar.crawler.elsevier.requests.Session")
    def test_non_200_timeout_broken_xml_and_metadata_only_are_unavailable(self, session_factory):
        session_factory.return_value.get.side_effect = [
            MagicMock(status_code=302, content=b"redirect"),
            MagicMock(status_code=401, content=b"auth failed"),
            requests.Timeout("slow"),
            MagicMock(status_code=200, content=b"<broken"),
            MagicMock(status_code=200, content=METADATA_ONLY_XML),
        ]
        client = ElsevierFullTextClient("test-api-key", "test-inst-token")

        statuses = [client.fetch(SCIENCEDIRECT_URL).status for _ in range(5)]

        self.assertEqual(
            statuses,
            ["http_302", "http_401", "timeout", "invalid_xml", "body_unavailable"],
        )


class ArticleContentElsevierIntegrationTests(unittest.TestCase):
    @patch("builtins.print")
    @patch("trendradar.crawler.article_content.time.sleep")
    def test_transient_api_failures_retry_until_third_attempt_succeeds(
        self,
        sleep,
        print_mock,
    ):
        api_client = MagicMock()
        api_client.fetch.side_effect = [
            ElsevierFetchResult("", "request_failed"),
            ElsevierFetchResult("", "http_503"),
            ElsevierFetchResult("A" * 800, "full_text"),
        ]
        fetcher = ArticleContentFetcher(
            min_body_chars=300,
            elsevier_client=api_client,
        )
        fetcher.session = MagicMock()
        fetcher._is_public_http_url = MagicMock(return_value=True)

        result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

        self.assertEqual(result.fetch_status, "elsevier_full_text")
        self.assertEqual(api_client.fetch.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.5, 1.0],
        )
        messages = "\n".join(str(call.args[0]) for call in print_mock.call_args_list)
        self.assertIn(TEST_ARTICLE_PII, messages)
        self.assertIn("1/3", messages)
        self.assertIn("request_failed", messages)
        self.assertIn("2/3", messages)
        self.assertIn("http_503", messages)
        fetcher.session.get.assert_not_called()

    @patch("trendradar.crawler.article_content.time.sleep")
    def test_three_transient_api_failures_then_fall_back_to_html(self, sleep):
        api_client = MagicMock()
        api_client.fetch.side_effect = [
            ElsevierFetchResult("", "timeout"),
            ElsevierFetchResult("", "http_429"),
            ElsevierFetchResult("", "http_500"),
        ]
        fetcher = ArticleContentFetcher(elsevier_client=api_client)
        fetcher.session = build_html_session(
            "<article><p>" + "B" * 400 + "</p></article>"
        )
        fetcher._is_public_http_url = MagicMock(return_value=True)

        result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

        self.assertEqual(result.fetch_status, "full_text")
        self.assertEqual(api_client.fetch.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.5, 1.0],
        )
        fetcher.session.get.assert_called_once()

    @patch("trendradar.crawler.article_content.time.sleep")
    def test_non_retryable_api_results_fall_back_without_retry(self, sleep):
        results = [
            ElsevierFetchResult("", "http_400"),
            ElsevierFetchResult("", "http_401"),
            ElsevierFetchResult("", "http_403"),
            ElsevierFetchResult("", "http_404"),
            ElsevierFetchResult("", "invalid_xml"),
            ElsevierFetchResult("", "body_unavailable"),
            ElsevierFetchResult("A" * 299, "full_text"),
        ]

        for api_result in results:
            with self.subTest(status=api_result.status, text_length=len(api_result.text)):
                api_client = MagicMock()
                api_client.fetch.return_value = api_result
                fetcher = ArticleContentFetcher(
                    min_body_chars=300,
                    elsevier_client=api_client,
                )
                fetcher.session = build_html_session(
                    "<article><p>" + "B" * 400 + "</p></article>"
                )
                fetcher._is_public_http_url = MagicMock(return_value=True)

                result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

                self.assertEqual(result.fetch_status, "full_text")
                api_client.fetch.assert_called_once_with(SCIENCEDIRECT_URL)
                fetcher.session.get.assert_called_once()
        sleep.assert_not_called()

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

    def test_failed_api_status_with_long_text_falls_back_to_existing_html(self):
        api_client = MagicMock()
        api_client.fetch.return_value = ElsevierFetchResult("A" * 800, "body_unavailable")
        fetcher = ArticleContentFetcher(min_body_chars=300, elsevier_client=api_client)
        fetcher.session = build_html_session("<article><p>" + "B" * 400 + "</p></article>")
        fetcher._is_public_http_url = MagicMock(return_value=True)

        result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

        self.assertEqual(result.level, "full_text")
        self.assertEqual(result.fetch_status, "full_text")
        self.assertEqual(result.text, "B" * 400)

    @patch("trendradar.crawler.article_content.time.sleep")
    def test_api_exception_retries_then_falls_back_to_existing_html(self, sleep):
        api_client = MagicMock()
        api_client.fetch.side_effect = requests.RequestException("API unavailable")
        fetcher = ArticleContentFetcher(elsevier_client=api_client)
        fetcher.session = build_html_session("<article><p>" + "B" * 400 + "</p></article>")
        fetcher._is_public_http_url = MagicMock(return_value=True)

        result = fetcher.get({"title": "Paper", "url": SCIENCEDIRECT_URL})

        self.assertEqual(result.level, "full_text")
        self.assertEqual(result.fetch_status, "full_text")
        self.assertEqual(api_client.fetch.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.5, 1.0],
        )

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
        compose = (PROJECT_ROOT / "docker/docker-compose-build.yml").read_text()
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
