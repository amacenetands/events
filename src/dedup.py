"""
SQLite-backed persistence: seen events, exhibition cache, html cache, parse/extraction cache.
"""
import json
import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from .models import Event
from .store import _conn, _now, _url_key

EXHIBITION_TTL_DAYS = 21
DOWNLOAD_TTL_DAYS = 3
PARSE_TTL_DAYS = 3


# ── seen events ───────────────────────────────────────────────────────────────

def filter_unseen(events: list[Event]) -> list[Event]:
    cutoff = (date.today() - timedelta(weeks=52)).isoformat()
    rows = _conn.execute(
        "SELECT fingerprint FROM event_state WHERE seen_date >= ?", (cutoff,)
    ).fetchall()
    seen = {r["fingerprint"] for r in rows}
    return [e for e in events if e.fingerprint() not in seen]


def mark_seen(events: list[Event]) -> None:
    today = date.today().isoformat()
    with _conn:
        for e in events:
            _conn.execute(
                """INSERT INTO event_state(fingerprint, seen_date) VALUES(?,?)
                   ON CONFLICT(fingerprint) DO UPDATE SET seen_date=excluded.seen_date
                   WHERE event_state.seen_date IS NULL""",
                (e.fingerprint(), today),
            )


# ── exhibition cache ──────────────────────────────────────────────────────────

def load_cached_exhibitions() -> list[Event]:
    today = date.today().isoformat()
    rows = _conn.execute(
        "SELECT event_json FROM event_state WHERE cached_until >= ? AND event_json IS NOT NULL",
        (today,),
    ).fetchall()
    events = []
    for r in rows:
        try:
            events.append(Event(**json.loads(r["event_json"])))
        except Exception:
            continue
    return events


def upsert_exhibitions(events: list[Event], ttl_days: int = EXHIBITION_TTL_DAYS) -> None:
    exhibitions = [e for e in events if not e.is_one_off]
    if not exhibitions:
        return
    cached_until = (date.today() + timedelta(days=ttl_days)).isoformat()
    with _conn:
        for e in exhibitions:
            _conn.execute(
                """INSERT INTO event_state(fingerprint, cached_until, source, event_json)
                   VALUES(?,?,?,?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                       cached_until=excluded.cached_until,
                       source=excluded.source,
                       event_json=excluded.event_json""",
                (e.fingerprint(), cached_until, e.source, e.model_dump_json()),
            )


def get_all_exhibition_ages(sources: list[str]) -> dict[str, int]:
    rows = _conn.execute(
        "SELECT source, cached_until FROM event_state WHERE cached_until IS NOT NULL"
    ).fetchall()
    today = date.today()
    source_set = set(sources)
    ages: dict[str, int] = {}
    for r in rows:
        src = r["source"] or ""
        if src not in source_set or src in ages:
            continue
        cached_until_str = r["cached_until"]
        if not cached_until_str:
            continue
        cached_until = date.fromisoformat(cached_until_str)
        age = max(0, EXHIBITION_TTL_DAYS - (cached_until - today).days)
        ages[src] = age
    return ages


def purge_expired_exhibitions() -> None:
    today = date.today().isoformat()
    with _conn:
        _conn.execute(
            """UPDATE event_state SET cached_until=NULL, source=NULL, event_json=NULL
               WHERE cached_until < ?""",
            (today,),
        )
        _conn.execute(
            "DELETE FROM event_state WHERE seen_date IS NULL AND cached_until IS NULL"
        )


# ── html download cache ───────────────────────────────────────────────────────

def get_cached_html(url: str, force: bool = False) -> Optional[str]:
    if force:
        return None
    row = _conn.execute(
        "SELECT html, fetched_at FROM scrapes WHERE url_key=?", (_url_key(url),)
    ).fetchone()
    if not row:
        return None
    fetched_at = datetime.fromisoformat(row["fetched_at"])
    if datetime.now(timezone.utc) - fetched_at > timedelta(days=DOWNLOAD_TTL_DAYS):
        return None
    return row["html"]


def save_html_cache(url: str, html: str, site_name: str = "") -> None:
    content_hash = hashlib.sha256(html.encode()).hexdigest()[:16]
    with _conn:
        _conn.execute(
            """INSERT OR REPLACE INTO scrapes(url_key, url, site_name, html, content_hash, fetched_at)
               VALUES(?,?,?,?,?,?)""",
            (_url_key(url), url, site_name, html, content_hash, _now()),
        )


def get_html_fetched_at(url: str) -> Optional[str]:
    row = _conn.execute(
        "SELECT fetched_at FROM scrapes WHERE url_key=?", (_url_key(url),)
    ).fetchone()
    return row["fetched_at"] if row else None


# ── parse / extraction cache ──────────────────────────────────────────────────

def get_cached_parse(
    site_name: str, force: bool = False, fetched_at: str | None = None
) -> Optional[list[dict]]:
    if force:
        return None
    row = _conn.execute(
        "SELECT events_json, extracted_at FROM extractions WHERE site_name=?",
        (site_name,),
    ).fetchone()
    if not row:
        return None
    extracted_at = datetime.fromisoformat(row["extracted_at"])
    if datetime.now(timezone.utc) - extracted_at > timedelta(days=PARSE_TTL_DAYS):
        return None
    if fetched_at and fetched_at > row["extracted_at"]:
        return None
    try:
        return json.loads(row["events_json"])
    except Exception:
        return None


def save_extraction(
    site_name: str, raw_events: list[dict], content_hash: str | None = None
) -> None:
    """Persist LLM-extracted events for a site. Serves both parse-cache and extraction-store roles."""
    with _conn:
        _conn.execute(
            """INSERT OR REPLACE INTO extractions(site_name, content_hash, events_json, extracted_at, event_count)
               VALUES(?,?,?,?,?)""",
            (site_name, content_hash, json.dumps(raw_events), _now(), len(raw_events)),
        )


def load_extraction(site_name: str) -> Optional[dict]:
    """Load last extraction result for a site."""
    row = _conn.execute(
        "SELECT * FROM extractions WHERE site_name=?", (site_name,)
    ).fetchone()
    if not row:
        return None
    try:
        return {
            "site_name": row["site_name"],
            "extracted_at": row["extracted_at"],
            "event_count": row["event_count"],
            "events": json.loads(row["events_json"]),
        }
    except Exception:
        return None
