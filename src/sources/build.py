"""RawEvent -> Event: the one place page data becomes Events (deterministic, no I/O)."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from ..config import VenueConfig
from ..models import Event, RawEvent, norm_category
from ..normalize import DateSpec, find_price, occurrences, parse_date_specs, parse_price_text, parse_time_text
from ..normalize.urls import _absolute
from .base import SourceCtx

__all__ = ["build_events", "build_event", "GENERIC_TITLE_RE", "MAX_INSTANCES", "min_age_of"]

MAX_INSTANCES = 60      # dated instances expanded per RawEvent
RAW_DATE_MAX = 120

# Placeholder titles ("Concert - Sat 5 ...") a source or model sometimes emits.
GENERIC_TITLE_RE = re.compile(
    r"^(concert|event|show|performance|talk|exhibition|screening)\s*[-–]\s*\w{3}\s+\d",
    re.IGNORECASE,
)
_WD_PLURAL = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")
_ISO_MIDNIGHT = re.compile(r"\d{4}-\d{2}-\d{2}[T ]00:00(?::00(?:\.0+)?)?(?!\d)")
_AGE_RANGE = re.compile(r"(\d{1,2})\s*(?:[-–—]|to)\s*\d{1,2}")
_AGE_PLUS = re.compile(r"(\d{1,2})\s*\+|(?:ages?|aged|over)\s*(\d{1,2})", re.I)
_AGE_BARE = re.compile(r"^\s*(\d{1,2})\s*$")
_AUDIENCE = (("kids", re.compile(r"\b(?:kids?|child(?:ren)?|children'?s|babies|toddlers?)\b", re.I)),
             ("family", re.compile(r"\bfamil(?:y|ies)\b", re.I)),
             ("adult", re.compile(r"\badults?(?:\s+only)?\b|\b18\s*\+", re.I)))


def _bump(ctx: SourceCtx, key: str) -> None:
    ctx.stats[key] = ctx.stats.get(key, 0) + 1


# ── fields ───────────────────────────────────────────────────────────────────

def _url(raw: RawEvent, listing: str) -> str:
    """Absolute fragment-free event URL; the listing URL when missing or unusable
    (fix.url_pattern is applied in venues.fixes)."""
    u = _absolute(raw.url or "", listing) if raw.url and raw.url != "null" else None
    return u or listing


def _time(raw: RawEvent, spec: DateSpec | None) -> str | None:
    """Time field > time in date spec > time in date text; ISO midnight = no time
    (fix.time_default is applied in venues.fixes)."""
    t = parse_time_text(raw.time_text or "", field=True)
    if t:
        return t
    derived = (spec.times[0] if spec is not None and spec.times else None) or parse_time_text(raw.date_text or "")
    if derived == "00:00" and _ISO_MIDNIGHT.search(raw.date_text or ""):
        derived = None
    return derived or None


def _price(raw: RawEvent) -> str | None:
    return parse_price_text(raw.price_text or "") or find_price(raw.desc or "")


def min_age_of(text: str) -> int | None:
    """Lower age bound of '18+', '5-11', 'ages 3', '3'; None if none."""
    for rx in (_AGE_RANGE, _AGE_PLUS, _AGE_BARE):
        m = rx.search(text or "")
        if m:
            return int(next(g for g in m.groups() if g))
    return None


def _audience(raw: RawEvent, hint_audience: str, min_age: int | None) -> str:
    """Stated audience > age guidance > venue hint (classify() refines from title/desc)."""
    for aud, rx in _AUDIENCE:
        if raw.audience_text and rx.search(raw.audience_text):
            return aud
    if min_age is not None:
        return "kids" if min_age <= 11 else "family" if min_age < 18 else "adult"
    return hint_audience


def _category(raw: RawEvent, cfg: VenueConfig) -> tuple[str, str]:
    """(category, audience hint): source type word, else the venue's category_hint."""
    if raw.category_text and _AUDIENCE[0][1].fullmatch(raw.category_text.strip()):
        return norm_category(cfg.category_hint), "kids"      # "kids" hint -> "other"
    cat = norm_category(raw.category_text) if raw.category_text else "other"
    if cat != "other":
        return cat, "kids" if cfg.category_hint == "kids" else "all"
    if cfg.category_hint == "kids":
        return "other", "kids"
    return norm_category(cfg.category_hint), "all"


# ── dates ────────────────────────────────────────────────────────────────────

def _recurrence_label(byday: frozenset[int] | None) -> str:
    """'Thursdays' for a single weekday, else 'multiple dates'."""
    if byday and len(byday) == 1:
        return _WD_PLURAL[next(iter(byday))]
    return "multiple dates"


def _first_on_or_after(spec: DateSpec, day: date) -> date:
    """First session of a weekday series on/after `day` (day itself when none in a week)."""
    occ = occurrences(spec, day, day + timedelta(days=7), max_n=1)
    return occ[0] if occ else day


def _instances(spec: DateSpec, today: date) -> list[dict[str, Any]]:
    label = _recurrence_label(spec.byday)       # display only; instances carry byday=None
    days = [d for d in spec.instances if d >= today][:MAX_INSTANCES]
    return [dict(date=d, kind="recurring", recurrence=label, byday=None) for d in days]


