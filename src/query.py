"""Orthogonal event filter: crawl once, query many ways. Pure functions."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Callable, Iterable

from .classify import is_elsewhere
from .config import DEFAULT_CITY, get_city
from .models import Event, is_ongoing

ALL_KINDS = frozenset({"one_off", "run", "recurring"})
TIME_BUCKETS = ("morning", "afternoon", "evening")
_TIME_ALIASES = {"daytime": ("morning", "afternoon"), "day": ("morning", "afternoon")}
PRESETS_YAML = Path(__file__).parent.parent / "pipelines.yaml"


@dataclass(frozen=True)
class Filter:
    city: str = DEFAULT_CITY                   # a cities.yaml key
    kinds: frozenset = ALL_KINDS
    audience: str = "any"                      # any | family | kids | adult
    min_age_max: int | None = None             # keep events with min_age None or <= this
    when: str = "next:14"                      # today|tomorrow|weekend|this-week|next-week|next:N|range:A..B
    time_of_day: frozenset = frozenset()       # subset of TIME_BUCKETS; empty = any
    strict_time: bool = False                  # True: events without a time are dropped when time_of_day set
    days: str = "any"                          # any | weekday | weekend | mon..sun[,..]
    categories: frozenset = frozenset()
    venues: frozenset = frozenset()            # lowercase venue/source names
    venue_tags: frozenset = frozenset()        # venue tags (e.g. museums)
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
    if w in ("this-week", "week"):
        return today, today + timedelta(days=6 - today.weekday())
    if w == "next-week":
        mon = today + timedelta(days=7 - today.weekday())
        return mon, mon + timedelta(days=6)
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
    raise ValueError(f"bad --when {when!r} (today|tomorrow|weekend|this-week|next-week|next:N|range:YYYY-MM-DD..YYYY-MM-DD)")


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

_WD_NAMES = {n: i for i, ns in enumerate((
    ("mon", "monday", "пн"), ("tue", "tues", "tuesday", "вт"), ("wed", "wednesday", "ср"),
    ("thu", "thur", "thurs", "thursday", "чт"), ("fri", "friday", "пт"), ("sat", "saturday", "сб"),
    ("sun", "sunday", "вс"))) for n in ns}


def _is_open_series(e: Event) -> bool:
    """Open-ended weekly series ("Every Saturday"): recurring with weekdays and no end.
    Contract: recurring + byday = series; recurring + byday None = one dated instance."""
    return e.kind == "recurring" and e.end_date is None and bool(e.byday)


def sessions_uncertain(e: Event) -> bool:
    """Multi-day listing whose session days are unknown (range only): matched as every day."""
    return e.kind != "one_off" and not e.byday and not e.sessions_known


def _spec(e: Event, today: date | None = None):
    """Event -> DateSpec for normalize.occurrences (None: single dated instance)."""
    from .normalize import DateSpec
    if e.kind == "one_off" or (e.kind == "recurring" and not e.byday):
        return None
    start = e.date
    if not e.start_known and today is not None:
        start = min(start, today)                     # "until X": on now
    return DateSpec(kind="run", start=start, end=e.end_date,
                    byday=frozenset(e.byday) if e.byday else None, instances=(), times=(),
                    sessions_known=e.sessions_known, start_known=e.start_known,
                    inconsistent=e.date_inconsistent, raw=e.raw_date or "", exdates=tuple(e.exdates or ()))


def event_days(e: Event, start: date, end: date, *, today: date | None = None,
               max_n: int = 400) -> list[date]:
    """Concrete dates the event happens on inside [start, end].

    one_off / dated recurring instance -> [date]; run or series with byday -> those weekdays
    between date and end_date (open-ended when end_date is None); run without byday -> every
    day (see sessions_uncertain() for the 'check days' flag). `today`: start_known=False
    events count as on from today."""
    if end < start:
        return []
    spec = _spec(e, today)
    if spec is None:
        return [e.date] if start <= e.date <= end else []
    from .normalize import occurrences
    return occurrences(spec, start, end, max_n=max_n)


def _overlaps(e: Event, start: date, end: date, today: date | None = None) -> bool:
    return bool(event_days(e, start, end, today=today, max_n=1))


def parse_days(days: str) -> frozenset[int] | None:
    """'any' -> None; 'weekday' | 'weekend' | 'fri' | 'sat,sun' -> weekday set (0=Mon)."""
    out: set[int] = set()
    for tok in str(days or "any").lower().replace(" ", "").split(","):
        if tok in ("", "any"):
            continue
        if tok in ("weekday", "weekdays"):
            out.update(range(5))
        elif tok in ("weekend", "weekends"):
            out.update((5, 6))
        elif tok.rstrip("s") in _WD_NAMES or tok in _WD_NAMES:
            out.add(_WD_NAMES.get(tok, _WD_NAMES.get(tok.rstrip("s"))))
        else:
            raise ValueError(f"bad days {days!r} (any|weekday|weekend|mon..sun[,..])")
    return frozenset(out) or None


def _day_ok(d: date, days: str) -> bool:
    wd = parse_days(days)
    return wd is None or d.weekday() in wd


def _days_ok(e: Event, days, start: date, end: date, today: date | None = None) -> bool:
    """days: filter string or a pre-parsed weekday set (None = any)."""
    wd = parse_days(days) if isinstance(days, str) else days
    if wd is None:
        return True
    return any(d.weekday() in wd for d in event_days(e, start, end, today=today))


def first_match_day(e: Event, f: "Filter", today: date | None = None) -> date | None:
    """First day inside the filter's window (and --days weekdays) the event actually happens on;
    None when it has none (callers fall back to e.date). One-offs: their own date."""
    if e.kind == "one_off":
        return e.date
    today = today or city_today(f)
    start, end = resolve_when(f.when, today)
    wd = parse_days(f.days)
    ds = [d for d in event_days(e, start, end, today=today) if wd is None or d.weekday() in wd]
    return min(ds) if ds else None


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
    wd = parse_days(f.days)                           # validate once (raises even with no events)
    cats = {c.lower() for c in f.categories}
    venues = {v.lower() for v in f.venues}
    out: list[Event] = []
    events = [e for e in events if not is_banned_url(e.url)]
    for e in events:
        if e.kind not in f.kinds:
            continue
        lo = max(start, today)                        # past days never match
        if not _overlaps(e, lo, end, today):
            continue
        if e.date < today and (e.kind == "one_off" or (e.kind == "recurring" and e.end_date is None)) \
                and not _is_open_series(e):
            continue                                  # past one-offs never pass
        if e.kind in ("run", "recurring") and e.end_date and e.end_date < today:
            continue
        if f.public_only and not e.public:
            continue
        if e.online and not f.include_online:
            continue
        if is_elsewhere(e, f.city):
            continue                                  # touring dates outside the city
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
                if f.strict_time and not is_ongoing(e) and not e.time:
                    continue
            elif not (bs & f.time_of_day):
                if not e.time and stats is not None:
                    stats["untimed_hidden"] = stats.get("untimed_hidden", 0) + 1
                continue
        if not _days_ok(e, wd, lo, end, today):
            continue
        if stats is not None and sessions_uncertain(e):
            stats["check_days"] = stats.get("check_days", 0) + 1
        out.append(e)
    if f.drop_religious:
        from .filters import drop_religious_services
        out = drop_religious_services(out)
    if f.new_only and unseen is not None:
        fresh = {id(x) for x in unseen([e for e in out if not is_ongoing(e)])}
        out = [e for e in out if is_ongoing(e) or id(e) in fresh]
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
        parts.append(f.days + "s only" if f.days in ("weekday", "weekend") else f"days: {f.days}")
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
