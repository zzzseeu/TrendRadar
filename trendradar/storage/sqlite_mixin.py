# coding=utf-8
"""
SQLite 存储 Mixin

提供共用的 SQLite 数据库操作逻辑，供 LocalStorageBackend 和 RemoteStorageBackend 复用。
"""

import hashlib
import json
import sqlite3
from abc import abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from trendradar.crawler.news_search import canonicalize_url, normalize_title
from trendradar.storage.base import NewsItem, NewsData, RSSItem, RSSData
from trendradar.utils.time import parse_storage_datetime
from trendradar.utils.url import normalize_url


class SQLiteStorageMixin:
    """
    SQLite 存储操作 Mixin

    子类需要实现以下抽象方法：
    - _get_connection(date, db_type) -> sqlite3.Connection
    - _get_configured_time() -> datetime
    - _format_date_folder(date) -> str
    - _format_time_filename() -> str
    """

    # ========================================
    # 抽象方法 - 子类必须实现
    # ========================================

    @abstractmethod
    def _get_connection(self, date: Optional[str] = None, db_type: str = "news") -> sqlite3.Connection:
        """获取数据库连接"""
        pass

    @abstractmethod
    def _get_configured_time(self) -> datetime:
        """获取配置时区的当前时间"""
        pass

    @abstractmethod
    def _format_date_folder(self, date: Optional[str] = None) -> str:
        """格式化日期文件夹名 (ISO 格式: YYYY-MM-DD)"""
        pass

    @abstractmethod
    def _format_time_filename(self) -> str:
        """格式化时间文件名 (格式: HH-MM)"""
        pass

    def _get_rss_connection(
        self, date: Optional[str] = None, strict: bool = False
    ) -> sqlite3.Connection:
        """获取 RSS 连接；远程后端可覆盖以启用严格存在性检查。"""
        return self._get_connection(date, db_type="rss")

    def _get_ai_connection(
        self, date: Optional[str] = None, strict: bool = False
    ) -> sqlite3.Connection:
        """获取 AI/news 连接；远程后端可覆盖以启用严格存在性检查。"""
        return self._get_connection(date, db_type="news")

    def _get_first_seen_ledger_connection(
        self, strict: bool = False
    ) -> sqlite3.Connection:
        """获取 first-seen 账本连接；具体路径和远端刷新由后端提供。"""
        raise NotImplementedError("存储后端不支持 RSS first-seen 账本")

    def _list_rss_history_sources_strict(
        self, through_date: str
    ) -> Dict[str, str]:
        """严格列举 RSS 日库及无需打开数据库即可比较的 provenance。"""
        raise NotImplementedError("存储后端不支持 RSS first-seen 回填")

    def _get_rss_source_version_strict(self, date: str) -> str:
        """返回 RSS 日库当前可靠 provenance。"""
        raise NotImplementedError("存储后端不支持 RSS 日库 provenance")

    def _persist_first_seen_ledger_strict(self) -> None:
        """持久化账本；本地后端无需额外动作。"""

    @staticmethod
    def _rss_identity_key(identity: tuple) -> str:
        """把公共 canonical identity 编码为稳定账本主键。"""
        return json.dumps(
            identity, ensure_ascii=False, separators=(",", ":")
        )

    @staticmethod
    def _rss_identity_from_key(identity_key: str) -> tuple:
        value = json.loads(identity_key)
        if not isinstance(value, list) or not value:
            raise RuntimeError(f"RSS identity 账本键无效: {identity_key!r}")
        return tuple(value)

    def _init_first_seen_ledger(self, conn: sqlite3.Connection) -> None:
        schema_path = Path(__file__).parent / "first_seen_schema.sql"
        with open(schema_path, "r", encoding="utf-8") as schema_file:
            conn.executescript(schema_file.read())

    def _rss_identity_for_ledger(
        self, title: str, feed_id: str, url: str
    ) -> tuple:
        canonical_url = canonicalize_url(url or "")
        if canonical_url:
            return ("url", canonical_url)
        normalized_title = normalize_title(title or "")
        if not normalized_title:
            return ()
        return ("title", feed_id, normalized_title)

    @staticmethod
    def _stable_rss_title_guid(title: str, feed_id: str) -> str:
        normalized = normalize_title(title or "")
        if not normalized:
            return ""
        digest = hashlib.sha256(
            f"{feed_id}\0{normalized}".encode("utf-8")
        ).hexdigest()
        return f"rss-title:{digest}"

    def _upsert_first_seen_rows(
        self,
        conn: sqlite3.Connection,
        rows,
    ) -> None:
        for identity, discovered, storage_date in rows:
            if not identity:
                continue
            parsed = parse_storage_datetime(
                discovered, storage_date, self.timezone
            )
            if parsed is None:
                raise RuntimeError(
                    f"RSS 首次发现时间无效: {storage_date}/{identity!r}"
                )
            conn.execute(
                """
                INSERT INTO rss_identity_first_seen
                    (identity_key, first_seen, storage_date, first_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(identity_key) DO UPDATE SET
                    first_seen = excluded.first_seen,
                    storage_date = excluded.storage_date,
                    first_seen_at = excluded.first_seen_at
                WHERE excluded.first_seen_at
                      < rss_identity_first_seen.first_seen_at
                """,
                (
                    self._rss_identity_key(identity),
                    discovered,
                    storage_date,
                    parsed.isoformat(),
                ),
            )

    def _read_rss_day_for_first_seen_sync(
        self,
        date: str,
        listed_version: str,
        watermark: int,
        allow_legacy_fallback: bool,
    ) -> Dict[str, Any]:
        """读取与 inventory provenance 绑定的单一 SQLite 快照。"""
        before = self._get_rss_source_version_strict(date)
        if before != listed_version:
            raise RuntimeError(f"RSS 日库版本在快照读取前变化: {date}")

        conn = self._get_rss_connection(date, strict=True)
        bound = self._get_rss_source_version_strict(date)
        if bound != listed_version:
            raise RuntimeError(f"RSS 日库版本在快照绑定时变化: {date}")

        try:
            conn.commit()
            conn.execute("BEGIN")
            cursor = conn.cursor()
            cursor.execute(
                """SELECT write_id, identity_key, first_seen, storage_date,
                          crawl_record_id, source_generation
                   FROM rss_first_seen_outbox
                   WHERE source_generation > ?
                   ORDER BY source_generation, write_id""",
                (watermark,),
            )
            outbox = [dict(row) for row in cursor.fetchall()]
            row = cursor.execute(
                """SELECT value FROM rss_storage_metadata
                   WHERE key = 'generation'"""
            ).fetchone()
            generation = int(row[0]) if row else 0
            if generation < watermark:
                raise RuntimeError(
                    f"RSS 日库 generation 回退: {date} "
                    f"{generation} < {watermark}"
                )

            rows = []
            # 旧版本数据库没有 durable outbox；只在首次迁移且确实没有
            # outbox 时读取 rss_items，后续 generation 永远只走增量 outbox。
            if allow_legacy_fallback and watermark == 0 and not outbox:
                cursor.execute("""
                    SELECT title, feed_id, url,
                           first_crawl_time, last_crawl_time
                    FROM rss_items
                """)
                for (
                    title,
                    feed_id,
                    url,
                    first_time,
                    last_time,
                ) in cursor.fetchall():
                    identity = self._rss_identity_for_ledger(
                        title, feed_id, url
                    )
                    rows.append((
                        identity,
                        first_time or last_time or "",
                        date,
                    ))

            after = self._get_rss_source_version_strict(date)
            if after != listed_version:
                raise RuntimeError(
                    f"RSS 日库版本在快照读取后变化: {date}"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {
            "outbox": outbox,
            "fallback_rows": rows,
            "generation": generation,
            "source_version": listed_version,
        }

    def _consume_first_seen_outboxes_strict(self, through_date: str) -> None:
        """仅打开 provenance 新增/变化的日库，幂等消费 durable outbox。"""
        sources = self._list_rss_history_sources_strict(through_date)
        conn = self._get_first_seen_ledger_connection(strict=True)
        self._init_first_seen_ledger(conn)
        stored_sources = {
            row[0]: (row[1], int(row[2]))
            for row in conn.execute(
                """SELECT source_key, source_version, watermark
                   FROM rss_first_seen_sources"""
            ).fetchall()
        }
        changed = [
            (
                date,
                source_version,
                stored_sources.get(date, (None, 0))[1],
                date not in stored_sources,
            )
            for date, source_version in sorted(sources.items())
            if stored_sources.get(date, (None, 0))[0] != source_version
        ]
        if not changed:
            if getattr(self, "_first_seen_needs_upload", False):
                self._persist_first_seen_ledger_strict()
            return

        pending = []
        for date, listed_version, watermark, is_initial in changed:
            payload = self._read_rss_day_for_first_seen_sync(
                date,
                listed_version,
                watermark,
                allow_legacy_fallback=is_initial,
            )
            pending.append((date, payload))

        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = self._get_configured_time().isoformat()
            for date, payload in pending:
                # 兼容升级前无 outbox 的日库；changed source 才做全表回填。
                self._upsert_first_seen_rows(
                    conn, payload["fallback_rows"]
                )
                for entry in payload["outbox"]:
                    processed = conn.execute(
                        """INSERT OR IGNORE INTO
                               rss_first_seen_processed_writes
                               (source_key, write_id, processed_at)
                           VALUES (?, ?, ?)""",
                        (date, entry["write_id"], now_str),
                    )
                    if processed.rowcount == 0:
                        continue
                    identity = self._rss_identity_from_key(
                        entry["identity_key"]
                    )
                    self._upsert_first_seen_rows(conn, [(
                        identity,
                        entry["first_seen"],
                        entry["storage_date"],
                    )])
                conn.execute(
                    """INSERT INTO rss_first_seen_sources
                           (source_key, source_version, watermark, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(source_key) DO UPDATE SET
                           source_version = excluded.source_version,
                           watermark = excluded.watermark,
                           updated_at = excluded.updated_at""",
                    (
                        date,
                        payload["source_version"],
                        payload["generation"],
                        now_str,
                    ),
                )
            conn.executemany(
                """INSERT INTO ledger_metadata(key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                [("schema_version", "2"), ("backfill_complete", "1")],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._first_seen_needs_upload = True
        self._persist_first_seen_ledger_strict()

    def _ensure_first_seen_ledger_strict(self, through_date: str) -> None:
        """兼容入口：同步变更 source 的 durable outbox。"""
        self._consume_first_seen_outboxes_strict(through_date)

    def _sync_first_seen_ledger_strict(self, data: RSSData) -> None:
        """兼容入口：禁止从 payload 生成 ledger-only identity。"""
        self._consume_first_seen_outboxes_strict(data.date)

    def _query_first_seen_ledger_strict(
        self, candidate_identities: set[tuple], through_date: str
    ) -> Dict[tuple, tuple[str, str]]:
        if not candidate_identities:
            return {}
        self._consume_first_seen_outboxes_strict(through_date)
        conn = self._get_first_seen_ledger_connection(strict=True)
        keys = [self._rss_identity_key(item) for item in candidate_identities]
        result: Dict[tuple, tuple[str, str]] = {}
        for offset in range(0, len(keys), 500):
            chunk = keys[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            cursor = conn.execute(
                f"""SELECT identity_key, first_seen, storage_date
                    FROM rss_identity_first_seen
                    WHERE identity_key IN ({placeholders})""",
                chunk,
            )
            for identity_key, first_seen, storage_date in cursor.fetchall():
                if storage_date <= through_date:
                    result[self._rss_identity_from_key(identity_key)] = (
                        first_seen,
                        storage_date,
                    )
        return result

    # ========================================
    # Schema 管理
    # ========================================

    def _get_schema_path(self, db_type: str = "news") -> Path:
        """
        获取 schema.sql 文件路径

        Args:
            db_type: 数据库类型 ("news" 或 "rss")

        Returns:
            schema 文件路径
        """
        if db_type == "rss":
            return Path(__file__).parent / "rss_schema.sql"
        return Path(__file__).parent / "schema.sql"

    def _get_ai_filter_schema_path(self) -> Path:
        """获取 AI 筛选 schema 文件路径"""
        return Path(__file__).parent / "ai_filter_schema.sql"

    def _init_tables(self, conn: sqlite3.Connection, db_type: str = "news") -> None:
        """
        从 schema.sql 初始化数据库表结构

        Args:
            conn: 数据库连接
            db_type: 数据库类型 ("news" 或 "rss")
        """
        schema_path = self._get_schema_path(db_type)

        if schema_path.exists():
            with open(schema_path, "r", encoding="utf-8") as f:
                schema_sql = f.read()
            conn.executescript(schema_sql)
        else:
            raise FileNotFoundError(f"Schema file not found: {schema_path}")

        # news 库额外加载 AI 筛选表结构
        if db_type == "news":
            ai_filter_schema = self._get_ai_filter_schema_path()
            if ai_filter_schema.exists():
                with open(ai_filter_schema, "r", encoding="utf-8") as f:
                    conn.executescript(f.read())
                self._migrate_ai_filter_schema(conn)

        if db_type == "rss":
            self._migrate_rss_schema(conn)

        conn.commit()

    def _migrate_rss_schema(self, conn: sqlite3.Connection) -> None:
        """幂等迁移已有 rss_items 表结构。"""
        cursor = conn.execute("PRAGMA table_info(rss_items)")
        columns = {row[1] for row in cursor.fetchall()}
        if "guid" not in columns:
            conn.execute("ALTER TABLE rss_items ADD COLUMN guid TEXT DEFAULT ''")
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_rss_guid_feed
                ON rss_items(guid, feed_id) WHERE guid != ''
            """)
        if "source_count" not in columns:
            conn.execute(
                "ALTER TABLE rss_items ADD COLUMN source_count INTEGER DEFAULT 1"
            )
        if "pre_hot_score" not in columns:
            conn.execute(
                "ALTER TABLE rss_items ADD COLUMN pre_hot_score REAL DEFAULT 0"
            )
        if "search_topic" not in columns:
            conn.execute(
                "ALTER TABLE rss_items ADD COLUMN search_topic TEXT DEFAULT ''"
            )
        if "search_providers" not in columns:
            conn.execute(
                "ALTER TABLE rss_items ADD COLUMN search_providers TEXT DEFAULT ''"
            )
        url_index = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND name = 'idx_rss_url_feed'"
        ).fetchone()
        if url_index and " WHERE " not in (url_index[0] or "").upper():
            conn.execute("DROP INDEX idx_rss_url_feed")
            conn.execute(
                "CREATE UNIQUE INDEX idx_rss_url_feed "
                "ON rss_items(url, feed_id) WHERE url != ''"
            )

    def _migrate_ai_filter_schema(self, conn: sqlite3.Connection) -> None:
        """为已有新闻数据库补充 AI 筛选证据、评分和逐条摘要字段。"""
        cursor = conn.execute("PRAGMA table_info(ai_filter_results)")
        columns = {row[1] for row in cursor.fetchall()}
        requires_reclassification = False
        if "content_level" not in columns:
            conn.execute(
                "ALTER TABLE ai_filter_results "
                "ADD COLUMN content_level TEXT DEFAULT 'title_only'"
            )
        if "risk_warning" not in columns:
            conn.execute(
                "ALTER TABLE ai_filter_results "
                "ADD COLUMN risk_warning TEXT DEFAULT ''"
            )
        if "content_excerpt" not in columns:
            conn.execute(
                "ALTER TABLE ai_filter_results "
                "ADD COLUMN content_excerpt TEXT DEFAULT ''"
            )
        if "importance_score" not in columns:
            conn.execute(
                "ALTER TABLE ai_filter_results "
                "ADD COLUMN importance_score REAL DEFAULT 0"
            )
            requires_reclassification = True
        if "ai_summary" not in columns:
            conn.execute(
                "ALTER TABLE ai_filter_results "
                "ADD COLUMN ai_summary TEXT DEFAULT ''"
            )
            requires_reclassification = True

        # 旧分类结果没有逐条摘要和重要性评分。仅在首次升级表结构时清理
        # 分类缓存，使现有新闻在下一轮按新提示词重新分析。
        if requires_reclassification:
            conn.execute("DELETE FROM ai_filter_results")
            conn.execute("DELETE FROM ai_filter_analyzed_news")

    # ========================================
    # 新闻数据存储
    # ========================================

    def _save_news_data_impl(
        self,
        data: NewsData,
        log_prefix: str = "[存储]",
        conn: Optional[sqlite3.Connection] = None,
    ) -> tuple[bool, int, int, int, int]:
        """
        保存新闻数据到 SQLite（核心实现）

        Args:
            data: 新闻数据
            log_prefix: 日志前缀

        Returns:
            (success, new_count, updated_count, title_changed_count, off_list_count)
        """
        try:
            conn = conn or self._get_connection(data.date)
            cursor = conn.cursor()

            # 获取配置时区的当前时间
            now_str = self._get_configured_time().strftime("%Y-%m-%d %H:%M:%S")

            # 首先同步平台信息到 platforms 表
            for source_id, source_name in data.id_to_name.items():
                cursor.execute("""
                    INSERT INTO platforms (id, name, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        updated_at = excluded.updated_at
                """, (source_id, source_name, now_str))

            # 统计计数器
            new_count = 0
            updated_count = 0
            title_changed_count = 0
            success_sources = []

            for source_id, news_list in data.items.items():
                success_sources.append(source_id)

                for item in news_list:
                    try:
                        # 标准化 URL（去除动态参数，如微博的 band_rank）
                        normalized_url = normalize_url(item.url, source_id) if item.url else ""

                        # 检查是否已存在（通过标准化 URL + platform_id）
                        if normalized_url:
                            cursor.execute("""
                                SELECT id, title FROM news_items
                                WHERE url = ? AND platform_id = ?
                            """, (normalized_url, source_id))
                            existing = cursor.fetchone()

                            if existing:
                                # 已存在，更新记录
                                existing_id, existing_title = existing

                                update_title = item.title
                                if (update_title and update_title.strip().startswith(("http://", "https://", "//"))
                                        and existing_title and not existing_title.strip().startswith(("http://", "https://", "//"))):
                                    update_title = existing_title

                                # 检查标题是否变化
                                if existing_title != update_title:
                                    # 记录标题变更
                                    cursor.execute("""
                                        INSERT INTO title_changes
                                        (news_item_id, old_title, new_title, changed_at)
                                        VALUES (?, ?, ?, ?)
                                    """, (existing_id, existing_title, update_title, now_str))
                                    title_changed_count += 1

                                # 记录排名历史
                                cursor.execute("""
                                    INSERT INTO rank_history
                                    (news_item_id, rank, crawl_time, created_at)
                                    VALUES (?, ?, ?, ?)
                                """, (existing_id, item.rank, data.crawl_time, now_str))

                                # 更新现有记录
                                cursor.execute("""
                                    UPDATE news_items SET
                                        title = ?,
                                        rank = ?,
                                        mobile_url = ?,
                                        last_crawl_time = ?,
                                        crawl_count = crawl_count + 1,
                                        updated_at = ?
                                    WHERE id = ?
                                """, (update_title, item.rank, item.mobile_url,
                                      data.crawl_time, now_str, existing_id))
                                updated_count += 1
                            else:
                                # 不存在，插入新记录（存储标准化后的 URL）
                                cursor.execute("""
                                    INSERT INTO news_items
                                    (title, platform_id, rank, url, mobile_url,
                                     first_crawl_time, last_crawl_time, crawl_count,
                                     created_at, updated_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                                """, (item.title, source_id, item.rank, normalized_url,
                                      item.mobile_url, data.crawl_time, data.crawl_time,
                                      now_str, now_str))
                                new_id = cursor.lastrowid
                                # 记录初始排名
                                cursor.execute("""
                                    INSERT INTO rank_history
                                    (news_item_id, rank, crawl_time, created_at)
                                    VALUES (?, ?, ?, ?)
                                """, (new_id, item.rank, data.crawl_time, now_str))
                                new_count += 1
                        else:
                            # URL 为空的情况，直接插入（不做去重）
                            cursor.execute("""
                                INSERT INTO news_items
                                (title, platform_id, rank, url, mobile_url,
                                 first_crawl_time, last_crawl_time, crawl_count,
                                 created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                            """, (item.title, source_id, item.rank, "",
                                  item.mobile_url, data.crawl_time, data.crawl_time,
                                  now_str, now_str))
                            new_id = cursor.lastrowid
                            # 记录初始排名
                            cursor.execute("""
                                INSERT INTO rank_history
                                (news_item_id, rank, crawl_time, created_at)
                                VALUES (?, ?, ?, ?)
                            """, (new_id, item.rank, data.crawl_time, now_str))
                            new_count += 1

                    except sqlite3.Error as e:
                        print(f"{log_prefix} 保存新闻条目失败 [{item.title[:30]}...]: {e}")

            total_items = new_count + updated_count

            # ========================================
            # 脱榜检测：检测上次在榜但这次不在榜的新闻
            # ========================================
            off_list_count = 0

            # 获取上一次抓取时间
            cursor.execute("""
                SELECT crawl_time FROM crawl_records
                WHERE crawl_time < ?
                ORDER BY crawl_time DESC
                LIMIT 1
            """, (data.crawl_time,))
            prev_record = cursor.fetchone()

            if prev_record:
                prev_crawl_time = prev_record[0]

                # 对于每个成功抓取的平台，检测脱榜
                for source_id in success_sources:
                    # 获取当前抓取中该平台的所有标准化 URL
                    current_urls = set()
                    for item in data.items.get(source_id, []):
                        normalized_url = normalize_url(item.url, source_id) if item.url else ""
                        if normalized_url:
                            current_urls.add(normalized_url)

                    # 查询上次在榜（last_crawl_time = prev_crawl_time）但这次不在榜的新闻
                    # 这些新闻是"第一次脱榜"，需要记录
                    cursor.execute("""
                        SELECT id, url FROM news_items
                        WHERE platform_id = ?
                          AND last_crawl_time = ?
                          AND url != ''
                    """, (source_id, prev_crawl_time))

                    for row in cursor.fetchall():
                        news_id, url = row[0], row[1]
                        if url not in current_urls:
                            # 插入脱榜记录（rank=0 表示脱榜）
                            cursor.execute("""
                                INSERT INTO rank_history
                                (news_item_id, rank, crawl_time, created_at)
                                VALUES (?, 0, ?, ?)
                            """, (news_id, data.crawl_time, now_str))
                            off_list_count += 1

            # 记录抓取信息
            cursor.execute("""
                INSERT OR REPLACE INTO crawl_records
                (crawl_time, total_items, created_at)
                VALUES (?, ?, ?)
            """, (data.crawl_time, total_items, now_str))

            # 获取刚插入的 crawl_record 的 ID
            cursor.execute("""
                SELECT id FROM crawl_records WHERE crawl_time = ?
            """, (data.crawl_time,))
            record_row = cursor.fetchone()
            if record_row:
                crawl_record_id = record_row[0]

                # 记录成功的来源
                for source_id in success_sources:
                    cursor.execute("""
                        INSERT OR REPLACE INTO crawl_source_status
                        (crawl_record_id, platform_id, status)
                        VALUES (?, ?, 'success')
                    """, (crawl_record_id, source_id))

                # 记录失败的来源
                for failed_id in data.failed_ids:
                    # 确保失败的平台也在 platforms 表中
                    cursor.execute("""
                        INSERT OR IGNORE INTO platforms (id, name, updated_at)
                        VALUES (?, ?, ?)
                    """, (failed_id, failed_id, now_str))

                    cursor.execute("""
                        INSERT OR REPLACE INTO crawl_source_status
                        (crawl_record_id, platform_id, status)
                        VALUES (?, ?, 'failed')
                    """, (crawl_record_id, failed_id))

            conn.commit()

            return True, new_count, updated_count, title_changed_count, off_list_count

        except Exception as e:
            print(f"{log_prefix} 保存失败: {e}")
            return False, 0, 0, 0, 0

    def _get_today_all_data_impl(self, date: Optional[str] = None) -> Optional[NewsData]:
        """
        获取指定日期的所有新闻数据（合并后）

        Args:
            date: 日期字符串，默认为今天

        Returns:
            合并后的新闻数据
        """
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()

            # 获取所有新闻数据（包含 id 用于查询排名历史）
            cursor.execute("""
                SELECT n.id, n.title, n.platform_id, p.name as platform_name,
                       n.rank, n.url, n.mobile_url,
                       n.first_crawl_time, n.last_crawl_time, n.crawl_count
                FROM news_items n
                LEFT JOIN platforms p ON n.platform_id = p.id
                ORDER BY n.platform_id, n.last_crawl_time
            """)

            rows = cursor.fetchall()
            if not rows:
                return None

            # 收集所有 news_item_id
            news_ids = [row[0] for row in rows]

            # 批量查询排名历史（同时获取时间和排名）
            # 过滤逻辑：只保留 last_crawl_time 之前的脱榜记录（rank=0）
            # 这样可以避免显示新闻永久脱榜后的无意义记录
            rank_history_map: Dict[int, List[int]] = {}
            rank_timeline_map: Dict[int, List[Dict[str, Any]]] = {}
            if news_ids:
                placeholders = ",".join("?" * len(news_ids))
                cursor.execute(f"""
                    SELECT rh.news_item_id, rh.rank, rh.crawl_time
                    FROM rank_history rh
                    JOIN news_items ni ON rh.news_item_id = ni.id
                    WHERE rh.news_item_id IN ({placeholders})
                      AND NOT (rh.rank = 0 AND rh.crawl_time > ni.last_crawl_time)
                    ORDER BY rh.news_item_id, rh.crawl_time
                """, news_ids)
                for rh_row in cursor.fetchall():
                    news_id, rank, crawl_time = rh_row[0], rh_row[1], rh_row[2]

                    if not crawl_time:
                        continue

                    # 构建 ranks 列表（去重，排除脱榜记录 rank=0）
                    if news_id not in rank_history_map:
                        rank_history_map[news_id] = []
                    if rank != 0 and rank not in rank_history_map[news_id]:
                        rank_history_map[news_id].append(rank)

                    # 构建 rank_timeline 列表（完整时间线，包含脱榜）
                    if news_id not in rank_timeline_map:
                        rank_timeline_map[news_id] = []
                    # 提取时间部分（HH:MM）
                    try:
                        time_part = crawl_time.split()[1][:5] if ' ' in crawl_time else crawl_time[:5]
                    except (IndexError, AttributeError):
                        time_part = "??:??"
                    rank_timeline_map[news_id].append({
                        "time": time_part,
                        "rank": rank if rank != 0 else None  # 0 转为 None 表示脱榜
                    })

            # 按 platform_id 分组
            items: Dict[str, List[NewsItem]] = {}
            id_to_name: Dict[str, str] = {}
            crawl_date = self._format_date_folder(date)

            for row in rows:
                news_id = row[0]
                platform_id = row[2]
                title = row[1]
                platform_name = row[3] or platform_id

                id_to_name[platform_id] = platform_name

                if platform_id not in items:
                    items[platform_id] = []

                # 获取排名历史，如果没有则使用当前排名
                ranks = rank_history_map.get(news_id, [row[4]])
                rank_timeline = rank_timeline_map.get(news_id, [])

                items[platform_id].append(NewsItem(
                    title=title,
                    source_id=platform_id,
                    source_name=platform_name,
                    rank=row[4],
                    url=row[5] or "",
                    mobile_url=row[6] or "",
                    crawl_time=row[8],  # last_crawl_time
                    ranks=ranks,
                    first_time=row[7],  # first_crawl_time
                    last_time=row[8],   # last_crawl_time
                    count=row[9],       # crawl_count
                    rank_timeline=rank_timeline,
                ))

            final_items = items

            # 获取失败的来源
            cursor.execute("""
                SELECT DISTINCT css.platform_id
                FROM crawl_source_status css
                JOIN crawl_records cr ON css.crawl_record_id = cr.id
                WHERE css.status = 'failed'
            """)
            failed_ids = [row[0] for row in cursor.fetchall()]

            # 获取最新的抓取时间
            cursor.execute("""
                SELECT crawl_time FROM crawl_records
                ORDER BY crawl_time DESC
                LIMIT 1
            """)

            time_row = cursor.fetchone()
            crawl_time = time_row[0] if time_row else self._format_time_filename()

            return NewsData(
                date=crawl_date,
                crawl_time=crawl_time,
                items=final_items,
                id_to_name=id_to_name,
                failed_ids=failed_ids,
            )

        except Exception as e:
            print(f"[存储] 读取数据失败: {e}")
            return None

    def _get_latest_crawl_data_impl(self, date: Optional[str] = None) -> Optional[NewsData]:
        """
        获取最新一次抓取的数据

        Args:
            date: 日期字符串，默认为今天

        Returns:
            最新抓取的新闻数据
        """
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()

            # 获取最新的抓取时间
            cursor.execute("""
                SELECT crawl_time FROM crawl_records
                ORDER BY crawl_time DESC
                LIMIT 1
            """)

            time_row = cursor.fetchone()
            if not time_row:
                return None

            latest_time = time_row[0]

            # 获取该时间的新闻数据（包含 id 用于查询排名历史）
            cursor.execute("""
                SELECT n.id, n.title, n.platform_id, p.name as platform_name,
                       n.rank, n.url, n.mobile_url,
                       n.first_crawl_time, n.last_crawl_time, n.crawl_count
                FROM news_items n
                LEFT JOIN platforms p ON n.platform_id = p.id
                WHERE n.last_crawl_time = ?
            """, (latest_time,))

            rows = cursor.fetchall()
            if not rows:
                return None

            # 收集所有 news_item_id
            news_ids = [row[0] for row in rows]

            # 批量查询排名历史（同时获取时间和排名）
            # 过滤逻辑：只保留 last_crawl_time 之前的脱榜记录（rank=0）
            # 这样可以避免显示新闻永久脱榜后的无意义记录
            rank_history_map: Dict[int, List[int]] = {}
            rank_timeline_map: Dict[int, List[Dict[str, Any]]] = {}
            if news_ids:
                placeholders = ",".join("?" * len(news_ids))
                cursor.execute(f"""
                    SELECT rh.news_item_id, rh.rank, rh.crawl_time
                    FROM rank_history rh
                    JOIN news_items ni ON rh.news_item_id = ni.id
                    WHERE rh.news_item_id IN ({placeholders})
                      AND NOT (rh.rank = 0 AND rh.crawl_time > ni.last_crawl_time)
                    ORDER BY rh.news_item_id, rh.crawl_time
                """, news_ids)
                for rh_row in cursor.fetchall():
                    news_id, rank, crawl_time = rh_row[0], rh_row[1], rh_row[2]

                    if not crawl_time:
                        continue

                    # 构建 ranks 列表（去重，排除脱榜记录 rank=0）
                    if news_id not in rank_history_map:
                        rank_history_map[news_id] = []
                    if rank != 0 and rank not in rank_history_map[news_id]:
                        rank_history_map[news_id].append(rank)

                    # 构建 rank_timeline 列表（完整时间线，包含脱榜）
                    if news_id not in rank_timeline_map:
                        rank_timeline_map[news_id] = []
                    # 提取时间部分（HH:MM）
                    try:
                        time_part = crawl_time.split()[1][:5] if ' ' in crawl_time else crawl_time[:5]
                    except (IndexError, AttributeError):
                        time_part = "??:??"
                    rank_timeline_map[news_id].append({
                        "time": time_part,
                        "rank": rank if rank != 0 else None  # 0 转为 None 表示脱榜
                    })

            items: Dict[str, List[NewsItem]] = {}
            id_to_name: Dict[str, str] = {}
            crawl_date = self._format_date_folder(date)

            for row in rows:
                news_id = row[0]
                platform_id = row[2]
                platform_name = row[3] or platform_id
                id_to_name[platform_id] = platform_name

                if platform_id not in items:
                    items[platform_id] = []

                # 获取排名历史，如果没有则使用当前排名
                ranks = rank_history_map.get(news_id, [row[4]])
                rank_timeline = rank_timeline_map.get(news_id, [])

                items[platform_id].append(NewsItem(
                    title=row[1],
                    source_id=platform_id,
                    source_name=platform_name,
                    rank=row[4],
                    url=row[5] or "",
                    mobile_url=row[6] or "",
                    crawl_time=row[8],  # last_crawl_time
                    ranks=ranks,
                    first_time=row[7],  # first_crawl_time
                    last_time=row[8],   # last_crawl_time
                    count=row[9],       # crawl_count
                    rank_timeline=rank_timeline,
                ))

            # 获取失败的来源（针对最新一次抓取）
            cursor.execute("""
                SELECT css.platform_id
                FROM crawl_source_status css
                JOIN crawl_records cr ON css.crawl_record_id = cr.id
                WHERE cr.crawl_time = ? AND css.status = 'failed'
            """, (latest_time,))

            failed_ids = [row[0] for row in cursor.fetchall()]

            return NewsData(
                date=crawl_date,
                crawl_time=latest_time,
                items=items,
                id_to_name=id_to_name,
                failed_ids=failed_ids,
            )

        except Exception as e:
            print(f"[存储] 获取最新数据失败: {e}")
            return None

    def _detect_new_titles_impl(self, current_data: NewsData) -> Dict[str, Dict]:
        """
        检测新增的标题

        该方法比较当前抓取数据与历史数据，找出新增的标题。
        关键逻辑：只有在历史批次中从未出现过的标题才算新增。

        Args:
            current_data: 当前抓取的数据

        Returns:
            新增的标题数据 {source_id: {title: NewsItem}}
        """
        try:
            # 获取历史数据
            historical_data = self._get_today_all_data_impl(current_data.date)

            if not historical_data:
                # 没有历史数据，所有都是新的
                new_titles = {}
                for source_id, news_list in current_data.items.items():
                    new_titles[source_id] = {item.title: item for item in news_list}
                return new_titles

            # 获取当前批次时间
            current_time = current_data.crawl_time

            # 收集历史标题（first_time < current_time 的标题）
            # 这样可以正确处理同一标题因 URL 变化而产生多条记录的情况
            historical_titles: Dict[str, set] = {}
            for source_id, news_list in historical_data.items.items():
                historical_titles[source_id] = set()
                for item in news_list:
                    first_time = item.first_time or item.crawl_time
                    if first_time < current_time:
                        historical_titles[source_id].add(item.title)

            # 检查是否有历史数据
            has_historical_data = any(len(titles) > 0 for titles in historical_titles.values())
            if not has_historical_data:
                # 第一次抓取，没有"新增"概念
                return {}

            # 检测新增
            new_titles = {}
            for source_id, news_list in current_data.items.items():
                hist_set = historical_titles.get(source_id, set())
                for item in news_list:
                    if item.title not in hist_set:
                        if source_id not in new_titles:
                            new_titles[source_id] = {}
                        new_titles[source_id][item.title] = item

            return new_titles

        except Exception as e:
            print(f"[存储] 检测新标题失败: {e}")
            return {}

    def _is_first_crawl_today_impl(self, date: Optional[str] = None) -> bool:
        """
        检查是否是当天第一次抓取

        Args:
            date: 日期字符串，默认为今天

        Returns:
            是否是第一次抓取
        """
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT COUNT(*) as count FROM crawl_records
            """)

            row = cursor.fetchone()
            count = row[0] if row else 0

            # 如果只有一条或没有记录，视为第一次抓取
            return count <= 1

        except Exception as e:
            print(f"[存储] 检查首次抓取失败: {e}")
            return True

    def _get_crawl_times_impl(self, date: Optional[str] = None) -> List[str]:
        """
        获取指定日期的所有抓取时间列表

        Args:
            date: 日期字符串，默认为今天

        Returns:
            抓取时间列表（按时间排序）
        """
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT crawl_time FROM crawl_records
                ORDER BY crawl_time
            """)

            rows = cursor.fetchall()
            return [row[0] for row in rows]

        except Exception as e:
            print(f"[存储] 获取抓取时间列表失败: {e}")
            return []

    # ========================================
    # 时间段执行记录（调度系统）
    # ========================================

    def _has_period_executed_impl(
        self,
        date_str: str,
        period_key: str,
        action: str,
        strict_read: bool = False,
    ) -> bool:
        """
        检查指定时间段的某个 action 今天是否已执行

        Args:
            date_str: 日期字符串 YYYY-MM-DD
            period_key: 时间段 key
            action: 动作类型 (analyze / push)

        Returns:
            是否已执行
        """
        try:
            conn = (
                self._get_ai_connection(date_str, strict=True)
                if strict_read
                else self._get_connection(date_str)
            )
            cursor = conn.cursor()

            # 先检查表是否存在
            cursor.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='period_executions'
            """)
            if not cursor.fetchone():
                return False

            cursor.execute("""
                SELECT 1 FROM period_executions
                WHERE execution_date = ? AND period_key = ? AND action = ?
            """, (date_str, period_key, action))

            return cursor.fetchone() is not None

        except Exception as e:
            if strict_read:
                raise
            print(f"[存储] 检查时间段执行记录失败: {e}")
            return False

    def _get_period_execution_at_impl(
        self, date_str: str, period_key: str, action: str, strict_read: bool = False
    ) -> Optional[str]:
        """返回指定数据库中周期执行记录的最近成功时间。"""
        try:
            if strict_read:
                conn = self._get_ai_connection(date_str, strict=True)
            else:
                conn = self._get_connection(date_str)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='period_executions'"
            )
            if not cursor.fetchone():
                return None
            cursor.execute(
                "SELECT executed_at FROM period_executions "
                "WHERE execution_date = ? AND period_key = ? AND action = ? "
                "ORDER BY executed_at DESC LIMIT 1",
                (date_str, period_key, action),
            )
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception as exc:
            raise RuntimeError(
                f"读取周期执行时间失败: {date_str}/{period_key}/{action}: {exc}"
            ) from exc

    def _record_period_execution_impl(
        self,
        date_str: str,
        period_key: str,
        action: str,
        conn: Optional[sqlite3.Connection] = None,
    ) -> bool:
        """
        记录时间段的 action 执行

        Args:
            date_str: 日期字符串 YYYY-MM-DD
            period_key: 时间段 key
            action: 动作类型 (analyze / push)

        Returns:
            是否记录成功
        """
        try:
            conn = conn or self._get_connection(date_str)
            cursor = conn.cursor()

            # 确保表存在
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS period_executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_date TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(execution_date, period_key, action)
                )
            """)

            now_str = self._get_configured_time().strftime("%Y-%m-%d %H:%M:%S")

            cursor.execute("""
                INSERT OR IGNORE INTO period_executions (execution_date, period_key, action, executed_at)
                VALUES (?, ?, ?, ?)
            """, (date_str, period_key, action, now_str))

            conn.commit()
            return True

        except Exception as e:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            print(f"[存储] 记录时间段执行失败: {e}")
            return False

    # ========================================
    # RSS 数据存储
    # ========================================

    def _save_rss_data_impl(self, data: RSSData, log_prefix: str = "[存储]") -> tuple[bool, int, int]:
        """
        保存 RSS 数据到 SQLite（以 URL 为唯一标识）

        Args:
            data: RSS 数据
            log_prefix: 日志前缀

        Returns:
            (success, new_count, updated_count)
        """
        conn = None
        try:
            conn = self._get_connection(data.date, db_type="rss")
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()

            now_str = self._get_configured_time().strftime("%Y-%m-%d %H:%M:%S")

            row = cursor.execute(
                """SELECT value FROM rss_storage_metadata
                   WHERE key = 'generation'"""
            ).fetchone()
            generation = (int(row[0]) if row else 0) + 1
            cursor.execute(
                """INSERT INTO rss_storage_metadata(key, value)
                   VALUES ('generation', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (str(generation),),
            )

            cursor.execute(
                """INSERT INTO rss_crawl_records
                       (crawl_time, total_items, created_at)
                   VALUES (?, 0, ?)
                   ON CONFLICT(crawl_time) DO UPDATE SET
                       total_items = 0,
                       created_at = excluded.created_at""",
                (data.crawl_time, now_str),
            )
            crawl_record_id = cursor.execute(
                "SELECT id FROM rss_crawl_records WHERE crawl_time = ?",
                (data.crawl_time,),
            ).fetchone()[0]

            # 同步 RSS 源信息到 rss_feeds 表
            for feed_id, feed_name in data.id_to_name.items():
                cursor.execute("""
                    INSERT INTO rss_feeds (id, name, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        updated_at = excluded.updated_at
                """, (feed_id, feed_name, now_str))

            # 统计计数器
            new_count = 0
            updated_count = 0

            for feed_id, rss_list in data.items.items():
                for item in rss_list:
                    item_guid = getattr(item, "guid", "") or ""
                    if not item.url and not item_guid:
                        item_guid = self._stable_rss_title_guid(
                            item.title, feed_id
                        )
                    if not item.url and not item_guid:
                        raise ValueError(
                            f"RSS 条目缺少稳定身份: {item.title!r}"
                        )
                    existing = None

                    if item_guid:
                        existing = cursor.execute(
                            """SELECT id, title FROM rss_items
                               WHERE guid = ? AND feed_id = ?""",
                            (item_guid, feed_id),
                        ).fetchone()
                    if not existing and item.url:
                        existing = cursor.execute(
                            """SELECT id, title FROM rss_items
                               WHERE url = ? AND feed_id = ?""",
                            (item.url, feed_id),
                        ).fetchone()

                    if existing:
                        item_id, existing_title = existing
                        update_title = item.title
                        if (
                            update_title
                            and update_title.strip().startswith(
                                ("http://", "https://", "//")
                            )
                            and existing_title
                            and not existing_title.strip().startswith(
                                ("http://", "https://", "//")
                            )
                        ):
                            update_title = existing_title
                        cursor.execute("""
                            UPDATE rss_items SET
                                title = ?,
                                url = CASE WHEN ? != '' THEN ? ELSE url END,
                                guid = CASE WHEN ? != '' THEN ? ELSE guid END,
                                published_at = ?, summary = ?, author = ?,
                                source_count = ?, pre_hot_score = ?,
                                search_topic = ?, search_providers = ?,
                                last_crawl_time = ?,
                                crawl_count = crawl_count + 1,
                                updated_at = ?
                            WHERE id = ?
                        """, (
                            update_title, item.url, item.url,
                            item_guid, item_guid, item.published_at,
                            item.summary, item.author, item.source_count,
                            item.pre_hot_score, item.search_topic,
                            item.search_providers, data.crawl_time,
                            now_str, item_id,
                        ))
                        updated_count += 1
                    else:
                        cursor.execute("""
                            INSERT INTO rss_items
                                (title, feed_id, url, guid, published_at,
                                 summary, author, first_crawl_time,
                                 last_crawl_time, crawl_count, source_count,
                                 pre_hot_score, search_topic, search_providers,
                                 created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                                    ?, ?, ?, ?, ?, ?)
                        """, (
                            item.title, feed_id, item.url, item_guid,
                            item.published_at, item.summary, item.author,
                            item.first_time or item.crawl_time or data.crawl_time,
                            data.crawl_time, item.source_count,
                            item.pre_hot_score, item.search_topic,
                            item.search_providers, now_str, now_str,
                        ))
                        item_id = cursor.lastrowid
                        new_count += 1

                    stored = cursor.execute(
                        """SELECT title, feed_id, url, first_crawl_time,
                                  last_crawl_time
                           FROM rss_items WHERE id = ?""",
                        (item_id,),
                    ).fetchone()
                    identity = self._rss_identity_for_ledger(
                        stored[0], stored[1], stored[2]
                    )
                    if not identity:
                        raise ValueError(
                            f"持久化 RSS 条目缺少 canonical identity: {item_id}"
                        )
                    cursor.execute(
                        """INSERT OR REPLACE INTO rss_first_seen_outbox
                               (write_id, identity_key, first_seen,
                                storage_date, crawl_record_id,
                                source_generation, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            f"{data.date}:{generation}:{item_id}",
                            self._rss_identity_key(identity),
                            stored[3] or stored[4] or data.crawl_time,
                            data.date, crawl_record_id, generation, now_str,
                        ),
                    )

            total_items = new_count + updated_count

            cursor.execute(
                """UPDATE rss_crawl_records SET total_items = ?
                   WHERE id = ?""",
                (total_items, crawl_record_id),
            )

            for feed_id in data.items.keys():
                cursor.execute("""
                    INSERT OR REPLACE INTO rss_crawl_status
                        (crawl_record_id, feed_id, status)
                    VALUES (?, ?, 'success')
                """, (crawl_record_id, feed_id))

            for failed_id in data.failed_ids:
                cursor.execute("""
                    INSERT OR IGNORE INTO rss_feeds (id, name, updated_at)
                    VALUES (?, ?, ?)
                """, (failed_id, failed_id, now_str))
                cursor.execute("""
                    INSERT OR REPLACE INTO rss_crawl_status
                        (crawl_record_id, feed_id, status)
                    VALUES (?, ?, 'failed')
                """, (crawl_record_id, failed_id))

            conn.commit()

            return True, new_count, updated_count

        except Exception as e:
            if conn is not None:
                conn.rollback()
            print(f"{log_prefix} 保存 RSS 数据失败: {e}")
            return False, 0, 0

    @staticmethod
    def _read_latest_rss_feed_statuses(cursor) -> Dict[str, str]:
        """读取当前日库中每个 RSS 源最新一次抓取状态。"""
        cursor.execute("""
            SELECT cs.feed_id, cs.status
            FROM rss_crawl_status cs
            JOIN rss_crawl_records cr ON cs.crawl_record_id = cr.id
            WHERE cr.id = (
                  SELECT MAX(cr2.id)
                  FROM rss_crawl_status cs2
                  JOIN rss_crawl_records cr2
                    ON cs2.crawl_record_id = cr2.id
                  WHERE cs2.feed_id = cs.feed_id
              )
            ORDER BY cs.feed_id
        """)
        return dict(cursor.fetchall())

    @classmethod
    def _get_latest_failed_rss_ids(cls, cursor) -> List[str]:
        """返回每个 RSS 源最新一次抓取仍为失败的源 ID。"""
        return [
            feed_id
            for feed_id, status in cls._read_latest_rss_feed_statuses(
                cursor
            ).items()
            if status == "failed"
        ]

    def _get_rss_feed_statuses_impl(
        self, date: Optional[str] = None, strict: bool = False
    ) -> Dict[str, str]:
        """返回指定日库中每个 RSS 源按记录 ID 判定的最新状态。"""
        conn = self._get_rss_connection(date, strict=strict)
        return self._read_latest_rss_feed_statuses(conn.cursor())

    def _get_rss_data_impl(
        self, date: Optional[str] = None, strict: bool = False
    ) -> Optional[RSSData]:
        """
        获取指定日期的所有 RSS 数据

        Args:
            date: 日期字符串（YYYY-MM-DD），默认为今天

        Returns:
            RSSData 对象；没有抓取记录返回 None；全源失败且零条目时返回空 RSSData；
            成功空抓取且零条目时也返回空 RSSData
        """
        try:
            conn = self._get_rss_connection(date, strict=strict)
            cursor = conn.cursor()

            # 获取所有 RSS 数据
            cursor.execute("""
                SELECT i.id, i.title, i.feed_id, f.name as feed_name,
                       i.url, i.published_at, i.summary, i.author,
                       i.first_crawl_time, i.last_crawl_time, i.crawl_count,
                       i.source_count, i.pre_hot_score, i.search_topic,
                       i.search_providers, i.guid
                FROM rss_items i
                LEFT JOIN rss_feeds f ON i.feed_id = f.id
                ORDER BY i.published_at DESC
            """)

            items: Dict[str, List[RSSItem]] = {}
            id_to_name: Dict[str, str] = {}
            crawl_date = self._format_date_folder(date)
            rows = cursor.fetchall()
            if not rows:
                cursor.execute("""
                    SELECT crawl_time FROM rss_crawl_records
                    ORDER BY id DESC
                    LIMIT 1
                """)
                time_row = cursor.fetchone()
                if not time_row:
                    return None

                cursor.execute("""
                    SELECT id, name FROM rss_feeds
                    ORDER BY id
                """)
                id_to_name = {
                    row[0]: row[1] or row[0]
                    for row in cursor.fetchall()
                }

                failed_ids = self._get_latest_failed_rss_ids(cursor)
                return RSSData(
                    date=crawl_date,
                    crawl_time=time_row[0],
                    items=items,
                    id_to_name=id_to_name,
                    failed_ids=failed_ids,
                )

            for row in rows:
                feed_id = row[2]
                feed_name = row[3] or feed_id

                id_to_name[feed_id] = feed_name

                if feed_id not in items:
                    items[feed_id] = []

                items[feed_id].append(RSSItem(
                    title=row[1],
                    feed_id=feed_id,
                    feed_name=feed_name,
                    url=row[4] or "",
                    guid=row[15] or "",
                    published_at=row[5] or "",
                    summary=row[6] or "",
                    author=row[7] or "",
                    crawl_time=row[9],
                    first_time=row[8],
                    last_time=row[9],
                    count=row[10],
                    source_count=row[11] if row[11] is not None else 1,
                    pre_hot_score=row[12] if row[12] is not None else 0.0,
                    search_topic=row[13] or "",
                    search_providers=row[14] or "",
                ))

            # 获取最新的抓取时间
            cursor.execute("""
                SELECT crawl_time FROM rss_crawl_records
                ORDER BY id DESC
                LIMIT 1
            """)
            time_row = cursor.fetchone()
            crawl_time = time_row[0] if time_row else self._format_time_filename()

            # 获取失败的源
            failed_ids = self._get_latest_failed_rss_ids(cursor)

            return RSSData(
                date=crawl_date,
                crawl_time=crawl_time,
                items=items,
                id_to_name=id_to_name,
                failed_ids=failed_ids,
            )

        except Exception as e:
            if strict:
                raise
            print(f"[存储] 读取 RSS 数据失败: {e}")
            return None

    def _detect_new_rss_items_impl(self, current_data: RSSData) -> Dict[str, List[RSSItem]]:
        """
        检测新增的 RSS 条目（增量模式）

        该方法比较当前抓取数据与历史数据，找出新增的 RSS 条目。
        关键逻辑：只有在历史批次中从未出现过的 URL 才算新增。

        Args:
            current_data: 当前抓取的 RSS 数据

        Returns:
            新增的 RSS 条目 {feed_id: [RSSItem, ...]}
        """
        try:
            # 获取历史数据
            historical_data = self._get_rss_data_impl(current_data.date)

            if not historical_data:
                # 没有历史数据，所有都是新的
                return current_data.items.copy()

            # 获取当前批次时间
            current_time = current_data.crawl_time
            current_datetime = parse_storage_datetime(
                current_time, current_data.date, self.timezone
            )

            # 收集历史 URL（first_time < current_time 的条目）
            historical_urls: Dict[str, set] = {}
            for feed_id, rss_list in historical_data.items.items():
                historical_urls[feed_id] = set()
                for item in rss_list:
                    first_time = item.first_time or item.crawl_time
                    first_datetime = parse_storage_datetime(
                        first_time, current_data.date, self.timezone
                    )
                    if first_datetime is not None and current_datetime is not None:
                        is_historical = first_datetime < current_datetime
                    else:
                        # 非法旧值保持原有 fail-soft 行为；可解析的混合格式绝不做字符串比较。
                        is_historical = first_time < current_time
                    if is_historical:
                        if item.url:
                            historical_urls[feed_id].add(item.url)

            # 检查是否有早于当前批次的历史数据
            has_historical_data = any(len(urls) > 0 for urls in historical_urls.values())
            if not has_historical_data:
                # 当天第一次抓取，所有条目都是新增
                return current_data.items.copy()

            # 检测新增
            new_items: Dict[str, List[RSSItem]] = {}
            for feed_id, rss_list in current_data.items.items():
                hist_set = historical_urls.get(feed_id, set())
                for item in rss_list:
                    # 通过 URL 判断是否新增
                    if item.url and item.url not in hist_set:
                        if feed_id not in new_items:
                            new_items[feed_id] = []
                        new_items[feed_id].append(item)

            return new_items

        except Exception as e:
            print(f"[存储] 检测新 RSS 条目失败: {e}")
            return {}

    def _get_latest_rss_data_impl(self, date: Optional[str] = None) -> Optional[RSSData]:
        """
        获取最新一次抓取的 RSS 数据（当前榜单模式）

        Args:
            date: 日期字符串（YYYY-MM-DD），默认为今天

        Returns:
            最新抓取的 RSS 数据，如果没有数据返回 None
        """
        try:
            conn = self._get_connection(date, db_type="rss")
            cursor = conn.cursor()

            # 获取最新的抓取时间
            cursor.execute("""
                SELECT id, crawl_time FROM rss_crawl_records
                ORDER BY id DESC
                LIMIT 1
            """)

            time_row = cursor.fetchone()
            if not time_row:
                return None

            latest_record_id = time_row[0]
            latest_time = time_row[1]

            # 获取该时间的 RSS 数据
            cursor.execute("""
                SELECT i.id, i.title, i.feed_id, f.name as feed_name,
                       i.url, i.published_at, i.summary, i.author,
                       i.first_crawl_time, i.last_crawl_time, i.crawl_count,
                       i.source_count, i.pre_hot_score, i.search_topic,
                       i.search_providers, i.guid
                FROM rss_items i
                LEFT JOIN rss_feeds f ON i.feed_id = f.id
                WHERE i.last_crawl_time = ?
                ORDER BY i.published_at DESC
            """, (latest_time,))

            rows = cursor.fetchall()
            if not rows:
                return None

            items: Dict[str, List[RSSItem]] = {}
            id_to_name: Dict[str, str] = {}
            crawl_date = self._format_date_folder(date)

            for row in rows:
                feed_id = row[2]
                feed_name = row[3] or feed_id

                id_to_name[feed_id] = feed_name

                if feed_id not in items:
                    items[feed_id] = []

                items[feed_id].append(RSSItem(
                    title=row[1],
                    feed_id=feed_id,
                    feed_name=feed_name,
                    url=row[4] or "",
                    guid=row[15] or "",
                    published_at=row[5] or "",
                    summary=row[6] or "",
                    author=row[7] or "",
                    crawl_time=row[9],
                    first_time=row[8],
                    last_time=row[9],
                    count=row[10],
                    source_count=row[11] if row[11] is not None else 1,
                    pre_hot_score=row[12] if row[12] is not None else 0.0,
                    search_topic=row[13] or "",
                    search_providers=row[14] or "",
                ))

            # 获取失败的源（针对最新一次抓取）
            cursor.execute("""
                SELECT cs.feed_id
                FROM rss_crawl_status cs
                WHERE cs.crawl_record_id = ? AND cs.status = 'failed'
            """, (latest_record_id,))

            failed_ids = [row[0] for row in cursor.fetchall()]

            return RSSData(
                date=crawl_date,
                crawl_time=latest_time,
                items=items,
                id_to_name=id_to_name,
                failed_ids=failed_ids,
            )

        except Exception as e:
            print(f"[存储] 获取最新 RSS 数据失败: {e}")
            return None

    # ========================================
    # AI 智能筛选 - 标签管理
    # ========================================

    def _tag_snapshot_from_connection(
        self, conn: sqlite3.Connection, interests_file: str
    ) -> Dict[str, Any]:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, tag, description, version, prompt_hash, priority
            FROM ai_filter_tags
            WHERE status = 'active' AND interests_file = ?
            ORDER BY priority ASC, id ASC
        """, (interests_file,))
        tags = [
            {
                "id": row[0], "tag": row[1], "description": row[2],
                "version": row[3], "prompt_hash": row[4],
                "priority": row[5],
            }
            for row in cursor.fetchall()
        ]
        hashes = {tag["prompt_hash"] for tag in tags}
        versions = {tag["version"] for tag in tags}
        if len(hashes) > 1 or len(versions) > 1:
            raise RuntimeError("严格 AI 标签快照存在混合 hash/version")
        if len({tag["tag"] for tag in tags}) != len(tags):
            raise RuntimeError("严格 AI 标签快照存在重复 active 标签")
        cursor.execute("SELECT MAX(version) FROM ai_filter_tags")
        row = cursor.fetchone()
        latest_version = row[0] if row and row[0] is not None else 0
        return {
            "tags": tags,
            "prompt_hash": next(iter(hashes), None),
            "version": next(iter(versions), 0),
            "latest_version": latest_version,
        }

    def _get_ai_filter_tag_snapshot_strict_impl(
        self,
        date: Optional[str] = None,
        interests_file: str = "ai_interests.txt",
    ) -> Dict[str, Any]:
        conn = self._get_ai_connection(date, strict=True)
        return self._tag_snapshot_from_connection(conn, interests_file)

    def _replace_ai_filter_tags_strict_impl(
        self,
        date: Optional[str],
        tags: List[Dict],
        version: int,
        prompt_hash: str,
        interests_file: str = "ai_interests.txt",
        conn: Optional[sqlite3.Connection] = None,
    ) -> Dict[str, Any]:
        if not tags:
            raise RuntimeError("严格 AI 标签替换不得保存空标签集")
        normalized = []
        for index, tag_data in enumerate(tags, start=1):
            tag = str(tag_data.get("tag", "")).strip()
            if not tag:
                raise RuntimeError("严格 AI 标签名称为空")
            try:
                priority = int(tag_data.get("priority", index))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("严格 AI 标签 priority 无效") from exc
            normalized.append({
                "tag": tag,
                "description": str(tag_data.get("description", "")).strip(),
                "priority": priority,
            })
        if len({item["tag"] for item in normalized}) != len(normalized):
            raise RuntimeError("严格 AI 标签替换包含重复标签")
        if len({item["priority"] for item in normalized}) != len(normalized):
            raise RuntimeError("严格 AI 标签替换包含重复 priority")

        conn = conn or self._get_ai_connection(date, strict=True)
        now_str = self._get_configured_time().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            cursor.execute(
                """SELECT id FROM ai_filter_tags
                   WHERE status = 'active' AND interests_file = ?""",
                (interests_file,),
            )
            old_ids = [row[0] for row in cursor.fetchall()]
            if old_ids:
                placeholders = ",".join("?" for _ in old_ids)
                cursor.execute(
                    f"""UPDATE ai_filter_results
                        SET status = 'deprecated', deprecated_at = ?
                        WHERE status = 'active'
                          AND tag_id IN ({placeholders})""",
                    [now_str] + old_ids,
                )
            cursor.execute(
                """UPDATE ai_filter_tags
                   SET status = 'deprecated', deprecated_at = ?
                   WHERE status = 'active' AND interests_file = ?""",
                (now_str, interests_file),
            )
            cursor.execute(
                "DELETE FROM ai_filter_analyzed_news WHERE interests_file = ?",
                (interests_file,),
            )
            for item in normalized:
                cursor.execute("""
                    INSERT INTO ai_filter_tags
                        (tag, description, priority, version, prompt_hash,
                         interests_file, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    item["tag"], item["description"], item["priority"],
                    version, prompt_hash, interests_file, now_str,
                ))
            snapshot = self._tag_snapshot_from_connection(
                conn, interests_file
            )
            conn.commit()
            return snapshot
        except Exception:
            conn.rollback()
            raise

    def _get_active_tags_impl(self, date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> List[Dict[str, Any]]:
        """获取指定兴趣文件的 active 标签列表"""
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, tag, description, version, prompt_hash, priority
                FROM ai_filter_tags
                WHERE status = 'active' AND interests_file = ?
                ORDER BY priority ASC, id ASC
            """, (interests_file,))

            return [
                {
                    "id": row[0], "tag": row[1], "description": row[2],
                    "version": row[3], "prompt_hash": row[4], "priority": row[5],
                }
                for row in cursor.fetchall()
            ]
        except Exception as e:
            print(f"[AI筛选] 获取标签失败: {e}")
            return []

    def _get_latest_prompt_hash_impl(self, date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> Optional[str]:
        """获取指定兴趣文件最新版本标签的 prompt_hash"""
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT prompt_hash FROM ai_filter_tags
                WHERE status = 'active' AND interests_file = ?
                ORDER BY version DESC
                LIMIT 1
            """, (interests_file,))
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception as e:
            print(f"[AI筛选] 获取 prompt_hash 失败: {e}")
            return None

    def _get_latest_tag_version_impl(self, date: Optional[str] = None) -> int:
        """获取最新版本号"""
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT MAX(version) FROM ai_filter_tags
            """)
            row = cursor.fetchone()
            return row[0] if row and row[0] is not None else 0
        except Exception as e:
            print(f"[AI筛选] 获取版本号失败: {e}")
            return 0

    def _deprecate_all_tags_impl(self, date: Optional[str] = None, interests_file: str = "ai_interests.txt") -> int:
        """将指定兴趣文件的 active 标签和关联的分类结果标记为 deprecated"""
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()
            now_str = self._get_configured_time().strftime("%Y-%m-%d %H:%M:%S")

            # 获取该兴趣文件的 active 标签 id
            cursor.execute(
                "SELECT id FROM ai_filter_tags WHERE status = 'active' AND interests_file = ?",
                (interests_file,)
            )
            tag_ids = [row[0] for row in cursor.fetchall()]

            if not tag_ids:
                return 0

            # 废弃标签
            placeholders = ",".join("?" * len(tag_ids))
            cursor.execute(f"""
                UPDATE ai_filter_tags
                SET status = 'deprecated', deprecated_at = ?
                WHERE id IN ({placeholders})
            """, [now_str] + tag_ids)
            tag_count = cursor.rowcount

            # 废弃关联的分类结果
            placeholders = ",".join("?" * len(tag_ids))
            cursor.execute(f"""
                UPDATE ai_filter_results
                SET status = 'deprecated', deprecated_at = ?
                WHERE tag_id IN ({placeholders}) AND status = 'active'
            """, [now_str] + tag_ids)

            conn.commit()
            print(f"[AI筛选] 已废弃 {tag_count} 个标签及关联分类结果")
            return tag_count
        except Exception as e:
            print(f"[AI筛选] 废弃标签失败: {e}")
            return 0

    def _save_tags_impl(
        self, date: Optional[str], tags: List[Dict], version: int, prompt_hash: str,
        interests_file: str = "ai_interests.txt"
    ) -> int:
        """保存新提取的标签"""
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()
            now_str = self._get_configured_time().strftime("%Y-%m-%d %H:%M:%S")

            count = 0
            for idx, tag_data in enumerate(tags, start=1):
                priority = tag_data.get("priority", idx)
                try:
                    priority = int(priority)
                except (TypeError, ValueError):
                    priority = idx
                cursor.execute("""
                    INSERT INTO ai_filter_tags
                    (tag, description, priority, version, prompt_hash, interests_file, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    tag_data["tag"],
                    tag_data.get("description", ""),
                    priority,
                    version,
                    prompt_hash,
                    interests_file,
                    now_str,
                ))
                count += 1

            conn.commit()
            return count
        except Exception as e:
            print(f"[AI筛选] 保存标签失败: {e}")
            return 0

    def _deprecate_specific_tags_impl(
        self, date: Optional[str], tag_ids: List[int]
    ) -> int:
        """废弃指定 ID 的标签及其关联分类结果（增量更新时使用）"""
        if not tag_ids:
            return 0
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()
            now_str = self._get_configured_time().strftime("%Y-%m-%d %H:%M:%S")

            placeholders = ",".join("?" * len(tag_ids))

            cursor.execute(f"""
                UPDATE ai_filter_tags
                SET status = 'deprecated', deprecated_at = ?
                WHERE id IN ({placeholders})
            """, [now_str] + tag_ids)
            tag_count = cursor.rowcount

            cursor.execute(f"""
                UPDATE ai_filter_results
                SET status = 'deprecated', deprecated_at = ?
                WHERE tag_id IN ({placeholders}) AND status = 'active'
            """, [now_str] + tag_ids)

            conn.commit()
            return tag_count
        except Exception as e:
            print(f"[AI筛选] 废弃指定标签失败: {e}")
            return 0

    def _update_tags_hash_impl(
        self, date: Optional[str], interests_file: str, new_hash: str
    ) -> int:
        """更新指定兴趣文件所有 active 标签的 prompt_hash（增量更新时使用）"""
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE ai_filter_tags
                SET prompt_hash = ?
                WHERE interests_file = ? AND status = 'active'
            """, (new_hash, interests_file))
            count = cursor.rowcount

            conn.commit()
            return count
        except Exception as e:
            print(f"[AI筛选] 更新标签 hash 失败: {e}")
            return 0

    # ========================================
    # AI 智能筛选 - 分类结果管理
    # ========================================

    def _update_tag_descriptions_impl(
        self, date: Optional[str], tag_updates: List[Dict],
        interests_file: str = "ai_interests.txt"
    ) -> int:
        """按 tag 名匹配，更新 active 标签的 description 字段"""
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()

            count = 0
            for t in tag_updates:
                tag_name = t.get("tag", "")
                description = t.get("description", "")
                if not tag_name:
                    continue
                cursor.execute("""
                    UPDATE ai_filter_tags
                    SET description = ?
                    WHERE tag = ? AND interests_file = ? AND status = 'active'
                """, (description, tag_name, interests_file))
                count += cursor.rowcount

            conn.commit()
            return count
        except Exception as e:
            print(f"[AI筛选] 更新标签描述失败: {e}")
            return 0

    def _update_tag_priorities_impl(
        self, date: Optional[str], tag_priorities: List[Dict],
        interests_file: str = "ai_interests.txt"
    ) -> int:
        """按 tag 名匹配，更新 active 标签的 priority 字段"""
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()

            count = 0
            for t in tag_priorities:
                tag_name = t.get("tag", "")
                priority = t.get("priority")
                if not tag_name:
                    continue
                try:
                    priority = int(priority)
                except (TypeError, ValueError):
                    continue
                cursor.execute("""
                    UPDATE ai_filter_tags
                    SET priority = ?
                    WHERE tag = ? AND interests_file = ? AND status = 'active'
                """, (priority, tag_name, interests_file))
                count += cursor.rowcount

            conn.commit()
            return count
        except Exception as e:
            print(f"[AI筛选] 更新标签优先级失败: {e}")
            return 0

    # ========================================
    # AI 智能筛选 - 已分析新闻追踪
    # ========================================

    def _save_analyzed_news_impl(
        self, date: Optional[str], news_ids: List[int], source_type: str,
        interests_file: str, prompt_hash: str, matched_ids: set
    ) -> int:
        """批量记录已分析的新闻（匹配与不匹配都记录）"""
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()
            now_str = self._get_configured_time().strftime("%Y-%m-%d %H:%M:%S")

            count = 0
            for nid in news_ids:
                try:
                    cursor.execute("""
                        INSERT OR REPLACE INTO ai_filter_analyzed_news
                        (news_item_id, source_type, interests_file, prompt_hash, matched, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        nid, source_type, interests_file, prompt_hash,
                        1 if nid in matched_ids else 0,
                        now_str,
                    ))
                    count += 1
                except Exception:
                    pass

            conn.commit()
            return count
        except Exception as e:
            print(f"[AI筛选] 保存已分析记录失败: {e}")
            return 0

    def _get_analyzed_news_ids_impl(
        self, date: Optional[str] = None, source_type: str = "hotlist",
        interests_file: str = "ai_interests.txt", strict: bool = False
    ) -> set:
        """获取已分析过的新闻 ID 集合（用于去重）"""
        try:
            conn = self._get_ai_connection(date, strict=strict)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT news_item_id FROM ai_filter_analyzed_news
                WHERE source_type = ? AND interests_file = ?
            """, (source_type, interests_file))

            return {row[0] for row in cursor.fetchall()}
        except Exception as e:
            if strict:
                raise
            print(f"[AI筛选] 获取已分析ID失败: {e}")
            return set()

    def _clear_analyzed_news_impl(
        self, date: Optional[str] = None, interests_file: str = "ai_interests.txt"
    ) -> int:
        """清除指定兴趣文件的所有已分析记录（全量重分类时使用）"""
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()

            cursor.execute("""
                DELETE FROM ai_filter_analyzed_news
                WHERE interests_file = ?
            """, (interests_file,))

            count = cursor.rowcount
            conn.commit()
            return count
        except Exception as e:
            print(f"[AI筛选] 清除已分析记录失败: {e}")
            return 0

    def _clear_unmatched_analyzed_news_impl(
        self, date: Optional[str] = None, interests_file: str = "ai_interests.txt"
    ) -> int:
        """清除不匹配的已分析记录，让这些新闻有机会被新标签重新分析"""
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()

            cursor.execute("""
                DELETE FROM ai_filter_analyzed_news
                WHERE interests_file = ? AND matched = 0
            """, (interests_file,))

            count = cursor.rowcount
            conn.commit()
            return count
        except Exception as e:
            print(f"[AI筛选] 清除不匹配记录失败: {e}")
            return 0

    # ========================================
    # AI 智能筛选 - 分类结果管理（原有）
    # ========================================

    def _save_filter_results_impl(
        self, date: Optional[str], results: List[Dict]
    ) -> int:
        """批量保存分类结果"""
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()
            now_str = self._get_configured_time().strftime("%Y-%m-%d %H:%M:%S")

            count = 0
            for r in results:
                try:
                    cursor.execute("""
                        INSERT INTO ai_filter_results
                        (news_item_id, source_type, tag_id, relevance_score,
                         content_level, risk_warning, content_excerpt,
                         importance_score, ai_summary, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        r["news_item_id"],
                        r.get("source_type", "hotlist"),
                        r["tag_id"],
                        r.get("relevance_score", 0.0),
                        r.get("content_level", "title_only"),
                        r.get("risk_warning", ""),
                        r.get("content_excerpt", ""),
                        r.get("importance_score", 0.0),
                        r.get("ai_summary", ""),
                        now_str,
                    ))
                    count += 1
                except sqlite3.IntegrityError:
                    pass  # 重复记录，跳过

            conn.commit()
            return count
        except Exception as e:
            print(f"[AI筛选] 保存分类结果失败: {e}")
            return 0

    @staticmethod
    def _delete_strict_results_for_ids(
        cursor: sqlite3.Cursor,
        source_type: str,
        news_ids: List[int],
        interests_file: str,
    ) -> None:
        if not news_ids:
            return
        placeholders = ",".join("?" * len(news_ids))
        cursor.execute(
            f"DELETE FROM ai_filter_results "
            f"WHERE source_type = ? AND news_item_id IN ({placeholders}) "
            f"AND tag_id IN (SELECT id FROM ai_filter_tags "
            f"WHERE interests_file = ?)",
            [source_type, *news_ids, interests_file],
        )

    def _replace_ai_filter_batch_strict_impl(
        self,
        date: Optional[str],
        results: List[Dict],
        succeeded_news_ids: List[int],
        succeeded_rss_ids: List[int],
        interests_file: str,
        prompt_hash: str,
        conn: Optional[sqlite3.Connection] = None,
    ) -> Dict[str, int]:
        """在单一事务中替换本轮结果和 analyzed 状态，并读回核验。"""
        news_ids = list(dict.fromkeys(succeeded_news_ids))
        rss_ids = list(dict.fromkeys(succeeded_rss_ids))
        succeeded_by_type = {
            "hotlist": set(news_ids),
            "rss": set(rss_ids),
        }
        matched_by_type = {"hotlist": set(), "rss": set()}
        result_keys = set()

        for result in results:
            source_type = result.get("source_type", "hotlist")
            news_item_id = result.get("news_item_id")
            if (
                source_type not in succeeded_by_type
                or news_item_id not in succeeded_by_type[source_type]
            ):
                raise ValueError("分类结果包含本轮成功集合之外的 ID")
            key = (news_item_id, source_type, result.get("tag_id"))
            if key in result_keys:
                raise ValueError("分类结果包含重复 ID/tag")
            result_keys.add(key)
            matched_by_type[source_type].add(news_item_id)

        conn = conn or self._get_ai_connection(date, strict=True)
        cursor = conn.cursor()
        now_str = self._get_configured_time().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn.commit()
            cursor.execute("BEGIN IMMEDIATE")
            self._delete_strict_results_for_ids(
                cursor, "hotlist", news_ids, interests_file
            )
            self._delete_strict_results_for_ids(
                cursor, "rss", rss_ids, interests_file
            )

            for result in results:
                cursor.execute("""
                    INSERT INTO ai_filter_results
                    (news_item_id, source_type, tag_id, relevance_score,
                     content_level, risk_warning, content_excerpt,
                     importance_score, ai_summary, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    result["news_item_id"],
                    result.get("source_type", "hotlist"),
                    result["tag_id"],
                    result.get("relevance_score", 0.0),
                    result.get("content_level", "title_only"),
                    result.get("risk_warning", ""),
                    result.get("content_excerpt", ""),
                    result.get("importance_score", 0.0),
                    result.get("ai_summary", ""),
                    now_str,
                ))

            analyzed_count = 0
            for source_type, ids in (("hotlist", news_ids), ("rss", rss_ids)):
                for news_item_id in ids:
                    cursor.execute("""
                        INSERT OR REPLACE INTO ai_filter_analyzed_news
                        (news_item_id, source_type, interests_file, prompt_hash,
                         matched, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        news_item_id,
                        source_type,
                        interests_file,
                        prompt_hash,
                        1 if news_item_id in matched_by_type[source_type] else 0,
                        now_str,
                    ))
                    analyzed_count += 1

            stored_result_keys = set()
            for source_type, ids in (("hotlist", news_ids), ("rss", rss_ids)):
                if not ids:
                    continue
                placeholders = ",".join("?" * len(ids))
                cursor.execute(
                    f"SELECT news_item_id, source_type, tag_id "
                    f"FROM ai_filter_results WHERE status = 'active' "
                    f"AND source_type = ? AND news_item_id IN ({placeholders}) "
                    f"AND tag_id IN (SELECT id FROM ai_filter_tags "
                    f"WHERE interests_file = ?)",
                    [source_type, *ids, interests_file],
                )
                stored_result_keys.update(tuple(row) for row in cursor.fetchall())
            if stored_result_keys != result_keys:
                raise RuntimeError("分类结果写后读回不完整")

            stored_states = set()
            for source_type, ids in (("hotlist", news_ids), ("rss", rss_ids)):
                if not ids:
                    continue
                placeholders = ",".join("?" * len(ids))
                cursor.execute(
                    f"SELECT news_item_id, source_type, matched, prompt_hash "
                    f"FROM ai_filter_analyzed_news WHERE source_type = ? "
                    f"AND interests_file = ? AND news_item_id IN ({placeholders})",
                    [source_type, interests_file, *ids],
                )
                stored_states.update(tuple(row) for row in cursor.fetchall())
            expected_states = {
                (
                    news_item_id,
                    source_type,
                    1 if news_item_id in matched_by_type[source_type] else 0,
                    prompt_hash,
                )
                for source_type, ids in (("hotlist", news_ids), ("rss", rss_ids))
                for news_item_id in ids
            }
            if stored_states != expected_states:
                raise RuntimeError("已分析状态写后读回不完整")

            conn.commit()
            return {"results": len(result_keys), "analyzed": analyzed_count}
        except Exception:
            conn.rollback()
            raise

    def _get_active_filter_results_impl(
        self,
        date: Optional[str] = None,
        interests_file: str = "ai_interests.txt",
        strict: bool = False,
    ) -> List[Dict[str, Any]]:
        """获取指定兴趣文件的 active 分类结果，JOIN news_items 获取新闻详情"""
        try:
            conn = self._get_ai_connection(date, strict=strict)
            cursor = conn.cursor()

            # 热榜结果
            cursor.execute("""
                SELECT
                    r.news_item_id, r.source_type, r.tag_id, r.relevance_score,
                    r.content_level, r.risk_warning,
                    t.tag, t.description as tag_description, t.priority,
                    n.title, n.platform_id as source_id, p.name as source_name,
                    n.url, n.mobile_url, n.rank,
                    n.first_crawl_time, n.last_crawl_time, n.crawl_count,
                    r.content_excerpt, r.importance_score, r.ai_summary
                FROM ai_filter_results r
                JOIN ai_filter_tags t ON r.tag_id = t.id
                JOIN news_items n ON r.news_item_id = n.id
                LEFT JOIN platforms p ON n.platform_id = p.id
                WHERE r.status = 'active' AND r.source_type = 'hotlist'
                    AND t.status = 'active' AND t.interests_file = ?
                ORDER BY t.priority ASC, t.id ASC, r.relevance_score DESC
            """, (interests_file,))

            results = []
            hotlist_news_ids = []
            for row in cursor.fetchall():
                results.append({
                    "news_item_id": row[0], "source_type": row[1],
                    "tag_id": row[2], "relevance_score": row[3],
                    "content_level": row[4] or "title_only",
                    "risk_warning": row[5] or "",
                    "tag": row[6], "tag_description": row[7], "tag_priority": row[8],
                    "title": row[9], "source_id": row[10],
                    "source_name": row[11] or row[10],
                    "url": row[12] or "", "mobile_url": row[13] or "",
                    "rank": row[14],
                    "first_time": row[15], "last_time": row[16],
                    "count": row[17],
                    "content_excerpt": row[18] or "",
                    "importance_score": row[19] or 0.0,
                    "ai_summary": row[20] or "",
                })
                hotlist_news_ids.append(row[0])

            # 批量查排名历史（热榜）
            ranks_map: Dict[int, List[int]] = {}
            rank_timeline_map: Dict[int, List[Dict[str, Any]]] = {}
            if hotlist_news_ids:
                unique_ids = list(set(hotlist_news_ids))
                placeholders = ",".join("?" * len(unique_ids))
                cursor.execute(f"""
                    SELECT news_item_id, rank, crawl_time FROM rank_history
                    WHERE news_item_id IN ({placeholders})
                    ORDER BY news_item_id, crawl_time
                """, unique_ids)
                for rh_row in cursor.fetchall():
                    nid, rank, crawl_time = rh_row[0], rh_row[1], rh_row[2]

                    if not crawl_time:
                        continue

                    if nid not in ranks_map:
                        ranks_map[nid] = []
                    if rank != 0 and rank not in ranks_map[nid]:
                        ranks_map[nid].append(rank)

                    if nid not in rank_timeline_map:
                        rank_timeline_map[nid] = []
                    try:
                        time_part = crawl_time.split()[1][:5] if ' ' in crawl_time else crawl_time[:5]
                    except (IndexError, AttributeError):
                        time_part = "??:??"
                    rank_timeline_map[nid].append({
                        "time": time_part,
                        "rank": rank if rank != 0 else None
                    })

            for item in results:
                item["ranks"] = ranks_map.get(item["news_item_id"], [item["rank"]])
                item["rank_timeline"] = rank_timeline_map.get(item["news_item_id"], [])

            # RSS 结果（如果有 rss 库）
            try:
                rss_conn = self._get_rss_connection(date, strict=strict)
                rss_cursor = rss_conn.cursor()

                # 从 news 库获取 rss 类型的分类结果 ID
                cursor.execute("""
                    SELECT r.news_item_id, r.tag_id, r.relevance_score,
                           r.content_level, r.risk_warning,
                           t.tag, t.description, t.priority,
                           r.content_excerpt, r.importance_score, r.ai_summary
                    FROM ai_filter_results r
                    JOIN ai_filter_tags t ON r.tag_id = t.id
                    WHERE r.status = 'active' AND r.source_type = 'rss'
                        AND t.status = 'active' AND t.interests_file = ?
                    ORDER BY t.priority ASC, t.id ASC, r.relevance_score DESC
                """, (interests_file,))

                rss_filter_rows = cursor.fetchall()
                if rss_filter_rows:
                    rss_ids = [row[0] for row in rss_filter_rows]
                    placeholders = ",".join("?" * len(rss_ids))
                    rss_cursor.execute(f"""
                        SELECT i.id, i.title, i.feed_id, f.name as feed_name,
                               i.url, i.published_at, i.source_count,
                               i.pre_hot_score, i.search_topic,
                               i.search_providers
                        FROM rss_items i
                        LEFT JOIN rss_feeds f ON i.feed_id = f.id
                        WHERE i.id IN ({placeholders})
                    """, rss_ids)

                    rss_info = {row[0]: row for row in rss_cursor.fetchall()}

                    for fr_row in rss_filter_rows:
                        rss_id = fr_row[0]
                        info = rss_info.get(rss_id)
                        if info:
                            results.append({
                                "news_item_id": rss_id,
                                "source_type": "rss",
                                "tag_id": fr_row[1],
                                "relevance_score": fr_row[2],
                                "content_level": fr_row[3] or "title_only",
                                "risk_warning": fr_row[4] or "",
                                "tag": fr_row[5],
                                "tag_description": fr_row[6],
                                "tag_priority": fr_row[7],
                                "content_excerpt": fr_row[8] or "",
                                "importance_score": fr_row[9] or 0.0,
                                "ai_summary": fr_row[10] or "",
                                "title": info[1],
                                "source_id": info[2],
                                "source_name": info[3] or info[2],
                                "url": info[4] or "",
                                "mobile_url": "",
                                "rank": 0,
                                "ranks": [],
                                "first_time": info[5] or "",
                                "last_time": info[5] or "",
                                "count": 1,
                                "source_count": info[6] if info[6] is not None else 1,
                                "pre_hot_score": info[7] if info[7] is not None else 0.0,
                                "search_topic": info[8] or "",
                                "search_providers": info[9] or "",
                            })
            except Exception as rss_exc:
                if strict:
                    raise
                print(
                    "[AI筛选] 读取 RSS 分类结果失败，已保留热榜结果: "
                    f"{type(rss_exc).__name__}: {rss_exc}"
                )

            return results
        except Exception as e:
            if strict:
                raise
            print(f"[AI筛选] 获取分类结果失败: {e}")
            return []

    def _get_all_news_ids_impl(self, date: Optional[str] = None) -> List[Dict]:
        """获取当日热榜新闻及链接（用于 AI 筛选分类与正文提取）"""
        try:
            conn = self._get_connection(date)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT n.id, n.title, n.platform_id, p.name as platform_name,
                       n.url, n.mobile_url
                FROM news_items n
                LEFT JOIN platforms p ON n.platform_id = p.id
                ORDER BY n.id
            """)

            return [
                {
                    "id": row[0], "title": row[1],
                    "source_id": row[2], "source_name": row[3] or row[2],
                    "url": row[4] or "", "mobile_url": row[5] or "",
                    "summary": "",
                }
                for row in cursor.fetchall()
            ]
        except Exception as e:
            print(f"[AI筛选] 获取新闻列表失败: {e}")
            return []

    def _get_all_rss_ids_impl(
        self, date: Optional[str] = None, strict: bool = False
    ) -> List[Dict]:
        """获取当日 RSS 条目、链接和摘要（用于 AI 筛选分类与正文提取）"""
        try:
            conn = self._get_rss_connection(date, strict=strict)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT i.id, i.title, i.feed_id, f.name as feed_name,
                       i.published_at, i.url, i.summary, i.author,
                       i.source_count, i.pre_hot_score, i.search_topic,
                       i.search_providers, i.guid
                FROM rss_items i
                LEFT JOIN rss_feeds f ON i.feed_id = f.id
                ORDER BY i.id
            """)

            return [
                {
                    "id": row[0], "title": row[1],
                    "source_id": row[2], "source_name": row[3] or row[2],
                    "published_at": row[4] or "",
                    "url": row[5] or "", "mobile_url": "",
                    "summary": row[6] or "", "author": row[7] or "",
                    "source_count": row[8] if row[8] is not None else 1,
                    "pre_hot_score": row[9] if row[9] is not None else 0.0,
                    "search_topic": row[10] or "",
                    "search_providers": row[11] or "",
                    "guid": row[12] or "",
                }
                for row in cursor.fetchall()
            ]
        except Exception as e:
            if strict:
                raise
            print(f"[AI筛选] 获取 RSS 列表失败: {e}")
            return []

    def _get_rss_discoveries_for_identities_impl(
        self,
        date: str,
        candidate_identities: set[tuple],
        strict: bool = False,
    ) -> Dict[tuple, tuple[str, str]]:
        """读取单个日库中候选 identity 的最早首次发现时间。"""
        if not candidate_identities:
            return {}
        try:
            conn = self._get_rss_connection(date, strict=strict)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT title, feed_id, url, first_crawl_time, last_crawl_time
                FROM rss_items
            """)
            earliest: Dict[tuple, tuple[str, str]] = {}
            earliest_at: Dict[tuple, datetime] = {}
            for title, feed_id, url, first_time, last_time in cursor.fetchall():
                canonical_url = canonicalize_url(url or "")
                identity = (
                    ("url", canonical_url)
                    if canonical_url
                    else ("title", feed_id, normalize_title(title or ""))
                )
                if identity not in candidate_identities:
                    continue
                discovered = first_time or last_time or ""
                parsed = parse_storage_datetime(
                    discovered, date, self.timezone
                )
                if parsed is None:
                    raise RuntimeError(
                        f"RSS 首次发现时间无效: {date}/{identity!r}"
                    )
                if identity not in earliest_at or parsed < earliest_at[identity]:
                    earliest_at[identity] = parsed
                    earliest[identity] = (discovered, date)
            return earliest
        except Exception:
            if strict:
                raise
            return {}

    def _merge_earliest_rss_discoveries(
        self,
        candidate_identities: set[tuple],
        dates,
    ) -> Dict[tuple, tuple[str, str]]:
        """严格合并多个日库的候选首次发现时间。"""
        earliest: Dict[tuple, tuple[str, str]] = {}
        earliest_at: Dict[tuple, datetime] = {}
        for date in sorted(set(dates)):
            daily = self._get_rss_discoveries_for_identities_impl(
                date, candidate_identities, strict=True
            )
            for identity, (discovered, storage_date) in daily.items():
                parsed = parse_storage_datetime(
                    discovered, storage_date, self.timezone
                )
                if parsed is None:
                    raise RuntimeError(
                        f"RSS 首次发现时间无效: {storage_date}/{identity!r}"
                    )
                if identity not in earliest_at or parsed < earliest_at[identity]:
                    earliest_at[identity] = parsed
                    earliest[identity] = (discovered, storage_date)
        return earliest
