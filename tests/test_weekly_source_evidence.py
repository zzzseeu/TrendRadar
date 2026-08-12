import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from trendradar.ai.filter import AIFilter, _InvalidClassificationResponse
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.ai.source_evidence import classify_source_evidence


class WeeklySourceEvidenceTests(unittest.TestCase):
    def test_direct_journal_and_preprint_feeds_are_declared_scholarly(self):
        config = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "config/config.yaml")
            .read_text(encoding="utf-8")
        )
        categories = {
            feed["id"]: feed.get("content_category")
            for feed in config["rss"]["feeds"]
        }

        self.assertEqual(
            {
                source_id
                for source_id, category in categories.items()
                if category == "scholarly"
            },
            {
                "nature-plants",
                "nature-genetics",
                "nature-biotechnology",
                "science",
                "molecular-plant",
                "plant-communications",
                "rice-science",
                "crop-journal",
                "biorxiv-plant-biology",
            },
        )

    def test_scholarly_feed_is_always_research(self):
        evidence = classify_source_evidence(
            {
                "source_id": "rice-science",
                "title": "Recent Trends in Rice Bran Oil Extraction",
                "content": "正文没有重复期刊名称。",
            },
            {"rice-science": "scholarly"},
        )

        self.assertEqual(evidence.module_type, "research")
        self.assertEqual(evidence.reason, "scholarly_source")

    def test_official_story_with_journal_name_is_research(self):
        evidence = classify_source_evidence(
            {
                "source_id": "irri-news",
                "content": "相关成果已发表于 Plant Communications，并给出试验结果。",
            },
            {},
        )

        self.assertEqual(evidence.module_type, "research")
        self.assertEqual(evidence.reason, "journal_name")

    def test_official_story_with_complete_paper_title_is_research(self):
        evidence = classify_source_evidence(
            {
                "source_id": "cnrri-research",
                "content": (
                    "团队发表题为《水稻耐盐基因调控网络及其育种应用》的论文，"
                    "并公开了完整试验结果。"
                ),
            },
            {},
        )

        self.assertEqual(evidence.module_type, "research")
        self.assertEqual(evidence.reason, "paper_title")

    def test_generic_research_claim_doi_and_author_do_not_upgrade_news(self):
        cases = (
            "研究表明该技术有助于增产。",
            "团队取得重要研究进展。",
            "作者张三介绍了成果，DOI: 10.1000/example。",
            "该成果已经发表，后续将推广应用。",
        )

        for content in cases:
            with self.subTest(content=content):
                evidence = classify_source_evidence(
                    {"source_id": "irri-news", "content": content},
                    {},
                )
                self.assertEqual(evidence.module_type, "current_events")
                self.assertEqual(evidence.reason, "no_publication_evidence")

    def test_missing_content_on_non_scholarly_source_is_current_events(self):
        evidence = classify_source_evidence(
            {"source_id": "agri-breeding-search", "title": "水稻产业消息"},
            {},
        )

        self.assertEqual(evidence.module_type, "current_events")


