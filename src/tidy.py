"""Query-load cleanup of cached events: noise rows, stale rows, series collapse, cross-source dupes.

Pure functions; main.run_query calls tidy() after classify(). Never mutates inputs.
"""
from __future__ import annotations
import re
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import urlparse
from .classify import _norm_title
from .models import Event

_GENERIC = {"performance", "performances", "event", "events", "matinee", "evening performance",
            "matinee performance", "show", "evening show", "matinee show", "evening", "tickets", "ticket"}
_NON_EVENT = re.compile(
    r"^(?:general |museum |gallery |free )?admission\b|\badmission$|\b(?:space |gift |museum )?shop$|^shop\b|"
    r"interactive guide|^(?:buy )?tickets?\b.{0,15}$|^gifts?(?: cards?| vouchers?| shop)?$|^(?:gift )?(?:cards?|vouchers?)$|"
    r"^(?:become a )?(?:members?|membership)(?:\b.{0,15})?$|plan your visit|^(?:the )?(?:caf[eé]|restaurant)(?: (?:&|and) bar)?$",
    re.I)


_CANCEL_TITLE = re.compile(
    r"\b(?:cancell?ed|postponed|called\s+off|rescheduled\s+tbc)\b|отмен[её]н(?:[аоы]|ы)?\b|отменено|перенесен[оаы]?\b|перенес[её]н[аоы]?\b", re.I)
_CANCEL_DESC = re.compile(
    r"^\W*(?:cancell?ed|postponed)\b"
    r"|\b(?:this\s+|the\s+)?(?:event|show|performance|concert|class|session|workshop|talk|tour|screening|gig)"
    r"\s+(?:has\s+been\s+|is\s+|was\s+|will\s+be\s+)?(?:cancell?ed|postponed)\b"
    r"|\b(?:has\s+been|is\s+now|was)\s+(?:cancell?ed|postponed)\b"
    r"|(?:мероприятие|событие|концерт|спектакль|показ|лекция|экскурсия)\s+(?:было\s+|будет\s+)?(?:отменен[оаы]?|отмен[её]н[оаы]?|перенесен[оаы]?|перенес[её]н[оаы]?)\b"
    r"|^\W*(?:отменено|отмен[её]н[оаы]?|перенесено)\b", re.I)


def is_cancelled(e: Event) -> bool:
    """Title says cancelled/postponed, or the description announces it (en + ru)."""
    return bool(_CANCEL_TITLE.search(e.title or "") or _CANCEL_DESC.search(e.description or ""))


def norm_title(t: str) -> str:
    """classify's title key: letters/digits of any script, casefolded."""
    return _norm_title(t or "")


def is_noise(e: Event) -> bool:
    n = norm_title(e.title)
    return not n or n in _GENERIC or bool(_NON_EVENT.search((e.title or "").strip()))


def _span_end(e: Event) -> date | None:
    return e.end_date if e.kind in ("run", "recurring") else None


def _weekly(ds: list[date]) -> bool:
    return len({d.weekday() for d in ds}) == 1 and all((b - a).days == 7 for a, b in zip(ds, ds[1:]))


def collapse_series(events: list[Event]) -> list[Event]:
    """>=3 distinct dates of same (source, title) -> one run (min..max), unless strictly weekly.
    The run keeps the earliest session time; times_vary when the sessions start at several."""
    groups: dict[tuple, list[Event]] = defaultdict(list)
    for e in events:
        if e.kind in ("one_off", "recurring") and e.end_date is None and not e.date_undetermined:
            groups[(e.source, norm_title(e.title))].append(e)
    drop: set[int] = set()
    add: list[Event] = []
    for g in groups.values():
        ds = sorted({e.date for e in g})
        if len(ds) < 3 or _weekly(ds) or (ds[-1] - ds[0]).days > 150:
            continue
        g.sort(key=lambda e: (e.date, e.time or ""))
        base = g[0]
        drop.update(id(e) for e in g)
        times = sorted({e.time for e in g if e.time})      # keep the session time(s): "from 10:30"
        add.append(base.model_copy(update={
            "kind": "run", "date": ds[0], "end_date": ds[-1], "time": times[0] if times else None,
            "times_vary": len(times) > 1, "recurrence": None}))
    return [e for e in events if id(e) not in drop] + add


def _listing_urls(events: list[Event]) -> set[str]:
    cnt: dict[str, int] = defaultdict(int)
    for e in events:
        cnt[(e.url or "").rstrip("/")] += 1
    return {u for u, n in cnt.items() if n >= 4}


def _url_rank(e: Event, listing: set[str]) -> tuple:
    u = (e.url or "").rstrip("/")
    path = urlparse(u).path
    low = f"{e.source}".lower()
    return (u not in listing and bool(path), (e.venue or "").lower() in (low,) or low in (e.venue or "").lower(),
            len(path))


