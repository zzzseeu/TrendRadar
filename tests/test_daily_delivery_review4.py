import math
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from trendradar.ai.filter import AIFilter
from trendradar.core.scheduler import Scheduler
from trendradar.crawler.news_search import normalize_title
from trendradar.storage.base import RSSData, RSSItem
from trendradar.storage.local import LocalStorageBackend
from trendradar.storage.remote import RemoteStorageBackend


def rss_data(date, crawl_time, *items):
    grouped = {}
    names = {}
    for item in items:
        grouped.setdefault(item.feed_id, []).append(item)
        names[item.feed_id] = item.feed_name or item.feed_id
    return RSSData(
        date=date,
        crawl_time=crawl_time,
        items=grouped,
        id_to_name=names,
        failed_ids=[],
    )


def local_backend(tmp):
    return LocalStorageBackend(
        data_dir=tmp,
        enable_txt=False,
        enable_html=False,
        timezone="Asia/Shanghai",
    )


class _Body:
    def __init__(self, payload):
        self.payload = payload

    def iter_chunks(self, chunk_size=1024 * 1024):
        del chunk_size
        yield self.payload


class _ConditionalS3:
    def __init__(self):
        self.objects = {}
        self.put_calls = []
        self.before_condition = None
        self.after_put = None
        self.fail_keys_once = set()
        self.sequence = 0

    def set(self, key, payload, version):
        self.objects[key] = {
            "payload": payload,
            "ETag": f'"{version}"',
            "VersionId": version,
            "LastModified": datetime(2026, 8, 9, 2, 0, self.sequence),
            "Size": len(payload),
        }

    def head_object(self, Bucket, Key):
        del Bucket
        obj = self.objects.get(Key)
        if obj is None:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "missing"}},
                "HeadObject",
            )
        return {
            key: obj[key]
            for key in ("ETag", "VersionId", "LastModified")
        }

    def get_object(self, Bucket, Key):
        del Bucket
        return {"Body": _Body(self.objects[Key]["payload"])}

    def put_object(self, Bucket, Key, Body, **kwargs):
        del Bucket
        self.put_calls.append((Key, dict(kwargs)))
        if self.before_condition is not None:
            hook, self.before_condition = self.before_condition, None
            hook(Key)
        current = self.objects.get(Key)
        if_match = kwargs.get("IfMatch")
        if_none_match = kwargs.get("IfNoneMatch")
        if (
            if_match is not None
            and (current is None or current["ETag"] != if_match)
        ) or (if_none_match == "*" and current is not None):
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
            )
        if Key in self.fail_keys_once:
            self.fail_keys_once.remove(Key)
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable"}}, "PutObject"
            )
        self.sequence += 1
        version = f"put-{self.sequence}"
        self.set(Key, Body, version)
        response = {
            "ETag": self.objects[Key]["ETag"],
            "VersionId": version,
        }
        if self.after_put is not None:
            hook, self.after_put = self.after_put, None
            hook(Key)
        return response

    def get_paginator(self, name):
        if name != "list_objects_v2":
            raise AssertionError(name)

        class _Paginator:
            def __init__(self, outer):
                self.outer = outer

            def paginate(self, Bucket, Prefix):
                del Bucket
                return [{"Contents": [
                    {
                        "Key": key,
                        "ETag": value["ETag"],
                        "LastModified": value["LastModified"],
                        "Size": value["Size"],
                    }
                    for key, value in sorted(self.outer.objects.items())
                    if key.startswith(Prefix)
                ]}]

        return _Paginator(self)


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
    backend._strict_local_authoritative = set()
    backend._first_seen_needs_upload = False
    return backend


def news_db_bytes(tmp, name, *, prompt_hash="old-hash"):
    data_dir = Path(tmp) / name
    backend = local_backend(data_dir)
    backend.replace_ai_filter_tags_strict(
        [{"tag": "旧标签", "description": "旧", "priority": 1}],
        1,
        prompt_hash,
        "2026-08-09",
        "rice.txt",
    )
    backend.cleanup()
    return (data_dir / "news" / "2026-08-09.db").read_bytes()


