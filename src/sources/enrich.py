"""Enrich pass (plan §1.4): fill gaps the listing left (time, price, sessions) from each
event's detail page. Gap-fill only — a listing value is never overwritten.

    events = await enrich_events(events, cfg, ctx, listing_url=fr.url)

- Runs when `cfg.enrich.fields` is non-empty (`time`, `price`, `sessions`). An event is a
  candidate when a requested field is missing (`time`/`price` None, `sessions_known` False)
  and it has its own URL (not the listing; matching `enrich.link_pattern` when set).
- One fetch per URL (shared page cache, offline-aware, src/fetch/detail.py), soonest
  events first, at most `enrich.max` pages per venue.
- Page sessions come from the first of: JSON-LD Event nodes, `<time>` tags, short visible
  lines naming a weekday ("Thursday 19 November", "10 am" on the next line) above any
  "You may also like" block — all through normalize.parse_date_specs.
- Sessions: a weekly pattern (>= 75% of its weekdays present) -> recurring + byday; one
  upcoming date on the whole page -> one_off; a sparse list (<= SPARSE_MAX) -> one dated
  instance per session. The listing end is never shrunk (extended when the page goes on).
- Never raises and never drops an event (`enrich.failed`).
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urldefrag, urljoin, urlsplit

from ..config import VenueConfig
from ..fetch.detail import fetch_detail
from ..models import Event
from ..normalize import parse_date_specs, parse_price_text
from .base import SourceCtx
from .jsonld import _schedule, _sold_out, event_nodes, price_text

__all__ = ["enrich_events", "needs", "parse_detail", "apply_detail", "Detail"]

MAX_CONCURRENT = 4
LINE_MAX = 80               # longer visible lines are prose, not session rows
SEGMENT_LINES = 12          # lines after a session row searched for "Sold out"
LAST_SEGMENT = 4            # last row's segment when there is no earlier row to size it by
PRICE_LINES = 60            # lines after the title searched for a price without session rows
WEEKLY_MIN = 0.75           # share of a weekday pattern's dates the page must list
SPARSE_MAX = 12             # sparse sessions up to this many become dated instances
FIELDS = ("time", "price", "sessions")
BROWSER_MODES = ("browser", "browser_stealth")
_WD = re.compile(r"\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?(?![a-z])"
                 r"|(?<![а-яё])(?:пн|вт|ср|чт|пт|сб|вс|понедельник|вторник|сред[аыу]|четверг|"
                 r"пятниц[аыу]|суббот[аыу]|воскресень[еяю])(?![а-яё])", re.I)
_TIME_ONLY = re.compile(r"^\s*\d{1,2}(?:[.:]\d{2})?\s*(?:[ap]\.?m\.?)?(?:\s*[–—&,-]\s*\d{1,2}(?:[.:]\d{2})?"
                        r"\s*(?:[ap]\.?m\.?)?)*\s*$", re.I)
# a status line that is only "Sold out" (optionally "(join waiting list)")
_SOLD = re.compile(r"(?<!almost )(?<!nearly )(?<!not )\bsold[\s-]*out\b(?!\s*\?)|распродан|билетов нет", re.I)
_WAITLIST = re.compile(r"\(?\s*join (?:the )?wait(?:ing)?[\s-]?list\s*\)?", re.I)
_RELATED = re.compile(r"^(?:you (?:may|might) also like|related(?: events| shows)?|more events|similar(?: events)?"
                      r"|see also|вам может понравиться|похожие(?: события)?)\b", re.I)
_BOOK = re.compile(r"^(?:book|buy)\b", re.I)
_AMT = re.compile(r"([£€$])\s?(\d{1,4}(?:[.,]\d{2})?)(?!\d)|(?<![\d.,])(\d{2,6}(?:[.,]\d{2})?)\s?(?:₽|руб)")
_FROM = re.compile(r"\bfrom\b:?\s*[£€$]\s?\d|\bот\s+\d+\s?(?:₽|руб)", re.I)
_RESTRICTED = re.compile(r"member|concession|\bconcs?\b|student|under[\s-]?\d|over[\s-]?\d|\d\s?\+|child|kids?\b"
                         r"|senior|unwaged|access|carer|companion|dance pass|fee\b|льгот|студен|детск|пенсион", re.I)
_DAY_IDX = {"mo": 0, "tu": 1, "we": 2, "th": 3, "fr": 4, "sa": 5, "su": 6}

Session = tuple[date, "str | None"]


@dataclass
class Detail:
    """What one detail page says about its event."""
    sessions: list[Session] = field(default_factory=list)   # (date, first time) sorted, unique
    price: str | None = None
    sold_out: bool = False
    byday: list[int] | None = None       # JSON-LD eventSchedule weekdays (no dated sessions)
    time: str | None = None              # JSON-LD / schedule time when sessions carry none


def _bump(ctx: SourceCtx, key: str, n: int = 1) -> None:
    ctx.stats[key] = ctx.stats.get(key, 0) + n


def _norm(u: str) -> str:
    return urldefrag(u or "")[0].rstrip("/")


def needs(e: Event, fields: list[str]) -> bool:
    """A requested field is missing on e."""
    return (("time" in fields and e.time is None) or ("price" in fields and e.price is None)
            or ("sessions" in fields and not e.sessions_known))


# ---------------------------------------------------------------- page parsing

def _specs_sessions(text: str, ctx: SourceCtx, order: str) -> list[Session] | None:
    """Dated sessions in text; None when nothing parsed. A bare midnight is no time."""
    specs = parse_date_specs(text, ctx.today, order=order, lang=ctx.city.lang, tz=ctx.city.tz)
    if not specs:
        return None
    out: list[Session] = []
    for sp in specs:
        t = sp.times[0] if sp.times else None
        t = None if t == "00:00" and "T00:00" in text else t
        if sp.instances:
            out += [(d, t) for d in sp.instances]
        elif sp.kind == "one_off" and sp.start is not None and sp.end is None:
            out.append((sp.start, t))
    return out


def _own_nodes(nodes: list[dict], url: str, title: str) -> list[dict]:
    """The page's own Event nodes: url/@id on this page or name == title; else all of them
    when they share one name (one event with several sessions)."""
    tl = (title or "").strip().lower()
    own = [n for n in nodes
           if any(_norm(urljoin(url, str(n.get(k) or ""))) == _norm(url) for k in ("url", "@id") if n.get(k))
           or (tl and str(n.get("name") or "").strip().lower() == tl)]
    if own:
        return own
    names = {str(n.get("name") or "").strip().lower() for n in nodes}
    return nodes if len(names) == 1 else []


def _jsonld(html: str, url: str, title: str, ctx: SourceCtx, order: str, d: Detail) -> list[Session]:
    nodes = _own_nodes(event_nodes(html), url, title)
    out: list[Session] = []
    for n in nodes:
        start, end = str(n.get("startDate") or ""), str(n.get("endDate") or "")
        s = _specs_sessions(start, ctx, order) if start else None
        e = _specs_sessions(end, ctx, order) if end else None
        if s and (not e or e[0][0] == s[0][0]):          # single-day node = one session
            out += s
        elif s and s[0][1] and d.time is None:
            d.time = s[0][1]
        sch = _schedule(n)
        days = sch.get("byDay")
        days = days if isinstance(days, list) else [days] if days else []
        idx = sorted({_DAY_IDX[k] for x in days if (k := str(x).rsplit("/", 1)[-1].lower()[:2]) in _DAY_IDX})
        if idx:
            d.byday = idx
            m = re.match(r"(\d{1,2}:\d{2})", str(sch.get("startTime") or ""))
            d.time = d.time or (m.group(1) if m else None)
        if d.price is None and (p := price_text(n, ctx.city.currency)):
            d.price = parse_price_text(p)
    d.sold_out = bool(nodes) and all(_sold_out(n) for n in nodes)
    return out


def _other_page(href: str, url: str) -> bool:
    """href leads to another page on the same site (a related-event card, not a booking link)."""
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return False
    full = urljoin(url, href)
    return urlsplit(full).hostname == urlsplit(url).hostname and _norm(full) != _norm(url)


def _foreign(tag, url: str) -> bool:
    """<time> inside a link to another page (a related-event card)."""
    a = tag.find_parent("a", href=True)
    return a is not None and _norm(urljoin(url, a["href"])) != _norm(url)


def _time_tags(soup, url: str, ctx: SourceCtx, order: str) -> list[Session]:
    out: list[Session] = []
    for t in soup.find_all("time"):
        if _foreign(t, url):
            continue
        out += _specs_sessions(t.get("datetime") or t.get_text(" ", strip=True), ctx, order) or []
    return out


def _text_rows(lines: list[str], ctx: SourceCtx, order: str) -> tuple[list[Session], list[int], int]:
    """(sessions, their line indexes, unparsed rows) from short weekday+digit lines; a
    following time-only line ("10 am") is joined."""
    out: list[Session] = []
    rows: list[int] = []
    unparsed = 0
    for i, ln in enumerate(lines):
        if len(ln) > LINE_MAX or not _WD.search(ln) or not re.search(r"\d", ln):
            continue
        txt = ln + (" " + lines[i + 1] if i + 1 < len(lines) and _TIME_ONLY.match(lines[i + 1]) else "")
        got = _specs_sessions(txt, ctx, order)
        if got is None:
            unparsed += 1
        elif got:
            out += got
            rows.append(i)
    return out, rows, unparsed


def _sold_line(x: str) -> bool:
    """The line is essentially only "Sold out" (± "join waiting list")."""
    core = _WAITLIST.sub("", x).strip(" .!()–—-:|·")
    return len(core) <= 20 and bool(_SOLD.search(core)) and not re.sub(_SOLD, "", core).strip(" .!()–—-:|·")


def _sold_rows(lines: list[str], rows: list[int], headings: set[str]) -> bool:
    """Every session row's segment says "Sold out". A row's segment ends at the next row; the
    last one is as long as the longest earlier segment (else LAST_SEGMENT) and stops at a
    price, "Book…" or heading line."""
    if not rows:
        return False
    segs = [lines[r:min(n, r + SEGMENT_LINES)] for r, n in zip(rows, rows[1:])]
    size = max((len(s) for s in segs), default=LAST_SEGMENT)
    last: list[str] = [lines[rows[-1]]]
    for x in lines[rows[-1] + 1:rows[-1] + size]:
        if x in headings or _BOOK.match(x) or _AMT.search(x):
            break
        last.append(x)
    return all(any(_sold_line(x) for x in s) for s in segs + [last])


def _uniq(ss: list[Session]) -> list[Session]:
    """Sorted, one entry per (date, time); a timeless entry is dropped when its date has a time."""
    timed = {d for d, t in ss if t}
    return sorted({(d, t) for d, t in ss if t or d not in timed}, key=lambda s: (s[0], s[1] or ""))


def _amounts(seg: str) -> list[tuple[str, float]]:
    out = []
    for m in _AMT.finditer(seg):
        sym, num = (m[1], m[2]) if m[1] else ("₽", m[3])
        try:
            out.append((sym, float(num.replace(",", "."))))
        except ValueError:
            pass
    return out


def _scoped_price(lines: list[str], start: int, rows: list[int]) -> str | None:
    """Price between the title line and the first session row: an explicit "Prices from £N"
    first, else the lowest non-zero general tier (member/concession/... tiers and fees
    skipped), else Free when only zero amounts. Falls back to the first row's segment."""
    stop = next((r for r in rows if r > start), None)
    windows = [lines[start:stop if stop is not None else start + PRICE_LINES]]
    if stop is not None:
        windows.append(lines[stop:stop + SEGMENT_LINES])
    for win in windows:
        for ln in win:
            if _FROM.search(ln) and not _RESTRICTED.search(ln) and (p := parse_price_text(ln)):
                return p
        found: list[tuple[str, float]] = []
        for ln in win:
            for seg in re.split(r"\s[/|;]\s|,\s", ln):
                if not _RESTRICTED.search(seg):
                    found += _amounts(seg)
        if found:
            paid = [a for a in found if a[1] > 0]
            if not paid:
                return "Free"
            sym, v = min(paid, key=lambda a: a[1])
            return parse_price_text(f"{v:.2f} ₽" if sym == "₽" else f"{sym}{v:.2f}")
    return None