def _is_own(e: Event) -> bool:
    """The row comes from the venue's own site (source name ~ venue name), not an aggregator."""
    src, ven = norm_title(e.source or ""), norm_title(e.venue or "")
    return bool(src and ven and (src in ven or ven in src))


def _same_venue(a: Event, b: Event) -> bool:
    x, y = norm_title(a.venue or ""), norm_title(b.venue or "")
    return bool(x and y and (x in y or y in x))


def _merge_into(keep: Event, others: list[Event]) -> Event:
    """keep + fields it lacks (time, price, description, min_age) from the dropped duplicates."""
    upd = {}
    for f in ("time", "price", "description", "min_age"):
        if getattr(keep, f) in (None, ""):
            v = next((getattr(o, f) for o in others if getattr(o, f) not in (None, "")), None)
            if v is not None:
                upd[f] = v
    return keep.model_copy(update=upd) if upd else keep


def _span(e: Event) -> tuple[date, date]:
    return e.date, e.end_date or e.date


def dedupe_cross_source(events: list[Event]) -> list[Event]:
    """Same event listed by several sources -> one row, the venue's own source preferred
    (richer time/price/url), missing fields merged in from the dropped rows.
    1. one-offs with the same (title, date): same time -> best source wins; an untimed row loses
       to a timed row of another source.
    2. an aggregator run/series row whose dates are covered by the venue's own rows of the same
       title (or the reverse: its one-off falls inside the venue's own run) is dropped."""
    listing = _listing_urls(events)
    groups: dict[tuple, list[Event]] = defaultdict(list)
    for e in events:
        if e.kind == "one_off":
            groups[(norm_title(e.title), e.date)].append(e)
    drop: set[int] = set()
    repl: dict[int, Event] = {}
    for g in groups.values():
        if len({e.source for e in g}) < 2:
            continue
        by_time: dict = defaultdict(list)
        for e in g:
            by_time[e.time].append(e)
        keepers: dict = {}
        for t, rows in by_time.items():
            if len({e.source for e in rows}) < 2:
                keepers[t] = rows
                continue
            best = max(rows, key=lambda e: _url_rank(e, listing))
            lost = [e for e in rows if e.source != best.source]
            drop.update(id(e) for e in lost)
            repl[id(best)] = _merge_into(best, lost)
            keepers[t] = [e for e in rows if e.source == best.source]
        timed = [e for t, rows in keepers.items() if t for e in rows]
        for e in keepers.get(None, []):
            others = [o for o in timed if o.source != e.source]
            if others:
                drop.add(id(e))
                o = max(others, key=lambda o: _url_rank(o, listing))
                repl[id(o)] = _merge_into(repl.get(id(o), o), [e])
    live = [e for e in events if id(e) not in drop]
    own: dict[str, list[Event]] = defaultdict(list)
    for e in live:
        if _is_own(e):
            own[norm_title(e.title)].append(e)
    for e in live:
        if _is_own(e):
            continue
        for s in own.get(norm_title(e.title), ()):
            if s.source == e.source or not _same_venue(e, s) or e.date_undetermined or s.date_undetermined:
                continue
            (a0, a1), (b0, b1) = _span(e), _span(s)
            if a0 <= b1 and b0 <= a1 and (e.kind != "one_off" or s.kind != "one_off"):
                drop.add(id(e))
                repl[id(s)] = _merge_into(repl.get(id(s), s), [e])
                break
    return [repl.get(id(e), e) for e in events if id(e) not in drop]


def tidy(events: list[Event], today: date, site_names: set[str] | None = None) -> list[Event]:
    """site_names: lower-case configured source names; rows from other sources are dropped."""
    horizon = today + timedelta(days=426)      # ~14 months
    out: list[Event] = []
    for e in events:
        if site_names is not None and (e.source or "").lower() not in site_names:
            continue
        if is_noise(e) or is_cancelled(e):
            continue
        if e.kind in ("one_off",) and e.date < today:
            continue
        if e.kind in ("run", "recurring") and e.end_date and e.end_date < today:
            continue
        if e.kind == "recurring" and e.end_date is None and e.date < today:
            continue
        upd: dict = {}
        if "wonderlab" in e.title.lower() and (e.source or "").lower() == "science museum":
            upd["date_undetermined"] = True
            upd["kind"] = "run"
        if e.end_date and e.end_date > horizon:
            upd["end_date"] = None             # absurd end: show as open-ended
        out.append(e.model_copy(update=upd) if upd else e)
    # undetermined: one row per (source, title)
    seen: set = set()
    res: list[Event] = []
    for e in out:
        if e.date_undetermined:
            k = (e.source, norm_title(e.title))
            if k in seen:
                continue
            seen.add(k)
        res.append(e)
    res = collapse_series(res)
    return dedupe_cross_source(res)
