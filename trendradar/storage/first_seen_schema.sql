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