def _title_line(lines: list[str], names: list[str]) -> int:
    """Index of the earliest line that is the page's title (og:title, listing title, h1), else
    the earliest containing one; 0 when none."""
    names = [n.strip().lower() for n in names if n and n.strip()]
    low = [ln.lower() for ln in lines]
    for same in (str.__eq__, str.__contains__):          # earliest exact line, then containment
        hits = [i for i, ln in enumerate(low) if any(same(ln, nm) for nm in names)]
        if hits:
            return hits[0]
    return 0


def _drop_related(lines: list[str], levels: dict[str, int]) -> list[str]:
    """Lines without "You may also like"-style sections: from such a line to the next heading
    of a higher level (h2 after an h3 block), or to the end when it is no known heading."""
    out: list[str] = []
    skip: int | None = None             # level of the related heading being skipped
    for ln in lines:
        if skip is not None:
            if levels.get(ln, 9) < skip:
                skip = None
            else:
                continue
        if len(ln) <= LINE_MAX and _RELATED.match(ln):
            skip = levels.get(ln, 0)
            continue
        out.append(ln)
    return out


def parse_detail(html: str, url: str, title: str, cfg: VenueConfig, ctx: SourceCtx) -> Detail:
    """Sessions / price / sold-out from one detail page (`url` = post-redirect)."""
    from bs4 import BeautifulSoup
    order = cfg.fix.date_order
    d = Detail()
    ld = _jsonld(html, url, title, ctx, order, d)
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", attrs={"property": "og:title"})
    og_t = re.split(r"\s+[|–—]\s+", og.get("content", ""))[0] if og else ""
    tags = _time_tags(soup, url, ctx, order)
    for t in soup(["script", "style", "noscript", "template", "head"]):
        t.decompose()
    for a in soup.find_all("a", href=True):       # related-event cards carry their own dates
        if _other_page(a["href"], url) and _WD.search(a.get_text(" ")) and re.search(r"\d", a.get_text()):
            a.decompose()
    h1s = [h.get_text("\n", strip=True).splitlines() for h in soup.find_all("h1")]
    names = [og_t, title] + [x for ls in h1s for x in ls[:1]] + [" ".join(ls) for ls in h1s]
    levels: dict[str, int] = {}
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        t = h.get_text(" ", strip=True)
        levels[t] = min(levels.get(t, 9), int(h.name[1]))
    lines = _drop_related(soup.get_text("\n", strip=True).splitlines(), levels)
    headings = set(levels)
    text, rows, unparsed = _text_rows(lines, ctx, order)
    if unparsed:
        _bump(ctx, "enrich.sessions_unparsed", unparsed)
    d.sessions = _uniq(ld or tags or text)
    d.sold_out = d.sold_out or _sold_rows(lines, rows, headings)
    if d.price is None:
        d.price = _scoped_price(lines, _title_line(lines, names), rows)
    return d


