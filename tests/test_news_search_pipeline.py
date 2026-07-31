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
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.core.loader import _load_rss_config
from trendradar.crawler.news_search import NewsSearchResult, SearchArticle
from trendradar.report.formatter import format_title_for_platform
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

    def test_loader_tolerates_null_and_non_mapping_rss_sections(self):
        for value in (None, []):
            with self.subTest(rss=value):
                loaded = _load_rss_config({"rss": value})

                self.assertFalse(loaded["NEWS_SEARCH"]["ENABLED"])
                self.assertEqual(
                    loaded["NEWS_SEARCH"]["PROVIDERS"],
                    {"gdelt": True, "google_news": True},
                )
                self.assertEqual(loaded["NEWS_SEARCH"]["TOPICS"], [])
                self.assertEqual(loaded["NEWS_SEARCH"]["AUTHORITY_DOMAINS"], [])

    def test_loader_normalizes_malformed_news_search_collections(self):
        loaded = _load_rss_config({
            "rss": {
                "news_search": {
                    "enabled": True,
                    "providers": ["gdelt"],
                    "topics": {"id": "not-a-list"},
                    "authority_domains": "reuters.com",
                }
            }
        })

        search = loaded["NEWS_SEARCH"]
        self.assertTrue(search["ENABLED"])
        self.assertEqual(
            search["PROVIDERS"],
            {"gdelt": True, "google_news": True},
        )
        self.assertEqual(search["TOPICS"], [])
        self.assertEqual(search["AUTHORITY_DOMAINS"], [])

    def test_loader_treats_null_news_search_as_disabled_defaults(self):
        loaded = _load_rss_config({"rss": {"news_search": None}})

        search = loaded["NEWS_SEARCH"]
        self.assertFalse(search["ENABLED"])
        self.assertEqual(
            search["PROVIDERS"],
            {"gdelt": True, "google_news": True},
        )
        self.assertEqual(search["TOPICS"], [])
        self.assertEqual(search["AUTHORITY_DOMAINS"], [])


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

    def test_merge_collision_preserves_existing_fixed_source_and_warns(self):
        fixed = RSSItem(
            title="Fixed source item",
            feed_id=SEARCH_FEED_ID,
            url="https://fixed.example/item",
        )
        rss_data = RSSData(
            date="2026-07-31",
            crawl_time="15:00",
            items={SEARCH_FEED_ID: [fixed]},
            id_to_name={SEARCH_FEED_ID: "Existing fixed feed"},
            failed_ids=["fixed-failure"],
        )

        output = StringIO()
        with redirect_stdout(output):
            merge_news_search_into_rss(
                rss_data,
                NewsSearchResult(items=[SEARCH_HOTSPOT]),
            )

        self.assertEqual(rss_data.items[SEARCH_FEED_ID], [fixed])
        self.assertEqual(rss_data.id_to_name[SEARCH_FEED_ID], "Existing fixed feed")
        self.assertEqual(rss_data.failed_ids, ["fixed-failure"])
        self.assertIn("[新闻搜索]", output.getvalue())
        self.assertIn("冲突", output.getvalue())

    def test_merge_name_only_collision_does_not_create_synthetic_items(self):
        rss_data = RSSData(
            date="2026-07-31",
            crawl_time="15:00",
            items={"regular": [RSSItem(title="Regular", feed_id="regular")]},
            id_to_name={
                "regular": "Regular feed",
                SEARCH_FEED_ID: "Reserved fixed feed name",
            },
        )

        output = StringIO()
        with redirect_stdout(output):
            merge_news_search_into_rss(
                rss_data,
                NewsSearchResult(items=[SEARCH_HOTSPOT]),
            )

        self.assertNotIn(SEARCH_FEED_ID, rss_data.items)
        self.assertEqual(
            rss_data.id_to_name[SEARCH_FEED_ID],
            "Reserved fixed feed name",
        )
        self.assertIn("[新闻搜索]", output.getvalue())


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
            providers={"gdelt": True, "google_news": True},
        )
        search_class.return_value.search.assert_called_once_with()
        saved = analyzer.storage_manager.saved[0]
        self.assertIn("regular", saved.items)
        self.assertEqual(saved.items[SEARCH_FEED_ID][0].title, SEARCH_HOTSPOT.title)
        self.assertEqual(analyzer._rss_source_failed, 1)

    @patch("trendradar.__main__.AgriculturalNewsSearch")
    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_runtime_null_news_search_still_saves_fixed_rss(
        self, fetcher_class, search_class
    ):
        analyzer = self._analyzer(enabled=True)
        analyzer.ctx.rss_config["NEWS_SEARCH"] = None
        fetcher_class.return_value.fetch_all.return_value = self._fixed_rss_data()

        analyzer._crawl_rss_data()

        search_class.assert_not_called()
        saved = analyzer.storage_manager.saved[0]
        self.assertEqual(list(saved.items), ["regular"])
        self.assertEqual(saved.failed_ids, ["fixed-failure"])
        analyzer._process_rss_data_by_mode.assert_called_once_with(saved)

    @patch("trendradar.crawler.news_search.GoogleNewsRSSClient.fetch")
    @patch("trendradar.crawler.news_search.GDELTClient.fetch")
    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_both_disabled_providers_make_zero_calls_and_preserve_fixed_rss(
        self, fetcher_class, gdelt_fetch, google_fetch
    ):
        analyzer = self._analyzer(enabled=True)
        analyzer.ctx.rss_config["NEWS_SEARCH"]["PROVIDERS"] = {
            "gdelt": False,
            "google_news": False,
        }
        fetcher_class.return_value.fetch_all.return_value = self._fixed_rss_data()

        analyzer._crawl_rss_data()

        gdelt_fetch.assert_not_called()
        google_fetch.assert_not_called()
        saved = analyzer.storage_manager.saved[0]
        self.assertEqual(list(saved.items), ["regular"])
        self.assertEqual(saved.id_to_name, {"regular": "Regular feed"})
        self.assertEqual(saved.failed_ids, ["fixed-failure"])

    @patch("trendradar.__main__.AgriculturalNewsSearch")
    @patch("trendradar.crawler.rss.RSSFetcher")
    def test_runtime_collision_preserves_fixed_source_and_failure_stats(
        self, fetcher_class, search_class
    ):
        analyzer = self._analyzer(enabled=True)
        fixed = self._fixed_rss_data()
        collision_item = RSSItem(
            title="Configured fixed search feed",
            feed_id=SEARCH_FEED_ID,
            url="https://fixed.example/item",
        )
        fixed.items[SEARCH_FEED_ID] = [collision_item]
        fixed.id_to_name[SEARCH_FEED_ID] = "Configured fixed feed"
        fetcher_class.return_value.fetch_all.return_value = fixed
        search_class.return_value.search.return_value = NewsSearchResult(
            items=[SEARCH_HOTSPOT]
        )

        output = StringIO()
        with redirect_stdout(output):
            analyzer._crawl_rss_data()

        saved = analyzer.storage_manager.saved[0]
        self.assertEqual(saved.items[SEARCH_FEED_ID], [collision_item])
        self.assertEqual(saved.id_to_name[SEARCH_FEED_ID], "Configured fixed feed")
        self.assertEqual(saved.failed_ids, ["fixed-failure"])
        self.assertEqual(analyzer._rss_source_failed, 1)
        self.assertIn("[新闻搜索]", output.getvalue())
        self.assertIn("冲突", output.getvalue())

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


