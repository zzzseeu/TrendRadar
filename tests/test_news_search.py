from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock

from trendradar.crawler.news_search import (
    AgriculturalNewsSearch,
    GDELTClient,
    GoogleNewsRSSClient,
    SearchArticle,
    canonicalize_url,
)


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

    def test_gdelt_skips_date_only_timestamp(self):
        payload = {"articles": [{
            "title": "Date-only timestamp",
            "url": "https://example.org/date-only",
            "seendate": "2026-07-31",
        }]}

        articles = GDELTClient().parse(payload, "genomic-breeding")

        self.assertEqual(articles, [])

    def test_google_rss_parses_title_source_and_pubdate(self):
        article = GoogleNewsRSSClient().parse(GOOGLE_RSS, "gene-editing", "zh")[0]

        self.assertEqual(article.title, "水稻基因编辑取得新进展")
        self.assertEqual(article.publisher, "示例农业报")
        self.assertTrue(article.published_at.endswith("+00:00"))
        self.assertEqual(article.url, "https://news.google.com/rss/articles/example")
        self.assertEqual(article.providers, {"google_news"})

    def test_google_rss_skips_title_empty_after_source_suffix_removal(self):
        content = GOOGLE_RSS.replace(
            "水稻基因编辑取得新进展 - 示例农业报",
            " - 示例农业报",
        )

        articles = GoogleNewsRSSClient().parse(content, "gene-editing", "zh")

        self.assertEqual(articles, [])


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


def article(
    title: str,
    published_at: str,
    *,
    url: str = "https://example.com/article",
    publisher: str = "Example News",
    language: str = "en",
    providers: set[str] | None = None,
) -> SearchArticle:
    return SearchArticle(
        title=title,
        url=url,
        published_at=published_at,
        publisher=publisher,
        language=language,
        topic="gene-editing",
        providers=providers or {"gdelt"},
    )


class SearchAggregationTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = AgriculturalNewsSearch(
            authority_domains=("reuters.com",),
            now_func=lambda: datetime(2026, 7, 31, 15, tzinfo=timezone.utc),
        )

    def test_rejects_missing_future_and_expired_dates(self):
        result = self.coordinator.aggregate(
            [
                article("missing", ""),
                article("future", "2026-07-31T15:01:00+00:00"),
                article("old", "2026-07-30T14:59:59+00:00"),
                article("fresh", "2026-07-31T14:00:00+00:00"),
            ],
            now="2026-07-31T15:00:00+00:00",
        )

        self.assertEqual([item.title for item in result], ["fresh"])

    def test_merges_similar_reports_counts_publishers_and_prefers_authority(self):
        result = self.coordinator.aggregate([
            article(
                "New gene-editing breakthrough improves rice breeding",
                "2026-07-31T13:00:00+00:00",
                url="https://example.com/rice?utm_source=gdelt",
                publisher="Example News",
                providers={"gdelt"},
            ),
            article(
                "New gene editing breakthrough improves rice breeding!",
                "2026-07-31T12:00:00+00:00",
                url="https://www.reuters.com/world/rice",
                publisher="Reuters",
                providers={"google_news"},
            ),
        ])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source_count, 2)
        self.assertEqual(result[0].url, "https://www.reuters.com/world/rice")
        self.assertEqual(result[0].providers, {"gdelt", "google_news"})
        self.assertEqual(
            canonicalize_url("HTTPS://Example.COM/path/?utm_source=x&keep=1#part"),
            "https://example.com/path?keep=1",
        )

    def test_calculates_pre_hot_score_from_coverage_authority_and_recency(self):
        result = self.coordinator.aggregate([
            article(
                "Rice breeding update",
                "2026-07-31T03:00:00+00:00",
                url="https://news.reuters.com/rice",
                publisher="Reuters",
                providers={"gdelt"},
            ),
            article(
                "Rice breeding update",
                "2026-07-31T03:00:00+00:00",
                url="https://other.example/rice",
                publisher="Other News",
                providers={"google_news"},
            ),
        ])

        self.assertEqual(result[0].pre_hot_score, 0.7333)


class SearchFailureToleranceTests(unittest.TestCase):
    def test_one_provider_failure_does_not_drop_other_results(self):
        gdelt = MagicMock()
        gdelt.fetch.side_effect = RuntimeError("GDELT unavailable")
        google = MagicMock()
        google.fetch.return_value = [article(
            "Rice gene editing update",
            "2026-07-31T14:00:00+00:00",
            providers={"google_news"},
        )]
        coordinator = AgriculturalNewsSearch(
            gdelt_client=gdelt,
            google_news_client=google,
            topics=[{"id": "gene-editing", "zh": "水稻 基因编辑", "en": "rice gene editing"}],
            now_func=lambda: datetime(2026, 7, 31, 15, tzinfo=timezone.utc),
        )

        result = coordinator.search()

        self.assertEqual(result.failed_providers, ["gdelt"])
        self.assertEqual(len(result.items), 1)
        self.assertEqual(google.fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
