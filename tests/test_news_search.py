from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock

from trendradar.crawler.news_search import (
    AgriculturalNewsSearch,
    GDELTClient,
    GoogleNewsRSSClient,
    SearchArticle,
    canonicalize_url,
    title_similarity,
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
    def test_provider_parsers_reject_unsafe_article_urls(self):
        gdelt_payload = {"articles": [
            {
                "title": f"unsafe-{index}",
                "url": url,
                "seendate": "20260731T080000Z",
            }
            for index, url in enumerate((
                "javascript:alert(1)",
                "data:text/html,payload",
                "file:///tmp/article",
                "/relative/article",
                "https://example.org/line\nbreak",
                "https://",
                "https://example.org/" + "x" * 4096,
            ))
        ]}
        google_items = "".join(
            "<item><title>Unsafe article</title>"
            f"<link>{url}</link>"
            "<pubDate>Fri, 31 Jul 2026 08:00:00 GMT</pubDate>"
            "</item>"
            for url in (
                "javascript:alert(1)",
                "data:text/plain,payload",
                "file:///tmp/article",
                "/relative/article",
            )
        )
        google_feed = f"<rss><channel>{google_items}</channel></rss>"

        self.assertEqual(GDELTClient().parse(gdelt_payload, "topic"), [])
        self.assertEqual(GoogleNewsRSSClient().parse(google_feed, "topic", "en"), [])

    def test_canonicalize_url_fails_closed_for_unsafe_urls(self):
        for unsafe in (
            "javascript:alert(1)",
            "data:text/plain,payload",
            "file:///tmp/article",
            "/relative/article",
            "https://example.org/control\x00char",
            "https://",
        ):
            with self.subTest(url=unsafe):
                self.assertEqual(canonicalize_url(unsafe), "")

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

    def test_gdelt_normalizes_language_and_publisher_domain(self):
        payload = {"articles": [{
            "title": "Wheat update",
            "url": "https://www.reuters.com/wheat",
            "domain": "WWW.Reuters.COM",
            "language": "English",
            "seendate": "20260731T080000Z",
        }]}

        article = GDELTClient().parse(payload, "genomic-breeding")[0]

        self.assertEqual(article.language, "en")
        self.assertEqual(article.publisher_domain, "reuters.com")

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

    def test_google_rss_uses_source_domain_and_normalized_language(self):
        content = GOOGLE_RSS.replace("https://example.cn", "https://www.reuters.com")

        article = GoogleNewsRSSClient().parse(content, "gene-editing", "zh")[0]

        self.assertEqual(article.language, "zh")
        self.assertEqual(article.publisher_domain, "reuters.com")
        self.assertEqual(article.url, "https://news.google.com/rss/articles/example")

    def test_google_rss_skips_title_empty_after_source_suffix_removal(self):
        content = GOOGLE_RSS.replace(
            "水稻基因编辑取得新进展 - 示例农业报",
            " - 示例农业报",
        )

        articles = GoogleNewsRSSClient().parse(content, "gene-editing", "zh")

        self.assertEqual(articles, [])


class ProviderRequestTests(unittest.TestCase):
    def test_provider_clients_forward_proxy_configuration_to_direct_first_session(self):
        with unittest.mock.patch(
            "trendradar.crawler.news_search.DirectFirstSession"
        ) as session_class:
            GDELTClient(
                use_proxy=True,
                proxy_url="http://proxy:7892",
                timeout=9,
            )
            GoogleNewsRSSClient(
                use_proxy=False,
                proxy_url="http://proxy:7892",
                timeout=11,
            )

        self.assertEqual(session_class.call_count, 2)
        self.assertEqual(
            session_class.call_args_list[0].kwargs["use_proxy"], True
        )
        self.assertEqual(
            session_class.call_args_list[0].kwargs["proxy_url"],
            "http://proxy:7892",
        )
        self.assertEqual(
            session_class.call_args_list[1].kwargs["use_proxy"], False
        )
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
    publisher_domain: str = "",
) -> SearchArticle:
    values = {
        "title": title,
        "url": url,
        "published_at": published_at,
        "publisher": publisher,
        "language": language,
        "topic": "gene-editing",
        "providers": providers or {"gdelt"},
    }
    if publisher_domain:
        values["publisher_domain"] = publisher_domain
    return SearchArticle(**values)


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
                publisher_domain="reuters.com",
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
                publisher_domain="other.example",
                providers={"google_news"},
            ),
        ])

        self.assertEqual(result[0].pre_hot_score, 0.7333)

    def test_merges_english_provider_labels_and_deduplicates_media_domain(self):
        result = self.coordinator.aggregate([
            article(
                "Rice gene editing advances",
                "2026-07-31T14:00:00+00:00",
                url="https://www.reuters.com/rice",
                publisher="Reuters",
                publisher_domain="reuters.com",
                language="English",
                providers={"gdelt"},
            ),
            article(
                "Rice gene-editing advances",
                "2026-07-31T13:00:00+00:00",
                url="https://news.google.com/rss/articles/reuters-rice",
                publisher="Reuters",
                publisher_domain="www.reuters.com",
                language="en",
                providers={"google_news"},
            ),
        ])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source_count, 1)
        self.assertEqual(result[0].publisher_domain, "reuters.com")

    def test_merges_chinese_provider_labels(self):
        result = self.coordinator.aggregate([
            article(
                "水稻基因编辑取得新进展",
                "2026-07-31T14:00:00+00:00",
                language="Chinese",
            ),
            article(
                "水稻基因编辑取得新进展！",
                "2026-07-31T13:00:00+00:00",
                url="https://other.example/rice",
                language="zh",
                publisher="Other News",
                publisher_domain="other.example",
                providers={"google_news"},
            ),
        ])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].language, "zh")

    def test_keeps_google_jump_link_when_its_publisher_is_authoritative(self):
        result = self.coordinator.aggregate([
            article(
                "Rice breeding advances",
                "2026-07-31T14:00:00+00:00",
                url="https://example.com/rice",
                publisher="Example News",
                publisher_domain="example.com",
            ),
            article(
                "Rice breeding advances!",
                "2026-07-31T13:00:00+00:00",
                url="https://news.google.com/rss/articles/reuters-rice",
                publisher="Reuters",
                publisher_domain="reuters.com",
                providers={"google_news"},
            ),
        ])

        self.assertEqual(result[0].url, "https://news.google.com/rss/articles/reuters-rice")
        self.assertEqual(result[0].publisher_domain, "reuters.com")

    def test_does_not_merge_title_similarity_across_languages(self):
        result = self.coordinator.aggregate([
            article("Rice breeding update", "2026-07-31T14:00:00+00:00", language="en"),
            article(
                "Rice breeding update",
                "2026-07-31T14:00:00+00:00",
                url="https://example.com/translated",
                language="zh",
            ),
        ])

        self.assertEqual(len(result), 2)

    def test_merges_canonical_urls_even_when_languages_differ(self):
        result = self.coordinator.aggregate([
            article(
                "Rice breeding update",
                "2026-07-31T14:00:00+00:00",
                url="https://example.com/rice/?utm_source=gdelt",
                language="en",
            ),
            article(
                "水稻育种进展",
                "2026-07-31T13:00:00+00:00",
                url="https://example.com/rice#google",
                language="zh",
                providers={"google_news"},
            ),
        ])

        self.assertEqual(len(result), 1)

    def test_accepts_exactly_zero_and_twenty_four_hour_ages(self):
        result = self.coordinator.aggregate([
            article("new", "2026-07-31T15:00:00+00:00"),
            article("boundary", "2026-07-30T15:00:00+00:00", url="https://example.com/old"),
        ])

        self.assertEqual([item.title for item in result], ["new", "boundary"])

    def test_uses_newest_primary_when_no_publisher_is_authoritative(self):
        result = self.coordinator.aggregate([
            article("Rice breeding update", "2026-07-31T12:00:00+00:00"),
            article(
                "Rice breeding update!",
                "2026-07-31T14:00:00+00:00",
                url="https://other.example/rice",
                publisher="Other News",
            ),
        ])

        self.assertEqual(result[0].url, "https://other.example/rice")

    def test_title_similarity_uses_transitive_components(self):
        coordinator = AgriculturalNewsSearch(
            similarity_threshold=0.6,
            now_func=lambda: datetime(2026, 7, 31, 15, tzinfo=timezone.utc),
        )
        reports = [
            article(
                "alpha beta gamma delta",
                "2026-07-31T12:00:00+00:00",
                url="https://a.example/story",
                publisher="A News",
                publisher_domain="a.example",
            ),
            article(
                "alpha beta gamma epsilon",
                "2026-07-31T13:00:00+00:00",
                url="https://b.example/story",
                publisher="B News",
                publisher_domain="b.example",
            ),
            article(
                "alpha epsilon zeta eta",
                "2026-07-31T14:00:00+00:00",
                url="https://c.example/story",
                publisher="C News",
                publisher_domain="c.example",
            ),
        ]
        self.assertGreaterEqual(
            title_similarity(reports[0].title, reports[1].title), 0.6
        )
        self.assertGreaterEqual(
            title_similarity(reports[1].title, reports[2].title), 0.6
        )
        self.assertLess(
            title_similarity(reports[0].title, reports[2].title), 0.6
        )

        result = coordinator.aggregate(reports)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].title, reports[2].title)
        self.assertEqual(result[0].source_count, 3)
        self.assertEqual(result[0].related_publishers, {"A News", "B News"})

    def test_url_bridge_merges_two_existing_title_components(self):
        coordinator = AgriculturalNewsSearch(
            similarity_threshold=0.8,
            now_func=lambda: datetime(2026, 7, 31, 15, tzinfo=timezone.utc),
        )
        reports = [
            article(
                "rice breeding alpha report",
                "2026-07-31T11:00:00+00:00",
                url="https://a.example/one",
                publisher="A",
                publisher_domain="a.example",
            ),
            article(
                "wheat genomics beta report",
                "2026-07-31T12:00:00+00:00",
                url="https://b.example/two",
                publisher="B",
                publisher_domain="b.example",
            ),
            article(
                "rice breeding alpha report!",
                "2026-07-31T13:00:00+00:00",
                url="https://bridge.example/story",
                publisher="Bridge A",
                publisher_domain="bridge-a.example",
            ),
            article(
                "wheat genomics beta report!",
                "2026-07-31T14:00:00+00:00",
                url="https://bridge.example/story?utm_source=x",
                publisher="Bridge B",
                publisher_domain="bridge-b.example",
            ),
        ]

        result = coordinator.aggregate(reports)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source_count, 4)

    def test_google_report_without_source_url_counts_publisher_name(self):
        report = article(
            "Rice breeding report",
            "2026-07-31T14:00:00+00:00",
            url="https://news.google.com/rss/articles/report",
            publisher="Named Publisher",
            providers={"google_news"},
            publisher_domain="",
        )

        result = self.coordinator.aggregate([report])

        self.assertEqual(result[0].source_count, 1)