class RSSFirstSeenOutboxTests(unittest.TestCase):
    def test_title_only_item_and_matching_outbox_are_persisted_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = local_backend(tmp)
            title = "A title-only rice breeding result"
            data = rss_data(
                "2026-08-09",
                "2026-08-09 09:00:01",
                RSSItem(title=title, feed_id="journal"),
                RSSItem(title="A second title-only result", feed_id="journal"),
            )
            try:
                self.assertTrue(backend.save_rss_data(data))
                stored = backend.get_rss_data("2026-08-09")
                self.assertEqual(stored.get_total_count(), 2)
                item = next(
                    item for item in stored.items["journal"]
                    if item.title == title
                )
                self.assertEqual(item.title, title)
                self.assertTrue(item.guid.startswith("rss-title:"))

                conn = backend._get_rss_connection("2026-08-09")
                outbox = conn.execute(
                    """SELECT identity_key, first_seen, storage_date
                       FROM rss_first_seen_outbox"""
                ).fetchall()
                self.assertEqual(len(outbox), 2)
                self.assertTrue(any(
                    normalize_title(title) in row[0] for row in outbox
                ))

                identity = ("title", "journal", normalize_title(title))
                earliest = backend.get_earliest_rss_discoveries_strict(
                    {identity}, "2026-08-09"
                )
                self.assertEqual(
                    earliest[identity],
                    ("2026-08-09 09:00:01", "2026-08-09"),
                )
            finally:
                backend.cleanup()


class RemoteOutboxRecoveryTests(unittest.TestCase):
    def test_new_backend_recovers_remote_raw_outbox_after_ledger_upload_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            s3.fail_keys_once.add("rss/first-seen-v1.db")
            first = remote_backend(Path(tmp) / "first", s3)
            data = rss_data(
                "2026-08-09",
                "2026-08-09 09:00:01",
                RSSItem(
                    title="Remote durable", feed_id="journal",
                    url="https://example.org/remote-durable",
                ),
            )
            try:
                self.assertFalse(first.save_rss_data(data))
                self.assertIn("rss/2026-08-09.db", s3.objects)
                self.assertNotIn("rss/first-seen-v1.db", s3.objects)
            finally:
                first.cleanup()

            second = remote_backend(Path(tmp) / "second", s3)
            try:
                identity = ("url", "https://example.org/remote-durable")
                earliest = second.get_earliest_rss_discoveries_strict(
                    {identity}, "2026-08-09"
                )
                self.assertEqual(
                    earliest[identity],
                    ("2026-08-09 09:00:01", "2026-08-09"),
                )
                self.assertIn("rss/first-seen-v1.db", s3.objects)
                raw_calls = [
                    kwargs for key, kwargs in s3.put_calls
                    if key == "rss/2026-08-09.db"
                ]
                ledger_calls = [
                    kwargs for key, kwargs in s3.put_calls
                    if key == "rss/first-seen-v1.db"
                ]
                self.assertEqual(raw_calls[-1]["IfNoneMatch"], "*")
                self.assertEqual(ledger_calls[-1]["IfNoneMatch"], "*")
            finally:
                second.cleanup()


class RemoteConditionalCASTests(unittest.TestCase):
    def test_existing_and_create_uploads_send_real_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            s3.set("existing.db", b"old", "v1")
            backend = remote_backend(tmp, s3)
            backend._remote_provenance["existing.db"] = (
                "v1", '"v1"', "2026-08-09T02:00:00"
            )
            backend._remote_provenance["new.db"] = None
            backend._strict_local_authoritative.update(
                {"existing.db", "new.db"}
            )
            backend._conditional_put_strict(
                "existing.db", b"changed", "application/x-sqlite3"
            )
            backend._conditional_put_strict(
                "new.db", b"created", "application/x-sqlite3"
            )
            calls = {key: kwargs for key, kwargs in s3.put_calls}
            self.assertEqual(calls["existing.db"]["IfMatch"], '"v1"')
            self.assertEqual(calls["new.db"]["IfNoneMatch"], "*")

    def test_competition_before_put_after_put_and_on_create_fails_closed(self):
        scenarios = ("before_put", "after_put", "create")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                s3 = _ConditionalS3()
                key = f"{scenario}.db"
                backend = remote_backend(tmp, s3)
                if scenario == "create":
                    backend._remote_provenance[key] = None
                    s3.before_condition = lambda changed_key: s3.set(
                        changed_key, b"other", "other-writer"
                    )
                else:
                    s3.set(key, b"old", "v1")
                    backend._remote_provenance[key] = (
                        "v1", '"v1"', "2026-08-09T02:00:00"
                    )
                    hook = lambda changed_key: s3.set(
                        changed_key, b"other", "other-writer"
                    )
                    if scenario == "before_put":
                        s3.before_condition = hook
                    else:
                        s3.after_put = hook
                backend._strict_local_authoritative.add(key)
                with self.assertRaises(Exception):
                    backend._conditional_put_strict(
                        key, b"mine", "application/x-sqlite3"
                    )
                self.assertIn(key, backend._strict_local_authoritative)


