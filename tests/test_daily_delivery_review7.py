import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytz

from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.storage.manager import StorageManager

from tests.test_daily_delivery_review4 import (
    _ConditionalS3,
    news_db_bytes,
    remote_backend,
)


DATE = "2026-08-09"
KEY = f"news/{DATE}.db"


def shanghai(hour=10, minute=0):
    return pytz.timezone("Asia/Shanghai").localize(
        datetime(2026, 8, 9, hour, minute)
    )


def manager_for(backend, data_dir):
    manager = StorageManager(
        backend_type="local",
        data_dir=str(data_dir),
        enable_txt=False,
        enable_html=False,
        timezone="Asia/Shanghai",
    )
    manager._backend = backend
    return manager


def pipeline_for(storage, *, strict):
    return AIFilterPipeline(
        {
            "TIMEZONE": "Asia/Shanghai",
            "RSS": {"ENABLED": False, "FRESHNESS_FILTER": {}},
            "AI": {},
            "AI_FILTER": {
                "INTERESTS_FILE": "rice.txt",
                "BATCH_SIZE": 10,
                "BATCH_INTERVAL": 0,
            },
            "FILTER": {},
        },
        storage,
        shanghai,
        rss_ids_authoritative=strict,
        strict=strict,
        operation_date=DATE if strict else None,
    )


def configured_filter(prompt_hash, *, extracted_tag="新标签"):
    ai_filter = MagicMock()
    ai_filter.load_interests_content.return_value = "rice breeding"
    ai_filter.compute_interests_hash.return_value = prompt_hash
    ai_filter.extract_tags.return_value = [{
        "tag": extracted_tag,
        "description": "new description",
    }]
    return ai_filter


def remote_prompt_hash(s3, cache_dir):
    observer = remote_backend(cache_dir, s3)
    try:
        return observer.get_ai_filter_tag_snapshot_strict(
            DATE, "rice.txt"
        )["prompt_hash"]
    finally:
        observer.cleanup()


class StrictBatchAbortTests(unittest.TestCase):
    def test_pipeline_validation_failure_aborts_first_tag_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            s3 = _ConditionalS3()
            s3.set(KEY, news_db_bytes(tmp, "validation-baseline"), "v1")
            backend = remote_backend(root / "writer-cache", s3)
            manager = manager_for(backend, root / "manager")
            pipeline = pipeline_for(manager, strict=True)
            ai_filter = configured_filter("new-hash")

            try:
                with patch(
                    "trendradar.ai.filter_pipeline.AIFilter",
                    return_value=ai_filter,
                ), patch.object(
                    pipeline,
                    "_validate_strict_tag_snapshot",
                    side_effect=RuntimeError("injected validation failure"),
                ):
                    result = pipeline.run()

                self.assertFalse(result.success)
                self.assertEqual(
                    remote_prompt_hash(s3, root / "observer-cache"),
                    "old-hash",
                )
                self.assertFalse(backend._batch_mode)
                self.assertEqual(backend._batch_dirty, set())
                self.assertEqual(backend._batch_snapshots, {})
                self.assertNotIn(KEY, backend._strict_local_authoritative)
                local_path = backend._get_local_db_path(DATE, "news")
                self.assertNotIn(str(local_path), backend._db_connections)
                self.assertFalse(Path(f"{local_path}-wal").exists())
                self.assertFalse(Path(f"{local_path}-shm").exists())
            finally:
                backend.cleanup()

    def test_pipeline_second_begin_failure_aborts_first_tag_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            s3 = _ConditionalS3()
            s3.set(KEY, news_db_bytes(tmp, "second-begin-baseline"), "v1")
            backend = remote_backend(root / "writer-cache", s3)
            manager = manager_for(backend, root / "manager")
            pipeline = pipeline_for(manager, strict=True)
            ai_filter = configured_filter("new-hash")
            original_begin = backend._begin_news_mutation
            begin_calls = 0

            def fail_second_begin(date):
                nonlocal begin_calls
                begin_calls += 1
                if begin_calls == 2:
                    raise RuntimeError("injected second begin failure")
                return original_begin(date)

            try:
                with patch(
                    "trendradar.ai.filter_pipeline.AIFilter",
                    return_value=ai_filter,
                ), patch.object(
                    backend,
                    "_begin_news_mutation",
                    side_effect=fail_second_begin,
                ):
                    result = pipeline.run()

                self.assertEqual(begin_calls, 2)
                self.assertFalse(result.success)
                self.assertEqual(
                    remote_prompt_hash(s3, root / "observer-cache"),
                    "old-hash",
                )
                self.assertFalse(backend._batch_mode)
                self.assertEqual(backend._batch_dirty, set())
                self.assertEqual(backend._batch_snapshots, {})
                self.assertNotIn(KEY, backend._strict_local_authoritative)
            finally:
                backend.cleanup()


