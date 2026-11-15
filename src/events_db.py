"""Crawl output store: one row per (city, event_key) in cache/memory.db `events`."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

from .models import Event
from .store import _conn, _now
from .query import is_banned_url

CRAWL_RETENTION_DAYS = 90
_DDL = """
CREATE TABLE IF NOT EXISTS events (
    city         TEXT NOT NULL,
    event_key    TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    source       TEXT NOT NULL,
    event_json   TEXT NOT NULL,
    tags         TEXT NOT NULL DEFAULT '[]',
    first_seen   TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    crawl_id     TEXT NOT NULL,
    PRIMARY KEY (city, event_key)
);
CREATE INDEX IF NOT EXISTS idx_events_city ON events(city);
CREATE TABLE IF NOT EXISTS crawls (
    crawl_id   TEXT PRIMARY KEY,
    city       TEXT NOT NULL,
    crawled_at TEXT NOT NULL,
    n_events   INTEGER NOT NULL,
    oldest_fetched_at TEXT
);
"""
_ready = False


def init_db() -> None:
    """Create/migrate tables (lazy; safe to call repeatedly)."""
    global _ready
    if _ready:
        return
    cols = [r[1] for r in _conn.execute("PRAGMA table_info(events)")]
    if cols and "event_key" not in cols:       # old schema: PK (city, fingerprint) -> rebuild
        old = _conn.execute("SELECT * FROM events").fetchall()
        with _conn:
            _conn.execute("DROP TABLE events")
            _conn.executescript(_DDL)
            for r in old:
                try:
                    key = Event(**json.loads(r["event_json"])).event_key()
                except Exception:
                    continue
                _conn.execute(
                    "INSERT OR REPLACE INTO events(city,event_key,fingerprint,source,event_json,tags,"
                    "first_seen,extracted_at,crawl_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (r["city"], key, r["fingerprint"], r["source"], r["event_json"], r["tags"],
                     r["first_seen"], r["extracted_at"], r["crawl_id"]))
    else:
        _conn.executescript(_DDL)
    ccols = [r[1] for r in _conn.execute("PRAGMA table_info(crawls)")]
    if "oldest_fetched_at" not in ccols:
        _conn.execute("ALTER TABLE crawls ADD COLUMN oldest_fetched_at TEXT")
    _ready = True


@dataclass
class Loaded:
    events: list[Event]
    tags_by_source: dict[str, set]
    first_seen: dict[str, str]         # fingerprint -> earliest iso ts
    crawled_at: datetime | None
    n_events: int
    n_bad: int = 0                     # undecodable rows skipped
    oldest_fetched_at: datetime | None = None


def _errored(sources: set[str], errors: list[str]) -> set[str]:
    return {s for s in sources if any(er == s or er.startswith(s + " (") or er.startswith(s + ":")
                                      for er in errors)}


def save_crawl(city: str, events: list[Event], tags_by_source: dict[str, list[str]],
               errors: list[str] | None = None, oldest_fetched_at: str | None = None) -> str:
    """Merge this crawl into the city's rows.

    Rows are replaced only for sources that produced events AND did not error this crawl.
    Sources that errored (events merged in, old rows kept), came back empty/thin or were not
    crawled keep their previous rows. first_seen is preserved per fingerprint.
    """
    init_db()
    errors = errors or []
    crawl_id = uuid.uuid4().hex[:12]
    now = _now()
    old = {}
    for r in _conn.execute("SELECT fingerprint, first_seen FROM events WHERE city=?", (city,)):
        if r["fingerprint"] not in old or r["first_seen"] < old[r["fingerprint"]]:
            old[r["fingerprint"]] = r["first_seen"]
    new_sources = {e.source for e in events}
    bad = _errored(new_sources, errors)
    replace_sources = new_sources - bad
    with _conn:
        for s in replace_sources:
            _conn.execute("DELETE FROM events WHERE city=? AND source=?", (city, s))
        seen: set[str] = set()
        for e in events:
            k = e.event_key()
            if k in seen:
                continue
            seen.add(k)
            fp = e.fingerprint()
            _conn.execute(
                "INSERT OR REPLACE INTO events(city,event_key,fingerprint,source,event_json,tags,first_seen,"
                "extracted_at,crawl_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (city, k, fp, e.source, e.model_dump_json(),
                 json.dumps(tags_by_source.get(e.source, [])), old.get(fp, now), now, crawl_id),
            )
        total = _conn.execute("SELECT COUNT(*) FROM events WHERE city=?", (city,)).fetchone()[0]
        _conn.execute("INSERT INTO crawls(crawl_id,city,crawled_at,n_events,oldest_fetched_at) VALUES(?,?,?,?,?)",
                      (crawl_id, city, now, total, oldest_fetched_at))
        cut = (datetime.now(timezone.utc) - timedelta(days=CRAWL_RETENTION_DAYS)).isoformat()
        _conn.execute("DELETE FROM crawls WHERE crawled_at < ?", (cut,))
    return crawl_id


def load_events(city: str) -> Loaded:
    init_db()
    rows = _conn.execute(
        "SELECT fingerprint, source, event_json, tags, first_seen FROM events WHERE city=?", (city,)
    ).fetchall()
    events, tags, first, bad = [], {}, {}, 0
    for r in rows:
        try:
            e = Event(**json.loads(r["event_json"]))
        except Exception:
            bad += 1
            continue
        if is_banned_url(e.url):
            continue
        events.append(e)
        tags[r["source"]] = set(json.loads(r["tags"] or "[]"))
        fp = r["fingerprint"]
        if fp not in first or r["first_seen"] < first[fp]:
            first[fp] = r["first_seen"]
    if bad:
        print(f"WARNING: {bad} undecodable {city} event rows skipped")
    c = _conn.execute(
        "SELECT crawled_at, n_events, oldest_fetched_at FROM crawls WHERE city=? ORDER BY crawled_at DESC LIMIT 1",
        (city,)).fetchone()
    at = datetime.fromisoformat(c["crawled_at"]) if c else None
    of = None
    if c and c["oldest_fetched_at"]:
        try:
            of = datetime.fromisoformat(c["oldest_fetched_at"])
            if of.tzinfo is None:
                of = of.replace(tzinfo=timezone.utc)
        except ValueError:
            of = None
    return Loaded(events, tags, first, at, c["n_events"] if c else 0, bad, of)


def age_days(at: datetime | None) -> float | None:
    if at is None:
        return None
    return (datetime.now(timezone.utc) - at).total_seconds() / 86400


def purge_banned(city: str | None = None) -> int:
    """Delete rows whose event url host is banned (eventbrite.*). Returns rows deleted."""
    init_db()
    rows = _conn.execute("SELECT city, event_key, event_json FROM events" + (" WHERE city=?" if city else ""),
                         (city,) if city else ()).fetchall()
    n = 0
    with _conn:
        for r in rows:
            try:
                url = json.loads(r["event_json"]).get("url")
            except Exception:
                continue
            if is_banned_url(url):
                _conn.execute("DELETE FROM events WHERE city=? AND event_key=?", (r["city"], r["event_key"]))
                n += 1
    return n
