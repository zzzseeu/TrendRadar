import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trendradar.__main__ import (
    NewsAnalyzer,
    SEARCH_FEED_ID,
    merge_news_search_into_rss,
)
from trendradar.core.loader import _load_rss_config
from trendradar.crawler.news_search import NewsSearchResult, SearchArticle
from trendradar.storage.base import RSSData, RSSItem
from trendradar.storage.local import LocalStorageBackend


SEARCH_HOTSPOT = SearchArticle(
    title="Rice gene editing breakthrough",
    url="https://example.org/rice?utm_source=gdelt#details",
    published_at="2026-07-31T08:00:00+00:00",
    publisher="Example Agriculture",
    language="en",
    topic="gene-editing",
    providers={"google_news", "gdelt"},
    summary="A concise breeding summary.",
    source_count=2,
    pre_hot_score=0.8123,
)


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


class NewsSearchRSSMergeTests(unittest.TestCase):
    def test_merge_search_result_maps_all_rss_metadata(self):
        regular = RSSItem(
            title="Regular RSS",
            feed_id="regular",
            url="https://example.org/regular",
        )
        rss_data = RSSData(
            date="2026-07-31",
            crawl_time="15:00",
            items={"regular": [regular]},
            id_to_name={"regular": "Regular feed"},
        )

        merge_news_search_into_rss(
            rss_data,
            NewsSearchResult(items=[SEARCH_HOTSPOT], failed_providers=["gdelt"]),
        )

        self.assertEqual(rss_data.items["regular"], [regular])
        item = rss_data.items[SEARCH_FEED_ID][0]
        self.assertEqual(item.title, SEARCH_HOTSPOT.title)
        self.assertEqual(item.feed_id, SEARCH_FEED_ID)
        self.assertEqual(item.feed_name, "农业育种热点搜索")
        self.assertEqual(item.url, SEARCH_HOTSPOT.url)
        self.assertEqual(item.guid, "https://example.org/rice")
        self.assertEqual(item.published_at, SEARCH_HOTSPOT.published_at)
        self.assertEqual(item.summary, SEARCH_HOTSPOT.summary)
        self.assertEqual(item.author, SEARCH_HOTSPOT.publisher)
        self.assertEqual(item.source_count, 2)
        self.assertEqual(item.pre_hot_score, SEARCH_HOTSPOT.pre_hot_score)
        self.assertEqual(item.search_topic, SEARCH_HOTSPOT.topic)
        self.assertEqual(item.search_providers, "gdelt,google_news")
        self.assertEqual(item.crawl_time, "15:00")
        self.assertEqual(item.first_time, "15:00")
        self.assertEqual(item.last_time, "15:00")
        self.assertEqual(rss_data.id_to_name[SEARCH_FEED_ID], "农业育种热点搜索")

    def test_merge_empty_search_result_leaves_fixed_rss_unchanged(self):
        regular = RSSItem(title="Regular RSS", feed_id="regular")
        rss_data = RSSData(
            date="2026-07-31",
            crawl_time="15:00",
            items={"regular": [regular]},
            id_to_name={"regular": "Regular feed"},
        )

        merge_news_search_into_rss(rss_data, NewsSearchResult(items=[]))

        self.assertEqual(rss_data.items, {"regular": [regular]})
        self.assertEqual(rss_data.id_to_name, {"regular": "Regular feed"})


class _StorageStub:
    def __init__(self):
        self.saved = []

    def save_rss_data(self, rss_data):
        self.saved.append(rss_data)
        return True