class RSSFirstSeenOutboxAdditionalTests(unittest.TestCase):
    def test_one_item_sqlite_failure_rolls_back_items_crawl_and_outbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = local_backend(tmp)
            conn = backend._get_rss_connection("2026-08-09")
            conn.execute("""
                CREATE TRIGGER reject_bad_rss_item
                BEFORE INSERT ON rss_items
                WHEN NEW.title = 'Bad item'
                BEGIN
                    SELECT RAISE(ABORT, 'reject bad item');
                END
            """)
            conn.commit()
            data = rss_data(
                "2026-08-09",
                "2026-08-09 09:00:01",
                RSSItem(
                    title="Good item", feed_id="journal",
                    url="https://example.org/good",
                ),
                RSSItem(
                    title="Bad item", feed_id="journal",
                    url="https://example.org/bad",
                ),
            )
            try:
                self.assertFalse(backend.save_rss_data(data))
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM rss_items").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM rss_crawl_records"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM rss_first_seen_outbox"
                    ).fetchone()[0],
                    0,
                )
            finally:
                backend.cleanup()

    def test_new_backend_recovers_committed_outbox_without_old_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = local_backend(tmp)
            data = rss_data(
                "2026-08-09",
                "2026-08-09 09:00:01",
                RSSItem(
                    title="Durable before crash", feed_id="journal",
                    url="https://example.org/durable",
                ),
            )
            original_consume = getattr(
                first, "_consume_first_seen_outboxes_strict", None
            )
            self.assertTrue(callable(original_consume))
            calls = 0

            def fail_after_raw(through_date):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("ledger unavailable after raw commit")
                return original_consume(through_date)

            try:
                with patch.object(
                    first,
                    "_consume_first_seen_outboxes_strict",
                    side_effect=fail_after_raw,
                ):
                    self.assertFalse(first.save_rss_data(data))
            finally:
                first.cleanup()

            second = local_backend(tmp)
            try:
                empty_next_run = rss_data(
                    "2026-08-10", "2026-08-10 09:00:01"
                )
                self.assertTrue(second.save_rss_data(empty_next_run))
                identity = ("url", "https://example.org/durable")
                earliest = second.get_earliest_rss_discoveries_strict(
                    {identity}, "2026-08-10"
                )
                self.assertEqual(
                    earliest[identity],
                    ("2026-08-09 09:00:01", "2026-08-09"),
                )
            finally:
                second.cleanup()

    def test_stable_source_versions_skip_history_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = local_backend(tmp)
            try:
                backend._save_rss_data_impl(rss_data(
                    "2026-08-08",
                    "2026-08-08 09:00:01",
                    RSSItem(
                        title="Stable", feed_id="journal",
                        url="https://example.org/stable",
                    ),
                ))
                identity = ("url", "https://example.org/stable")
                backend.get_earliest_rss_discoveries_strict(
                    {identity}, "2026-08-09"
                )
                with patch.object(
                    backend,
                    "_get_rss_connection",
                    wraps=backend._get_rss_connection,
                ) as history_open:
                    backend.get_earliest_rss_discoveries_strict(
                        {identity}, "2026-08-09"
                    )
                    history_open.assert_not_called()
            finally:
                backend.cleanup()


