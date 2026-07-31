import sqlite3
import tempfile
import unittest
from pathlib import Path

from trendradar.core.loader import _load_rss_config
from trendradar.storage.base import RSSData, RSSItem
from trendradar.storage.local import LocalStorageBackend


class NewsSearchConfigTests(unittest.TestCase):
    def test_loader_exposes_validated_news_search_config(self):
        loaded = _load_rss_config({
            "rss": {
                "news_search": {
                    "enabled": True,
                    "max_results_per_provider": 40,
                    "max_hotspots": 5,
                    "similarity_threshold": 0.86,
                    "topics": [{
                        "id": "gene-editing",
                        "zh": "作物 基因编辑 育种",
                        "en": "crop gene editing breeding",
                    }],
                }
            }
        })

        search = loaded["NEWS_SEARCH"]
        self.assertTrue(search["ENABLED"])
        self.assertEqual(search["MAX_RESULTS_PER_PROVIDER"], 40)
        self.assertEqual(search["MAX_HOTSPOTS"], 5)
        self.assertEqual(search["TOPICS"][0]["id"], "gene-editing")

    def test_loader_falls_back_for_non_numeric_news_search_limits(self):
        loaded = _load_rss_config({
            "rss": {
                "news_search": {
                    "max_results_per_provider": "many",
                    "max_hotspots": "several",
                    "similarity_threshold": "high",
                }
            }
        })

        search = loaded["NEWS_SEARCH"]
        self.assertEqual(search["MAX_RESULTS_PER_PROVIDER"], 50)
        self.assertEqual(search["MAX_HOTSPOTS"], 5)
        self.assertEqual(search["SIMILARITY_THRESHOLD"], 0.86)


class NewsSearchStorageTests(unittest.TestCase):
    def test_search_metadata_survives_rss_item_round_trip(self):
        item = RSSItem(
            title="Breeding hotspot",
            feed_id="agri-breeding-search",
            source_count=3,
            pre_hot_score=0.82,
            search_topic="gene-editing",
            search_providers="gdelt,google_news",
        )

        restored = RSSItem.from_dict(item.to_dict())

        self.assertEqual(restored.source_count, 3)
        self.assertEqual(restored.pre_hot_score, 0.82)
        self.assertEqual(restored.search_topic, "gene-editing")
        self.assertEqual(restored.search_providers, "gdelt,google_news")

    def test_regular_rss_item_uses_search_metadata_defaults(self):
        item = RSSItem(title="Regular RSS", feed_id="regular")

        self.assertEqual(item.source_count, 1)
        self.assertEqual(item.pre_hot_score, 0.0)
        self.assertEqual(item.search_topic, "")
        self.assertEqual(item.search_providers, "")

    def test_search_metadata_survives_daily_sqlite_save_and_read_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = LocalStorageBackend(
                data_dir=temp_dir,
                enable_txt=False,
                enable_html=False,
            )
            date = "2026-07-31"
            item = RSSItem(
                title="Breeding hotspot",
                feed_id="agri-breeding-search",
                url="https://example.org/breeding",
                published_at="2026-07-31T08:00:00+00:00",
                source_count=3,
                pre_hot_score=0.82,
                search_topic="gene-editing",
                search_providers="gdelt,google_news",
            )
            regular_item = RSSItem(
                title="Regular RSS",
                feed_id="regular",
                url="https://example.org/regular",
            )
            data = RSSData(
                date=date,
                crawl_time="16-00",
                items={item.feed_id: [item], regular_item.feed_id: [regular_item]},
                id_to_name={
                    item.feed_id: "农业育种热点搜索",
                    regular_item.feed_id: "普通 RSS",
                },
            )

            self.assertTrue(backend.save_rss_data(data))

            all_ids = backend.get_all_rss_ids(date)
            self.assertEqual(len(all_ids), 2)
            stored_ids = {stored["source_id"]: stored for stored in all_ids}
            self.assertEqual(stored_ids[item.feed_id]["source_count"], 3)
            self.assertEqual(stored_ids[item.feed_id]["pre_hot_score"], 0.82)
            self.assertEqual(stored_ids[item.feed_id]["search_topic"], "gene-editing")
            self.assertEqual(
                stored_ids[item.feed_id]["search_providers"],
                "gdelt,google_news",
            )
            self.assertEqual(stored_ids[regular_item.feed_id]["source_count"], 1)
            self.assertEqual(stored_ids[regular_item.feed_id]["pre_hot_score"], 0.0)
            self.assertEqual(stored_ids[regular_item.feed_id]["search_topic"], "")
            self.assertEqual(stored_ids[regular_item.feed_id]["search_providers"], "")

            stored = backend.get_rss_data(date).items[item.feed_id][0]
            latest = backend.get_latest_rss_data(date).items[item.feed_id][0]
            for restored in (stored, latest):
                self.assertEqual(restored.source_count, 3)
                self.assertEqual(restored.pre_hot_score, 0.82)
                self.assertEqual(restored.search_topic, "gene-editing")
                self.assertEqual(restored.search_providers, "gdelt,google_news")

            for conn in backend._db_connections.values():
                conn.close()

    def test_existing_rss_schema_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            rss_dir = Path(temp_dir) / "rss"
            rss_dir.mkdir(parents=True)
            db_path = rss_dir / "2026-07-31.db"
            conn = sqlite3.connect(db_path)
            conn.execute("""
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
                )
            """)
            conn.commit()
            conn.close()

            backend = LocalStorageBackend(
                data_dir=temp_dir,
                enable_txt=False,
                enable_html=False,
            )
            migrated = backend._get_connection("2026-07-31", db_type="rss")
            backend._migrate_rss_schema(migrated)
            backend._migrate_rss_schema(migrated)

            columns = {
                row[1]: row for row in migrated.execute("PRAGMA table_info(rss_items)")
            }
            self.assertEqual(columns["source_count"][4], "1")
            self.assertEqual(columns["pre_hot_score"][4], "0")
            self.assertEqual(columns["search_topic"][4], "''")
            self.assertEqual(columns["search_providers"][4], "''")
            migrated.close()