# ---------------------------------------------------------------- applying

def _belongs(e: Event, up: list[Session], today: date) -> list[Session]:
    """Upcoming sessions of e: its date (one_off / dated instance), else from its start
    (or today) to its end — open-ended when it has none — on its weekdays when known."""
    if e.kind == "one_off" or (e.kind == "recurring" and e.end_date is None and not e.byday):
        return [s for s in up if s[0] == e.date]
    lo = max(e.date if e.start_known else today, today)
    return [s for s in up if s[0] >= lo and (e.end_date is None or s[0] <= e.end_date)
            and (not e.byday or s[0].weekday() in e.byday)]


def _end(e: Event, last: date, ctx: SourceCtx) -> date | None:
    """Listing end, never shrunk: extended to the page's last session; None stays None."""
    if e.end_date is None:
        _bump(ctx, "enrich.end_unknown")
        return None
    if last > e.end_date:
        _bump(ctx, "enrich.end_extended")
        return last
    return e.end_date


def _time_upd(rel: list[Session], ctx: SourceCtx) -> dict:
    """Earliest session time; times_vary when the sessions have several."""
    times = sorted({t for _, t in rel if t})
    if not times:
        return {}
    _bump(ctx, "enrich.filled.time")
    if len(times) > 1:
        _bump(ctx, "enrich.time_varies")
        return {"time": times[0], "times_vary": True}
    return {"time": times[0]}


