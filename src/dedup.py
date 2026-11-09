"""
SQLite-backed persistence: seen events, html cache, parse/extraction cache.
"""
import json
import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from .models import Event
from .store import _conn, _now, _url_key

DOWNLOAD_TTL_DAYS = 7
PARSE_TTL_DAYS = 7


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


def oldest_fetched_at(urls: list[str]) -> Optional[str]:
    """Oldest scrapes.fetched_at (ISO) among urls that have a stored row, else None.
    A download-TTL / content-hash hit reuses old html; callers use this to flag
    stale crawls as stale instead of fresh."""
    keys = [_url_key(u) for u in dict.fromkeys(urls)]
    if not keys:
        return None
    q = ",".join("?" * len(keys))
    row = _conn.execute(
        f"SELECT MIN(fetched_at) AS m FROM scrapes WHERE url_key IN ({q})", keys
    ).fetchone()
    return row["m"] if row and row["m"] else None


def get_cached_html_any(url: str) -> Optional[str]:
    """Return stored html for url ignoring TTL — fetch stage is authoritative."""
    row = _conn.execute(
        "SELECT html FROM scrapes WHERE url_key=?", (_url_key(url),)
    ).fetchone()
    return row["html"] if row else None


# ── parse / extraction cache ──────────────────────────────────────────────────

# Models whose 0-event parse is trusted as final. An empty result from the weak
# local model (qwen) is provisional — a later haiku/claude run must be able to
# re-parse it (see _row_valid below and the in-run upgrade in extract.py).
_TRUSTED_EMPTY_MODELS = {"haiku", "claude"}


def get_cached_parse(
    site_name: str,
    force: bool = False,
    fetched_at: str | None = None,
    content_hash: str | None = None,
    model: str | None = None,
    any_model: bool = True,
    prefer: str = "haiku",
) -> Optional[tuple[list[dict], str]]:
    """Return (events_list, hit_type_with_model) on cache hit, or None on miss.

    hit_type is one of: "content-hash:<model>", "parse cache:<model>".

    Candidate set:
    - any_model=True  → all rows for site_name (prefer row whose model==prefer).
    - any_model=False → only the row whose model==model (skip if model is None).

    Hit priority per row:
    1. content_hash match — stored hash equals passed hash → VALID regardless of TTL.
    2. TTL + fetched_at freshness.
    """
    if force:
        return None

    rows = _conn.execute(
        "SELECT events_json, extracted_at, content_hash, model FROM extractions WHERE site_name=?",
        (site_name,),
    ).fetchall()
    if not rows:
        return None

    # Candidate filtering
    if any_model:
        candidates = list(rows)
    else:
        if model is None:
            return None
        candidates = [r for r in rows if r["model"] == model]
    if not candidates:
        return None

    def _row_valid(row) -> Optional[tuple[list[dict], str]]:
        """Return (events, hit_type_label) if row is valid, else None."""
        row_model = row["model"]
        try:
            events = json.loads(row["events_json"])
        except Exception:
            return None
        # A 0-event parse from the weak local model (qwen) is provisional: it must
        # not permanently block a re-parse by a stronger model (haiku/claude), or a
        # silent qwen miss stays cached forever. Trust empties only from haiku.
        # Skip this guard in compare mode (any_model=False), which inspects each
        # model's own cached result by design.
        if any_model and not events and row_model not in _TRUSTED_EMPTY_MODELS:
            return None
        if content_hash is not None:
            if row["content_hash"] == content_hash:
                return (events, f"content-hash:{row_model}")
            if row["content_hash"]:
                return None
            # legacy row with no stored hash → fall through to TTL

        extracted_at = datetime.fromisoformat(row["extracted_at"])
        if datetime.now(timezone.utc) - extracted_at > timedelta(days=PARSE_TTL_DAYS):
            return None
        if fetched_at and fetched_at > row["extracted_at"]:
            return None
        return (events, f"parse cache:{row_model}")

    # Sort: prefer row first, then by extracted_at desc
    def _sort_key(r):
        return (0 if r["model"] == prefer else 1, tuple(-ord(c) for c in r["extracted_at"]))

    # Never let an empty result shadow a non-empty one: prefer valid non-empty
    # rows (prefer model, then freshest); fall back to a valid trusted empty.
    empty_hit = None
    for row in sorted(candidates, key=_sort_key):
        result = _row_valid(row)
        if result is None:
            continue
        if result[0]:
            return result
        if empty_hit is None:
            empty_hit = result
    return empty_hit


def save_extraction(
    site_name: str, raw_events: list[dict], content_hash: str | None = None, model: str = "haiku"
) -> None:
    """Persist LLM-extracted events for a (site, model) pair."""
    with _conn:
        _conn.execute(
            """INSERT OR REPLACE INTO extractions(site_name, model, content_hash, events_json, extracted_at, event_count)
               VALUES(?,?,?,?,?,?)""",
            (site_name, model, content_hash, json.dumps(raw_events), _now(), len(raw_events)),
        )


def load_extraction(site_name: str) -> Optional[dict]:
    """Load last extraction result for a site — haiku row preferred, else most recent."""
    rows = _conn.execute(
        "SELECT * FROM extractions WHERE site_name=? ORDER BY CASE model WHEN 'haiku' THEN 0 ELSE 1 END, extracted_at DESC",
        (site_name,),
    ).fetchall()
    if not rows:
        return None
    row = rows[0]
    try:
        return {
            "site_name": row["site_name"],
            "model": row["model"],
            "extracted_at": row["extracted_at"],
            "event_count": row["event_count"],
            "events": json.loads(row["events_json"]),
        }
    except Exception:
        return None
