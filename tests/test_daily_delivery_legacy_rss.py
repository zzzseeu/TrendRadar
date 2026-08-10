import sqlite3
import tempfile
import unittest
from pathlib import Path

from trendradar.storage.local import LocalStorageBackend


class LegacyRSSFirstSeenBackfillTests(unittest.TestCase):
    def test_strict_backfill_reads_legacy_database_without_migrating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            rss_dir = Path(tmp) / "rss"
            rss_dir.mkdir(parents=True)
            date = "2026-07-27"
            db_path = rss_dir / f"{date}.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE rss_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    feed_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    guid TEXT DEFAULT '',
                    published_at TEXT,
                    summary TEXT,
                    author TEXT,
                    first_crawl_time TEXT NOT NULL,
                    last_crawl_time TEXT NOT NULL,
                    crawl_count INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX idx_rss_url_feed
                    ON rss_items(url, feed_id);
                INSERT INTO rss_items (
                    title, feed_id, url, first_crawl_time, last_crawl_time
                ) VALUES (
                    'Legacy rice breeding article',
                    'legacy-journal',
                    'https://example.org/legacy-rice',
                    '2026-07-27 08:00:00',
                    '2026-07-27 08:00:00'
                );
                """
            )
            conn.close()
            version_before = db_path.stat().st_size, db_path.stat().st_mtime_ns

            backend = LocalStorageBackend(
                data_dir=tmp,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            identity = ("url", "https://example.org/legacy-rice")
            try:
                earliest = backend.get_earliest_rss_discoveries_strict(
                    {identity}, date
                )
                self.assertEqual(
                    earliest[identity],
                    ("2026-07-27 08:00:00", date),
                )

                ledger = backend._get_first_seen_ledger_connection(strict=True)
                recorded_version = ledger.execute(
                    """SELECT source_version FROM rss_first_seen_sources
                       WHERE source_key = ?""",
                    (date,),
                ).fetchone()[0]
                self.assertEqual(
                    recorded_version,
                    backend._get_rss_source_version_strict(date),
                )
            finally:
                backend.cleanup()

            self.assertEqual(
                (db_path.stat().st_size, db_path.stat().st_mtime_ns),
                version_before,
            )
            read_only = sqlite3.connect(
                f"{db_path.resolve().as_uri()}?mode=ro", uri=True
            )
            try:
                tables = {
                    row[0]
                    for row in read_only.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                read_only.close()
            self.assertNotIn("rss_storage_metadata", tables)
            self.assertNotIn("rss_first_seen_outbox", tables)


if __name__ == "__main__":
    unittest.main()
