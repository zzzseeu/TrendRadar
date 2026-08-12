import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytz
from botocore.exceptions import ClientError

from trendradar.ai.analyzer import AIAnalyzer
from trendradar.ai.filter import AIFilter
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.core.daily_delivery import DailyDeliveryAggregator
from trendradar.storage.base import RSSData, RSSItem, StorageBackend
from trendradar.storage.local import LocalStorageBackend
from trendradar.storage.remote import RemoteStorageBackend


def shanghai(year, month, day, hour, minute, second=0):
    return pytz.timezone("Asia/Shanghai").localize(
        datetime(year, month, day, hour, minute, second)
    )


def rss_data(date, crawl_time, title, url, feed_id="journal"):
    return RSSData(
        date=date,
        crawl_time=crawl_time,
        items={feed_id: [RSSItem(
            title=title,
            feed_id=feed_id,
            feed_name=feed_id,
            url=url,
            first_time=crawl_time,
        )]},
        id_to_name={feed_id: feed_id},
        failed_ids=[],
    )


class _Body:
    def __init__(self, payload):
        self.payload = payload

    def iter_chunks(self, chunk_size=1024 * 1024):
        del chunk_size
        yield self.payload


class _VersionedS3:
    def __init__(self):
        self.objects = {}
        self.sequence = 0

    def set(self, key, payload, version):
        self.objects[key] = {
            "payload": payload,
            "ETag": f'"{version}"',
            "VersionId": version,
            "LastModified": datetime(2026, 8, 9, 2, 0),
        }

    def head_object(self, Bucket, Key):
        del Bucket
        obj = self.objects.get(Key)
        if obj is None:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "missing"}},
                "HeadObject",
            )
        return {key: obj[key] for key in ("ETag", "VersionId", "LastModified")}

    def get_object(self, Bucket, Key):
        del Bucket
        return {"Body": _Body(self.objects[Key]["payload"])}

    def put_object(self, Bucket, Key, Body, **kwargs):
        del Bucket
        current = self.objects.get(Key)
        if (
            kwargs.get("IfMatch") is not None
            and (
                current is None
                or current["ETag"] != kwargs["IfMatch"]
            )
        ) or (
            kwargs.get("IfNoneMatch") == "*" and current is not None
        ):
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
            )
        self.sequence += 1
        version = f"put-{self.sequence}"
        self.set(Key, Body, version)
        return {"VersionId": version, "ETag": f'"{version}"'}

    def get_paginator(self, name):
        if name != "list_objects_v2":
            raise AssertionError(name)
        paginator = MagicMock()
        paginator.paginate.side_effect = self._paginate
        return paginator

    def _paginate(self, Bucket, Prefix):
        del Bucket
        return [{"Contents": [
            {"Key": key}
            for key in sorted(self.objects)
            if key.startswith(Prefix)
        ]}]


class _NoVersionAdvanceS3(_VersionedS3):
    def put_object(self, Bucket, Key, Body, **kwargs):
        del Bucket
        current = self.objects[Key]
        if kwargs.get("IfMatch") not in (None, current["ETag"]):
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
            )
        current["payload"] = Body
        return {
            "VersionId": current["VersionId"],
            "ETag": current["ETag"],
        }


def remote_backend(tmp, s3):
    backend = RemoteStorageBackend.__new__(RemoteStorageBackend)
    backend.bucket_name = "bucket"
    backend.temp_dir = Path(tmp) / "remote-cache"
    backend.temp_dir.mkdir(parents=True, exist_ok=True)
    backend.timezone = "Asia/Shanghai"
    backend.s3_client = s3
    backend._db_connections = {}
    backend._downloaded_files = []
    backend._batch_mode = False
    backend._batch_dirty = set()
    backend._remote_provenance = {}
    return backend


class _FailSoftThirdPartyStorage(StorageBackend):
    def __init__(self, data):
        self.data = data

    @property
    def backend_name(self):
        return "third-party"

    @property
    def supports_txt(self):
        return False

    def save_news_data(self, data):
        del data
        return True

    def get_today_all_data(self, date=None):
        del date
        return None

    def get_latest_crawl_data(self, date=None):
        del date
        return None

    def detect_new_titles(self, current_data):
        del current_data
        return {}

    def save_txt_snapshot(self, data):
        del data
        return None

    def save_html_report(self, html_content, filename):
        del html_content, filename
        return None

    def is_first_crawl_today(self, date=None):
        del date
        return True

    def cleanup(self):
        return None

    def cleanup_old_data(self, retention_days):
        del retention_days
        return 0

    def get_rss_data(self, date=None):
        return self.data if date == self.data.date else None

    def get_rss_feed_statuses(self, date=None):
        return {"journal": "success"} if date == self.data.date else {}

    def get_all_rss_ids(self, date=None):
        if date != self.data.date:
            return []
        return [{"id": 1, "title": "Rice", "source_id": "journal"}]


