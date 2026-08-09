import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.test_daily_delivery_review4 import (
    _ConditionalS3,
    news_db_bytes,
    remote_backend,
)
from tests.test_daily_delivery_review7 import (
    DATE,
    KEY,
    configured_filter,
    pipeline_for,
    remote_prompt_hash,
)


def enable_persistent_wal(backend):
    conn = backend._get_ai_connection(DATE, strict=True)
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.commit()
    if str(mode).lower() != "wal":
        raise AssertionError(f"WAL mode unavailable: {mode}")
    return conn, backend._get_local_db_path(DATE, "news")


def replace_tags(backend, prompt_hash="new-hash"):
    return backend.replace_ai_filter_tags_strict(
        [{
            "tag": "新标签",
            "description": "must be transactional",
            "priority": 1,
        }],
        2,
        prompt_hash,
        DATE,
        "rice.txt",
    )


class RemoteWALSnapshotTests(unittest.TestCase):
    def _backend(self, root, name):
        s3 = _ConditionalS3()
        s3.set(KEY, news_db_bytes(root, name), "v1")
        backend = remote_backend(Path(root) / f"{name}-cache", s3)
        enable_persistent_wal(backend)
        return s3, backend

    def test_ordinary_zero_noop_keeps_first_wal_mutation_at_batch_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3, backend = self._backend(tmp, "ordinary-wal")
            try:
                backend.begin_batch()
                self.assertEqual(
                    backend.update_ai_filter_tags_hash(
                        "rice.txt", "ordinary-wal-hash", DATE
                    ),
                    1,
                )
                self.assertEqual(
                    backend.clear_unmatched_analyzed_news(DATE, "rice.txt"),
                    0,
                )
                self.assertTrue(backend.end_batch())

                self.assertEqual(
                    remote_prompt_hash(s3, Path(tmp) / "ordinary-observer"),
                    "ordinary-wal-hash",
                )
            finally:
                backend.cleanup()

    def test_strict_batch_end_uploads_committed_wal_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3, backend = self._backend(tmp, "strict-wal-end")
            try:
                backend.begin_batch()
                self.assertIsNotNone(replace_tags(backend, "strict-wal-hash"))
                local_path = backend._get_local_db_path(DATE, "news")
                self.assertTrue(Path(f"{local_path}-wal").exists())
                self.assertTrue(backend.end_batch_strict())

                self.assertEqual(
                    remote_prompt_hash(s3, Path(tmp) / "strict-observer"),
                    "strict-wal-hash",
                )
            finally:
                backend.cleanup()

    def test_strict_abort_restores_wal_snapshot_and_never_uploads(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3, backend = self._backend(tmp, "strict-wal-abort")
            try:
                backend.begin_batch()
                self.assertIsNotNone(replace_tags(backend, "aborted-wal-hash"))
                self.assertTrue(backend.abort_batch())

                local = backend.get_ai_filter_tag_snapshot_strict(
                    DATE, "rice.txt"
                )
                self.assertEqual(local["prompt_hash"], "old-hash")
                self.assertEqual(
                    remote_prompt_hash(s3, Path(tmp) / "abort-observer"),
                    "old-hash",
                )
            finally:
                backend.cleanup()


class RemoteRollbackFailureStateTests(unittest.TestCase):
    def _mutated_batch(self, root, name, *, wal=False):
        s3 = _ConditionalS3()
        s3.set(KEY, news_db_bytes(root, name), "v1")
        backend = remote_backend(Path(root) / f"{name}-cache", s3)
        if wal:
            enable_persistent_wal(backend)
        backend.begin_batch()
        self.assertIsNotNone(replace_tags(backend, f"{name}-dirty"))
        return s3, backend

    def _assert_remote_and_reloaded_local_are_old(self, root, s3, backend):
        self.assertEqual(
            remote_prompt_hash(s3, Path(root) / "fresh-observer"),
            "old-hash",
        )
        snapshot = backend.get_ai_filter_tag_snapshot_strict(DATE, "rice.txt")
        self.assertEqual(snapshot["prompt_hash"], "old-hash")

    def test_abort_replace_failure_safely_invalidates_then_redownloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3, backend = self._mutated_batch(tmp, "replace-failure")
            original_replace = Path.replace
            failed = False

            def fail_rollback_replace(path, target):
                nonlocal failed
                if not failed and path.name.endswith(".rollback"):
                    failed = True
                    raise OSError("injected rollback replace failure")
                return original_replace(path, target)

            try:
                with patch.object(Path, "replace", new=fail_rollback_replace):
                    with self.assertRaisesRegex(RuntimeError, "回滚"):
                        backend.abort_batch()
                self.assertTrue(failed)
                self._assert_remote_and_reloaded_local_are_old(
                    tmp, s3, backend
                )
            finally:
                backend.cleanup()

    def test_abort_close_failure_safely_invalidates_then_redownloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3, backend = self._mutated_batch(tmp, "close-failure")
            original_close = backend._close_cached_connection
            close_calls = 0

            def fail_first_close(local_path):
                nonlocal close_calls
                close_calls += 1
                if close_calls == 1:
                    raise OSError("injected close failure")
                return original_close(local_path)

            try:
                with patch.object(
                    backend,
                    "_close_cached_connection",
                    side_effect=fail_first_close,
                ):
                    with self.assertRaisesRegex(RuntimeError, "回滚"):
                        backend.abort_batch()
                self.assertGreaterEqual(close_calls, 2)
                self._assert_remote_and_reloaded_local_are_old(
                    tmp, s3, backend
                )
            finally:
                backend.cleanup()

    def test_abort_sidecar_failure_safely_invalidates_then_redownloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3, backend = self._mutated_batch(tmp, "sidecar-failure")
            local_path = backend._get_local_db_path(DATE, "news")
            wal_path = Path(f"{local_path}-wal")
            wal_path.write_bytes(b"injected stale sidecar")
            original_unlink = Path.unlink
            failed = False

            def fail_first_wal_unlink(path, *args, **kwargs):
                nonlocal failed
                if (
                    not failed
                    and str(path).endswith("-wal")
                    and path.exists()
                ):
                    failed = True
                    raise OSError("injected WAL unlink failure")
                return original_unlink(path, *args, **kwargs)

            try:
                with patch.object(Path, "unlink", new=fail_first_wal_unlink):
                    with self.assertRaisesRegex(RuntimeError, "回滚"):
                        backend.abort_batch()
                self.assertTrue(failed)
                self._assert_remote_and_reloaded_local_are_old(
                    tmp, s3, backend
                )
            finally:
                backend.cleanup()

    def test_failed_restore_and_invalidation_poison_future_strict_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3, backend = self._mutated_batch(tmp, "poison")
            try:
                with patch.object(
                    backend,
                    "_restore_local_sqlite_snapshot",
                    side_effect=OSError("restore unavailable"),
                ), patch.object(
                    backend,
                    "_invalidate_local_sqlite_token",
                    side_effect=OSError("invalidation unavailable"),
                    create=True,
                ):
                    with self.assertRaisesRegex(RuntimeError, "回滚"):
                        backend.abort_batch()

                self.assertEqual(
                    remote_prompt_hash(s3, Path(tmp) / "poison-observer"),
                    "old-hash",
                )
                with self.assertRaisesRegex(RuntimeError, "poison|污染|不安全"):
                    backend.get_ai_filter_tag_snapshot_strict(DATE, "rice.txt")
                backend.begin_batch()
                with self.assertRaisesRegex(RuntimeError, "poison|污染|不安全"):
                    replace_tags(backend, "must-not-commit")
            finally:
                backend.cleanup()

    def test_abort_invalidates_dirty_token_without_before_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            s3.set(KEY, news_db_bytes(tmp, "no-snapshot"), "v1")
            backend = remote_backend(Path(tmp) / "no-snapshot-cache", s3)
            try:
                backend.begin_batch()
                conn = backend._get_ai_connection(DATE, strict=True)
                conn.execute(
                    "UPDATE ai_filter_tags SET prompt_hash = ?",
                    ("dirty-without-snapshot",),
                )
                conn.commit()
                self.assertTrue(backend._upload_sqlite(DATE, "news"))
                self.assertNotIn((DATE, "news"), backend._batch_snapshots)
                self.assertTrue(backend.abort_batch())

                self._assert_remote_and_reloaded_local_are_old(
                    tmp, s3, backend
                )
            finally:
                backend.cleanup()


class PipelineBatchTerminationTests(unittest.TestCase):
    @staticmethod
    def _ordinary_storage():
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
        return storage

    def test_ordinary_false_end_is_not_called_twice_by_cleanup(self):
        storage = self._ordinary_storage()
        storage.end_batch.side_effect = [False, True]
        pipeline = pipeline_for(storage, strict=False)
        pipeline._collect_pending_news = MagicMock(return_value=(
            [], [], [], set(), [], set(), 0
        ))

        with patch(
            "trendradar.ai.filter_pipeline.AIFilter",
            return_value=configured_filter("stable"),
        ):
            result = pipeline.run()

        self.assertFalse(result.success)
        self.assertEqual(storage.end_batch.call_count, 1)

    def test_pipeline_reports_original_and_rollback_failures(self):
        storage = MagicMock()
        storage.backend_name = "third-party"
        storage.get_ai_filter_tag_snapshot_strict.return_value = {
            "prompt_hash": "old-hash",
            "latest_version": 1,
            "active_tags": [],
        }
        storage.abort_batch.side_effect = RuntimeError("rollback exploded")
        pipeline = pipeline_for(storage, strict=True)

        with patch(
            "trendradar.ai.filter_pipeline.AIFilter",
            return_value=configured_filter("new-hash"),
        ), patch.object(
            pipeline,
            "_validate_strict_tag_snapshot",
            side_effect=RuntimeError("validation exploded"),
        ):
            result = pipeline.run()

        self.assertFalse(result.success)
        self.assertIn("validation exploded", result.error)
        self.assertIn("rollback exploded", result.error)


if __name__ == "__main__":
    unittest.main()