class DeterministicModuleContractTests(unittest.TestCase):
    def setUp(self):
        self.ai_filter = AIFilter.__new__(AIFilter)
        self.ai_filter.debug = False
        self.tags = [
            {"id": 11, "tag": "产业动态"},
            {"id": 12, "tag": "遗传育种"},
        ]
        self.titles = [
            {
                "id": 1,
                "title": "官方产业消息",
                "module_type": "current_events",
                "module_reason": "no_publication_evidence",
            },
            {
                "id": 2,
                "title": "期刊论文",
                "module_type": "research",
                "module_reason": "scholarly_source",
            },
            {
                "id": 3,
                "title": "无关内容",
                "module_type": "current_events",
                "module_reason": "no_publication_evidence",
            },
        ]

    def test_ai_only_decides_inclusion_and_cannot_choose_module(self):
        response = json.dumps(
            {
                "items": [
                    {
                        "id": 1,
                        "include": True,
                        "species_scope": "rice",
                        "tag_id": 11,
                        "score": 0.7,
                        "importance_score": 0.6,
                        "summary": "水稻产业动态。",
                    },
                    {
                        "id": 2,
                        "include": True,
                        "species_scope": "other_crop",
                        "tag_id": 12,
                        "score": 0.8,
                        "importance_score": 0.9,
                        "summary": "可借鉴的作物育种论文。",
                    },
                    {
                        "id": 3,
                        "include": False,
                        "species_scope": "not_applicable",
                        "score": 0.1,
                        "importance_score": 0.1,
                        "summary": "与关注方向无关。",
                    },
                ]
            }
        )

        results = self.ai_filter._parse_classify_response(
            response, self.titles, self.tags, strict=True
        )

        self.assertEqual(
            [row["module_type"] for row in results],
            ["current_events", "research"],
        )
        self.assertEqual([row["news_item_id"] for row in results], [1, 2])

    def test_ai_module_override_is_rejected(self):
        response = json.dumps(
            {
                "items": [
                    {
                        "id": 1,
                        "include": True,
                        "module_type": "research",
                        "species_scope": "rice",
                        "tag_id": 11,
                        "score": 0.7,
                        "importance_score": 0.6,
                        "summary": "试图改变模块。",
                    },
                    {
                        "id": 2,
                        "include": False,
                        "species_scope": "not_applicable",
                        "score": 0.1,
                        "importance_score": 0.1,
                        "summary": "不保留。",
                    },
                    {
                        "id": 3,
                        "include": False,
                        "species_scope": "not_applicable",
                        "score": 0.1,
                        "importance_score": 0.1,
                        "summary": "不保留。",
                    },
                ]
            }
        )

        with self.assertRaises(_InvalidClassificationResponse):
            self.ai_filter._parse_classify_response(
                response, self.titles, self.tags, strict=True
            )

    def test_missing_deterministic_module_metadata_is_rejected(self):
        response = json.dumps(
            {
                "items": [
                    {
                        "id": 1,
                        "include": False,
                        "species_scope": "not_applicable",
                        "score": 0.1,
                        "importance_score": 0.1,
                        "summary": "不保留。",
                    }
                ]
            }
        )

        with self.assertRaises(_InvalidClassificationResponse):
            self.ai_filter._parse_classify_response(
                response,
                [{"id": 1, "title": "缺少模块证据"}],
                self.tags,
                strict=True,
            )

    def test_strict_batch_retries_one_transient_classification_failure(self):
        pipeline = AIFilterPipeline.__new__(AIFilterPipeline)
        pipeline._strict = True
        pipeline._rss_feeds = []
        pipeline._enrich_pending_items = lambda items, _label: items
        ai_filter = MagicMock()
        ai_filter.classify_batch.side_effect = [None, []]
        pending = [{
            "id": 1,
            "title": "水稻产业动态",
            "source_name": "Official",
            "source_id": "irri-news",
            "url": "https://example.org/rice",
            "content": "水稻产业动态正文",
            "content_level": "full_text",
            "risk_warning": "",
            "module_type": "current_events",
            "module_reason": "no_publication_evidence",
        }]

        results, news_ids, rss_ids = pipeline._classify_batches(
            ai_filter,
            [],
            pending,
            [{"id": 1, "tag": "水稻产业"}],
            "水稻兴趣",
            {"BATCH_SIZE": 10, "BATCH_INTERVAL": 0},
        )

        self.assertEqual(results, [])
        self.assertEqual(news_ids, [])
        self.assertEqual(rss_ids, [1])
        self.assertEqual(ai_filter.classify_batch.call_count, 2)


class SourceEvidencePipelineTests(unittest.TestCase):
    def test_enriched_items_receive_fixed_modules_before_ai(self):
        pipeline = AIFilterPipeline.__new__(AIFilterPipeline)
        pipeline._rss_feeds = [
            {"id": "rice-science", "content_category": "scholarly"},
            {"id": "irri-news", "content_category": "official"},
        ]
        pipeline._strict = True
        pipeline._enrich_pending_items = lambda items, _label: [
            dict(item) for item in items
        ]

        captured = []

        class FakeFilter:
            def classify_batch(self, titles, _tags, _interests, strict=False):
                captured.extend(titles)
                return []

        pipeline._classify_batches(
            FakeFilter(),
            [],
            [
                {
                    "id": 1,
                    "source_id": "rice-science",
                    "title": "期刊条目",
                    "content": "标题与摘要",
                },
                {
                    "id": 2,
                    "source_id": "irri-news",
                    "title": "机构消息",
                    "content": "团队取得水稻研究进展。",
                },
                {
                    "id": 3,
                    "source_id": "irri-news",
                    "title": "论文消息",
                    "content": "成果发表于 Rice Science。",
                },
            ],
            [{"id": 1, "tag": "水稻"}],
            "水稻优先",
            {"BATCH_SIZE": 20, "BATCH_INTERVAL": 0},
        )

        self.assertEqual(
            [item["module_type"] for item in captured],
            ["research", "current_events", "research"],
        )
        self.assertEqual(
            [item["module_reason"] for item in captured],
            ["scholarly_source", "no_publication_evidence", "journal_name"],
        )

if __name__ == "__main__":
    unittest.main()