class DailyDeliveryFirstSeenLedgerTests(unittest.TestCase):
    def test_legacy_backfill_happens_once_then_queries_only_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            try:
                # 直接写日库模拟升级前已有历史，不触发新保存路径的账本同步。
                backend._save_rss_data_impl(rss_data(
                    "2026-08-07", "2026-08-07 09:00:00",
                    "Old", "https://example.org/story?utm_source=old",
                ))
                backend._save_rss_data_impl(rss_data(
                    "2026-08-09", "2026-08-09 09:00:00",
                    "New", "https://example.org/story?utm_source=new",
                ))
                identity = ("url", "https://example.org/story")

                with patch.object(
                    backend,
                    "_open_rss_history_snapshot_strict",
                    wraps=backend._open_rss_history_snapshot_strict,
                ) as history_open:
                    first = backend.get_earliest_rss_discoveries_strict(
                        {identity}, "2026-08-09"
                    )
                    self.assertGreater(history_open.call_count, 0)
                    history_open.reset_mock()
                    second = backend.get_earliest_rss_discoveries_strict(
                        {identity}, "2026-08-09"
                    )
                    history_open.assert_not_called()

                self.assertEqual(first, second)
                self.assertEqual(
                    first[identity],
                    ("2026-08-07 09:00:00", "2026-08-07"),
                )
                self.assertTrue(
                    (Path(tmp) / "rss" / "first-seen-v1.db").exists()
                )
            finally:
                backend.cleanup()

    def test_failed_ledger_update_makes_save_fail_and_retry_backfills(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            data = rss_data(
                "2026-08-09", "2026-08-09 09:00:01",
                "Retry", "https://example.org/retry",
            )
            try:
                with patch.object(
                    backend,
                    "_sync_first_seen_ledger_strict",
                    side_effect=RuntimeError("ledger broken"),
                    create=True,
                ):
                    self.assertFalse(backend.save_rss_data(data))

                self.assertTrue(backend.save_rss_data(data))
                identity = ("url", "https://example.org/retry")
                self.assertEqual(
                    backend.get_earliest_rss_discoveries_strict(
                        {identity}, "2026-08-09"
                    )[identity],
                    ("2026-08-09 09:00:01", "2026-08-09"),
                )
            finally:
                backend.cleanup()

    def test_first_seen_is_immutable_when_later_feed_rediscovers_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            try:
                self.assertTrue(backend.save_rss_data(rss_data(
                    "2026-08-08", "2026-08-08 09:00:00",
                    "First", "https://example.org/immutable?utm_source=a", "a",
                )))
                self.assertTrue(backend.save_rss_data(rss_data(
                    "2026-08-09", "2026-08-09 09:00:00",
                    "Later", "https://example.org/immutable?utm_source=b", "b",
                )))
                identity = ("url", "https://example.org/immutable")
                self.assertEqual(
                    backend.get_earliest_rss_discoveries_strict(
                        {identity}, "2026-08-09"
                    )[identity],
                    ("2026-08-08 09:00:00", "2026-08-08"),
                )
            finally:
                backend.cleanup()


class DailyDeliveryRemoteVersionTests(unittest.TestCase):
    @staticmethod
    def _news_db_bytes(tmp, executed_at):
        data_dir = Path(tmp) / executed_at.replace(":", "-")
        backend = LocalStorageBackend(
            data_dir=str(data_dir),
            enable_txt=False,
            enable_html=False,
            timezone="Asia/Shanghai",
        )
        now = shanghai(2026, 8, 9, *map(int, executed_at.split(":")[:2]))
        with patch.object(backend, "_get_configured_time", return_value=now):
            backend.record_period_execution(
                "2026-08-09", "daily_delivery", "push"
            )
        backend.cleanup()
        return (data_dir / "news" / "2026-08-09.db").read_bytes()

    @staticmethod
    def _rss_db_bytes(tmp):
        data_dir = Path(tmp) / "rss-source"
        backend = LocalStorageBackend(
            data_dir=str(data_dir),
            enable_txt=False,
            enable_html=False,
            timezone="Asia/Shanghai",
        )
        backend._save_rss_data_impl(rss_data(
            "2026-08-09", "2026-08-09 09:00:00",
            "Appeared", "https://example.org/appeared",
        ))
        backend.cleanup()
        return (data_dir / "rss" / "2026-08-09.db").read_bytes()

    @staticmethod
    def _ledger_db_bytes(tmp, identity, first_seen, storage_date):
        path = Path(tmp) / f"ledger-{storage_date}.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE ledger_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE rss_identity_first_seen (
                identity_key TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                storage_date TEXT NOT NULL,
                first_seen_at TEXT NOT NULL
            );
            CREATE INDEX idx_rss_identity_first_seen_at
                ON rss_identity_first_seen(first_seen_at);
        """)
        conn.executemany(
            "INSERT INTO ledger_metadata(key, value) VALUES (?, ?)",
            [("schema_version", "1"), ("backfill_complete", "1")],
        )
        conn.execute(
            """INSERT INTO rss_identity_first_seen
               (identity_key, first_seen, storage_date, first_seen_at)
               VALUES (?, ?, ?, ?)""",
            (
                json.dumps(identity, ensure_ascii=False, separators=(",", ":")),
                first_seen,
                storage_date,
                f"{storage_date}T{first_seen[-8:]}+08:00",
            ),
        )
        conn.commit()
        conn.close()
        return path.read_bytes()

    @staticmethod
    def _news_tag_db_bytes(tmp):
        data_dir = Path(tmp) / "tag-source"
        backend = LocalStorageBackend(
            data_dir=str(data_dir),
            enable_txt=False,
            enable_html=False,
            timezone="Asia/Shanghai",
        )
        backend.save_ai_filter_tags(
            [{"tag": "旧标签", "description": "旧", "priority": 1}],
            1,
            "old-hash",
            date="2026-08-09",
            interests_file="rice.txt",
        )
        backend.cleanup()
        return (data_dir / "news" / "2026-08-09.db").read_bytes()

    def test_checkpoint_strict_read_refreshes_changed_remote_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _VersionedS3()
            s3.set(
                "news/2026-08-09.db",
                self._news_db_bytes(tmp, "10:00"),
                "v1",
            )
            backend = remote_backend(tmp, s3)
            try:
                self.assertEqual(
                    backend.get_latest_period_execution(
                        "daily_delivery", "push", "2026-08-09"
                    ),
                    "2026-08-09 10:00:00",
                )
                s3.set(
                    "news/2026-08-09.db",
                    self._news_db_bytes(tmp, "10:30"),
                    "v2",
                )

                self.assertEqual(
                    backend.get_latest_period_execution(
                        "daily_delivery", "push", "2026-08-09"
                    ),
                    "2026-08-09 10:30:00",
                )
            finally:
                backend.cleanup()

    def test_remote_rss_save_does_not_replace_local_update_during_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _VersionedS3()
            s3.set("rss/2026-08-09.db", self._rss_db_bytes(tmp), "v1")
            backend = remote_backend(tmp, s3)
            try:
                self.assertTrue(backend.save_rss_data(rss_data(
                    "2026-08-09", "2026-08-09 09:30:00",
                    "New local item", "https://example.org/new-local",
                )))
                saved_path = Path(tmp) / "saved-remote-rss.db"
                saved_path.write_bytes(s3.objects["rss/2026-08-09.db"]["payload"])
                conn = sqlite3.connect(saved_path)
                try:
                    titles = {
                        row[0] for row in conn.execute(
                            "SELECT title FROM rss_items"
                        ).fetchall()
                    }
                finally:
                    conn.close()
                self.assertIn("Appeared", titles)
                self.assertIn("New local item", titles)
            finally:
                backend.cleanup()

    def test_remote_rss_save_rejects_concurrent_day_db_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _VersionedS3()
            original = self._rss_db_bytes(tmp)
            s3.set("rss/2026-08-09.db", original, "v1")
            s3.set(
                "rss/first-seen-v1.db",
                self._ledger_db_bytes(
                    tmp,
                    ("url", "https://example.org/appeared"),
                    "2026-08-09 09:00:00",
                    "2026-08-09",
                ),
                "ledger-v1",
            )
            backend = remote_backend(tmp, s3)
            original_put = backend._conditional_put_strict
            changed = False

            def concurrent_change(key, content, content_type):
                nonlocal changed
                if key == "rss/2026-08-09.db" and not changed:
                    changed = True
                    s3.set(key, original, "other-writer")
                return original_put(key, content, content_type)

            try:
                with patch.object(
                    backend,
                    "_conditional_put_strict",
                    side_effect=concurrent_change,
                ):
                    self.assertFalse(backend.save_rss_data(rss_data(
                        "2026-08-09", "2026-08-09 09:30:00",
                        "Concurrent", "https://example.org/concurrent",
                    )))
                self.assertEqual(
                    s3.objects["rss/2026-08-09.db"]["VersionId"],
                    "other-writer",
                )
            finally:
                backend.cleanup()


    def test_strict_rss_read_refreshes_cached_404_when_object_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _VersionedS3()
            backend = remote_backend(tmp, s3)
            try:
                self.assertEqual(
                    backend.get_all_rss_ids_strict("2026-08-09"), []
                )
                s3.set(
                    "rss/2026-08-09.db", self._rss_db_bytes(tmp), "v1"
                )

                rows = backend.get_all_rss_ids_strict("2026-08-09")

                self.assertEqual([row["title"] for row in rows], ["Appeared"])
            finally:
                backend.cleanup()

    def test_first_seen_ledger_rejects_concurrent_remote_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _VersionedS3()
            backend = remote_backend(tmp, s3)
            operation = getattr(
                backend, "_upload_first_seen_ledger_strict", None
            )
            self.assertTrue(callable(operation))
            backend._ensure_first_seen_ledger_strict("2026-08-09")
            s3.set("rss/first-seen-v1.db", b"new remote", "other-writer")

            with self.assertRaisesRegex(RuntimeError, "版本"):
                operation()

    def test_first_seen_strict_query_refreshes_changed_ledger_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _VersionedS3()
            identity = ("url", "https://example.org/versioned")
            s3.set(
                "rss/first-seen-v1.db",
                self._ledger_db_bytes(
                    tmp, identity, "2026-08-08 09:00:00", "2026-08-08"
                ),
                "v1",
            )
            backend = remote_backend(tmp, s3)
            try:
                first = backend.get_earliest_rss_discoveries_strict(
                    {identity}, "2026-08-09"
                )
                self.assertEqual(first[identity][1], "2026-08-08")

                s3.set(
                    "rss/first-seen-v1.db",
                    self._ledger_db_bytes(
                        tmp, identity, "2026-08-07 09:00:00", "2026-08-07"
                    ),
                    "v2",
                )
                refreshed = backend.get_earliest_rss_discoveries_strict(
                    {identity}, "2026-08-09"
                )
                self.assertEqual(refreshed[identity][1], "2026-08-07")
            finally:
                backend.cleanup()

    def test_strict_tag_batch_upload_reads_back_new_remote_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _VersionedS3()
            s3.set(
                "news/2026-08-09.db", self._news_tag_db_bytes(tmp), "v1"
            )
            backend = remote_backend(tmp, s3)
            try:
                backend.begin_batch()
                old = backend.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                self.assertEqual(old["prompt_hash"], "old-hash")
                backend.replace_ai_filter_tags_strict(
                    [{"tag": "新标签", "description": "新", "priority": 1}],
                    2,
                    "new-hash",
                    "2026-08-09",
                    "rice.txt",
                )
                backend.end_batch_strict()

                refreshed = backend.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                self.assertEqual(refreshed["prompt_hash"], "new-hash")
                self.assertNotEqual(
                    s3.objects["news/2026-08-09.db"]["VersionId"], "v1"
                )
            finally:
                backend.cleanup()

    def test_strict_tag_batch_rejects_upload_without_version_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _NoVersionAdvanceS3()
            s3.set(
                "news/2026-08-09.db", self._news_tag_db_bytes(tmp), "v1"
            )
            backend = remote_backend(tmp, s3)
            try:
                backend.begin_batch()
                backend.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                backend.replace_ai_filter_tags_strict(
                    [{"tag": "新标签", "description": "新", "priority": 1}],
                    2,
                    "new-hash",
                    "2026-08-09",
                    "rice.txt",
                )
                with self.assertRaisesRegex(RuntimeError, "版本"):
                    backend.end_batch_strict()
            finally:
                backend.cleanup()


class DailyDeliveryStrictClassificationProtocolTests(unittest.TestCase):
    def setUp(self):
        self.ai_filter = AIFilter.__new__(AIFilter)
        self.ai_filter.debug = False
        self.ai_filter.classify_system = "只返回 JSON"
        self.ai_filter.classify_user = (
            "{interests_content}\n{tags_list}\n{news_count}\n{news_list}"
        )
        self.ai_filter.summary_grounding_review_enabled = False
        self.ai_filter.client = MagicMock()
        self.titles = [
            {
                "id": 1,
                "title": "第一条水稻新闻",
                "content": "第一条水稻新闻正文",
                "content_level": "full_text",
                "module_type": "current_events",
                "module_reason": "no_publication_evidence",
            },
            {
                "id": 2,
                "title": "第二条水稻新闻",
                "content": "第二条水稻新闻正文",
                "content_level": "full_text",
                "module_type": "current_events",
                "module_reason": "no_publication_evidence",
            },
        ]
        self.tags = [
            {"id": 18, "tag": "水稻育种", "description": "育种进展"}
        ]
        self.valid = json.dumps({"items": [
            {
                "id": 1, "include": True, "species_scope": "rice",
                "tag_id": 1,
                "score": 0.82, "importance_score": 0.78,
                "summary": "第一条新闻的证据摘要",
            },
            {
                "id": 2, "include": False,
                "species_scope": "not_applicable", "score": 0.1,
                "importance_score": 0.1, "summary": "第二条新闻无关",
            },
        ]})

    def test_each_strict_protocol_violation_triggers_one_successful_repair(self):
        invalid_responses = {
            "unknown_news_id": [{
                "id": 999, "include": True, "species_scope": "rice",
                "tag_id": 18, "score": 0.8,
                "importance_score": 0.7, "summary": "未知新闻",
            }],
            "unknown_tag_id": [{
                "id": 1, "include": True, "species_scope": "rice",
                "tag_id": 999, "score": 0.8,
                "importance_score": 0.7, "summary": "未知标签",
            }],
            "missing_required_field": [{
                "id": 1, "include": True, "species_scope": "rice",
                "tag_id": 18, "score": 0.8,
                "summary": "缺少重要性",
            }],
            "illegal_element": ["not-an-object"],
            "duplicate_news_id": [
                {
                    "id": 1, "include": True, "species_scope": "rice",
                    "tag_id": 18, "score": 0.8,
                    "importance_score": 0.7, "summary": "第一份",
                },
                {
                    "id": 1, "include": True, "species_scope": "rice",
                    "tag_id": 18, "score": 0.7,
                    "importance_score": 0.6, "summary": "重复项",
                },
            ],
            "empty_summary": [{
                "id": 1, "include": True, "species_scope": "rice",
                "tag_id": 18, "score": 0.8,
                "importance_score": 0.7, "summary": "",
            }],
        }
        for name, invalid in invalid_responses.items():
            with self.subTest(name=name):
                self.ai_filter.client.chat.reset_mock()
                self.ai_filter.client.chat.side_effect = [
                    json.dumps({"items": invalid}, ensure_ascii=False),
                    self.valid,
                ]
                result = self.ai_filter.classify_batch(
                    self.titles, self.tags, "育种", strict=True
                )
                self.assertEqual([item["news_item_id"] for item in result], [1])
                self.assertEqual(self.ai_filter.client.chat.call_count, 2)

    def test_strict_protocol_violation_after_repair_fails_whole_batch(self):
        invalid = json.dumps({"items": [{
            "id": 999, "include": True, "species_scope": "rice",
            "tag_id": 18, "score": 0.8,
            "importance_score": 0.7, "summary": "未知新闻",
        }]})
        self.ai_filter.client.chat.side_effect = [invalid, invalid]

        result = self.ai_filter.classify_batch(
            self.titles, self.tags, "育种", strict=True
        )

        self.assertIsNone(result)
        self.assertEqual(self.ai_filter.client.chat.call_count, 2)


class DailyDeliveryStrictGroundingFinalTests(unittest.TestCase):
    def test_initial_narrative_then_empty_grounding_is_strict_failure(self):
        analyzer = AIAnalyzer.__new__(AIAnalyzer)
        analyzer.ai_config = {
            "MODEL": "test", "TIMEOUT": 1, "MAX_TOKENS": 100
        }
        analyzer.analysis_config = {}
        analyzer.get_time_func = lambda: shanghai(2026, 8, 9, 10, 0)
        analyzer.debug = False
        analyzer.client = MagicMock(api_key="secret")
        analyzer.client.chat.side_effect = [
            json.dumps({"rss_insights": "有证据支持的初稿"}),
            "{}",
        ]
        analyzer.max_news = 50
        analyzer.include_rss = True
        analyzer.include_rank_timeline = False
        analyzer.include_standalone = False
        analyzer.grounding_review_enabled = True
        analyzer.language = "Chinese"
        analyzer.system_prompt = "system"
        analyzer.user_prompt_template = (
            "{report_mode}{report_type}{current_time}{news_count}{rss_count}"
            "{platforms}{keywords}{news_content}{rss_content}{language}"
            "{standalone_content}"
        )

        result = analyzer.analyze(
            [],
            [{
                "word": "育种",
                "count": 1,
                "titles": [{
                    "title": "Rice breeding",
                    "source_name": "Journal",
                    "time_display": "2026-08-09 09:30",
                }],
            }],
            report_mode="daily_delivery",
            strict=True,
        )

        self.assertFalse(result.success)
        self.assertIn("摘要", result.error)


class DailyDeliveryStrictTagLifecycleTests(unittest.TestCase):
    @staticmethod
    def _backend(tmp):
        return LocalStorageBackend(
            data_dir=tmp,
            enable_txt=False,
            enable_html=False,
            timezone="Asia/Shanghai",
        )

    def test_strict_tag_replace_is_atomic_and_removes_old_active_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            try:
                backend.save_ai_filter_tags(
                    [{"tag": "旧标签", "description": "旧描述", "priority": 1}],
                    1,
                    "old-hash",
                    interests_file="rice.txt",
                )

                snapshot = backend.replace_ai_filter_tags_strict(
                    [
                        {"tag": "新标签一", "description": "新描述一", "priority": 1},
                        {"tag": "新标签二", "description": "新描述二", "priority": 2},
                    ],
                    2,
                    "new-hash",
                    interests_file="rice.txt",
                )

                self.assertEqual(snapshot["prompt_hash"], "new-hash")
                self.assertEqual(snapshot["version"], 2)
                self.assertEqual(
                    [tag["tag"] for tag in snapshot["tags"]],
                    ["新标签一", "新标签二"],
                )
                self.assertEqual(
                    [tag["priority"] for tag in snapshot["tags"]], [1, 2]
                )
                active = backend.get_ai_filter_tag_snapshot_strict(
                    interests_file="rice.txt"
                )
                self.assertEqual(active, snapshot)
            finally:
                backend.cleanup()

    def test_strict_tag_replace_rolls_back_on_midway_sqlite_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            try:
                backend.save_ai_filter_tags(
                    [{"tag": "旧标签", "description": "旧描述", "priority": 1}],
                    1,
                    "old-hash",
                    interests_file="rice.txt",
                )
                conn = backend._get_connection()
                conn.execute("""
                    CREATE TRIGGER reject_bad_tag
                    BEFORE INSERT ON ai_filter_tags
                    WHEN NEW.tag = '坏标签'
                    BEGIN
                        SELECT RAISE(ABORT, 'injected tag failure');
                    END
                """)
                conn.commit()

                with self.assertRaisesRegex(Exception, "injected tag failure"):
                    backend.replace_ai_filter_tags_strict(
                        [
                            {"tag": "新标签", "description": "新", "priority": 1},
                            {"tag": "坏标签", "description": "坏", "priority": 2},
                        ],
                        2,
                        "new-hash",
                        interests_file="rice.txt",
                    )

                snapshot = backend.get_ai_filter_tag_snapshot_strict(
                    interests_file="rice.txt"
                )
                self.assertEqual(snapshot["prompt_hash"], "old-hash")
                self.assertEqual(
                    [tag["tag"] for tag in snapshot["tags"]], ["旧标签"]
                )
            finally:
                backend.cleanup()

    @staticmethod
    def _pipeline(storage):
        return AIFilterPipeline(
            config={
                "TIMEZONE": "Asia/Shanghai",
                "RSS": {"ENABLED": False, "FRESHNESS_FILTER": {}},
                "AI": {},
                "AI_FILTER": {"INTERESTS_FILE": "rice.txt"},
                "FILTER": {},
            },
            storage_manager=storage,
            get_time_func=lambda: shanghai(2026, 8, 9, 10, 0),
            strict=True,
        )

    @staticmethod
    def _run_pipeline(storage, replace_snapshot):
        storage.backend_name = "fake"
        storage.get_ai_filter_tag_snapshot_strict.return_value = {
            "tags": [{
                "id": 1, "tag": "旧标签", "description": "旧",
                "priority": 1, "version": 1, "prompt_hash": "old-hash",
            }],
            "prompt_hash": "old-hash",
            "version": 1,
            "latest_version": 1,
        }
        storage.replace_ai_filter_tags_strict.return_value = replace_snapshot
        storage.replace_ai_filter_batch_strict.return_value = {
            "results": 0, "analyzed": 0
        }
        storage.get_active_ai_filter_results_strict.return_value = []
        pipeline = DailyDeliveryStrictTagLifecycleTests._pipeline(storage)
        pipeline._collect_pending_news = MagicMock(
            return_value=([], [], [], set(), [], set(), 0)
        )
        with patch("trendradar.ai.filter_pipeline.AIFilter") as filter_class:
            ai_filter = filter_class.return_value
            ai_filter.load_interests_content.return_value = "new interests"
            ai_filter.compute_interests_hash.return_value = "new-hash"
            ai_filter.extract_tags.return_value = [{
                "tag": "新标签", "description": "新"
            }]
            result = pipeline.run()
        return result

    def test_strict_tag_snapshot_read_failure_is_pipeline_failure(self):
        storage = MagicMock()
        storage.backend_name = "fake"
        storage.get_ai_filter_tag_snapshot_strict.side_effect = RuntimeError(
            "tag read broken"
        )
        pipeline = self._pipeline(storage)
        with patch("trendradar.ai.filter_pipeline.AIFilter") as filter_class:
            ai_filter = filter_class.return_value
            ai_filter.load_interests_content.return_value = "new interests"
            ai_filter.compute_interests_hash.return_value = "new-hash"
            result = pipeline.run()
        self.assertFalse(result.success)
        self.assertIn("标签", result.error)
        storage.get_latest_prompt_hash.assert_not_called()

    def test_strict_tag_save_zero_is_pipeline_failure(self):
        storage = MagicMock()
        result = self._run_pipeline(storage, {
            "tags": [], "prompt_hash": "new-hash", "version": 2,
            "latest_version": 2,
        })
        self.assertFalse(result.success)
        self.assertIn("标签", result.error)

    def test_strict_tag_readback_with_old_residual_is_pipeline_failure(self):
        storage = MagicMock()
        result = self._run_pipeline(storage, {
            "tags": [
                {
                    "id": 1, "tag": "旧标签", "description": "旧",
                    "priority": 1, "version": 1,
                    "prompt_hash": "old-hash",
                },
                {
                    "id": 2, "tag": "新标签", "description": "新",
                    "priority": 2, "version": 2,
                    "prompt_hash": "new-hash",
                },
            ],
            "prompt_hash": "new-hash", "version": 2,
            "latest_version": 2,
        })
        self.assertFalse(result.success)
        self.assertIn("标签", result.error)


class DailyDeliveryThirdPartyStrictCapabilityTests(unittest.TestCase):
    def test_fail_soft_backend_remains_usable_but_daily_delivery_fails_closed(self):
        data = rss_data(
            "2026-08-09",
            "2026-08-09 09:30:00",
            "Rice",
            "https://example.org/rice",
        )
        backend = _FailSoftThirdPartyStorage(data)

        self.assertIs(backend.get_rss_data("2026-08-09"), data)
        self.assertEqual(
            backend.get_rss_feed_statuses("2026-08-09"),
            {"journal": "success"},
        )
        self.assertEqual(len(backend.get_all_rss_ids("2026-08-09")), 1)
        with self.assertRaises(NotImplementedError):
            backend.get_rss_data_strict("2026-08-09")
        with self.assertRaises(NotImplementedError):
            backend.get_rss_feed_statuses_strict("2026-08-09")
        with self.assertRaises(NotImplementedError):
            backend.get_all_rss_ids_strict("2026-08-09")

        aggregator = DailyDeliveryAggregator(backend, "Asia/Shanghai")
        with self.assertRaises(NotImplementedError):
            aggregator.build(shanghai(2026, 8, 9, 10, 0), None)

if __name__ == "__main__":
    unittest.main()
