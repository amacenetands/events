"""`fix:` block semantics — the one place they are applied (build_events, after build,
before the venue's post_build hook). `date_order` is the exception: it must reach the date
parser, so build._specs passes it to normalize. Order:

    drop_title_re  drop events whose title matches (re.search, case-insensitive)
    keep_title_re  drop events whose title does not match (special-events-only cinemas)
    url_pattern    event URLs that do not match fall back to the listing URL
    weekly         title regex (re.search, case-insensitive; anchor with ^) -> weekdays on
                   runs/series ({"^WordPlay": [4]}), start moved like byday; one-offs
                   and undated events untouched
    byday          force weekdays on every run/series; a run's start moves to its first
                   session (no session in range -> left as is, stat fix.byday.no_session)
    range_means_weekly  a run still without weekdays is weekly on day D only when both ends
                   fall on D and the date text names D alone ("Fri 13 Nov – Fri 18 Dec") or
                   has a recurrence cue ("every", "weekly", "Fridays"). Start/end labels are
                   never a session list ("Thu 12 Nov – Sat 14 Nov" stays a daily run). Bare
                   same-weekday ends need the object form {min_days, title_re}. Otherwise
                   sessions_known=False. Weekly on <= 4 days -> kind recurring (normalize.series_kind).
    time_default   fill `time` when missing (last; not on exhibitions, TBC events or runs whose
                   sessions are unknown — a default there would be invented)

Counts land in ctx.stats["fix.<name>"].
"""
from __future__ import annotations

import re
from datetime import timedelta

from ..config import VenueConfig
from ..models import Event
from ..normalize import series_kind, weekday_of
from ..normalize.urls import is_map_url

__all__ = ["apply_fixes", "apply_fixes_stored"]

_CUE = re.compile(r"\b(?:every|each|weekly)\b|(?:mon|tues|wednes|thurs|fri|satur|sun)days\b|еженедельн|кажд"
                  r"|(?:понедельник|вторник|сред|четверг|пятниц|суббот|воскресень)ам\b", re.I)
_WD_PLURAL = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")


def _bump(ctx, key: str) -> None:
    ctx.stats[key] = ctx.stats.get(key, 0) + 1


def _series(e: Event) -> bool:
    """A dated run or recurring series (not one dated instance, not TBC)."""
    return not e.date_undetermined and (
        e.kind == "run" or (e.kind == "recurring" and (bool(e.byday) or e.end_date is not None)))


def _first_session(e: Event, days: list[int]) -> Event | None:
    """`date` moved to the first of `days` on/after it; None when the range has none."""
    for i in range(7):
        d = e.date + timedelta(days=i)
        if e.end_date is not None and d > e.end_date:
            return None
        if d.weekday() in days:
            return e if i == 0 else e.model_copy(update={"date": d})
    return None


def _set_days(e: Event, days: list[int], cue: bool = True) -> Event:
    """Weekdays set; kind from normalize.series_kind (a weekly programme on <= 4 days is
    recurring, bounded or open; 5+ days or forced opening days (cue=False) stay a run)."""
    upd: dict = dict(byday=sorted(set(days)), sessions_known=True)
    upd["kind"] = series_kind(days, cue)
    if upd["kind"] == "recurring":
        upd["recurrence"] = _WD_PLURAL[days[0]] if len(set(days)) == 1 else "multiple dates"
    return e.model_copy(update=upd)


def _weekly_days(e: Event, rule) -> list[int] | None:
    """[D] when the run is a weekly series on D under `rule` (see module doc), else None."""
    if not (e.end_date and e.start_known and e.end_date > e.date):
        return None
    d = e.date.weekday()
    if e.end_date.weekday() != d:
        return None
    text = e.raw_date or ""
    named = weekday_of(text)
    if (named is not None and named == frozenset({d})) or (not named and _CUE.search(text)):
        return [d]
    if not isinstance(rule, bool):          # opt-in: bare same-weekday ends
        if (e.end_date - e.date).days >= rule.min_days and (
                not rule.title_re or re.search(rule.title_re, e.title, re.I)):
            return [d]
    return None


