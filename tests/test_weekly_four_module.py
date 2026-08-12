import json
import unittest
from pathlib import Path

from trendradar.ai.filter import AIFilter
from trendradar.core.weekly import select_weekly_modules


ROOT = Path(__file__).resolve().parents[1]


def _item(module_type, species_scope, index, **overrides):
    item = {
        "module_type": module_type,
        "species_scope": species_scope,
        "title": f"{module_type}-{species_scope}-{index}",
        "url": f"https://example.org/{module_type}/{species_scope}/{index}",
        "source_name": "Source",
        "relevance_score": 0.8,
        "importance_score": 1 - index / 100,
        "content_level": "full_text",
        "published_at": "2026-08-09T08:00:00+08:00",
    }
    item.update(overrides)
    return item


class WeeklyFourModuleContractTests(unittest.TestCase):
    def test_strict_classification_preserves_industry_and_species_scope(self):
        ai_filter = AIFilter.__new__(AIFilter)
        ai_filter.debug = False
        titles = [
            {"id": 1, "title": "水稻最低收购价"},
            {"id": 2, "title": "水稻订单生产启动"},
            {"id": 3, "title": "小麦基因组研究"},
            {"id": 4, "title": "无关内容"},
        ]
        tags = [
            {"id": 11, "tag": "政策"},
            {"id": 12, "tag": "产业"},
            {"id": 13, "tag": "科研"},
        ]
        response = json.dumps({"items": [
            {"id": 1, "module_type": "policy", "species_scope": "rice",
             "tag_id": 11, "score": 0.8, "importance_score": 0.9,
             "summary": "稻谷政策"},
            {"id": 2, "module_type": "industry", "species_scope": "rice",
             "tag_id": 12, "score": 0.7, "importance_score": 0.8,
             "summary": "水稻产业动态"},
            {"id": 3, "module_type": "research", "species_scope": "other_crop",
             "tag_id": 13, "score": 0.6, "importance_score": 0.7,
             "summary": "其他作物科研"},
            {"id": 4, "module_type": "exclude", "species_scope": "not_applicable",
             "score": 0.1, "importance_score": 0.1, "summary": "无关"},
        ]}, ensure_ascii=False)

        results = ai_filter._parse_classify_response(
            response, titles, tags, strict=True
        )

        self.assertEqual(
            [(row["module_type"], row["species_scope"]) for row in results],
            [
                ("policy", "rice"),
                ("industry", "rice"),
                ("research", "other_crop"),
            ],
        )

    def test_selection_is_rice_only_for_policy_and_industry_and_rice_first_for_research(self):
        items = [
            _item("policy", "other_crop", 1, importance_score=1.0),
            _item("policy", "rice", 2),
            _item("industry", "other_crop", 3, importance_score=1.0),
            _item("industry", "rice", 4),
            _item("research", "other_crop", 5, importance_score=1.0),
            _item("research", "rice", 6, importance_score=0.4),
        ]

        selection = select_weekly_modules(
            items, min_score=0.5, limit_per_module=2
        )

        self.assertEqual([row["species_scope"] for row in selection.policy], ["rice"])
        self.assertEqual([row["species_scope"] for row in selection.industry], ["rice"])
        self.assertEqual(
            [row["species_scope"] for row in selection.research],
            ["rice", "other_crop"],
        )

    def test_classification_prompt_defines_four_modules_and_species_scope(self):
        prompt = (ROOT / "config/ai_filter/prompt.txt").read_text(encoding="utf-8")
        for token in (
            "policy", "industry", "research", "exclude",
            "rice", "other_crop", "not_applicable", "species_scope",
        ):
            self.assertIn(token, prompt)


if __name__ == "__main__":
    unittest.main()
