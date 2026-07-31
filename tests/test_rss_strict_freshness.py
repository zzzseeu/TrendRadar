import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytz

from trendradar.__main__ import NewsAnalyzer
from trendradar.crawler.rss.fetcher import RSSFeedConfig, RSSFetcher
from trendradar.crawler.rss.parser import ParsedRSSItem
from trendradar.storage.base import RSSItem
from trendradar.utils.time import is_within_days


class StrictFreshnessTimeTests(unittest.TestCase):
    @patch("trendradar.utils.time.get_configured_time")
    def test_rejects_missing_invalid_future_and_older_than_24_hours(
        self,
        mock_now,
    ):
        mock_now.return_value = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 7, 31, 15, 0)
        )

        self.assertFalse(is_within_days("", 1, "Asia/Shanghai"))
        self.assertFalse(is_within_days("not-a-date", 1, "Asia/Shanghai"))
        self.assertFalse(
            is_within_days(
                "2026-07-31T15:01:00+08:00",
                1,
                "Asia/Shanghai",
            )
        )
        self.assertFalse(
            is_within_days(
                "2026-07-30T14:59:59+08:00",
                1,
                "Asia/Shanghai",
            )
        )

    @patch("trendradar.utils.time.get_configured_time")
    def test_accepts_exact_boundary_and_recent_publication(self, mock_now):
        mock_now.return_value = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 7, 31, 15, 0)
        )

        self.assertTrue(
            is_within_days(
                "2026-07-30T15:00:00+08:00",
                1,
                "Asia/Shanghai",
            )
        )
        self.assertTrue(
            is_within_days(
                "2026-07-31T14:59:59+08:00",
                1,
                "Asia/Shanghai",
            )
        )


class _Response:
    text = "<rss/>"
    apparent_encoding = "utf-8"
    encoding = "utf-8"

    def raise_for_status(self):
        return None


class _Session:
    def get(self, *args, **kwargs):
        return _Response()


class _Parser:
    def __init__(self, published_at):
        self.published_at = published_at

    def parse(self, text, feed_url):
        return [
            ParsedRSSItem(
                title="Example",
                url="https://example.org/article",
                published_at=self.published_at,
            )
        ]


class StrictFreshnessPipelineTests(unittest.TestCase):
    def test_fetcher_rejects_undated_item_when_filter_is_enabled(self):
        feed = RSSFeedConfig(
            id="example-rss",
            name="Example",
            url="https://example.org/feed.xml",
            max_age_days=1,
        )
        fetcher = RSSFetcher(
            [feed],
            freshness_enabled=True,
            default_max_age_days=1,
        )
        fetcher.session = _Session()
        fetcher.parser = _Parser("")

        items, error = fetcher.fetch_feed(feed)

        self.assertIsNone(error)
        self.assertEqual(items, [])

    def test_raw_conversion_rejects_undated_item_when_filter_is_enabled(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            rss_config={
                "FRESHNESS_FILTER": {"ENABLED": True, "MAX_AGE_DAYS": 1}
            },
            rss_feeds=[{"id": "example-rss", "max_age_days": 1}],
            config={"TIMEZONE": "Asia/Shanghai", "DEBUG": False},
        )

        result = analyzer._convert_rss_items_to_list(
            {
                "example-rss": [
                    RSSItem(
                        title="Undated",
                        feed_id="example-rss",
                        url="https://example.org/article",
                    )
                ]
            },
            {"example-rss": "Example"},
        )

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