def _one(e: Event, cfg: VenueConfig, listing: str, ctx) -> Event | None:
    fx = cfg.fix
    if fx.drop_title_re and re.search(fx.drop_title_re, e.title, re.I):
        _bump(ctx, "fix.drop_title_re")
        return None
    if fx.keep_title_re and not re.search(fx.keep_title_re, e.title, re.I):
        _bump(ctx, "fix.keep_title_re")
        return None
    if fx.url_pattern and e.url != listing and not re.search(fx.url_pattern, e.url):
        e = e.model_copy(update={"url": listing})
        _bump(ctx, "fix.url_pattern")
    if fx.weekly and _series(e):
        for rx, days in fx.weekly.items():
            if re.search(rx, e.title, re.I):
                moved = _first_session(e, days)
                if moved is None:
                    _bump(ctx, "fix.weekly.no_session")
                else:
                    e = _set_days(moved, days)
                    _bump(ctx, "fix.weekly")
                break
    if fx.byday and _series(e) and e.byday != fx.byday:
        moved = _first_session(e, fx.byday)
        if moved is None:
            _bump(ctx, "fix.byday.no_session")
        else:
            e = _set_days(moved, fx.byday, cue=False)
            _bump(ctx, "fix.byday")
    if fx.range_means_weekly and e.kind == "run" and not e.byday:
        days = _weekly_days(e, fx.range_means_weekly)
        if days:
            e = _set_days(e, days)
            _bump(ctx, "fix.range_means_weekly")
        elif e.sessions_known:
            e = e.model_copy(update={"sessions_known": False})
            _bump(ctx, "fix.range_means_weekly.unknown")
    if (fx.time_default and not e.time and not e.date_undetermined and e.category != "exhibition"
            and not (e.kind != "one_off" and not e.byday and not e.sessions_known)):
        e = e.model_copy(update={"time": fx.time_default})
        _bump(ctx, "fix.time_default")
    return e


def unmap_urls(cfg: VenueConfig, events: list[Event], ctx) -> list[Event]:
    """Generic, every venue: a map/directions link is never an event URL; falls back to the listing URL."""
    if not any(is_map_url(e.url) for e in events):
        return events
    listing = cfg.resolved_url(ctx.today.year)
    out = []
    for e in events:
        if is_map_url(e.url):
            e = e.model_copy(update={"url": listing})
            _bump(ctx, "fix.map_url")
        out.append(e)
    return out


def apply_fixes(cfg: VenueConfig, events: list[Event], ctx) -> list[Event]:
    """Events with the venue's `fix:` block applied (dropped ones removed). Never raises:
    a failing fix leaves that event as built and logs one error line per venue."""
    events = unmap_urls(cfg, events, ctx)
    fx = cfg.fix
    if not (fx.drop_title_re or fx.keep_title_re or fx.url_pattern or fx.time_default or fx.weekly or fx.byday
            or fx.range_means_weekly):
        return events
    listing = cfg.resolved_url(ctx.today.year)
    out: list[Event] = []
    for e in events:
        try:
            f = _one(e, cfg, listing, ctx)
        except Exception as ex:   # noqa: BLE001
            _bump(ctx, "fix.failed")
            head = f"{cfg.name}: fix failed"
            if not any(x.startswith(head) for x in ctx.errors):
                ctx.errors.append(f"{head} ({type(ex).__name__}: {str(ex)[:80]})")
            f = e
        if f is not None:
            out.append(f)
    return out



def apply_fixes_stored(events: list[Event], venues, today, crawl_ids: list[str] | None = None,
                       latest_crawl: str | None = None) -> list[Event]:
    """Query-load pass: the `fix:` blocks over stored rows the latest crawl did not rebuild
    (venue disabled, or the row's crawl id is not the city's latest), so kept rows follow the
    current config. Freshly built rows already went through fixes → hooks → enrich and are
    returned untouched. Order is preserved. Without crawl ids every row counts as not rebuilt."""
    from types import SimpleNamespace
    by_name = {v.name: v for v in venues}
    ctx = SimpleNamespace(today=today, stats={}, errors=[])
    out: list[Event] = []
    for i, e in enumerate(events):
        cfg = by_name.get(e.source)
        fresh = (crawl_ids is not None and latest_crawl is not None and crawl_ids[i] == latest_crawl
                 and cfg is not None and not cfg.disabled)
        if cfg is None:
            out.append(e)
        elif fresh:
            out.extend(unmap_urls(cfg, [e], ctx))
        else:
            out.extend(apply_fixes(cfg, [e], ctx))
    return out
