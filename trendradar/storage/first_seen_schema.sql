-- RSS canonical identity 的版本化、不可变首次发现账本。
CREATE TABLE IF NOT EXISTS ledger_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rss_identity_first_seen (
    identity_key TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    storage_date TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rss_identity_first_seen_at
    ON rss_identity_first_seen(first_seen_at);

-- 每个 RSS 日库已消费的可靠 provenance 与单调 outbox watermark。
CREATE TABLE IF NOT EXISTS rss_first_seen_sources (
    source_key TEXT PRIMARY KEY,
    source_version TEXT NOT NULL,
    watermark INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- 幂等消费标记；跨库崩溃重放同一 write_id 不会重复产生状态。
CREATE TABLE IF NOT EXISTS rss_first_seen_processed_writes (
    source_key TEXT NOT NULL,
    write_id TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (source_key, write_id)
);