class SearchFailureToleranceTests(unittest.TestCase):
    def test_string_false_provider_flags_are_not_enabled(self):
        gdelt = MagicMock()
        google = MagicMock()
        coordinator = AgriculturalNewsSearch(
            gdelt_client=gdelt,
            google_news_client=google,
            providers={"gdelt": "false", "google_news": "0"},
            topics=[{"id": "gene-editing", "zh": "水稻", "en": "rice"}],
        )

        coordinator.search()

        gdelt.fetch.assert_not_called()
        google.fetch.assert_not_called()

    def test_malformed_direct_provider_flags_use_defaults_and_warn(self):
        gdelt = MagicMock()
        gdelt.fetch.return_value = []
        google = MagicMock()
        google.fetch.return_value = []
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()

        with redirect_stdout(output):
            coordinator = AgriculturalNewsSearch(
                gdelt_client=gdelt,
                google_news_client=google,
                providers={"gdelt": "yes", "google_news": []},
                topics=[{"id": "gene-editing", "zh": "水稻", "en": "rice"}],
            )
            coordinator.search()

        self.assertEqual(gdelt.fetch.call_count, 1)
        self.assertEqual(google.fetch.call_count, 2)
        self.assertIn("providers.gdelt", output.getvalue())
        self.assertIn("providers.google_news", output.getvalue())

    def test_provider_failures_log_provider_query_topic_and_exception_type(self):
        gdelt = MagicMock()
        gdelt.fetch.side_effect = TimeoutError("timeout")
        google = MagicMock()
        google.fetch.return_value = []
        coordinator = AgriculturalNewsSearch(
            gdelt_client=gdelt,
            google_news_client=google,
            topics=[{"id": "gene-editing", "zh": "水稻", "en": "rice"}],
        )

        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with redirect_stdout(output):
            result = coordinator.search()

        message = output.getvalue()
        self.assertEqual(result.failed_providers, ["gdelt"])
        self.assertIn("gdelt", message)
        self.assertIn("gene-editing", message)
        self.assertIn("rice", message)
        self.assertIn("TimeoutError", message)

    def test_provider_limit_is_total_budget_and_distributed_across_topics(self):
        def result_for(provider):
            def make_results(query, topic, *_args):
                return [
                    SearchArticle(
                        title=f"{query}-{index}",
                        published_at="2026-07-31T14:00:00+00:00",
                        url=f"https://{provider}-{topic}-{index}.example/story",
                        publisher=f"{provider}-{index}",
                        publisher_domain=f"{provider}-{topic}-{index}.example",
                        language="en",
                        topic=topic,
                        providers={provider},
                    )
                    for index in range(5)
                ]
            return make_results

        gdelt = MagicMock()
        gdelt.fetch.side_effect = result_for("gdelt")
        google = MagicMock()
        google.fetch.side_effect = result_for("google_news")
        coordinator = AgriculturalNewsSearch(
            gdelt_client=gdelt,
            google_news_client=google,
            topics=[
                {"id": "one", "zh": "中文一", "en": "english one"},
                {"id": "two", "zh": "中文二", "en": "english two"},
            ],
            max_results_per_provider=3,
            now_func=lambda: datetime(2026, 7, 31, 15, tzinfo=timezone.utc),
        )

        result = coordinator.search()

        provider_counts = {
            provider: sum(provider in item.providers for item in result.items)
            for provider in ("gdelt", "google_news")
        }
        self.assertLessEqual(provider_counts["gdelt"], 3)
        self.assertLessEqual(provider_counts["google_news"], 3)
        self.assertEqual({item.topic for item in result.items}, {"one", "two"})
        self.assertEqual(
            [call.args[2] for call in gdelt.fetch.call_args_list],
            [2, 1],
        )

    def test_small_provider_budget_does_not_let_first_query_monopolize(self):
        gdelt = MagicMock()
        gdelt.fetch.side_effect = lambda query, topic, limit: [
            SearchArticle(
                title=f"{topic}-{index}",
                published_at="2026-07-31T14:00:00+00:00",
                url=f"https://{topic}-{index}.example/story",
                publisher=topic,
                language="en",
                topic=topic,
                providers={"gdelt"},
            )
            for index in range(limit)
        ]
        google = MagicMock()
        coordinator = AgriculturalNewsSearch(
            gdelt_client=gdelt,
            google_news_client=google,
            providers={"gdelt": True, "google_news": False},
            topics=[
                {"id": "one", "en": "one"},
                {"id": "two", "en": "two"},
            ],
            max_results_per_provider=2,
            now_func=lambda: datetime(2026, 7, 31, 15, tzinfo=timezone.utc),
        )

        result = coordinator.search()

        self.assertEqual({item.topic for item in result.items}, {"one", "two"})
        self.assertEqual([call.args[2] for call in gdelt.fetch.call_args_list], [1, 1])
    def test_disabled_gdelt_provider_is_never_called(self):
        gdelt = MagicMock()
        google = MagicMock()
        google.fetch.return_value = []
        coordinator = AgriculturalNewsSearch(
            gdelt_client=gdelt,
            google_news_client=google,
            providers={"gdelt": False, "google_news": True},
            topics=[{"id": "gene-editing", "zh": "水稻", "en": "rice"}],
        )

        result = coordinator.search()

        gdelt.fetch.assert_not_called()
        self.assertEqual(google.fetch.call_count, 2)
        self.assertEqual(result.failed_providers, [])

    def test_both_disabled_providers_make_zero_calls(self):
        gdelt = MagicMock()
        google = MagicMock()
        coordinator = AgriculturalNewsSearch(
            gdelt_client=gdelt,
            google_news_client=google,
            providers={"gdelt": False, "google_news": False},
            topics=[{"id": "gene-editing", "zh": "水稻", "en": "rice"}],
        )

        result = coordinator.search()

        gdelt.fetch.assert_not_called()
        google.fetch.assert_not_called()
        self.assertEqual(result.items, [])
        self.assertEqual(result.failed_providers, [])

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

    def test_limits_google_results_across_all_queries(self):
        gdelt = MagicMock()
        gdelt.fetch.return_value = []
        google = MagicMock()
        google.fetch.side_effect = [
            [
                article("Chinese first", "2026-07-31T14:00:00+00:00"),
                article("Chinese second", "2026-07-31T14:00:00+00:00", url="https://example.com/2"),
            ],
            [
                article("English first", "2026-07-31T14:00:00+00:00", url="https://example.com/3"),
                article("English second", "2026-07-31T14:00:00+00:00", url="https://example.com/4"),
            ],
        ]
        coordinator = AgriculturalNewsSearch(
            gdelt_client=gdelt,
            google_news_client=google,
            topics=[{"id": "gene-editing", "zh": "水稻 基因编辑", "en": "rice gene editing"}],
            max_results_per_provider=1,
            now_func=lambda: datetime(2026, 7, 31, 15, tzinfo=timezone.utc),
        )

        result = coordinator.search()

        self.assertEqual([item.title for item in result.items], ["Chinese first"])

    def test_google_failure_is_deduplicated_and_other_provider_results_survive(self):
        gdelt = MagicMock()
        gdelt.fetch.return_value = [article("GDELT result", "2026-07-31T14:00:00+00:00")]
        google = MagicMock()
        google.fetch.side_effect = RuntimeError("Google unavailable")
        coordinator = AgriculturalNewsSearch(
            gdelt_client=gdelt,
            google_news_client=google,
            topics=[{"id": "gene-editing", "zh": "水稻", "en": "rice"}],
            now_func=lambda: datetime(2026, 7, 31, 15, tzinfo=timezone.utc),
        )

        result = coordinator.search()

        self.assertEqual(result.failed_providers, ["google_news"])
        self.assertEqual([item.title for item in result.items], ["GDELT result"])

    def test_both_provider_failures_return_empty_result(self):
        gdelt = MagicMock()
        gdelt.fetch.side_effect = RuntimeError("GDELT unavailable")
        google = MagicMock()
        google.fetch.side_effect = RuntimeError("Google unavailable")
        coordinator = AgriculturalNewsSearch(
            gdelt_client=gdelt,
            google_news_client=google,
            topics=[{"id": "gene-editing", "zh": "水稻", "en": "rice"}],
        )

        result = coordinator.search()

        self.assertEqual(result.items, [])
        self.assertEqual(result.failed_providers, ["gdelt", "google_news"])


if __name__ == "__main__":
    unittest.main()