class NewsSearchHotspotRankingTests(unittest.TestCase):
    @staticmethod
    def _pipeline(max_hotspots=5, min_score=0, highlight_top_n=20):
        return AIFilterPipeline(
            {
                "RSS": {
                    "ENABLED": True,
                    "FEEDS": [],
                    "FRESHNESS_FILTER": {"ENABLED": False, "MAX_AGE_DAYS": 1},
                    "NEWS_SEARCH": {"MAX_HOTSPOTS": max_hotspots},
                },
                "AI_FILTER": {
                    "HIGHLIGHT_TOP_N": highlight_top_n,
                    "MIN_SCORE": min_score,
                },
                "FILTER": {},
                "TIMEZONE": "Asia/Shanghai",
            },
            storage_manager=None,
            get_time_func=lambda: None,
        )

    @staticmethod
    def _search_result(index, tag="育种", **overrides):
        item = {
            "news_item_id": index,
            "tag": tag,
            "tag_priority": 1,
            "title": f"Search event {index}",
            "source_id": SEARCH_FEED_ID,
            "source_name": "农业育种热点搜索",
            "source_type": "rss",
            "url": f"https://example.org/search/{index}",
            "first_time": "2026-07-31T08:00:00+00:00",
            "last_time": "2026-07-31T08:00:00+00:00",
            "relevance_score": index / 10,
            "importance_score": index / 20,
            "source_count": index,
            "pre_hot_score": index / 10,
            "search_topic": "gene-editing",
            "search_providers": "gdelt,google_news",
        }
        item.update(overrides)
        return item

    @staticmethod
    def _flatten_search_items(result):
        return [
            item
            for tag in result.tags
            for item in tag["items"]
            if item["source_id"] == SEARCH_FEED_ID
        ]

    def test_search_results_use_combined_score_and_are_capped_at_five(self):
        raw_results = [self._search_result(index) for index in range(1, 7)]

        result = self._pipeline()._build_filter_result(
            raw_results=raw_results,
            tags=[{"tag": "育种", "priority": 1}],
            total_processed=6,
        )

        search_items = self._flatten_search_items(result)
        self.assertEqual(len(search_items), 5)
        self.assertEqual(
            [item["final_hot_score"] for item in search_items],
            sorted(
                [item["final_hot_score"] for item in search_items],
                reverse=True,
            ),
        )
        highest = search_items[0]
        self.assertEqual(highest["title"], "Search event 6")
        self.assertEqual(
            highest["final_hot_score"],
            round(0.45 * 0.6 + 0.35 * 0.6 + 0.20 * 0.3, 4),
        )

    def test_cap_is_global_across_tags_deduplicates_and_keeps_regular_rss(self):
        duplicate_low = self._search_result(
            10,
            tag="标签甲",
            title="Duplicated event",
            url="https://example.org/duplicate",
            relevance_score=0.4,
            importance_score=0.4,
            pre_hot_score=0.4,
        )
        duplicate_high = self._search_result(
            10,
            tag="标签乙",
            title="Duplicated event",
            url="https://example.org/duplicate",
            relevance_score=0.95,
            importance_score=0.9,
            pre_hot_score=0.9,
        )
        ordinary = {
            "news_item_id": 10,
            "tag": "标签甲",
            "tag_priority": 1,
            "title": "Ordinary RSS must remain",
            "source_id": "regular-feed",
            "source_name": "Regular feed",
            "source_type": "rss",
            "url": "https://example.org/duplicate",
            "relevance_score": 0.1,
            "importance_score": 0.1,
        }
        raw_results = [
            duplicate_low,
            ordinary,
            self._search_result(2, tag="标签甲"),
            duplicate_high,
            self._search_result(3, tag="标签乙"),
        ]

        result = self._pipeline(max_hotspots=2)._build_filter_result(
            raw_results=raw_results,
            tags=[
                {"tag": "标签甲", "priority": 1},
                {"tag": "标签乙", "priority": 2},
            ],
            total_processed=5,
        )

        search_items = self._flatten_search_items(result)
        self.assertEqual(len(search_items), 2)
        self.assertEqual(
            sum(item["title"] == "Duplicated event" for item in search_items),
            1,
        )
        duplicate = next(
            item for item in search_items if item["title"] == "Duplicated event"
        )
        self.assertEqual(duplicate["relevance_score"], 0.95)
        ordinary_items = [
            item
            for tag in result.tags
            for item in tag["items"]
            if item["source_id"] == "regular-feed"
        ]
        self.assertEqual(len(ordinary_items), 1)

    def test_non_default_cap_and_ties_keep_stable_input_order(self):
        tied = [
            self._search_result(
                index,
                relevance_score=0.8,
                importance_score=0.7,
                pre_hot_score=0.6,
            )
            for index in (30, 20, 10)
        ]

        result = self._pipeline(max_hotspots=2)._build_filter_result(
            raw_results=tied,
            tags=[{"tag": "育种", "priority": 1}],
            total_processed=3,
        )

        self.assertEqual(
            [item["title"] for item in self._flatten_search_items(result)],
            ["Search event 30", "Search event 20"],
        )

    def test_cap_is_filled_from_results_that_pass_minimum_relevance(self):
        below_threshold = self._search_result(
            99,
            title="Below threshold",
            relevance_score=0.6,
            importance_score=1.0,
            pre_hot_score=1.0,
        )
        eligible = [
            self._search_result(
                index,
                relevance_score=0.7 + index / 100,
                importance_score=0.5,
                pre_hot_score=0.5,
            )
            for index in range(1, 7)
        ]

        result = self._pipeline(max_hotspots=5, min_score=0.7)._build_filter_result(
            raw_results=[below_threshold, *eligible],
            tags=[{"tag": "育种", "priority": 1}],
            total_processed=7,
        )

        search_items = self._flatten_search_items(result)
        self.assertEqual(len(search_items), 5)
        self.assertNotIn("Below threshold", [item["title"] for item in search_items])

    def test_search_metadata_is_passed_to_report_title_entry(self):
        item = self._search_result(5)
        result = self._pipeline()._build_filter_result(
            raw_results=[item],
            tags=[{"tag": "育种", "priority": 1}],
            total_processed=1,
        )

        _, rss_stats, _ = self._pipeline().convert_to_report_data(result)
        title = rss_stats[0]["titles"][0]
        self.assertEqual(title["source_count"], 5)
        self.assertEqual(
            title["final_hot_score"],
            round(0.45 * 0.5 + 0.35 * 0.5 + 0.20 * 0.25, 4),
        )

    def test_final_hot_score_controls_filter_and_report_order(self):
        high_final = self._search_result(
            1,
            title="High final score",
            pre_hot_score=1.0,
            relevance_score=0.7,
            importance_score=0.1,
        )
        low_final = self._search_result(
            2,
            title="Low final score",
            pre_hot_score=0.0,
            relevance_score=1.0,
            importance_score=1.0,
        )

        result = self._pipeline()._build_filter_result(
            raw_results=[high_final, low_final],
            tags=[{"tag": "育种", "priority": 1}],
            total_processed=2,
        )

        search_items = self._flatten_search_items(result)
        self.assertEqual(
            [(item["title"], item["final_hot_score"]) for item in search_items],
            [("High final score", 0.715), ("Low final score", 0.55)],
        )
        _, rss_stats, _ = self._pipeline().convert_to_report_data(result)
        self.assertEqual(
            [title["title"] for title in rss_stats[0]["titles"]],
            ["High final score", "Low final score"],
        )

    def test_cross_tag_selection_ranks_are_global_and_regular_order_is_unchanged(self):
        low_final = self._search_result(
            1,
            tag="低热点标签",
            tag_priority=1,
            pre_hot_score=0.0,
            relevance_score=1.0,
            importance_score=1.0,
        )
        high_final = self._search_result(
            2,
            tag="高热点标签",
            tag_priority=2,
            pre_hot_score=1.0,
            relevance_score=0.7,
            importance_score=0.1,
        )
        regular_high = {
            "news_item_id": 20,
            "tag": "普通标签",
            "tag_priority": 3,
            "title": "Regular high importance",
            "source_id": "regular-feed",
            "source_type": "rss",
            "relevance_score": 0.8,
            "importance_score": 0.9,
        }
        regular_low = {
            **regular_high,
            "news_item_id": 21,
            "title": "Regular low importance",
            "importance_score": 0.8,
        }

        result = self._pipeline()._build_filter_result(
            raw_results=[low_final, regular_low, high_final, regular_high],
            tags=[
                {"tag": "低热点标签", "priority": 1},
                {"tag": "高热点标签", "priority": 2},
                {"tag": "普通标签", "priority": 3},
            ],
            total_processed=4,
        )

        search_ranks = [
            item["search_hotspot_rank"]
            for tag in result.tags
            for item in tag["items"]
            if item["source_id"] == SEARCH_FEED_ID
        ]
        self.assertEqual(set(search_ranks), {1, 2})
        for tag in result.tags:
            ranks = [
                item["search_hotspot_rank"]
                for item in tag["items"]
                if item["source_id"] == SEARCH_FEED_ID
            ]
            self.assertEqual(ranks, sorted(ranks))
        regular_titles = [
            item["title"]
            for tag in result.tags
            for item in tag["items"]
            if item["source_id"] == "regular-feed"
        ]
        self.assertEqual(
            regular_titles,
            ["Regular high importance", "Regular low importance"],
        )

    def test_cross_tag_interleaving_preserves_unique_ordered_tag_groups(self):
        rank_one = self._search_result(
            1,
            tag="标签甲",
            pre_hot_score=0.9,
            relevance_score=0.9,
            importance_score=0.9,
        )
        rank_two = self._search_result(
            2,
            tag="标签乙",
            pre_hot_score=0.8,
            relevance_score=0.8,
            importance_score=0.8,
        )
        rank_three = self._search_result(
            3,
            tag="标签甲",
            pre_hot_score=0.7,
            relevance_score=0.7,
            importance_score=0.7,
        )

        result = self._pipeline()._build_filter_result(
            raw_results=[rank_three, rank_two, rank_one],
            tags=[
                {"tag": "标签甲", "priority": 1},
                {"tag": "标签乙", "priority": 2},
            ],
            total_processed=3,
        )

        tag_names = [tag["tag"] for tag in result.tags]
        self.assertEqual(len(tag_names), len(set(tag_names)))
        self.assertEqual(
            {
                item["search_hotspot_rank"]
                for tag in result.tags
                for item in tag["items"]
                if item["source_id"] == SEARCH_FEED_ID
            },
            {1, 2, 3},
        )
        for tag in result.tags:
            ranks = [
                item["search_hotspot_rank"]
                for item in tag["items"]
                if item["source_id"] == SEARCH_FEED_ID
            ]
            self.assertEqual(ranks, sorted(ranks))

    def test_mixed_tag_is_not_split_or_duplicated_in_filter_and_report(self):
        search = self._search_result(
            1,
            tag="混合标签",
            pre_hot_score=0.9,
            relevance_score=0.8,
            importance_score=0.7,
        )
        ordinary = {
            "news_item_id": 2,
            "tag": "混合标签",
            "tag_priority": 4,
            "tag_description": "保留描述",
            "title": "Ordinary item",
            "source_id": "regular-feed",
            "source_type": "rss",
            "relevance_score": 0.9,
            "importance_score": 0.8,
        }

        pipeline = self._pipeline()
        result = pipeline._build_filter_result(
            raw_results=[ordinary, search],
            tags=[{"tag": "混合标签", "priority": 4}],
            total_processed=2,
        )

        self.assertEqual(len(result.tags), 1)
        self.assertEqual(result.tags[0]["tag"], "混合标签")
        self.assertEqual(result.tags[0]["description"], "保留描述")
        self.assertEqual(result.tags[0]["position"], 4)
        self.assertEqual(result.tags[0]["count"], 2)
        self.assertEqual(len(result.tags[0]["items"]), 2)
        _, rss_stats, _ = pipeline.convert_to_report_data(result)
        self.assertEqual(len(rss_stats), 1)
        self.assertEqual(rss_stats[0]["word"], "混合标签")
        self.assertEqual(rss_stats[0]["count"], 2)

    def test_highlights_keep_legacy_ranking_and_do_not_reserve_search_slots(self):
        search = self._search_result(
            1,
            pre_hot_score=1.0,
            relevance_score=0.7,
            importance_score=0.1,
        )
        ordinary = {
            "news_item_id": 2,
            "tag": "育种",
            "title": "Ordinary TOP",
            "source_id": "regular-feed",
            "source_type": "rss",
            "relevance_score": 0.9,
            "importance_score": 1.0,
            "content_level": "full_text",
        }

        result = self._pipeline(highlight_top_n=1)._build_filter_result(
            raw_results=[search, ordinary],
            tags=[{"tag": "育种", "priority": 1}],
            total_processed=2,
        )

        self.assertEqual(
            [item["title"] for item in result.highlights],
            ["Ordinary TOP"],
        )
        search_item = self._flatten_search_items(result)[0]
        self.assertNotIn("highlight_rank", search_item)
        ordinary_item = next(
            item
            for item in result.tags[0]["items"]
            if item["source_id"] == "regular-feed"
        )
        self.assertEqual(ordinary_item["highlight_rank"], 1)

    def test_deduplicates_by_id_or_canonical_url_with_transitive_overlap(self):
        same_id = self._search_result(
            1,
            title="Same ID",
            url="https://example.org/first",
            pre_hot_score=0.4,
            relevance_score=0.4,
            importance_score=0.4,
        )
        bridge = self._search_result(
            1,
            title="Bridge",
            url="https://EXAMPLE.org/story?utm_source=google#fragment",
            pre_hot_score=0.5,
            relevance_score=0.5,
            importance_score=0.5,
        )
        same_url = self._search_result(
            3,
            title="Canonical URL winner",
            url="https://example.org/story",
            pre_hot_score=0.9,
            relevance_score=0.9,
            importance_score=0.9,
        )

        result = self._pipeline()._build_filter_result(
            raw_results=[same_id, bridge, same_url],
            tags=[{"tag": "育种", "priority": 1}],
            total_processed=3,
        )

        search_items = self._flatten_search_items(result)
        self.assertEqual(len(search_items), 1)
        self.assertEqual(search_items[0]["title"], "Canonical URL winner")

    def test_removes_empty_tags_and_deleted_items_from_totals_and_highlights(self):
        winner = self._search_result(
            1,
            tag="保留标签",
            pre_hot_score=0.9,
            relevance_score=0.9,
            importance_score=0.9,
        )
        removed = self._search_result(
            2,
            tag="空标签",
            pre_hot_score=0.1,
            relevance_score=0.1,
            importance_score=0.1,
        )

        result = self._pipeline(max_hotspots=1)._build_filter_result(
            raw_results=[removed, winner],
            tags=[
                {"tag": "空标签", "priority": 1},
                {"tag": "保留标签", "priority": 2},
            ],
            total_processed=2,
        )

        self.assertEqual([tag["tag"] for tag in result.tags], ["保留标签"])
        self.assertEqual(result.total_matched, 1)
        self.assertEqual([item["title"] for item in result.highlights], [winner["title"]])


