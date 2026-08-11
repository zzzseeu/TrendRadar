import unittest

from trendradar.ai.filter import AIFilter
from trendradar.ai.filter_pipeline import AIFilterPipeline


def _filter_with_prompt(system: str, user: str) -> AIFilter:
    ai_filter = AIFilter.__new__(AIFilter)
    ai_filter.classify_system = system
    ai_filter.classify_user = user
    return ai_filter


class AIFilterRuleFingerprintTests(unittest.TestCase):
    def test_classification_prompt_change_changes_fingerprint(self):
        interests = "1. 水稻育种\n2. 其他作物育种"
        old_filter = _filter_with_prompt("旧分类规则", "{news_list}")
        new_filter = _filter_with_prompt("新分类规则", "{news_list}")

        self.assertNotEqual(
            old_filter.compute_interests_hash(interests),
            new_filter.compute_interests_hash(interests),
        )

    def test_interest_comments_do_not_change_fingerprint(self):
        ai_filter = _filter_with_prompt("分类规则", "{news_list}")

        self.assertEqual(
            ai_filter.compute_interests_hash("# Version: 1\n1. 水稻育种"),
            ai_filter.compute_interests_hash("# Version: 2\n1. 水稻育种"),
        )


class _IncrementalStorageStub:
    def __init__(self):
        self.cleared_files = []

    def update_ai_filter_tag_descriptions(self, tags, interests_file):
        return len(tags)

    def update_ai_filter_tag_priorities(self, tags, interests_file):
        return len(tags)

    def update_ai_filter_tags_hash(self, interests_file, current_hash):
        return 1

    def clear_analyzed_news(self, interests_file):
        self.cleared_files.append(interests_file)
        return 6


class IncrementalRuleInvalidationTests(unittest.TestCase):
    def test_incremental_update_without_new_tags_clears_all_analyzed_news(self):
        storage = _IncrementalStorageStub()
        pipeline = AIFilterPipeline(
            {"RSS": {"ENABLED": False}, "AI_FILTER": {}},
            storage,
            lambda: None,
        )

        pipeline._apply_incremental_update(
            old_tags=[{"id": 1, "tag": "育种"}],
            keep_tags=[{"tag": "育种", "description": "育种新闻"}],
            add_tags=[],
            remove_tags=[],
            change_ratio=0.0,
            threshold=0.6,
            new_version=2,
            current_hash="ai_interests.txt:new",
            effective_interests_file="ai_interests.txt",
        )

        self.assertEqual(storage.cleared_files, ["ai_interests.txt"])
