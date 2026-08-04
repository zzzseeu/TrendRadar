from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CropBreedingFilterRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.interests = (ROOT / "config/ai_interests.txt").read_text(
            encoding="utf-8"
        )
        cls.prompt = (ROOT / "config/ai_filter/prompt.txt").read_text(
            encoding="utf-8"
        )
        cls.config = (ROOT / "config/config.yaml").read_text(encoding="utf-8")

    def test_other_crops_are_admitted_by_breeding_value(self):
        self.assertIn("不作为准入前提", self.interests)
        self.assertIn("实质育种价值", self.interests)
        self.assertIn("不再要求先证明可迁移到水稻", self.prompt)
        self.assertNotIn(
            "其他作物只有在方法、资源或育种模式可明确迁移到水稻时才保留",
            self.prompt,
        )

    def test_title_only_breeding_news_has_an_admissible_score_band(self):
        self.assertIn("0.70～0.78", self.prompt)
        self.assertIn("仅标题显示：", self.prompt)
        self.assertIn("min_score: 0.7", self.config)

    def test_rice_priority_affects_ranking_not_admission(self):
        self.assertIn("水稻研究优先排序", self.interests)
        self.assertIn("排序和重要性评分", self.prompt)


if __name__ == "__main__":
    unittest.main()
