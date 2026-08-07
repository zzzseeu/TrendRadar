import unittest
from unittest.mock import MagicMock, patch

import requests

from trendradar.crawler.elsevier import (
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
