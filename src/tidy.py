"""Query-load cleanup of cached events: noise rows, stale rows, series collapse, cross-source dupes.

Pure functions; main.run_query calls tidy() after classify(). Never mutates inputs.
"""
from __future__ import annotations
import re
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import urlparse
from .models import Event

_GENERIC = {"performance", "performances", "event", "events", "matinee", "evening performance",
            "matinee performance", "show", "evening show", "matinee show", "evening", "tickets", "ticket"}
_NON_EVENT = re.compile(
    r"^(?:general |museum |gallery |free )?admission\b|\badmission$|\b(?:space |gift |museum )?shop$|^shop\b|"
    r"interactive guide|^(?:buy )?tickets?\b.{0,15}$|^gifts?(?: cards?| vouchers?| shop)?$|^(?:gift )?(?:cards?|vouchers?)$|"
    r"^(?:become a )?(?:members?|membership)(?:\b.{0,15})?$|plan your visit|^(?:the )?(?:caf[eé]|restaurant)(?: (?:&|and) bar)?$",
    re.I)


def norm_title(t: str) -> str:
    """Lower-case letters/digits of any script (Cyrillic titles must not normalise to '')."""
    return re.sub(r"[\W_]+", " ", (t or "").lower()).strip()


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


def dedupe_cross_source(events: list[Event]) -> list[Event]:
    listing = _listing_urls(events)
    groups: dict[tuple, list[Event]] = defaultdict(list)
    for e in events:
        if e.kind == "one_off":
            groups[(norm_title(e.title), e.date, e.time)].append(e)
    drop: set[int] = set()
    for g in groups.values():
        if len({e.source for e in g}) < 2:
            continue
        best = max(g, key=lambda e: _url_rank(e, listing))
        drop.update(id(e) for e in g if e.source != best.source)
    return [e for e in events if id(e) not in drop]


def tidy(events: list[Event], today: date, site_names: set[str] | None = None) -> list[Event]:
    """site_names: lower-case configured source names; rows from other sources are dropped."""
    horizon = today + timedelta(days=426)      # ~14 months
    out: list[Event] = []
    for e in events:
        if site_names is not None and (e.source or "").lower() not in site_names:
            continue
        if is_noise(e):
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
