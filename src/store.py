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
_conn.execute("PRAGMA busy_timeout=30000")

_SCHEMA = """
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
    site_name    TEXT,
    model        TEXT NOT NULL DEFAULT 'haiku',
    content_hash TEXT,
    events_json  TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    event_count  INTEGER NOT NULL,
    PRIMARY KEY (site_name, model)
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
"""
_conn.executescript(_SCHEMA)

# Migration: if extractions has no 'model' column, rename+recreate with provenance.
# Atomic: one explicit transaction (DDL is transactional in SQLite); rolls back on
# any error so a half-migrated schema can't lose rows. Idempotent (guarded by cols).
def _migrate_extractions() -> None:
    cols = [r[1] for r in _conn.execute("PRAGMA table_info(extractions)").fetchall()]
    if not cols or "model" in cols:
        return
    print("[migrate] extractions → (site_name,model)")
    prev = _conn.isolation_level
    _conn.isolation_level = None          # manual transaction control
    try:
        _conn.execute("BEGIN IMMEDIATE")
        _conn.execute("DROP TABLE IF EXISTS extractions_old")
        _conn.execute("ALTER TABLE extractions RENAME TO extractions_old")
        _conn.execute("""
            CREATE TABLE extractions (
                site_name    TEXT,
                model        TEXT NOT NULL DEFAULT 'haiku',
                content_hash TEXT,
                events_json  TEXT NOT NULL,
                extracted_at TEXT NOT NULL,
                event_count  INTEGER NOT NULL,
                PRIMARY KEY (site_name, model)
            )""")
        _conn.execute("""
            INSERT INTO extractions(site_name,model,content_hash,events_json,extracted_at,event_count)
                SELECT site_name,'haiku',content_hash,events_json,extracted_at,event_count
                FROM extractions_old""")
        _conn.execute("DROP TABLE extractions_old")
        _conn.execute("COMMIT")
    except Exception:
        try:
            _conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        _conn.isolation_level = prev


try:
    _migrate_extractions()
except Exception as _mig_exc:
    print(f"[migrate] extractions migration skipped (rolled back): {_mig_exc}")


# ── scrape fetch status (pipeline v3 fetch layer) ─────────────────────────────
# Additive columns on `scrapes`: last fetch outcome per URL + HTTP validators.
# `html`/`fetched_at` only ever move on a good fetch (ok / not_modified); a wall or
# error only updates status/status_at/note. A URL with no good scrape yet gets a
# placeholder row (html='', fetched_at=SCRAPE_EPOCH) so its status is still reported.
SCRAPE_STATUS_COLS = ("status", "status_at", "etag", "last_modified", "final_url", "note")
SCRAPE_EPOCH = "1970-01-01T00:00:00+00:00"


def _migrate_scrapes(conn: sqlite3.Connection | None = None) -> None:
    conn = conn or _conn
    cols = {r[1] for r in conn.execute("PRAGMA table_info(scrapes)").fetchall()}
    with conn:
        for c in SCRAPE_STATUS_COLS:
            if c not in cols:
                conn.execute(f"ALTER TABLE scrapes ADD COLUMN {c} TEXT")


try:
    _migrate_scrapes()
except Exception as _mig_exc:
    print(f"[migrate] scrapes status columns skipped: {_mig_exc}")


def get_scrape(url: str) -> dict | None:
    """Stored scrape row for url (html may be '' for a never-good placeholder)."""
    row = _conn.execute("SELECT * FROM scrapes WHERE url_key=?", (_url_key(url),)).fetchone()
    return dict(row) if row else None


def _with_migration(fn):
    """Retry once after (re)running _migrate_scrapes when the status columns are missing
    (e.g. the import-time migration lost a lock race)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except sqlite3.OperationalError:
            _migrate_scrapes()
            return fn(*a, **kw)
    return wrapper


@_with_migration
def save_scrape(url: str, site_name: str, html: str, *, status: str = "ok",
                etag: str | None = None, last_modified: str | None = None,
                final_url: str | None = None, note: str = "") -> None:
    """Store a good body (+validators, status). Only call for a good fetch."""
    now = _now()
    content_hash = hashlib.sha256(html.encode()).hexdigest()[:16]
    with _conn:
        _conn.execute(
            """INSERT OR REPLACE INTO scrapes(url_key, url, site_name, html, content_hash, fetched_at,
                   status, status_at, etag, last_modified, final_url, note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_url_key(url), url, site_name, html, content_hash, now,
             status, now, etag, last_modified, final_url, note),
        )


@_with_migration
def record_scrape_status(url: str, site_name: str, status: str, note: str = "", *,
                         revalidated: bool = False, etag: str | None = None,
                         last_modified: str | None = None, final_url: str | None = None) -> None:
    """Record a fetch outcome without touching the stored body. `revalidated` (a 304)
    also refreshes fetched_at and the validators the server sent."""
    now = _now()
    key = _url_key(url)
    with _conn:
        if _conn.execute("SELECT 1 FROM scrapes WHERE url_key=?", (key,)).fetchone() is None:
            _conn.execute(
                """INSERT INTO scrapes(url_key, url, site_name, html, content_hash, fetched_at,
                       status, status_at, final_url, note) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (key, url, site_name, "", "", SCRAPE_EPOCH, status, now, final_url, note),
            )
            return
        _conn.execute(
            """UPDATE scrapes SET status=?, status_at=?, note=?, site_name=?,
                   final_url=COALESCE(?, final_url) WHERE url_key=?""",
            (status, now, note, site_name, final_url, key),
        )
        if revalidated:
            _conn.execute(
                """UPDATE scrapes SET fetched_at=?, etag=COALESCE(?, etag),
                       last_modified=COALESCE(?, last_modified), final_url=COALESCE(?, final_url)
                   WHERE url_key=?""",
                (now, etag, last_modified, final_url, key),
            )


def scrape_status_rows(site_names: list[str]) -> list[dict]:
    """Last fetch status per site_name (latest status_at if a name has several URLs)."""
    names = list(dict.fromkeys(site_names))
    if not names:
        return []
    q = ",".join("?" * len(names))
    rows = _conn.execute(
        f"""SELECT site_name, url, status, status_at, note, fetched_at, etag, last_modified,
                   final_url, length(html) AS html_len
            FROM scrapes WHERE site_name IN ({q})
            ORDER BY COALESCE(status_at, fetched_at) DESC""", names,
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        if not d["html_len"]:
            d["fetched_at"] = None          # placeholder row: never had a good scrape
        out.setdefault(r["site_name"], d)
    return [out[n] for n in names if n in out]