class RemoteDirtyAndStrictPeriodTests(unittest.TestCase):
    def test_dirty_tag_snapshot_refuses_remote_refresh_over_local_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(key, news_db_bytes(tmp, "old"), "v1")
            backend = remote_backend(Path(tmp) / "cache", s3)
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
                self.assertIn(key, backend._strict_local_authoritative)
                local_snapshot = backend.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                self.assertEqual(local_snapshot["prompt_hash"], "new-hash")
                self.assertIn(key, backend._strict_local_authoritative)
                s3.set(
                    key,
                    news_db_bytes(tmp, "other", prompt_hash="other"),
                    "v2",
                )
                with self.assertRaisesRegex(RuntimeError, "本地修改"):
                    backend.get_ai_filter_tag_snapshot_strict(
                        "2026-08-09", "rice.txt"
                    )
            finally:
                backend.cleanup()

    def test_strict_period_cas_conflict_does_not_remain_locally_successful(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3 = _ConditionalS3()
            key = "news/2026-08-09.db"
            s3.set(key, news_db_bytes(tmp, "baseline"), "v1")
            backend = remote_backend(Path(tmp) / "cache", s3)
            try:
                backend.get_ai_filter_tag_snapshot_strict(
                    "2026-08-09", "rice.txt"
                )
                s3.before_condition = lambda changed_key: s3.set(
                    changed_key,
                    news_db_bytes(tmp, "winner", prompt_hash="winner"),
                    "other-writer",
                )
                self.assertFalse(backend.record_period_execution_strict(
                    "2026-08-09", "daily_delivery", "push"
                ))
                self.assertFalse(backend.has_period_executed(
                    "2026-08-09", "daily_delivery", "push"
                ))
            finally:
                backend.cleanup()

    def test_scheduler_routes_strict_period_record_to_explicit_api(self):
        scheduler = Scheduler.__new__(Scheduler)
        scheduler.storage = MagicMock()
        scheduler.timeline = {
            "default": {"report_mode": "current"},
            "periods": {
                "daily_delivery": {"report_mode": "daily_delivery"}
            },
        }
        scheduler.fallback_report_mode = "current"
        scheduler.storage.record_period_execution_strict.return_value = True
        self.assertTrue(scheduler.record_execution(
            "daily_delivery", "push", "2026-08-09"
        ))
        scheduler.storage.record_period_execution_strict.assert_called_once_with(
            "2026-08-09", "daily_delivery", "push"
        )


class StrictClassificationScalarTypeTests(unittest.TestCase):
    def setUp(self):
        self.filter = AIFilter.__new__(AIFilter)
        self.filter.debug = False
        self.filter.classify_system = "只返回 JSON"
        self.filter.classify_user = (
            "{interests_content}\n{tags_list}\n{news_count}\n{news_list}"
        )
        self.filter.summary_grounding_review_enabled = False
        self.filter.client = MagicMock()
        self.titles = [{
            "id": 1,
            "title": "Rice",
            "content": "Rice evidence",
            "content_level": "full_text",
        }]
        self.tags = [{"id": 9, "tag": "rice"}]

    def test_strict_scalar_types_reject_json_coercions_and_nonfinite_numbers(self):
        valid = {
            "id": 1,
            "module_type": "research",
            "tag_id": 9,
            "score": 0.8,
            "importance_score": 0.7,
            "summary": "evidence",
        }
        invalid_values = {
            "bool_news_id": ("id", True),
            "string_news_id": ("id", "1"),
            "bool_tag_id": ("tag_id", True),
            "string_tag_id": ("tag_id", "9"),
            "string_score": ("score", "0.8"),
            "bool_score": ("score", True),
            "nan_score": ("score", math.nan),
            "infinite_score": ("score", math.inf),
            "null_summary": ("summary", None),
            "object_summary": ("summary", {"text": "evidence"}),
            "list_summary": ("summary", ["evidence"]),
            "bool_summary": ("summary", True),
        }
        for name, (field, value) in invalid_values.items():
            with self.subTest(name=name):
                payload = dict(valid)
                payload[field] = value
                with self.assertRaises(Exception):
                    self.filter._parse_classify_response(
                        json.dumps([payload]),
                        self.titles,
                        self.tags,
                        strict=True,
                    )

    def test_invalid_scalar_triggers_one_repair_and_accepts_valid_repair(self):
        invalid = json.dumps([{
            "id": 1,
            "module_type": "research",
            "tag_id": 9,
            "score": "0.8",
            "importance_score": 0.7,
            "summary": "evidence",
        }])
        valid = json.dumps([{
            "id": 1,
            "module_type": "research",
            "tag_id": 9,
            "score": 0.8,
            "importance_score": 0.7,
            "summary": "evidence",
        }])
        self.filter.client.chat.side_effect = [invalid, valid]
        result = self.filter.classify_batch(
            self.titles, self.tags, strict=True
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(self.filter.client.chat.call_count, 2)

    def test_invalid_scalar_after_repair_rejects_entire_batch(self):
        invalid = json.dumps([{
            "id": 1,
            "module_type": "research",
            "tag_id": 9,
            "score": True,
            "importance_score": 0.7,
            "summary": "evidence",
        }])
        self.filter.client.chat.side_effect = [invalid, invalid]
        self.assertIsNone(self.filter.classify_batch(
            self.titles, self.tags, strict=True
        ))
        self.assertEqual(self.filter.client.chat.call_count, 2)