class NewsSearchRSSFlowTests(unittest.TestCase):
    def _analyzer(self, enabled=True):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            rss_enabled=True,
            rss_feeds=[{
                "id": "regular",
                "name": "Regular feed",
                "url": "https://example.org/rss.xml",
                "enabled": True,
            }],
            rss_config={
                "REQUEST_INTERVAL": 0,
                "TIMEOUT": 15,
                "USE_PROXY": False,
                "PROXY_URL": "",
                "FRESHNESS_FILTER": {"ENABLED": True, "MAX_AGE_DAYS": 1},
                "NEWS_SEARCH": {
                    "ENABLED": enabled,
                    "MAX_RESULTS_PER_PROVIDER": 40,
                    "MAX_HOTSPOTS": 5,
                    "SIMILARITY_THRESHOLD": 0.9,
                    "PROVIDERS": {"gdelt": True, "google_news": True},
                    "AUTHORITY_DOMAINS": ["example.org"],
                    "TOPICS": [{
                        "id": "gene-editing",
                        "zh": "作物 基因编辑 育种",
                        "en": "crop gene editing breeding",
                    }],
                },
            },
            config={"TIMEZONE": "Asia/Shanghai"},
        )
        analyzer.proxy_url = None
        analyzer.storage_manager = _StorageStub()
        analyzer._rss_source_total = 0
        analyzer._rss_source_failed = 0
        analyzer._process_rss_data_by_mode = Mock(return_value=([], [], [], set()))
        return analyzer

    @staticmethod
    def _fixed_rss_data():
        return RSSData(
            date="2026-07-31",
            crawl_time="15:00",
            items={
                "regular": [
                    RSSItem(
                        title="Regular RSS",
                        feed_id="regular",
                        url="https://example.org/regular",
                    )
                ]
            },
            id_to_name={"regular": "Regular feed"},
            failed_ids=["fixed-failure"],
        )

    @patch("trendradar.__main__.AgriculturalNewsSearch")
    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_disabled_search_makes_no_search_call(self, fetcher_class, search_class):
        analyzer = self._analyzer(enabled=False)
        fetcher_class.return_value.fetch_all.return_value = self._fixed_rss_data()

        analyzer._crawl_rss_data()

        search_class.assert_not_called()
        self.assertNotIn(SEARCH_FEED_ID, analyzer.storage_manager.saved[0].items)

    @patch("trendradar.__main__.AgriculturalNewsSearch")
    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_enabled_search_is_called_and_merged(self, fetcher_class, search_class):
        analyzer = self._analyzer(enabled=True)
        fetcher_class.return_value.fetch_all.return_value = self._fixed_rss_data()
        search_class.return_value.search.return_value = NewsSearchResult(
            items=[SEARCH_HOTSPOT]
        )

        analyzer._crawl_rss_data()

        search_class.assert_called_once_with(
            topics=analyzer.ctx.rss_config["NEWS_SEARCH"]["TOPICS"],
            max_results_per_provider=40,
            similarity_threshold=0.9,
            authority_domains=["example.org"],
        )
        search_class.return_value.search.assert_called_once_with()
        saved = analyzer.storage_manager.saved[0]
        self.assertIn("regular", saved.items)
        self.assertEqual(saved.items[SEARCH_FEED_ID][0].title, SEARCH_HOTSPOT.title)
        self.assertEqual(analyzer._rss_source_failed, 1)

    @patch("trendradar.__main__.AgriculturalNewsSearch")
    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_provider_failures_are_logged_without_changing_fixed_failure_count(
        self, fetcher_class, search_class
    ):
        analyzer = self._analyzer(enabled=True)
        fetcher_class.return_value.fetch_all.return_value = self._fixed_rss_data()
        search_class.return_value.search.return_value = NewsSearchResult(
            items=[SEARCH_HOTSPOT],
            failed_providers=["gdelt"],
        )

        output = StringIO()
        with redirect_stdout(output):
            analyzer._crawl_rss_data()

        self.assertIn("[新闻搜索]", output.getvalue())
        self.assertIn("gdelt", output.getvalue())
        self.assertEqual(analyzer._rss_source_failed, 1)
        self.assertEqual(analyzer.storage_manager.saved[0].failed_ids, ["fixed-failure"])
        self.assertIn(SEARCH_FEED_ID, analyzer.storage_manager.saved[0].items)

    @patch("trendradar.__main__.AgriculturalNewsSearch")
    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_search_exception_degrades_to_fixed_rss_data(
        self, fetcher_class, search_class
    ):
        analyzer = self._analyzer(enabled=True)
        fetcher_class.return_value.fetch_all.return_value = self._fixed_rss_data()
        search_class.return_value.search.side_effect = RuntimeError("provider outage")

        output = StringIO()
        with redirect_stdout(output):
            analyzer._crawl_rss_data()

        saved = analyzer.storage_manager.saved[0]
        self.assertEqual(list(saved.items), ["regular"])
        self.assertEqual(saved.failed_ids, ["fixed-failure"])
        self.assertEqual(analyzer._rss_source_failed, 1)
        self.assertIn("[新闻搜索]", output.getvalue())
        self.assertIn("provider outage", output.getvalue())

    @patch("trendradar.__main__.AgriculturalNewsSearch")
    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_empty_search_result_preserves_fixed_rss_data(
        self, fetcher_class, search_class
    ):
        analyzer = self._analyzer(enabled=True)
        fetcher_class.return_value.fetch_all.return_value = self._fixed_rss_data()
        search_class.return_value.search.return_value = NewsSearchResult(items=[])

        analyzer._crawl_rss_data()

        saved = analyzer.storage_manager.saved[0]
        self.assertEqual(list(saved.items), ["regular"])
        self.assertEqual(saved.id_to_name, {"regular": "Regular feed"})
