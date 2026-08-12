import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from trendradar.ai.filter import AIFilter
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.storage.local import LocalStorageBackend

from tests.test_daily_delivery_review4 import (
    _ConditionalS3,
    remote_backend,
)


DATE = "2026-08-09"
INTERESTS_FILE = "rice.txt"
PROMPT_HASH = "rice.txt:current"


def local_backend(data_dir):
    return LocalStorageBackend(
        data_dir=data_dir,
        enable_txt=False,
        enable_html=False,
        timezone="Asia/Shanghai",
    )


def seed_hotlist_rows(conn, ids=(1, 2, 3, 4)):
    conn.execute(
        "INSERT OR IGNORE INTO platforms (id, name) VALUES (?, ?)",
        ("journal", "Journal"),
    )
    for news_id in ids:
        conn.execute(
            """INSERT OR IGNORE INTO news_items
               (id, title, platform_id, rank, url,
                first_crawl_time, last_crawl_time)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                news_id,
                f"Rice item {news_id}",
                "journal",
                news_id,
                f"https://example.org/{news_id}",
                "2026-08-09 09:00:00",
                "2026-08-09 09:00:00",
            ),
        )
    conn.commit()


def seed_tag(backend, prompt_hash=PROMPT_HASH):
    backend.replace_ai_filter_tags_strict(
        [{"tag": "rice", "description": "rice", "priority": 1}],
        1,
        prompt_hash,
        DATE,
        INTERESTS_FILE,
    )
    return backend.get_ai_filter_tag_snapshot_strict(
        DATE, INTERESTS_FILE
    )["tags"][0]["id"]


def result(news_item_id, tag_id, module_type, species_scope="rice"):
    return {
        "news_item_id": news_item_id,
        "source_type": "hotlist",
        "tag_id": tag_id,
        "module_type": module_type,
        "species_scope": species_scope,
        "relevance_score": 0.9,
        "importance_score": 0.8,
        "ai_summary": f"summary {news_item_id}",
    }


def create_legacy_news_db(data_dir):
    db_path = Path(data_dir) / "news" / f"{DATE}.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE ai_filter_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_item_id INTEGER NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'hotlist',
            tag_id INTEGER NOT NULL,
            relevance_score REAL DEFAULT 0,
            content_level TEXT DEFAULT 'title_only',
            risk_warning TEXT DEFAULT '',
            content_excerpt TEXT DEFAULT '',
            importance_score REAL DEFAULT 0,
            ai_summary TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            deprecated_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(news_item_id, source_type, tag_id)
        );
        CREATE TABLE ai_filter_analyzed_news (
            news_item_id INTEGER NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'hotlist',
            interests_file TEXT NOT NULL DEFAULT 'ai_interests.txt',
            prompt_hash TEXT NOT NULL,
            matched INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (news_item_id, source_type, interests_file)
        );
        INSERT INTO ai_filter_results
            (news_item_id, source_type, tag_id, created_at)
        VALUES (1, 'hotlist', 9, '2026-08-09 09:00:00');
        INSERT INTO ai_filter_analyzed_news
            (news_item_id, source_type, interests_file, prompt_hash,
             matched, created_at)
        VALUES (1, 'hotlist', 'rice.txt', 'legacy', 1,
                '2026-08-09 09:00:00');
    """)
    conn.close()
    return db_path


def create_four_module_news_db(data_dir):
    """创建仍使用 policy/industry/research 的旧模块数据库。"""
    db_path = create_legacy_news_db(data_dir)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "ALTER TABLE ai_filter_results ADD COLUMN module_type TEXT "
        "CHECK(module_type IN ('policy', 'industry', 'research'))"
    )
    conn.execute(
        "ALTER TABLE ai_filter_results ADD COLUMN species_scope TEXT "
        "CHECK(species_scope IN ('rice', 'other_crop', 'not_applicable'))"
    )
    conn.commit()
    conn.close()
    return db_path