def _sessions_upd(e: Event, d: Detail, up: list[Session], ctx: SourceCtx) -> dict | list[date] | None:
    """Field update for e's sessions, a list of dates to split e into (sparse), or None."""
    today = ctx.today
    lo = max(e.date if e.start_known else today, today)
    mine = [s for s in up if s[0] >= lo]
    days = sorted({s[0] for s in mine})
    if len({s[0] for s in up}) == 1 and days:
        return dict(date=days[0], end_date=None, start_known=True, sessions_known=True, kind="one_off", byday=None)
    if len(days) >= 2:
        wd = sorted({x.weekday() for x in days})
        span = (days[-1] - days[0]).days + 1
        expected = sum(1 for k in range(span) if (days[0] + timedelta(days=k)).weekday() in wd)
        if len(days) < WEEKLY_MIN * expected:
            _bump(ctx, "enrich.sessions_sparse")
            if len(days) <= SPARSE_MAX:
                return days
            return dict(date=days[0], end_date=_end(e, days[-1], ctx), start_known=True)
        # keep a listing start that is still ahead and on the pattern (page may list from later)
        start = e.date if (e.start_known and today <= e.date <= days[0] and e.date.weekday() in wd) else days[0]
        daily = len(wd) == 7
        upd = dict(date=start, end_date=_end(e, days[-1], ctx), start_known=True, sessions_known=True,
                   kind="run" if daily else "recurring", byday=None if daily else wd)
        if not daily and not e.recurrence:
            from ..digest import days_label
            upd["recurrence"] = days_label(wd, ctx.city.lang)
        return upd
    if d.byday and not d.sessions:
        upd = dict(sessions_known=True, kind="recurring", byday=d.byday)
        if not e.recurrence:
            from ..digest import days_label
            upd["recurrence"] = days_label(d.byday, ctx.city.lang)
        return upd
    return None


