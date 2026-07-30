import unittest
from types import SimpleNamespace

from trendradar.__main__ import NewsAnalyzer
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.core.analyzer import count_rss_frequency
from trendradar.report.formatter import format_title_for_platform
from trendradar.report.html import render_html_content
from trendradar.report.rss_html import render_rss_html_content
from trendradar.storage.base import RSSItem
from trendradar.utils.article_links import build_reader_url


class RiceScienceReaderUrlTests(unittest.TestCase):
    def test_builds_reader_url_and_removes_tracking_query(self):
        result = build_reader_url(
            "rice-science",
            "https://www.sciencedirect.com/science/article/pii/"
            "S1672630826000879?dgcid=rss_sd_all",
        )
        self.assertEqual(
            result,
            "https://r.jina.ai/http://www.sciencedirect.com/science/article/pii/"
            "S1672630826000879",
        )

    def test_rejects_other_feeds_hosts_and_paths(self):
        self.assertEqual(
            build_reader_url(
                "molecular-plant",
                "https://www.sciencedirect.com/science/article/pii/S1672630826000879",
            ),
            "",
        )
        self.assertEqual(
            build_reader_url(
                "rice-science",
                "https://example.com/science/article/pii/S1672630826000879",
            ),
            "",
        )
        self.assertEqual(
            build_reader_url(
                "rice-science",
                "https://www.sciencedirect.com/journal/rice-science",
            ),
            "",
        )


class RiceScienceReaderUrlPropagationTests(unittest.TestCase):
    url = (
        "https://www.sciencedirect.com/science/article/pii/"
        "S1672630826000879?dgcid=rss_sd_all"
    )
    reader_url = (
        "https://r.jina.ai/http://www.sciencedirect.com/science/article/pii/"
        "S1672630826000879"
    )

    def test_raw_rss_conversion_adds_reader_url(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            rss_config={
                "FRESHNESS_FILTER": {"ENABLED": False, "MAX_AGE_DAYS": 1}
            },
            rss_feeds=[{"id": "rice-science", "max_age_days": 1}],
            config={"TIMEZONE": "Asia/Shanghai", "DEBUG": False},
        )
        result = analyzer._convert_rss_items_to_list(
            {
                "rice-science": [
                    RSSItem(
                        title="Test",
                        feed_id="rice-science",
                        url=self.url,
                    )
                ]
            },
            {"rice-science": "Rice Science"},
        )
        self.assertEqual(result[0]["reader_url"], self.reader_url)

    def test_keyword_stats_preserve_reader_url(self):
        stats, _ = count_rss_frequency(
            [{
                "title": "Rice breeding",
                "feed_id": "rice-science",
                "feed_name": "Rice Science",
                "url": self.url,
                "reader_url": self.reader_url,
                "published_at": "",
            }],
            [],
            [],
            quiet=True,
        )
        self.assertEqual(stats[0]["titles"][0]["reader_url"], self.reader_url)

    def test_ai_report_generates_reader_url_only_for_rice_science(self):
        pipeline = AIFilterPipeline(
            {
                "RSS": {
                    "ENABLED": True,
                    "FEEDS": [],
                    "FRESHNESS_FILTER": {"ENABLED": False, "MAX_AGE_DAYS": 1},
                },
                "AI_FILTER": {},
                "FILTER": {},
                "TIMEZONE": "Asia/Shanghai",
            },
            storage_manager=None,
            get_time_func=lambda: None,
        )
        result = pipeline._build_filter_result(
            raw_results=[{
                "tag": "水稻",
                "title": "Rice breeding",
                "source_id": "rice-science",
                "source_name": "Rice Science",
                "source_type": "rss",
                "url": self.url,
                "ranks": [1],
            }],
            tags=[{"tag": "水稻", "priority": 1}],
            total_processed=1,
        )
        self.assertEqual(result.highlights[0]["reader_url"], self.reader_url)
        _, rss_stats, _ = pipeline.convert_to_report_data(result)
        self.assertEqual(
            rss_stats[0]["titles"][0]["reader_url"],
            self.reader_url,
        )


class RiceScienceDualLinkRenderingTests(unittest.TestCase):
    official_url = (
        "https://www.sciencedirect.com/science/article/pii/S1672630826000879"
    )
    reader_url = (
        "https://r.jina.ai/http://www.sciencedirect.com/science/article/pii/"
        "S1672630826000879"
    )

    def _title_data(self):
        return {
            "title": "Rice breeding",
            "source_name": "Rice Science",
            "time_display": "",
            "count": 1,
            "ranks": [1],
            "rank_threshold": 5,
            "url": self.official_url,
            "mobile_url": "",
            "reader_url": self.reader_url,
            "is_new": False,
        }

    def test_wework_title_contains_official_and_reader_links(self):
        content = format_title_for_platform("wework", self._title_data())
        self.assertIn(f"[Rice breeding]({self.official_url})", content)
        self.assertIn(f"[📖 备用阅读]({self.reader_url})", content)

    def test_wework_without_reader_url_keeps_single_link(self):
        item = self._title_data()
        item["reader_url"] = ""
        content = format_title_for_platform("wework", item)
        self.assertNotIn("备用阅读", content)

    def test_main_and_rss_html_contain_reader_link(self):
        title = self._title_data()
        report_html = render_html_content(
            {
                "stats": [],
                "new_titles": [],
                "failed_ids": [],
                "total_new_count": 0,
            },
            total_titles=1,
            rss_items=[{"word": "水稻", "count": 1, "titles": [title]}],
        )
        rss_html = render_rss_html_content(
            [{
                "title": "Rice breeding",
                "feed_id": "rice-science",
                "feed_name": "Rice Science",
                "url": self.official_url,
                "reader_url": self.reader_url,
            }],
            total_count=1,
        )
        for content in (report_html, rss_html):
            self.assertIn("📖 备用阅读", content)
            self.assertIn(self.reader_url, content)

    def test_rss_html_without_reader_url_keeps_single_link(self):
        content = render_rss_html_content(
            [{
                "title": "Other source",
                "feed_id": "other",
                "feed_name": "Other",
                "url": "https://example.com/article",
            }],
            total_count=1,
        )
        self.assertIn("https://example.com/article", content)
        self.assertNotIn("备用阅读", content)

    def test_rss_html_escapes_reader_url(self):
        content = render_rss_html_content(
            [{
                "title": "Rice breeding",
                "feed_id": "rice-science",
                "feed_name": "Rice Science",
                "url": self.official_url,
                "reader_url": 'https://example.com/?a=1&b="x"',
            }],
            total_count=1,
        )
        self.assertIn(
            "https://example.com/?a=1&amp;b=&quot;x&quot;",
            content,
        )
