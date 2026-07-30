import unittest

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