class OrdinaryBatchCommitResultTests(unittest.TestCase):
    @staticmethod
    def _backend_and_manager(root, s3):
        backend = remote_backend(root / "writer-cache", s3)
        backend._get_configured_time = shanghai
        return backend, manager_for(backend, root / "manager")

    def test_manager_propagates_remote_batch_commit_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            s3 = _ConditionalS3()
            s3.set(KEY, news_db_bytes(tmp, "manager-baseline"), "v1")
            backend, manager = self._backend_and_manager(root, s3)
            try:
                manager.begin_batch()
                self.assertEqual(
                    manager.update_ai_filter_tags_hash(
                        "rice.txt", "locally-dirty", DATE
                    ),
                    1,
                )
                s3.fail_keys_once.add(KEY)

                self.assertIs(manager.end_batch(), False)
                self.assertEqual(
                    remote_prompt_hash(s3, root / "observer-cache"),
                    "old-hash",
                )
            finally:
                backend.cleanup()

    def test_ordinary_pipeline_fails_when_deferred_remote_commit_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            s3 = _ConditionalS3()
            s3.set(KEY, news_db_bytes(tmp, "pipeline-baseline"), "v1")
            backend, manager = self._backend_and_manager(root, s3)
            pipeline = pipeline_for(manager, strict=False)
            pending = {
                "id": 7,
                "title": "Rice breeding result",
                "source_id": "journal",
                "source_name": "Journal",
                "url": "https://example.org/rice",
                "content": "Rice breeding evidence",
                "first_time": "2026-08-09 09:30:00",
            }
            pipeline._collect_pending_news = MagicMock(return_value=(
                [pending], [], [pending], set(), [], set(), 0
            ))
            pipeline._enrich_pending_items = lambda items, _label: items
            ai_filter = configured_filter("old-hash")
            ai_filter.classify_batch.return_value = [{
                "news_item_id": 7,
                "tag_id": 1,
                "relevance_score": 0.9,
                "importance_score": 0.8,
                "ai_summary": "Rice breeding summary",
            }]
            s3.fail_keys_once.add(KEY)

            try:
                with patch(
                    "trendradar.ai.filter_pipeline.AIFilter",
                    return_value=ai_filter,
                ):
                    result = pipeline.run()

                self.assertFalse(result.success)
                self.assertIn("持久化", result.error)
                self.assertEqual(
                    remote_prompt_hash(s3, root / "observer-cache"),
                    "old-hash",
                )
            finally:
                backend.cleanup()

    def test_ordinary_pipeline_accepts_legacy_none_batch_result(self):
        storage = MagicMock()
        storage.backend_name = "third-party"
        storage.get_latest_prompt_hash.return_value = "stable"
        storage.get_active_ai_filter_tags.return_value = [{
            "id": 1,
            "tag": "育种",
            "description": "",
            "priority": 1,
        }]
        storage.get_active_ai_filter_results.return_value = []
        storage.end_batch.return_value = None
        pipeline = pipeline_for(storage, strict=False)
        pipeline._collect_pending_news = MagicMock(return_value=(
            [], [], [], set(), [], set(), 0
        ))
        ai_filter = configured_filter("stable")

        with patch(
            "trendradar.ai.filter_pipeline.AIFilter",
            return_value=ai_filter,
        ):
            result = pipeline.run()

        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
