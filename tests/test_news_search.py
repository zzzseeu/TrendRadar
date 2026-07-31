import unittest
from unittest.mock import MagicMock

from trendradar.crawler.news_search import GDELTClient, GoogleNewsRSSClient


GOOGLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>水稻基因编辑取得新进展 - 示例农业报</title>
      <link>https://news.google.com/rss/articles/example</link>
      <pubDate>Fri, 31 Jul 2026 08:00:00 GMT</pubDate>
      <source url="https://example.cn">示例农业报</source>
    </item>
  </channel>
</rss>"""


class ProviderParsingTests(unittest.TestCase):
    def test_gdelt_parses_direct_article_and_seen_date(self):
        payload = {"articles": [{
            "title": "New genomic selection method improves wheat breeding",
            "url": "https://example.org/wheat?utm_source=gdelt",
            "domain": "example.org",
            "language": "English",
            "seendate": "20260731T080000Z",
        }]}

        article = GDELTClient().parse(payload, "genomic-breeding")[0]

        self.assertEqual(article.publisher, "example.org")
        self.assertEqual(article.published_at, "2026-07-31T08:00:00+00:00")
        self.assertEqual(article.topic, "genomic-breeding")
        self.assertEqual(article.providers, {"gdelt"})

    def test_gdelt_skips_incomplete_items_and_parses_iso_timestamp(self):
        payload = {"articles": [
            {"title": "Missing URL", "seendate": "20260731T080000Z"},
            {"title": "Missing date", "url": "https://example.org/missing-date"},
            {
                "title": "ISO timestamp",
                "url": "https://example.org/iso",
                "seendate": "2026-07-31T08:00:00Z",
            },
        ]}

        articles = GDELTClient().parse(payload, "genomic-breeding")

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].published_at, "2026-07-31T08:00:00+00:00")

    def test_google_rss_parses_title_source_and_pubdate(self):
        article = GoogleNewsRSSClient().parse(GOOGLE_RSS, "gene-editing", "zh")[0]

        self.assertEqual(article.title, "水稻基因编辑取得新进展")
        self.assertEqual(article.publisher, "示例农业报")
        self.assertTrue(article.published_at.endswith("+00:00"))
        self.assertEqual(article.url, "https://news.google.com/rss/articles/example")
        self.assertEqual(article.providers, {"google_news"})


class ProviderRequestTests(unittest.TestCase):
    def test_gdelt_builds_24_hour_article_list_params(self):
        self.assertEqual(
            GDELTClient().build_params("wheat breeding", 25),
            {
                "query": "wheat breeding",
                "mode": "artlist",
                "format": "json",
                "timespan": "24h",
                "sort": "datedesc",
                "maxrecords": 25,
            },
        )

    def test_google_rss_builds_localized_24_hour_params(self):
        client = GoogleNewsRSSClient()

        self.assertEqual(
            client.build_params("水稻 基因编辑", "zh"),
            {
                "q": "水稻 基因编辑 when:1d",
                "hl": "zh-CN",
                "gl": "CN",
                "ceid": "CN:zh-Hans",
            },
        )
        self.assertEqual(
            client.build_params("rice gene editing", "en"),
            {
                "q": "rice gene editing when:1d",
                "hl": "en-US",
                "gl": "US",
                "ceid": "US:en",
            },
        )

    def test_gdelt_fetch_uses_injected_session_and_parses_json(self):
        session = MagicMock()
        response = MagicMock()
        response.json.return_value = {"articles": [{
            "title": "Wheat update",
            "url": "https://example.org/wheat",
            "seendate": "20260731T080000Z",
        }]}
        session.get.return_value = response
        client = GDELTClient(session=session, timeout=12)

        articles = client.fetch("wheat breeding", "genomic-breeding", 10)

        session.get.assert_called_once_with(
            client.endpoint,
            params=client.build_params("wheat breeding", 10),
            timeout=12,
        )
        response.raise_for_status.assert_called_once_with()
        self.assertEqual(articles[0].title, "Wheat update")

    def test_google_rss_fetch_uses_injected_session_and_keeps_jump_link(self):
        session = MagicMock()
        response = MagicMock(text=GOOGLE_RSS)
        session.get.return_value = response
        client = GoogleNewsRSSClient(session=session, timeout=8)

        articles = client.fetch("水稻 基因编辑", "gene-editing", "zh")

        session.get.assert_called_once_with(
            client.endpoint,
            params=client.build_params("水稻 基因编辑", "zh"),
            timeout=8,
        )
        response.raise_for_status.assert_called_once_with()
        self.assertEqual(articles[0].url, "https://news.google.com/rss/articles/example")


if __name__ == "__main__":
    unittest.main()
