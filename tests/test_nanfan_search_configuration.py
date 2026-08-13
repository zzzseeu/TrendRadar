import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class NanfanSearchConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = yaml.safe_load(
            (ROOT / "config/config.yaml").read_text(encoding="utf-8")
        )
        cls.interests = (ROOT / "config/ai_interests.txt").read_text(
            encoding="utf-8"
        )

    def test_nanfan_has_an_independent_bilingual_search_topic(self):
        topics = self.config["rss"]["news_search"]["topics"]
        matching = [topic for topic in topics if topic["id"] == "nanfan-breeding"]

        self.assertEqual(
            matching,
            [
                {
                    "id": "nanfan-breeding",
                    "zh": "南繁 水稻 育种 制种 种业",
                    "en": "Hainan Nanfan rice breeding seed industry",
                }
            ],
        )

    def test_hainan_official_domains_are_authority_sources(self):
        domains = self.config["rss"]["news_search"]["authority_domains"]

        self.assertIn("hainan.gov.cn", domains)
        self.assertIn("sanya.gov.cn", domains)

    def test_existing_major_project_interest_covers_nanfan(self):
        item = next(
            line
            for line in self.interests.splitlines()
            if line.startswith("17. 水稻重大科研项目与研究机构：")
        )

        for phrase in (
            "国家南繁科研育种基地",
            "南繁硅谷",
            "海南自由贸易港种业",
            "水稻南繁加代",
            "制种",
            "品种选育",
            "科研平台建设",
        ):
            self.assertIn(phrase, item)


if __name__ == "__main__":
    unittest.main()
