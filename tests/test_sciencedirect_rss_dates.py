import unittest

from trendradar.ai.filter import AIFilterResult
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.crawler.rss.parser import RSSParser


def _rss_item(description: str) -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Rice Science</title>
    <item>
      <title>Example rice article</title>
      <link>https://www.sciencedirect.com/science/article/pii/S123</link>
      <guid>https://www.sciencedirect.com/science/article/pii/S123</guid>
      <description><![CDATA[{description}]]></description>
    </item>
  </channel>
</rss>
"""


class _RSSStorageStub:
    def get_all_news_ids(self):
        return []

    def get_analyzed_news_ids(self, source_type, interests_file):
        return set()

    def get_all_rss_ids(self):
        return [
            {
                "id": 1,
                "source_id": "rice-science",
                "published_at": "",
            },
            {
                "id": 2,
                "source_id": "example-rss",
                "published_at": "",
            },
        ]


def _pipeline(storage=None) -> AIFilterPipeline:
    return AIFilterPipeline(
        {
            "RSS": {
                "ENABLED": True,
                "FEEDS": [
                    {
                        "id": "rice-science",
                        "url": (
                            "https://rss.sciencedirect.com/publication/"
                            "science/16726308"
                        ),
                    },
                    {
                        "id": "example-rss",
                        "url": "https://example.org/feed.xml",
                    },
                ],
            },
            "AI_FILTER": {"MIN_SCORE": 0.7},
        },
        storage or _RSSStorageStub(),
        lambda: None,
    )


class ScienceDirectRSSDateTests(unittest.TestCase):
    def test_sciencedirect_date_is_extracted_from_description(self):
        items = RSSParser().parse(
            _rss_item(
                "<p>Publication date: Available online 1 July 2026</p>"
                "<p><b>Source:</b> Rice Science</p>"
            ),
            "https://rss.sciencedirect.com/publication/science/16726308",
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].published_at, "2026-07-01")

    def test_sciencedirect_fallback_preserves_date_only(self):
        items = RSSParser().parse(
            _rss_item("Available online 9 August 2026"),
            "https://rss.sciencedirect.com/publication/science/22145141",
        )

        self.assertEqual(items[0].published_at, "2026-08-09")

    def test_standard_rss_struct_time_is_explicit_utc(self):
        value = RSSParser()._parse_date({
            "published_parsed": (2026, 8, 9, 1, 2, 3, 0, 0, 0),
        })

        self.assertEqual(value, "2026-08-09T01:02:03+00:00")

    def test_sciencedirect_item_without_publication_date_is_rejected(self):
        items = RSSParser().parse(
            _rss_item("<p><b>Source:</b> Rice Science</p>"),
            "https://rss.sciencedirect.com/publication/science/16726308",
        )

        self.assertEqual(items, [])

    def test_other_rss_parser_can_preserve_missing_publication_date(self):
        items = RSSParser().parse(
            _rss_item("<p>No publication date is available.</p>"),
            "https://example.org/feed.xml",
        )

        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0].published_at)

    def test_undated_items_from_all_feeds_remain_pending_without_scope(self):
        pending = _pipeline()._collect_pending_news("ai_interests.txt")

        pending_rss = pending[1]
        scope_filtered_rss = pending[-1]
        self.assertEqual([item["id"] for item in pending_rss], [1, 2])
        self.assertEqual(scope_filtered_rss, 0)

    def test_undated_sciencedirect_item_is_reported_without_scope(self):
        result = AIFilterResult(
            success=True,
            tags=[
                {
                    "tag": "水稻育种",
                    "count": 1,
                    "items": [
                        {
                            "title": "Undated ScienceDirect article",
                            "source_id": "rice-science",
                            "source_name": "Rice Science",
                            "url": "https://www.sciencedirect.com/article",
                            "first_time": "",
                            "last_time": "",
                            "relevance_score": 0.9,
                            "source_type": "rss",
                        }
                    ],
                }
            ],
        )

        _, rss_stats, _ = _pipeline().convert_to_report_data(result)

        self.assertEqual(rss_stats[0]["count"], 1)


if __name__ == "__main__":
    unittest.main()
