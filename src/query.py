"""Orthogonal event filter: crawl once, query many ways. Pure functions."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Callable, Iterable

from .classify import is_elsewhere
from .config import get_city
from .models import Event

ALL_KINDS = frozenset({"one_off", "run", "recurring"})
TIME_BUCKETS = ("morning", "afternoon", "evening")
_TIME_ALIASES = {"daytime": ("morning", "afternoon"), "day": ("morning", "afternoon")}
PRESETS_YAML = Path(__file__).parent.parent / "pipelines.yaml"


@dataclass(frozen=True)
class Filter:
    city: str = "london"                       # london | moscow
    kinds: frozenset = ALL_KINDS
    audience: str = "any"                      # any | family | kids | adult
    min_age_max: int | None = None             # keep events with min_age None or <= this
    when: str = "next:14"                      # today|tomorrow|weekend|next:N|range:A..B
    time_of_day: frozenset = frozenset()       # subset of TIME_BUCKETS; empty = any
    strict_time: bool = False                  # True: events without a time are dropped when time_of_day set
    days: str = "any"                          # any | weekday | weekend
    categories: frozenset = frozenset()
    venues: frozenset = frozenset()            # lowercase venue/source names
    venue_tags: frozenset = frozenset()        # sites.yaml pipeline tags (e.g. museums)
    drop_religious: bool = True
    new_only: bool = False                     # drop one-offs/recurring already sent (history)
    # presentation hints carried with a saved filter (not used by query())
    rank: bool | None = None
    lang: str = "en"
    label: str | None = None
    coverage: bool = False
    tz: str | None = None                      # IANA tz; default from city (cities.yaml)
    public_only: bool = True                   # drop schools/members/trade/private events
    include_online: bool = False               # keep webinar/livestream-only events


_BANNED_HOST = re.compile(r"(^|\.)eventbrite\.[a-z.]+$", re.I)


def is_banned_url(url: str | None) -> bool:
    """Sources we never surface (eventbrite.*)."""
    from urllib.parse import urlparse
    try:
        return bool(_BANNED_HOST.search(urlparse(url or "").hostname or ""))
    except ValueError:
        return False


def city_today(f: "Filter") -> date:
    """'Today' in the filter's city timezone (not the machine's)."""
    return city_now(f).date()


def _city_tz(city: str | None) -> str | None:
    try:
        return get_city(city).tz
    except KeyError:
        return None


def city_now(f: "Filter") -> datetime:
    tz = f.tz or _city_tz(f.city)
    return datetime.now(ZoneInfo(tz)).replace(tzinfo=None) if tz else datetime.now()


# ── when → dates ─────────────────────────────────────────────────────────────

def resolve_when(when: str, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    w = (when or "").strip().lower()
    if w == "today":
        return today, today
    if w == "tomorrow":
        d = today + timedelta(days=1)
        return d, d
    if w == "weekend":
        wd = today.weekday()
        if wd == 5:
            return today, today + timedelta(days=1)
        if wd == 6:
            return today, today
        sat = today + timedelta(days=5 - wd)
        return sat, sat + timedelta(days=1)
    m = re.fullmatch(r"next:(\d+)", w)
    if m:
        n = max(int(m.group(1)), 1)
        return today, today + timedelta(days=n - 1)      # N days, today inclusive
    m = re.fullmatch(r"range:(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", w)
    if m:
        s, e = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
        if e < s:
            raise ValueError(f"range end before start: {when!r}")
        return s, e
    raise ValueError(f"bad --when {when!r} (today|tomorrow|weekend|next:N|range:YYYY-MM-DD..YYYY-MM-DD)")


def parse_time_arg(v) -> frozenset:
    """'any' | 'evening' | 'morning,afternoon' | 'daytime' | list → frozenset of buckets."""
    if v is None:
        return frozenset()
    items = [v] if isinstance(v, str) else list(v)
    out: set[str] = set()
    for it in items:
        for tok in str(it).split(","):
            tok = tok.strip().lower()
            if not tok or tok == "any":
                continue
            if tok in _TIME_ALIASES:
                out.update(_TIME_ALIASES[tok])
            elif tok in TIME_BUCKETS:
                out.add(tok)
            else:
                raise ValueError(f"bad time bucket {tok!r}")
    return frozenset(out)


def time_bucket(t: str | None) -> str | None:
    if not t:
        return None
    try:
        h = int(t.split(":")[0])
    except (ValueError, IndexError):
        return None
    return "morning" if h < 12 else ("afternoon" if h < 17 else "evening")


_EVENING_CATS = frozenset({"concert", "comedy", "theatre", "film"})
_DAY_CATS = frozenset({"exhibition", "tour"})
_DAYISH = re.compile(r"\b(matin[eé]e|morning|lunch(?:time)?|afternoon|brunch|family|kids?|children)\b", re.I)


def infer_buckets(e: Event) -> frozenset | None:
    """Time buckets for an event: from start time; else a deterministic guess for untimed
    one-offs (concert/comedy/theatre/film -> evening; exhibition/tour -> daytime; matinee-ish
    wording -> daytime). Runs/recurring series are open-hours: compatible with any bucket (None).
    Returns frozenset() when an untimed one-off cannot be placed."""
    b = time_bucket(e.time)
    if b:
        return frozenset({b})
    if e.kind != "one_off":
        return None
    if e.category in _EVENING_CATS:
        if _DAYISH.search(f"{e.title} {e.description or ''}"):
            return frozenset({"morning", "afternoon"})
        return frozenset({"evening"})
    if e.category in _DAY_CATS:
        return frozenset({"morning", "afternoon"})
    return frozenset()


# ── predicates ───────────────────────────────────────────────────────────────

def _span(e: Event) -> tuple[date, date | None]:
    """(start, end) of the event; end None = open-ended."""
    if e.kind == "run" or (e.kind == "recurring" and e.end_date):
        return e.date, e.end_date
    return e.date, e.date


def _overlaps(e: Event, start: date, end: date) -> bool:
    s, en = _span(e)
    return s <= end and (en is None or en >= start)


def _day_ok(d: date, days: str) -> bool:
    return days == "any" or (d.weekday() >= 5) == (days == "weekend")


def _days_ok(e: Event, days: str, start: date, end: date) -> bool:
    if days == "any":
        return True
    s, en = _span(e)
    lo, hi = max(s, start), min(en or end, end)
    d = lo
    for _ in range(14):
        if d > hi:
            return False
        if _day_ok(d, days):
            return True
        d += timedelta(days=1)
    return True


def _audience_ok(e: Event, f: Filter) -> bool:
    a = f.audience
    if a == "any":
        ok = True
    elif a == "family":
        ok = e.audience in ("kids", "family")
    elif a == "kids":
        ok = e.audience == "kids"
    elif a == "adult":
        ok = e.audience in ("adult", "all")
    else:
        raise ValueError(f"bad audience {a!r}")
    if ok and f.min_age_max is not None and e.min_age is not None and e.min_age > f.min_age_max:
        return False
    return ok


def query(
    events: Iterable[Event],
    f: Filter,
    today: date | None = None,
    tags_by_source: dict[str, set] | None = None,
    unseen: Callable[[list[Event]], list[Event]] | None = None,
    stats: dict | None = None,
) -> list[Event]:
    """Apply Filter to events. Pure (history dedup only via the injected `unseen`)."""
    today = today or city_today(f)
    start, end = resolve_when(f.when, today)
    cats = {c.lower() for c in f.categories}
    venues = {v.lower() for v in f.venues}
    out: list[Event] = []
    events = [e for e in events if not is_banned_url(e.url)]
    for e in events:
        if e.kind not in f.kinds:
            continue
        if not _overlaps(e, start, end):
            continue
        if e.date < today and (e.kind == "one_off" or (e.kind == "recurring" and e.end_date is None)):
            continue                                  # past one-offs never pass
        if e.kind in ("run", "recurring") and e.end_date and e.end_date < today:
            continue
        if f.public_only and not e.public:
            continue
        if e.online and not f.include_online:
            continue
        if is_elsewhere(e):
            continue                                  # touring dates outside London
        if not _audience_ok(e, f):
            continue
        if cats and e.category not in cats:
            continue
        if venues and e.venue.lower() not in venues and e.source.lower() not in venues:
            continue
        if f.venue_tags:
            tags = (tags_by_source or {}).get(e.source, set())
            if not (set(f.venue_tags) & set(tags)):
                continue
        if f.time_of_day:
            bs = infer_buckets(e)
            if bs is None:                      # run / recurring series: open hours
                if f.strict_time and e.kind != "run" and not e.time:
                    continue
            elif not (bs & f.time_of_day):
                if not e.time and stats is not None:
                    stats["untimed_hidden"] = stats.get("untimed_hidden", 0) + 1
                continue
        if not _days_ok(e, f.days, start, end):
            continue
        out.append(e)
    if f.drop_religious:
        from .filters import drop_religious_services
        out = drop_religious_services(out)
    if f.new_only and unseen is not None:
        fresh = {id(x) for x in unseen([e for e in out if e.kind != "run"])}
        out = [e for e in out if e.kind == "run" or id(e) in fresh]
    return out


# ── describe ─────────────────────────────────────────────────────────────────

def describe(f: Filter, today: date | None = None) -> str:
    s, e = resolve_when(f.when, today or city_today(f))
    if s == e:
        when = s.strftime("%a %d %b")
    else:
        when = f"{s.strftime('%a %d %b')} – {e.strftime('%a %d %b')}"
    parts = [f.city.capitalize(), when]
    if f.audience != "any":
        parts.append({"family": "family-friendly", "kids": "kids only", "adult": "adults"}[f.audience])
    if f.min_age_max is not None:
        parts.append(f"min age ≤ {f.min_age_max}")
    if f.kinds != ALL_KINDS:
        parts.append("/".join(sorted(f.kinds)).replace("_", "-"))
    if f.time_of_day:
        parts.append("+".join(b for b in TIME_BUCKETS if b in f.time_of_day) + (" (timed only)" if f.strict_time else ""))
    if f.days != "any":
        parts.append(f.days + "s only")
    if f.categories:
        parts.append("categories: " + ", ".join(sorted(f.categories)))
    if f.venues:
        parts.append("venues: " + ", ".join(sorted(f.venues)))
    if f.venue_tags:
        parts.append("venue tags: " + ", ".join(sorted(f.venue_tags)))
    if f.new_only:
        parts.append("new only")
    if not f.public_only:
        parts.append("incl. private/school events")
    if f.include_online:
        parts.append("incl. online")
    return " · ".join(parts)


# ── presets (saved filters) ──────────────────────────────────────────────────

_LIST_KEYS = {"kinds", "categories", "venues", "venue_tags"}


def filter_from_dict(d: dict, **overrides) -> Filter:
    d = dict(d or {})
    d.update({k: v for k, v in overrides.items() if v is not None})
    kw: dict = {}
    for k, v in d.items():
        if k == "time":
            kw["time_of_day"] = parse_time_arg(v)
        elif k == "kinds":
            ks = frozenset(v.split(",") if isinstance(v, str) else v)
            bad = ks - ALL_KINDS
            if bad:
                raise ValueError(f"bad kinds {sorted(bad)}")
            kw["kinds"] = ks
        elif k in _LIST_KEYS:
            kw[k] = frozenset(x.strip().lower() for x in (v.split(",") if isinstance(v, str) else v) if x)
        elif k in Filter.__dataclass_fields__:
            kw[k] = v
        else:
            raise ValueError(f"unknown filter key {k!r}")
    return Filter(**kw)


def load_presets(path: Path = PRESETS_YAML) -> dict[str, dict]:
    import yaml
    with open(path) as fh:
        return (yaml.safe_load(fh) or {}).get("presets", {})


def preset_filter(name: str, **overrides) -> Filter:
    presets = load_presets()
    if name not in presets:
        raise SystemExit(f"Unknown preset {name!r}. Known: {sorted(presets)}")
    return filter_from_dict(presets[name], **overrides)