class NewsSearchCoverageFormattingTests(unittest.TestCase):
    @staticmethod
    def _title_data(source_count):
        return {
            "title": "Rice <gene> & breeding",
            "source_name": "Search & News",
            "time_display": "",
            "count": 1,
            "ranks": [],
            "rank_threshold": 5,
            "url": "https://example.org/article?a=1&b=2",
            "mobile_url": "",
            "reader_url": "",
            "is_new": False,
            "source_count": source_count,
        }

    def test_major_notification_platforms_show_multi_source_coverage(self):
        for platform in (
            "wework",
            "dingtalk",
            "feishu",
            "bark",
            "telegram",
            "ntfy",
            "slack",
        ):
            with self.subTest(platform=platform):
                rendered = format_title_for_platform(
                    platform,
                    self._title_data(source_count=3),
                )
                self.assertIn("🔥 3家来源", rendered)

    def test_html_escapes_content_and_uses_coverage_count_span(self):
        rendered = format_title_for_platform(
            "html",
            self._title_data(source_count=3),
        )

        self.assertIn(
            '<span class="coverage-count">🔥 3家来源</span>',
            rendered,
        )
        self.assertIn("Rice &lt;gene&gt; &amp; breeding", rendered)
        self.assertNotIn("Rice <gene>", rendered)

    def test_single_source_hides_coverage_on_all_supported_styles(self):
        for platform in (
            "wework",
            "dingtalk",
            "feishu",
            "bark",
            "telegram",
            "ntfy",
            "slack",
            "html",
        ):
            with self.subTest(platform=platform):
                rendered = format_title_for_platform(
                    platform,
                    self._title_data(source_count=1),
                )
                self.assertNotIn("家来源", rendered)
                self.assertNotIn("coverage-count", rendered)
