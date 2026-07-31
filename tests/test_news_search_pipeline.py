import unittest

from trendradar.core.loader import _load_rss_config


class NewsSearchConfigTests(unittest.TestCase):
    def test_loader_exposes_validated_news_search_config(self):
        loaded = _load_rss_config({
            "rss": {
                "news_search": {
                    "enabled": True,
                    "max_results_per_provider": 40,
                    "max_hotspots": 5,
                    "similarity_threshold": 0.86,
                    "topics": [{
                        "id": "gene-editing",
                        "zh": "作物 基因编辑 育种",
                        "en": "crop gene editing breeding",
                    }],
                }
            }
        })

        search = loaded["NEWS_SEARCH"]
        self.assertTrue(search["ENABLED"])
        self.assertEqual(search["MAX_RESULTS_PER_PROVIDER"], 40)
        self.assertEqual(search["MAX_HOTSPOTS"], 5)
        self.assertEqual(search["TOPICS"][0]["id"], "gene-editing")

    def test_loader_falls_back_for_non_numeric_news_search_limits(self):
        loaded = _load_rss_config({
            "rss": {
                "news_search": {
                    "max_results_per_provider": "many",
                    "max_hotspots": "several",
                    "similarity_threshold": "high",
                }
            }
        })

        search = loaded["NEWS_SEARCH"]
        self.assertEqual(search["MAX_RESULTS_PER_PROVIDER"], 50)
        self.assertEqual(search["MAX_HOTSPOTS"], 5)
        self.assertEqual(search["SIMILARITY_THRESHOLD"], 0.86)