def remote_news_db_bytes(tmp, name):
    data_dir = Path(tmp) / name
    backend = local_backend(data_dir)
    try:
        conn = backend._get_connection(DATE)
        seed_hotlist_rows(conn)
        seed_tag(backend)
    finally:
        backend.cleanup()
    return (data_dir / "news" / f"{DATE}.db").read_bytes()


class AIFilterModuleMigrationTests(unittest.TestCase):
    def test_migration_rebuilds_old_module_constraint_without_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            create_four_module_news_db(tmp)
            backend = local_backend(tmp)
            try:
                conn = backend._get_connection(DATE)
                table_sql = conn.execute(
                    """SELECT sql FROM sqlite_master
                       WHERE type = 'table' AND name = 'ai_filter_results'"""
                ).fetchone()[0]
                self.assertIn(
                    "module_type IN ('current_events', 'research')",
                    table_sql,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM ai_filter_results"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM ai_filter_analyzed_news"
                    ).fetchone()[0],
                    0,
                )
                conn.execute(
                    """INSERT INTO ai_filter_results
                       (news_item_id, source_type, tag_id, module_type,
                        species_scope, created_at)
                       VALUES (2, 'rss', 1, 'current_events', 'rice',
                               '2026-08-09 10:00:00')"""
                )
                conn.commit()
            finally:
                backend.cleanup()

    def test_legacy_migration_adds_nullable_checked_column_and_clears_both_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            create_legacy_news_db(tmp)
            backend = local_backend(tmp)
            try:
                conn = backend._get_connection(DATE)
                columns = {
                    row[1]: row for row in conn.execute(
                        "PRAGMA table_info(ai_filter_results)"
                    )
                }
                self.assertIn("module_type", columns)
                self.assertEqual(columns["module_type"][3], 0)
                self.assertIsNone(columns["module_type"][4])
                table_sql = conn.execute(
                    """SELECT sql FROM sqlite_master
                       WHERE type = 'table' AND name = 'ai_filter_results'"""
                ).fetchone()[0]
                self.assertIn(
                    "module_type IN ('current_events', 'research')",
                    table_sql,
                )
                self.assertIn("species_scope", columns)
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM ai_filter_results"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM ai_filter_analyzed_news"
                    ).fetchone()[0],
                    0,
                )

                backend._migrate_ai_filter_schema(conn)
                self.assertEqual(
                    [row[1] for row in conn.execute(
                        "PRAGMA table_info(ai_filter_results)"
                    )].count("module_type"),
                    1,
                )
            finally:
                backend.cleanup()

    def test_strict_read_fails_closed_for_null_or_unknown_legacy_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            create_legacy_news_db(tmp)
            backend = local_backend(tmp)
            try:
                conn = backend._get_connection(DATE)
                seed_hotlist_rows(conn)
                tag_id = seed_tag(backend)
                self.assertEqual(
                    backend.save_ai_filter_results([
                        result(1, tag_id, "current_events")
                    ], DATE),
                    1,
                )
                for invalid in (None, "weather"):
                    with self.subTest(module_type=invalid):
                        conn.execute("PRAGMA ignore_check_constraints = ON")
                        conn.execute(
                            "UPDATE ai_filter_results SET module_type = ?",
                            (invalid,),
                        )
                        conn.execute("PRAGMA ignore_check_constraints = OFF")
                        conn.commit()
                        with self.assertRaises(RuntimeError):
                            backend.get_active_ai_filter_results_strict(
                                DATE, INTERESTS_FILE
                            )
                        conn.execute("PRAGMA ignore_check_constraints = ON")
                        conn.execute(
                            "UPDATE ai_filter_results "
                            "SET module_type = 'current_events'"
                        )
                        conn.execute("PRAGMA ignore_check_constraints = OFF")
                        conn.commit()
            finally:
                backend.cleanup()


class LocalAIFilterModuleStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backend = local_backend(self.tmp.name)
        self.conn = self.backend._get_connection(DATE)
        seed_hotlist_rows(self.conn)
        self.tag_id = seed_tag(self.backend)

    def tearDown(self):
        self.backend.cleanup()
        self.tmp.cleanup()

    def _run_ordinary_pipeline(self, response=None, classify_results=None):
        if classify_results is None:
            ai_filter = AIFilter.__new__(AIFilter)
            ai_filter.debug = False
            ai_filter.classify_system = "只返回 JSON"
            ai_filter.classify_user = (
                "{interests_content}\n{tags_list}\n{news_count}\n{news_list}"
            )
            ai_filter.summary_grounding_review_enabled = False
            ai_filter.client = MagicMock()
            ai_filter.client.chat.return_value = json.dumps(response)
            ai_filter.load_interests_content = MagicMock(return_value="rice")
            ai_filter.compute_interests_hash = MagicMock(
                return_value=PROMPT_HASH
            )
        else:
            ai_filter = MagicMock()
            ai_filter.load_interests_content.return_value = "rice"
            ai_filter.compute_interests_hash.return_value = PROMPT_HASH
            ai_filter.classify_batch.return_value = classify_results
        pipeline = AIFilterPipeline(
            {
                "TIMEZONE": "Asia/Shanghai",
                "RSS": {"ENABLED": False},
                "AI": {},
                "AI_FILTER": {
                    "INTERESTS_FILE": INTERESTS_FILE,
                    "BATCH_SIZE": 10,
                    "BATCH_INTERVAL": 0,
                },
                "FILTER": {},
            },
            self.backend,
            lambda: datetime(2026, 8, 9, 10, 0),
        )
        pipeline._enrich_pending_items = lambda items, _label: [
            (
                dict(item, content="成果发表于 Rice Science。")
                if item.get("id") == 2
                else item
            )
            for item in items
        ]
        with patch.object(
            self.backend,
            "_get_configured_time",
            return_value=datetime(2026, 8, 9, 10, 0),
        ), patch(
            "trendradar.ai.filter_pipeline.AIFilter",
            return_value=ai_filter,
        ):
            return pipeline.run()

    def test_ordinary_real_parser_uses_source_evidence_modules(self):
        seed_hotlist_rows(self.conn, ids=(1, 2, 3, 4, 5))
        self.conn.execute(
            "UPDATE news_items SET title = ? WHERE id = 2",
            ("成果发表于 Rice Science",),
        )
        self.conn.commit()
        pipeline_result = self._run_ordinary_pipeline(response=[
            {
                "id": 1,
                "include": True,
                "species_scope": "rice",
                "tag_id": self.tag_id,
                "score": 0.9,
                "importance_score": 0.8,
                "summary": "policy summary",
            },
            {
                "id": 2,
                "include": True,
                "species_scope": "rice",
                "tag_id": self.tag_id,
                "score": 0.8,
                "importance_score": 0.9,
                "summary": "research summary",
            },
            {
                "id": 3,
                "include": False,
                "species_scope": "not_applicable",
                "tag_id": self.tag_id,
                "score": 0.1,
                "importance_score": 0.1,
                "summary": "exclude summary",
            },
            {
                "id": 4,
                "module_type": "weather",
                "include": True,
                "species_scope": "rice",
                "tag_id": self.tag_id,
                "score": 0.7,
                "importance_score": 0.7,
                "summary": "invalid summary",
            },
            {
                "id": 5,
                "species_scope": "rice",
                "tag_id": self.tag_id,
                "score": 0.6,
                "importance_score": 0.6,
                "summary": "missing module summary",
            },
        ])

        self.assertTrue(pipeline_result.success)
        rows = self.conn.execute(
            """SELECT news_item_id, module_type
               FROM ai_filter_results ORDER BY news_item_id"""
        ).fetchall()
        self.assertEqual(
            [(row[0], row[1]) for row in rows],
            [(1, "current_events"), (2, "research")],
        )
        analyzed = self.conn.execute(
            """SELECT news_item_id, matched
               FROM ai_filter_analyzed_news ORDER BY news_item_id"""
        ).fetchall()
        self.assertEqual(
            [(row[0], row[1]) for row in analyzed],
            [(1, 1), (2, 1), (3, 0), (4, 0), (5, 0)],
        )

    def test_ordinary_second_result_insert_failure_leaves_no_state(self):
        self.conn.execute("""
            CREATE TRIGGER fail_second_ordinary_result
            BEFORE INSERT ON ai_filter_results
            WHEN NEW.news_item_id = 2
            BEGIN
                SELECT RAISE(ABORT, 'second ordinary result failed');
            END
        """)
        self.conn.commit()

        pipeline_result = self._run_ordinary_pipeline(classify_results=[
            result(1, self.tag_id, "current_events"),
            result(2, self.tag_id, "research"),
        ])

        self.assertFalse(pipeline_result.success)
        self.assertIn("保存数量不一致", pipeline_result.error)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM ai_filter_results"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM ai_filter_analyzed_news"
            ).fetchone()[0],
            0,
        )

    def test_ordinary_write_and_read_round_trip_both_persisted_modules(self):
        self.assertEqual(
            self.backend.save_ai_filter_results([
                result(1, self.tag_id, "current_events"),
                result(2, self.tag_id, "research", "other_crop"),
            ], DATE),
            2,
        )

        stored = self.backend.get_active_ai_filter_results(
            DATE, INTERESTS_FILE
        )
        self.assertEqual(
            {
                row["news_item_id"]: (
                    row["module_type"], row["species_scope"]
                )
                for row in stored
            },
            {
                1: ("current_events", "rice"),
                2: ("research", "other_crop"),
            },
        )

    def test_ordinary_write_rejects_invalid_batch_without_partial_rows(self):
        for invalid in ("exclude", "weather", None):
            with self.subTest(module_type=invalid):
                self.assertEqual(
                    self.backend.save_ai_filter_results([
                        result(1, self.tag_id, "current_events"),
                        result(2, self.tag_id, invalid),
                    ], DATE),
                    0,
                )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM ai_filter_results"
                    ).fetchone()[0],
                    0,
                )

    def test_strict_write_rejects_invalid_modules_without_analyzed_state(self):
        for invalid in ("exclude", "weather", None):
            with self.subTest(module_type=invalid):
                with self.assertRaises(ValueError):
                    self.backend.replace_ai_filter_batch_strict(
                        [result(1, self.tag_id, invalid)],
                        [1],
                        [],
                        INTERESTS_FILE,
                        PROMPT_HASH,
                        DATE,
                    )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM ai_filter_results"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM ai_filter_analyzed_news"
                    ).fetchone()[0],
                    0,
                )

    def test_strict_second_analyzed_insert_failure_rolls_back_results_and_states(self):
        self.conn.execute("""
            CREATE TRIGGER fail_second_analyzed
            BEFORE INSERT ON ai_filter_analyzed_news
            WHEN NEW.news_item_id = 2
            BEGIN
                SELECT RAISE(ABORT, 'second analyzed insert failed');
            END
        """)
        self.conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.backend.replace_ai_filter_batch_strict(
                [
                    result(1, self.tag_id, "current_events"),
                    result(2, self.tag_id, "research"),
                ],
                [1, 2],
                [],
                INTERESTS_FILE,
                PROMPT_HASH,
                DATE,
            )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM ai_filter_results"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM ai_filter_analyzed_news"
            ).fetchone()[0],
            0,
        )

    def test_analyzed_ids_require_current_hash_and_valid_matched_result(self):
        self.assertEqual(
            self.backend.save_ai_filter_results([
                result(2, self.tag_id, "research")
            ], DATE),
            1,
        )
        self.conn.executemany("""
            INSERT OR REPLACE INTO ai_filter_analyzed_news
                (news_item_id, source_type, interests_file, prompt_hash,
                 matched, created_at)
            VALUES (?, 'hotlist', ?, ?, ?, '2026-08-09 10:00:00')
        """, [
            (1, INTERESTS_FILE, PROMPT_HASH, 0),
            (2, INTERESTS_FILE, PROMPT_HASH, 1),
            (3, INTERESTS_FILE, PROMPT_HASH, 1),
            (4, INTERESTS_FILE, "rice.txt:old", 0),
        ])
        self.conn.commit()

        self.assertEqual(
            self.backend.get_analyzed_news_ids(
                "hotlist", DATE, INTERESTS_FILE
            ),
            {1, 2},
        )