def apply_detail(e: Event, d: Detail, fields: list[str], ctx: SourceCtx) -> list[Event]:
    """e with missing requested fields filled from d (never overwrites); a sparse session
    list splits it into one dated instance per session."""
    up = [s for s in d.sessions if s[0] >= ctx.today]
    out: list[Event] = [e]
    if "sessions" in fields and not e.sessions_known:
        got = _sessions_upd(e, d, up, ctx)
        if isinstance(got, list):
            _bump(ctx, "enrich.filled.sessions")
            _bump(ctx, "enrich.instances", len(got))
            out = []
            for day in got:
                inst = e.model_copy(update=dict(date=day, end_date=None, kind="recurring", byday=None,
                                                sessions_known=True, start_known=True))
                if "time" in fields and e.time is None:
                    inst = inst.model_copy(update=_time_upd([s for s in up if s[0] == day], ctx))
                out.append(inst)
            return [_rest(x, d, fields, ctx) for x in out]
        if got:
            if "sessions_known" in got:
                _bump(ctx, "enrich.filled.sessions")
            out = [e.model_copy(update=got)]
    ev = out[0]
    if "time" in fields and ev.time is None:
        rel = _belongs(ev, up, ctx.today)
        upd = _time_upd(rel, ctx)
        if not upd and not rel and d.time and not d.sessions:
            upd = {"time": d.time}
            _bump(ctx, "enrich.filled.time")
        if upd:
            ev = ev.model_copy(update=upd)
    return [_rest(ev, d, fields, ctx)]


