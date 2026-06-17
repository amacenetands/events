"""
SQLite persistence engine — single shared WAL connection.
Replaces: seen_events.json, exhibition_cache.json, parse_cache.json,
          download_meta.json, downloads/, extractions/
"""
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime, timezone

CACHE_DIR = Path(__file__).parent.parent / "cache"
DB_PATH = CACHE_DIR / "memory.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _url_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:24]


# one shared connection, WAL, dict rows
CACHE_DIR.mkdir(exist_ok=True)
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")

_conn.executescript("""
-- STORE 1: raw scrapes (html as TEXT — single source of truth, no orphan files)
CREATE TABLE IF NOT EXISTS scrapes (
    url_key      TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    site_name    TEXT NOT NULL,
    html         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);
-- STORE 2: parsed LLM results (merges old parse_cache.json + extractions/*.json)
CREATE TABLE IF NOT EXISTS extractions (
    site_name    TEXT PRIMARY KEY,
    content_hash TEXT,
    events_json  TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    event_count  INTEGER NOT NULL
);
-- TAG: per-event state (seen history + exhibition retention w/ payload)
CREATE TABLE IF NOT EXISTS event_state (
    fingerprint  TEXT PRIMARY KEY,
    seen_date    TEXT,
    cached_until TEXT,
    source       TEXT,
    event_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen  ON event_state(seen_date);
CREATE INDEX IF NOT EXISTS idx_until ON event_state(cached_until);
""")