class RemoteAIFilterModuleStorageTests(unittest.TestCase):
    def _baseline(self, tmp):
        s3 = _ConditionalS3()
        key = f"news/{DATE}.db"
        s3.set(key, remote_news_db_bytes(tmp, "baseline"), "v1")
        return s3, key

    def _write_current_events(self, backend):
        tag_id = backend.get_ai_filter_tag_snapshot_strict(
            DATE, INTERESTS_FILE
        )["tags"][0]["id"]
        return backend.replace_ai_filter_batch_strict(
            [result(1, tag_id, "current_events")],
            [1],
            [],
            INTERESTS_FILE,
            PROMPT_HASH,
            DATE,
        )

    def test_successful_cas_persists_module_for_new_observer(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3, _key = self._baseline(tmp)
            writer = remote_backend(Path(tmp) / "writer", s3)
            observer = remote_backend(Path(tmp) / "observer", s3)
            try:
                self.assertEqual(
                    self._write_current_events(writer),
                    {"results": 1, "analyzed": 1},
                )
                rows = observer.get_active_ai_filter_results_strict(
                    DATE, INTERESTS_FILE
                )
                self.assertEqual(
                    [(row["news_item_id"], row["module_type"]) for row in rows],
                    [(1, "current_events")],
                )
            finally:
                writer.cleanup()
                observer.cleanup()

    def test_cas_conflict_and_put_failure_restore_before_image(self):
        for failure in ("cas", "put"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                s3, key = self._baseline(tmp)
                writer = remote_backend(Path(tmp) / "writer", s3)
                try:
                    writer.get_ai_filter_tag_snapshot_strict(
                        DATE, INTERESTS_FILE
                    )
                    if failure == "cas":
                        s3.before_condition = lambda changed_key: s3.set(
                            changed_key,
                            remote_news_db_bytes(tmp, "winner"),
                            "winner",
                        )
                    else:
                        s3.fail_keys_once.add(key)

                    with self.assertRaises(Exception):
                        self._write_current_events(writer)

                    self.assertEqual(
                        writer.get_active_ai_filter_results_strict(
                            DATE, INTERESTS_FILE
                        ),
                        [],
                    )
                    self.assertEqual(
                        writer.get_analyzed_news_ids_strict(
                            "hotlist", DATE, INTERESTS_FILE
                        ),
                        set(),
                    )
                    self.assertNotIn(key, writer._strict_local_authoritative)
                finally:
                    writer.cleanup()

    def test_batch_end_failure_restores_before_image_without_analyzed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3, key = self._baseline(tmp)
            writer = remote_backend(Path(tmp) / "writer", s3)
            try:
                writer.begin_batch()
                self.assertEqual(
                    self._write_current_events(writer),
                    {"results": 1, "analyzed": 1},
                )
                s3.fail_keys_once.add(key)
                with self.assertRaises(RuntimeError):
                    writer.end_batch_strict()

                self.assertEqual(
                    writer.get_active_ai_filter_results_strict(
                        DATE, INTERESTS_FILE
                    ),
                    [],
                )
                self.assertEqual(
                    writer.get_analyzed_news_ids_strict(
                        "hotlist", DATE, INTERESTS_FILE
                    ),
                    set(),
                )
                self.assertNotIn(key, writer._strict_local_authoritative)
            finally:
                writer.cleanup()


if __name__ == "__main__":
    unittest.main()
