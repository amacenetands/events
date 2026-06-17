"""
JSON-backed persistence: seen events, exhibition cache, download cache, parse cache.
"""
import json
import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from .models import Event

CACHE_DIR = Path(__file__).parent.parent / "cache"
DOWNLOAD_DIR = CACHE_DIR / "downloads"
EXTRACTION_DIR = CACHE_DIR / "extractions"

EXHIBITION_TTL_DAYS = 21
DOWNLOAD_TTL_DAYS = 3
PARSE_TTL_DAYS = 3

_SEEN_F = CACHE_DIR / "seen_events.json"
_EXHB_F = CACHE_DIR / "exhibition_cache.json"
_PARSE_F = CACHE_DIR / "parse_cache.json"
_DLOAD_META_F = CACHE_DIR / "download_meta.json"


def _site_slug(site_name: str) -> str:
    import re
    return re.sub(r"[^\w]+", "_", site_name.lower()).strip("_")


def _ensure_cache() -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    EXTRACTION_DIR.mkdir(exist_ok=True)


def _load(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def _save(path: Path, data: dict) -> None:
    _ensure_cache()
    path.write_text(json.dumps(data, indent=2))


# ── seen events ──────────────────────────────────────────────────────────────

def filter_unseen(events: list[Event]) -> list[Event]:
    data = _load(_SEEN_F)
    cutoff = (date.today() - timedelta(weeks=52)).isoformat()
    data = {k: v for k, v in data.items() if v.get("seen_date", "") >= cutoff}
    seen = set(data.keys())
    return [e for e in events if e.fingerprint() not in seen]


def mark_seen(events: list[Event]) -> None:
    data = _load(_SEEN_F)
    today = date.today().isoformat()
    for e in events:
        fp = e.fingerprint()
        if fp not in data:
            data[fp] = {"title": e.title, "seen_date": today}
    _save(_SEEN_F, data)


# ── exhibition cache ──────────────────────────────────────────────────────────

def load_cached_exhibitions() -> list[Event]:
    data = _load(_EXHB_F)
    today = date.today().isoformat()
    events = []
    for _fp, entry in data.items():
        if entry.get("cached_until", "") >= today:
            try:
                events.append(Event(**json.loads(entry["event_json"])))
            except Exception:
                continue
    return events


def upsert_exhibitions(events: list[Event], ttl_days: int = EXHIBITION_TTL_DAYS) -> None:
    exhibitions = [e for e in events if not e.is_one_off]
    if not exhibitions:
        return
    data = _load(_EXHB_F)
    cached_until = (date.today() + timedelta(days=ttl_days)).isoformat()
    for e in exhibitions:
        data[e.fingerprint()] = {
            "event_json": e.model_dump_json(),
            "source": e.source,
            "cached_until": cached_until,
        }
    _save(_EXHB_F, data)


def get_all_exhibition_ages(sources: list[str]) -> dict[str, int]:
    data = _load(_EXHB_F)
    today = date.today()
    source_set = set(sources)
    ages: dict[str, int] = {}
    for entry in data.values():
        src = entry.get("source", "")
        if src not in source_set or src in ages:
            continue
        cached_until_str = entry.get("cached_until", "")
        if not cached_until_str:
            continue
        cached_until = date.fromisoformat(cached_until_str)
        age = max(0, EXHIBITION_TTL_DAYS - (cached_until - today).days)
        ages[src] = age
    return ages


def purge_expired_exhibitions() -> None:
    data = _load(_EXHB_F)
    today = date.today().isoformat()
    data = {k: v for k, v in data.items() if v.get("cached_until", "") >= today}
    _save(_EXHB_F, data)


# ── download cache ────────────────────────────────────────────────────────────

def _url_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:24]


def get_cached_html(url: str, force: bool = False) -> Optional[str]:
    if force:
        return None
    meta = _load(_DLOAD_META_F)
    key = _url_key(url)
    entry = meta.get(key)
    if not entry:
        return None
    fetched_at = datetime.fromisoformat(entry["fetched_at"])
    if datetime.utcnow() - fetched_at > timedelta(days=DOWNLOAD_TTL_DAYS):
        return None
    html_path = DOWNLOAD_DIR / f"{key}.html"
    if not html_path.exists():
        return None
    return html_path.read_text(encoding="utf-8")


def save_html_cache(url: str, html: str, site_name: str = "") -> None:
    _ensure_cache()
    key = _url_key(url)
    meta = _load(_DLOAD_META_F)
    meta[key] = {"url": url, "site_name": site_name, "fetched_at": datetime.utcnow().isoformat()}
    _save(_DLOAD_META_F, meta)
    (DOWNLOAD_DIR / f"{key}.html").write_text(html, encoding="utf-8")


# ── parse cache ───────────────────────────────────────────────────────────────

def get_html_fetched_at(url: str) -> Optional[str]:
    meta = _load(_DLOAD_META_F)
    entry = meta.get(_url_key(url))
    return entry.get("fetched_at") if entry else None


def get_cached_parse(site_name: str, force: bool = False, fetched_at: str | None = None) -> Optional[list[dict]]:
    if force:
        return None
    data = _load(_PARSE_F)
    entry = data.get(site_name)
    if not entry:
        return None
    cached_at_str = entry["cached_at"]
    cached_at = datetime.fromisoformat(cached_at_str)
    if datetime.utcnow() - cached_at > timedelta(days=PARSE_TTL_DAYS):
        return None
    if fetched_at and fetched_at > cached_at_str:
        return None
    try:
        return json.loads(entry["events_json"])
    except Exception:
        return None


def save_parse_cache(site_name: str, events: list[dict]) -> None:
    data = _load(_PARSE_F)
    data[site_name] = {
        "events_json": json.dumps(events),
        "cached_at": datetime.utcnow().isoformat(),
    }
    _save(_PARSE_F, data)


# ── extraction store (permanent, per-site) ────────────────────────────────────

def save_extraction(site_name: str, raw_events: list[dict]) -> None:
    """Persist all LLM-extracted events for a site. No TTL — permanent audit log."""
    _ensure_cache()
    path = EXTRACTION_DIR / f"{_site_slug(site_name)}.json"
    data = {
        "site_name": site_name,
        "extracted_at": datetime.utcnow().isoformat(),
        "event_count": len(raw_events),
        "events": raw_events,
    }
    path.write_text(json.dumps(data, indent=2))


def load_extraction(site_name: str) -> Optional[dict]:
    """Load last extraction result for a site."""
    path = EXTRACTION_DIR / f"{_site_slug(site_name)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None