def _series(spec: DateSpec, cfg: VenueConfig, today: date) -> dict[str, Any]:
    """run / recurring without instances. Bounded runs keep their start; open-ended ones
    (no end) start today or at their first session after it."""
    byday = sorted(spec.byday) if spec.byday else None
    sessions_known = spec.sessions_known      # fix.range_means_weekly: venues.fixes
    if spec.end and spec.start and spec.end <= spec.start:
        return dict(date=spec.start, kind="one_off", start_known=spec.start_known)
    if spec.kind == "recurring" and not byday:      # repeat rule without weekdays: no occurrences -> TBC
        return dict(date=max(spec.start or today, today), kind="one_off", start_known=spec.start_known,
                    date_undetermined=True)
    start = spec.start or today
    if spec.end is None or spec.start is None:
        start = max(start, today)
        if spec.byday:
            start = _first_on_or_after(spec, start)
    out = dict(date=start, end_date=spec.end, kind=spec.kind, byday=byday,
               sessions_known=sessions_known, start_known=spec.start_known)
    if spec.kind == "recurring":
        out["recurrence"] = _recurrence_label(spec.byday)
    return out


def _date_fields(spec: DateSpec, cfg: VenueConfig, today: date) -> list[dict[str, Any]]:
    """Per-Event date/kind fields for one spec (several for an instance list)."""
    if spec.instances:
        out = _instances(spec, today)
    elif spec.kind == "one_off":
        out = [dict(date=spec.start or today, kind="one_off", start_known=spec.start_known)]
    else:
        out = [_series(spec, cfg, today)]
    for f in out:
        f["date_inconsistent"] = spec.inconsistent
    return out


def _specs(raw: RawEvent, cfg: VenueConfig, ctx: SourceCtx) -> list[DateSpec]:
    order = "mdy" if cfg.fix.date_order == "mdy" else "dmy"   # ymd: ISO is order-free
    return parse_date_specs(raw.date_text or "", ctx.today, end_text=raw.end_text or "",
                            recurrence_text=raw.recurrence_text or "", order=order,
                            lang=ctx.city.lang, tz=ctx.city.tz)


# ── build ────────────────────────────────────────────────────────────────────

def _dropped_title(title: str) -> bool:
    """Empty or placeholder title (fix.drop_title_re is applied in venues.fixes)."""
    return not title or bool(GENERIC_TITLE_RE.match(title))


def _live(e: Event, today: date) -> bool:
    """Not over: open-ended series are always live."""
    if e.end_date is None and e.kind != "one_off":
        return True
    return (e.end_date or e.date) >= today


def _build(raw: RawEvent, cfg: VenueConfig, ctx: SourceCtx) -> list[Event]:
    title = (raw.title or "").strip()
    if _dropped_title(title):
        _bump(ctx, "build.dropped_title")
        return []
    specs = _specs(raw, cfg, ctx)
    category, hint_aud = _category(raw, cfg)
    min_age = min_age_of(raw.age_text) if raw.age_text else None
    common = dict(
        title=title, venue=(raw.venue or "").strip() or cfg.name, source=cfg.name,
        url=_url(raw, cfg.resolved_url(ctx.today.year)), description=raw.desc or None,
        price=_price(raw), category=category, audience=_audience(raw, hint_aud, min_age), min_age=min_age,
        raw_date=(raw.date_text or "")[:RAW_DATE_MAX] or None, sold_out=bool(raw.sold_out),
    )
    groups = ([(s, f) for s in specs for f in _date_fields(s, cfg, ctx.today)] if specs
              else [(None, dict(date=ctx.today, date_undetermined=True))])
    if not groups:                     # every instance already past
        _bump(ctx, "build.dropped_past")
        return []
    out: list[Event] = []
    for spec, fields in groups:
        e = Event(**common, time=_time(raw, spec), **fields)
        if _live(e, ctx.today):
            out.append(e)
        else:
            _bump(ctx, "build.dropped_past")
    return out


def build_event(raw: RawEvent, cfg: VenueConfig, ctx: SourceCtx) -> list[Event]:
    """Events for one RawEvent ([] when dropped); never raises (failures -> stats + one error line per venue)."""
    try:
        return _build(raw, cfg, ctx)
    except Exception as ex:   # noqa: BLE001 — one bad record must not sink the venue
        _bump(ctx, "build.failed")
        head = f"{cfg.name}: build failed"
        if not any(e.startswith(head) for e in ctx.errors):
            ctx.errors.append(f"{head} ({type(ex).__name__}: {str(ex)[:80]}; first: {raw.title[:40]!r})")
        return []


def build_events(raws: list[RawEvent], cfg: VenueConfig, ctx: SourceCtx) -> list[Event]:
    """Events for all RawEvents of one venue: hook post_raw -> build -> `fix:` block
    (venues.fixes) -> hook post_build -> de-duplicated on event_key() unless the description
    or url differs within one source (stat build.kept_variant)."""
    from ..venues import run_hook
    from ..venues.fixes import apply_fixes
    raws = run_hook("post_raw", cfg, raws, ctx)
    seen: dict[str, tuple[str, set[tuple]]] = {}
    built: list[Event] = []
    for raw in raws:
        src = raw.source_kind or ""
        for e in build_event(raw, cfg, ctx):
            k, variant = e.event_key(), (e.description, e.url)
            if k in seen:
                first_src, variants = seen[k]
                if src != first_src or variant in variants:
                    continue                        # same event again (or from a merged source)
                # one source, same title/venue/date/time, another description or link: a
                # separate session (e.g. matinée + evening cards without times) — keep both
                _bump(ctx, "build.kept_variant")
                variants.add(variant)
            else:
                seen[k] = (src, {variant})
            built.append(e)
    built = run_hook("post_build", cfg, apply_fixes(cfg, built, ctx), ctx)
    done: set[tuple] = set()
    out: list[Event] = []
    for e in built:                                 # fixes/hooks may have made two identical
        k = (e.event_key(), e.description, e.url)
        if k not in done:
            done.add(k)
            out.append(e)
    return out
