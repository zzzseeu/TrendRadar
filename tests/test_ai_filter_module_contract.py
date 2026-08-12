import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from trendradar.ai.filter import AIFilter, _InvalidClassificationResponse
from trendradar.core.loader import _load_ai_filter_config


ROOT = Path(__file__).resolve().parents[1]


class AIFilterModuleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zh_config = yaml.safe_load(
            (ROOT / "config/config.yaml").read_text(encoding="utf-8")
        )
        cls.en_config = yaml.safe_load(
            (ROOT / "config/config.en.yaml").read_text(encoding="utf-8")
        )
        cls.loaded = _load_ai_filter_config(cls.zh_config)
        cls.interests = (ROOT / "config/ai_interests.txt").read_text(
            encoding="utf-8"
        )
        cls.classify_prompt = (ROOT / "config/ai_filter/prompt.txt").read_text(
            encoding="utf-8"
        )
        cls.extract_prompt = (
            ROOT / "config/ai_filter/extract_prompt.txt"
        ).read_text(encoding="utf-8")
        cls.update_prompt = (
            ROOT / "config/ai_filter/update_tags_prompt.txt"
        ).read_text(encoding="utf-8")

    def test_global_min_score_and_policy_topics_are_single_source_of_truth(self):
        self.assertEqual(self.zh_config["ai_filter"]["min_score"], 0.5)
        self.assertEqual(self.en_config["ai_filter"]["min_score"], 0.5)
        self.assertEqual(self.loaded["MIN_SCORE"], 0.5)
        topic_ids = {
            topic["id"]
            for topic in self.zh_config["rss"]["news_search"]["topics"]
        }
        self.assertTrue({
            "seed-policy",
            "breeding-policy-support",
            "breeding-policy-implementation",
        }.issubset(topic_ids))

    def test_publicity_meetings_and_inspections_are_not_hard_exclusions(self):
        combined = (
            self.interests
            + self.classify_prompt
            + self.extract_prompt
            + self.update_prompt
        )
        self.assertIn("政策优先", combined)
        self.assertIn("领导调研", combined)
        self.assertNotIn("会议宣传、培训招生和纯营销内容", combined)

    def test_strict_classification_covers_every_input_and_persists_modules(self):
        ai_filter = AIFilter.__new__(AIFilter)
        ai_filter.debug = False
        titles = [
            {"id": 1, "title": "政策", "content_level": "title_only"},
            {"id": 2, "title": "科研", "content_level": "summary"},
            {"id": 3, "title": "无关", "content_level": "full_text"},
        ]
        tags = [{"id": 11, "tag": "政策"}, {"id": 12, "tag": "科研"}]
        response = json.dumps([
            {"id": 1, "module_type": "policy", "species_scope": "rice", "tag_id": 11,
             "score": 0.50, "importance_score": 0.80, "summary": "政策部署"},
            {"id": 2, "module_type": "research", "species_scope": "rice", "tag_id": 12,
             "score": 0.49, "importance_score": 0.90, "summary": "科研成果"},
            {"id": 3, "module_type": "exclude", "species_scope": "not_applicable",
             "score": 0.10, "importance_score": 0.10, "summary": "内容无关"},
        ])

        results = ai_filter._parse_classify_response(response, titles, tags, strict=True)

        self.assertEqual([row["module_type"] for row in results], ["policy", "research"])
        self.assertEqual([row["relevance_score"] for row in results], [0.50, 0.49])
        self.assertEqual([row["news_item_id"] for row in results], [1, 2])

    def test_strict_classification_rejects_missing_ids_invalid_modules_and_tags(self):
        ai_filter = AIFilter.__new__(AIFilter)
        ai_filter.debug = False
        titles = [{"id": 1, "title": "政策"}, {"id": 2, "title": "科研"}]
        tags = [{"id": 11, "tag": "政策"}]
        valid = {
            "id": 1, "module_type": "policy", "species_scope": "rice", "tag_id": 11,
            "score": 0.5, "importance_score": 0.8, "summary": "政策部署",
        }
        cases = {
            "missing_id": [valid],
            "duplicate_id": [valid, dict(valid)],
            "unknown_id": [
                dict(valid, id=999),
                dict(valid, id=2, module_type="exclude", species_scope="not_applicable", summary="无关"),
            ],
            "unknown_module": [
                dict(valid, module_type="unknown"),
                dict(valid, id=2, module_type="exclude", species_scope="not_applicable", summary="无关"),
            ],
            "missing_persisted_tag": [
                {key: value for key, value in valid.items() if key != "tag_id"},
                dict(valid, id=2, module_type="exclude", species_scope="not_applicable", summary="无关"),
            ],
            "nan_score": [
                dict(valid, score=float("nan")),
                dict(valid, id=2, module_type="exclude", species_scope="not_applicable", summary="无关"),
            ],
            "boolean_importance": [
                dict(valid, importance_score=True),
                dict(valid, id=2, module_type="exclude", species_scope="not_applicable", summary="无关"),
            ],
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(_InvalidClassificationResponse):
                    ai_filter._parse_classify_response(
                        json.dumps(payload), titles, tags, strict=True
                    )

    def test_strict_classification_rejects_non_string_module_types(self):
        ai_filter = AIFilter.__new__(AIFilter)
        ai_filter.debug = False
        titles = [{"id": 1, "title": "政策"}, {"id": 2, "title": "无关"}]
        tags = [{"id": 11, "tag": "政策"}]
        valid = {
            "id": 1, "module_type": "policy", "species_scope": "rice", "tag_id": 11,
            "score": 0.5, "importance_score": 0.8, "summary": "政策部署",
        }
        for module_type in ([], {}):
            with self.subTest(module_type=module_type):
                payload = [
                    dict(valid, module_type=module_type),
                    {
                        "id": 2, "module_type": "exclude", "species_scope": "not_applicable", "score": 0.1,
                        "importance_score": 0.1, "summary": "内容无关",
                    },
                ]
                with self.assertRaises(_InvalidClassificationResponse):
                    ai_filter._parse_classify_response(
                        json.dumps(payload), titles, tags, strict=True
                    )

    def test_non_string_module_type_triggers_one_repair(self):
        ai_filter = AIFilter.__new__(AIFilter)
        ai_filter.debug = False
        ai_filter.classify_system = "只返回 JSON"
        ai_filter.classify_user = "{interests_content}\n{tags_list}\n{news_count}\n{news_list}"
        ai_filter.summary_grounding_review_enabled = False
        ai_filter.client = MagicMock()
        invalid = json.dumps([
            {
                "id": 1, "module_type": [], "species_scope": "rice", "tag_id": 1,
                "score": 0.5, "importance_score": 0.8, "summary": "政策部署",
            },
            {
                "id": 2, "module_type": "exclude", "species_scope": "not_applicable", "score": 0.1,
                "importance_score": 0.1, "summary": "内容无关",
            },
        ])
        repaired = json.dumps([
            {
                "id": 1, "module_type": "policy", "species_scope": "rice", "tag_id": 1,
                "score": 0.5, "importance_score": 0.8, "summary": "政策部署",
            },
            {
                "id": 2, "module_type": "exclude", "species_scope": "not_applicable", "score": 0.1,
                "importance_score": 0.1, "summary": "内容无关",
            },
        ])
        ai_filter.client.chat.side_effect = [invalid, repaired]

        result = ai_filter.classify_batch(
            [
                {"id": 1, "title": "政策", "content": "政策"},
                {"id": 2, "title": "无关", "content": "无关"},
            ],
            [{"id": 11, "tag": "政策"}],
            "政策优先",
            strict=True,
        )

        self.assertEqual([item["news_item_id"] for item in result], [1])
        self.assertEqual(ai_filter.client.chat.call_count, 2)

    def test_raw_scores_are_never_rewritten_by_content_level(self):
        ai_filter = AIFilter.__new__(AIFilter)
        ai_filter.debug = False
        tags = [{"id": 1, "tag": "育种"}]
        titles = [
            {"id": 1, "title": "标题", "content_level": "title_only"},
            {"id": 2, "title": "摘要", "content_level": "summary"},
            {"id": 3, "title": "正文", "content_level": "full_text"},
        ]
        response = json.dumps([
            {"id": 1, "module_type": "policy", "species_scope": "rice", "tag_id": 1, "score": 0.1},
            {"id": 2, "module_type": "research", "species_scope": "rice", "tag_id": 1, "score": 0.5},
            {"id": 3, "module_type": "research", "species_scope": "rice", "tag_id": 1, "score": 0.9},
        ])

        results = ai_filter._parse_classify_response(response, titles, tags)

        self.assertEqual([row["relevance_score"] for row in results], [0.1, 0.5, 0.9])

    def test_nonempty_strict_batch_rejects_empty_repair_response(self):
        ai_filter = AIFilter.__new__(AIFilter)
        ai_filter.debug = False
        ai_filter.classify_system = "只返回 JSON"
        ai_filter.classify_user = "{interests_content}\n{tags_list}\n{news_count}\n{news_list}"
        ai_filter.summary_grounding_review_enabled = False
        ai_filter.client = MagicMock()
        ai_filter.client.chat.side_effect = ["[]", "[]"]

        result = ai_filter.classify_batch(
            [{"id": 1, "title": "政策", "content": "政策"}],
            [{"id": 11, "tag": "政策"}],
            "政策优先",
            strict=True,
        )

        self.assertIsNone(result)
        self.assertEqual(ai_filter.client.chat.call_count, 2)


if __name__ == "__main__":
    unittest.main()