def _rest(e: Event, d: Detail, fields: list[str], ctx: SourceCtx) -> Event:
    upd: dict = {}
    if "price" in fields and e.price is None and d.price:
        upd["price"] = d.price
        _bump(ctx, "enrich.filled.price")
    if d.sold_out and not e.sold_out:
        upd["sold_out"] = True
        _bump(ctx, "enrich.sold_out")
    return e.model_copy(update=upd) if upd else e


# ---------------------------------------------------------------- driver

def _plan(events: list[Event], cfg: VenueConfig, ctx: SourceCtx, listing: set[str]) -> dict[str, list[int]]:
    """{url: event indexes} to enrich (one entry per normalised URL), soonest first, capped."""
    ec = cfg.enrich
    rx = re.compile(ec.link_pattern) if ec.link_pattern else None
    by_url: dict[str, tuple[str, list[int]]] = {}
    for i, e in enumerate(events):
        if not needs(e, ec.fields):
            continue
        _bump(ctx, "enrich.candidates")
        u = _norm(e.url)
        if not u or u in listing or (rx and not rx.search(e.url)):
            _bump(ctx, "enrich.skipped_url")
            continue
        by_url.setdefault(u, (e.url, []))[1].append(i)
    keys = sorted(by_url, key=lambda k: min(events[i].date for i in by_url[k][1]))
    if len(keys) > ec.max:
        _bump(ctx, "enrich.skipped_cap", len(keys) - ec.max)
    return dict(by_url[k] for k in keys[:max(ec.max, 0)])


async def _enrich(events: list[Event], cfg: VenueConfig, ctx: SourceCtx, listing_url: str | None) -> list[Event]:
    from ..fetch import HOST_GAP, HostThrottle
    listing = {_norm(x) for x in (cfg.resolved_url(ctx.today.year), listing_url) if x}
    plan = _plan(events, cfg, ctx, listing)
    if not plan:
        return events
    sem, throttle = asyncio.Semaphore(MAX_CONCURRENT), HostThrottle()
    gap = cfg.fetch.gap if cfg.fetch.gap is not None else HOST_GAP
    browser = cfg.fetch.mode if cfg.fetch.mode in BROWSER_MODES else None
    out: list[list[Event]] = [[e] for e in events]

    async def one(url: str, idx: list[int]) -> None:
        try:
            got = await fetch_detail(url, cfg, ctx, sem, prefix="enrich", throttle=throttle, gap=gap,
                                     browser=browser, get=lambda u, f: _cache_get(u, f),
                                     put=lambda u, h, n: _cache_put(u, h, n))
            if got is None:
                return
            det = parse_detail(got[0], got[1], events[idx[0]].title, cfg, ctx)
            new = {i: apply_detail(events[i], det, cfg.enrich.fields, ctx) for i in idx}
            for i, evs in new.items():
                out[i] = evs
        except Exception:   # noqa: BLE001 — one bad page never touches the listing events
            _bump(ctx, "enrich.failed")

    _bump(ctx, "enrich.pages", len(plan))
    await asyncio.gather(*(one(u, ix) for u, ix in plan.items()))
    seen: set[tuple] = set()
    res: list[Event] = []
    for e in (x for evs in out for x in evs):     # two listing rows may now be the same session
        k = (e.event_key(), e.description, e.url)
        if k not in seen:
            seen.add(k)
            res.append(e)
    return res


# One page cache for every detail fetch: looked up through follow_links at call time, so a
# stub of follow_links._cache_get/_cache_put (tests, the eval harness) covers enrich too.
def _cache_get(url: str, force: bool) -> str | None:
    from . import follow_links as FL
    return FL._cache_get(url, force)


def _cache_put(url: str, html: str, site_name: str) -> None:
    from . import follow_links as FL
    FL._cache_put(url, html, site_name)


async def enrich_events(events: list[Event], cfg: VenueConfig, ctx: SourceCtx,
                        listing_url: str | None = None) -> list[Event]:
    """events with gaps filled from detail pages; the input unchanged on any error."""
    if not cfg.enrich or not cfg.enrich.fields or not events:
        return events
    try:
        return await _enrich(events, cfg, ctx, listing_url)
    except Exception:   # noqa: BLE001
        _bump(ctx, "enrich.failed")
        return events
