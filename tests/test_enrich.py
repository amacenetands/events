"""Enrich pass (src/sources/enrich.py): detail-page gap-fill. Offline: httpx.MockTransport +
stubbed page cache; dates derived from `today` so the tests hold on any day."""
import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest

from src import venues
from src.config import VenueConfig, get_city
from src.digest import schedule_label, time_label
from src.fetch.types import FetchResult, FetchStatus
from src.models import Event
from src.sources import enrich as EN
from src.sources import follow_links as FL
from src.sources.base import SourceCtx
from src.venues import VenueHook, venue_hook

TODAY = date.today()
LISTING = "https://venue.test/whats-on/"
URL = LISTING + "show/"


def _next(wd: int, after: date = TODAY, weeks: int = 0) -> date:
    """First date with weekday wd strictly after `after`, plus `weeks`."""
    d = after + timedelta(days=1)
    return d + timedelta(days=(wd - d.weekday()) % 7 + 7 * weeks)


def _long(d: date) -> str:
    return f"{d:%A} {d.day} {d:%B} {d.year}"


def _cfg(fetch=None, **enrich) -> VenueConfig:
    return VenueConfig(name="Test Venue", url=LISTING, extract=["jsonld"], fetch=fetch or {"gap": 0},
                       enrich={"fields": ["time", "price", "sessions"], **enrich})


def _ev(title="Show", d=None, end=None, url=URL, **kw) -> Event:
    d = d or _next(3)
    base = dict(title=title, venue="Test Venue", date=d, end_date=end, url=url,
                kind="run" if end else "one_off", sessions_known=end is None)
    return Event(**{**base, **kw})


def _page(body: str, title: str = "Show", pre: str = "") -> str:
    return (f"<html><head><meta property='og:title' content='{title}'></head>"
            f"<body>{pre}<h1>{title}</h1>{body}</body></html>")


def _rows(dates_times: list[tuple[date, str]], tail: str = "Book now") -> str:
    return "".join(f"<div><p>{_long(d)}</p><p>{t}</p><p>{tail}</p></div>" for d, t in dates_times)


@pytest.fixture
def cache(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr(FL, "_cache_get", lambda url, force: None if force else store.get(url))
    monkeypatch.setattr(FL, "_cache_put", lambda url, html, name: store.__setitem__(url, html))
    return store


def _run(events, cfg, pages: dict[str, str], seen: list | None = None, offline=False):
    def handle(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(req.url))
        body = pages.get(str(req.url))
        return httpx.Response(200, text=body) if body is not None else httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            ctx = SourceCtx(http=http, today=TODAY, window_days=90, city=get_city("london"), offline=offline)
            return await EN.enrich_events(events, cfg, ctx, listing_url=LISTING), ctx
    return asyncio.run(go())


def _one(e, body, cfg=None, **kw):
    out, ctx = _run([e], cfg or _cfg(), {e.url: body}, **kw)
    return out, ctx


# ---------------------------------------------------------------- sessions

def test_weekly_sessions_keep_listing_end(cache):
    t1, t3 = _next(3), _next(3, weeks=2)
    end = t3 + timedelta(days=3)
    e = _ev(d=TODAY, end=end)
    page = _page("<p>Prices from: £12.00</p>" + _rows([(t1, "10 am"), (_next(3, weeks=1), "10 am"), (t3, "10 am")]))
    (out,), ctx = _one(e, page)
    assert out.byday == [3] and out.kind == "recurring" and out.sessions_known and out.recurrence == "Thursdays"
    assert (out.date, out.end_date) == (t1, end)                  # never shrunk to the last session
    assert out.time == "10:00" and not out.times_vary and out.price == "From £12"
    assert ctx.stats["enrich.filled.sessions"] == 1 and ctx.stats["enrich.filled.price"] == 1


def test_listing_start_on_pattern_kept(cache):
    t0 = _next(3)
    e = _ev(d=t0, end=_next(3, weeks=4))
    (out,), _ = _one(e, _page(_rows([(_next(3, weeks=k), "7pm") for k in (1, 2, 3, 4)])))
    assert out.date == t0 and out.byday == [3]


def test_sessions_beyond_listing_end_extend(cache):
    t1, t2, t3 = _next(5), _next(5, weeks=1), _next(5, weeks=2)
    e = _ev(d=t1, end=t2)
    (out,), ctx = _one(e, _page(_rows([(t1, "2pm"), (t2, "2pm"), (t3, "2pm")])))
    assert out.end_date == t3 and ctx.stats["enrich.end_extended"] == 1


def test_page_shows_only_next_n(cache):
    end = TODAY + timedelta(days=90)
    e = _ev(d=TODAY, end=end)
    (out,), _ = _one(e, _page(_rows([(_next(3, weeks=k), "11am") for k in range(3)])))
    assert out.byday == [3] and out.end_date == end and out.sessions_known


def test_open_run_with_today_session(cache):
    w = TODAY.weekday()
    e = _ev(d=TODAY, kind="run", sessions_known=False)               # open run: no end
    ses = [(TODAY + timedelta(days=7 * k), "6pm") for k in range(3)]
    (out,), ctx = _one(e, _page(_rows(ses)))
    assert out.byday == [w] and out.end_date is None and out.kind == "recurring" and out.date == TODAY
    assert out.time == "18:00" and ctx.stats["enrich.end_unknown"] == 1


def test_time_varies_earliest_and_flag(cache):
    t1, t2 = _next(5), _next(5, weeks=1)
    e = _ev(d=t1, end=t2)
    (out,), ctx = _one(e, _page(_rows([(t1, "11 am"), (t1, "10 am"), (t2, "10 am"), (t2, "11 am")])))
    assert out.byday == [5] and out.time == "10:00" and out.times_vary and ctx.stats["enrich.time_varies"] == 1


def test_one_off_several_times_flagged(cache):
    t1 = _next(6)
    e = _ev(d=t1)
    (out,), _ = _one(e, _page(_rows([(t1, "2pm"), (t1, "11am")])))
    assert out.time == "11:00" and out.times_vary


def test_single_upcoming_session_becomes_one_off(cache):
    t1 = _next(4)
    e = _ev(d=TODAY, end=TODAY + timedelta(days=20))
    (out,), _ = _one(e, _page(_rows([(t1, "7.30pm")])))
    assert out.kind == "one_off" and out.date == t1 and out.end_date is None
    assert out.byday is None and out.sessions_known and out.time == "19:30"


def test_fortnightly_becomes_instances(cache):
    days = [_next(3, weeks=2 * k) for k in range(4)]                  # 4 of 7 Thursdays: sparse
    e = _ev(d=TODAY, end=days[-1])
    out, ctx = _one(e, _page(_rows([(d, "10am") for d in days])))
    assert [x.date for x in out] == days and ctx.stats["enrich.sessions_sparse"] == 1
    assert all(x.kind == "recurring" and x.byday is None and x.end_date is None and x.sessions_known
               and x.time == "10:00" for x in out)
    assert ctx.stats["enrich.instances"] == 4


def test_seven_weekdays_over_six_weeks_not_daily(cache):
    days = [_next(k, weeks=k) for k in range(7)]                      # one per weekday, spread out
    e = _ev(d=TODAY, end=max(days))
    out, ctx = _one(e, _page(_rows([(d, "8pm") for d in days])))
    assert len(out) == 7 and all(x.byday is None for x in out) and ctx.stats["enrich.sessions_sparse"] == 1


def test_seven_weekdays_daily_is_run(cache):
    days = [TODAY + timedelta(days=k) for k in range(1, 9)]
    e = _ev(d=TODAY, end=days[-1])
    (out,), _ = _one(e, _page(_rows([(d, "7pm") for d in days])))
    assert out.kind == "run" and out.byday is None and out.sessions_known


def test_all_sessions_past_unchanged(cache):
    past = [TODAY - timedelta(days=k) for k in (3, 10)]
    e = _ev(d=TODAY, end=TODAY + timedelta(days=30))
    (out,), _ = _one(e, _page(_rows([(d, "7pm") for d in past])))
    assert out.sessions_known is False and out.byday is None and out.time is None


def test_jsonld_schedule_byday(cache):
    node = {"@context": "https://schema.org", "@type": "Event", "name": "Show",
            "startDate": str(TODAY), "endDate": str(TODAY + timedelta(days=60)),
            "eventSchedule": {"@type": "Schedule", "byDay": "https://schema.org/Saturday", "startTime": "11:00"}}
    e = _ev(d=TODAY, end=TODAY + timedelta(days=60))
    (out,), _ = _one(e, _page(f'<script type="application/ld+json">{json.dumps(node)}</script>'))
    assert out.byday == [5] and out.sessions_known and out.kind == "recurring" and out.time == "11:00"


def test_jsonld_sessions_and_soldout(cache):
    t1, t2 = _next(6), _next(6, weeks=1)
    nodes = [{"@context": "https://schema.org", "@type": "Event", "name": "Show", "startDate": f"{d}T14:00:00",
              "offers": {"price": "10", "priceCurrency": "GBP", "availability": "https://schema.org/SoldOut"}}
             for d in (t1, t2)]
    e = _ev(d=t1, end=t2 + timedelta(days=2))
    (out,), _ = _one(e, _page(f'<script type="application/ld+json">{json.dumps(nodes)}</script>'))
    assert out.byday == [6] and out.time == "14:00" and out.price == "£10" and out.sold_out


def test_related_block_and_cards_ignored(cache):
    t1, t2, sat = _next(3), _next(3, weeks=1), _next(5)
    card = f'<a href="/whats-on/other/"><p>{_long(_next(1))}</p><p>7pm</p></a>'
    body = card + _rows([(t1, "7pm"), (t2, "7pm")]) + f"<h2>You may also like</h2>{_rows([(sat, '3pm')])}"
    e = _ev(d=TODAY, end=t2)
    (out,), _ = _one(e, _page(body))
    assert out.byday == [3]


def test_related_card_time_tags_ignored(cache):
    t1, other = _next(3), _next(0)
    page = _page(f'<time datetime="{t1}T19:30">x</time>'
                 f'<a href="/whats-on/other/"><time datetime="{other}T11:00">y</time></a>')
    (out,), _ = _one(_ev(d=t1), page)
    assert out.time == "19:30" and not out.times_vary


def test_ru_session_rows(cache):
    t1 = _next(5)
    ru_mon = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
              "октября", "ноября", "декабря"][t1.month - 1]
    (out,), _ = _one(_ev(d=t1), _page(f"<p>сб {t1.day} {ru_mon} 19:00</p>"))
    assert out.time == "19:00"


# ---------------------------------------------------------------- sold out

def test_sold_out_every_session(cache):
    t1, t2 = _next(1), _next(1, weeks=1)
    e = _ev(d=t1, end=t2)
    rows = _rows([(t1, "8 pm"), (t2, "8 pm")], tail="Wheelchair spaces available</p><p>Sold out")
    (out,), ctx = _one(e, _page(rows))
    assert out.sold_out and ctx.stats["enrich.sold_out"] == 1
    cache.clear()
    mixed = _rows([(t1, "8 pm")], tail="Sold out") + _rows([(t2, "8 pm")], tail="Book now")
    (out,), _ = _one(e, _page(mixed))
    assert not out.sold_out


@pytest.mark.parametrize("status", ["Almost sold out", "Not sold out", "Sold out?", "Sold out shows added"])
def test_sold_out_false_positives(cache, status):
    t1 = _next(2)
    (out,), _ = _one(_ev(d=t1), _page(_rows([(t1, "7pm")], tail=status)))
    assert not out.sold_out


def test_sold_out_waitlist_counts(cache):
    t1 = _next(2)
    (out,), _ = _one(_ev(d=t1), _page(_rows([(t1, "7pm")], tail="Sold Out (join waiting list)")))
    assert out.sold_out


def test_related_card_sold_out_ignored(cache):
    t1 = _next(2)
    body = (f"<p>{_long(t1)}</p><p>7pm</p><p>£10</p><h2>Other shows</h2><p>Another Show</p><p>Sold out</p>")
    (out,), _ = _one(_ev(d=t1), _page(body))
    assert not out.sold_out


# ---------------------------------------------------------------- price

def test_price_ignores_banner_before_title_and_member_tier(cache):
    t1 = _next(2)
    body = "<p>Become a Member from £60</p><p>Premium £45 / Standard £15</p>" + _rows([(t1, "7pm")])
    (out,), _ = _one(_ev(d=t1), _page(body, pre="<p>Gift vouchers £5</p><p>Members from £60</p>"))
    assert out.price == "£15"


def test_price_concession_skipped(cache):
    t1 = _next(2)
    (out,), _ = _one(_ev(d=t1), _page("<p>Tickets £20, Concessions £8</p>" + _rows([(t1, "7pm")])))
    assert out.price == "£20"


# ---------------------------------------------------------------- driver

def test_gap_fill_never_overwrites(cache):
    t1, t2 = _next(2), _next(2, weeks=1)
    page = _page("<p>Tickets £5</p>" + _rows([(t1, "7pm"), (t2, "7pm")]))
    e = _ev(d=t1, time="20:00", price="£30")
    (out,), ctx = _one(e, page)
    assert out == e and "enrich.candidates" not in ctx.stats
    (out2,), _ = _one(_ev(d=t1, time="20:00"), page)
    assert out2.time == "20:00" and out2.price == "£5"


def test_cap_soonest_first_and_url_dedupe(cache):
    seen: list[str] = []
    evs = [_ev(title=f"S{i}", d=TODAY + timedelta(days=10 - i), url=f"{LISTING}s{i}/") for i in range(5)]
    evs.append(_ev(title="S4", d=TODAY + timedelta(days=30), url=f"{LISTING}s4#tickets"))   # same page as s4/
    pages = {e.url: _page("<p>£8</p>", e.title) for e in evs}
    out, ctx = _run(evs, _cfg(max=2), pages, seen)
    assert sorted(seen) == [f"{LISTING}s3/", f"{LISTING}s4/"]
    assert ctx.stats["enrich.skipped_cap"] == 3 and ctx.stats["enrich.pages"] == 2
    assert [e.price for e in out] == [None, None, None, "£8", "£8", "£8"]


def test_link_pattern_and_listing_url_skipped(cache):
    seen: list[str] = []
    evs = [_ev(title="A", url=LISTING), _ev(title="B", url=""), _ev(title="C", url="https://venue.test/shop/x"),
           _ev(title="D", url=f"{LISTING}d/")]
    _, ctx = _run(evs, _cfg(link_pattern=r"/whats-on/[^/]+/$"), {}, seen)
    assert seen == [f"{LISTING}d/"] and ctx.stats["enrich.skipped_url"] == 3


def test_offline_uncached_and_cache_replay(cache):
    e = _ev()
    out, ctx = _one(e, _page("<p>£9</p>"), offline=True)
    assert out == [e] and ctx.stats["enrich.offline_uncached"] == 1
    cache[e.url] = _page("<p>£9</p>")
    (out,), ctx = _run([e], _cfg(), {}, offline=True)
    assert out.price == "£9" and ctx.stats["enrich.cache_hit"] == 1


def test_exception_safety(cache, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad page")
    monkeypatch.setattr(EN, "parse_detail", boom)
    e = _ev()
    out, ctx = _one(e, _page("<p>£9</p>"))
    assert out == [e] and ctx.stats["enrich.failed"] == 1
    monkeypatch.setattr(EN, "_plan", boom)
    out, ctx = _run([e], _cfg(), {})
    assert out == [e] and ctx.stats["enrich.failed"] == 1


def test_http_errors_counted(cache):
    e = _ev()
    out, ctx = _run([e], _cfg(), {})
    assert out == [e] and ctx.stats["enrich.http_404"] == 1 and cache == {}


def test_no_enrich_config_is_identity(cache):
    cfg = VenueConfig(name="Test Venue", url=LISTING, extract=["jsonld"])
    out, ctx = _run([_ev()], cfg, {})
    assert out == [_ev()] and not ctx.stats


def test_gap_reaches_throttle(cache, monkeypatch):
    import src.fetch as F
    gaps: list[float] = []

    class Rec:
        async def wait(self, url, gap):
            gaps.append(gap)
    monkeypatch.setattr(F, "HostThrottle", Rec)
    e = _ev()
    _one(e, _page("<p>£9</p>"), cfg=_cfg(fetch={"gap": 2.5}))
    assert gaps == [2.5]


def test_browser_path_stubbed(cache, monkeypatch):
    import src.fetch.browser as B
    calls: list[dict] = []

    async def fake(url, **kw):
        calls.append({"url": url, **kw})
        return FetchResult(FetchStatus.OK, body=_page("<p>£14</p>"), url=url)
    monkeypatch.setattr(B, "fetch_browser", fake)
    cfg = _cfg(fetch={"mode": "browser", "gap": 0, "wait_for": ".main", "delay": 1.5})
    e = _ev()
    out, ctx = _run([e], cfg, {}, offline=True)                        # offline: no render
    assert calls == [] and out == [e] and ctx.stats["enrich.offline_uncached"] == 1
    (out,), ctx = _run([e], cfg, {})
    assert out.price == "£14" and calls[0]["wait_for"] == ".main" and calls[0]["delay"] == 1.5
    assert ctx.stats["enrich.fetched"] == 1 and not calls[0]["stealth"]


def test_parse_venue_runs_enrich_and_post_enrich_hook(cache, monkeypatch):
    from src.sources.html_sites import parse_venue
    monkeypatch.setattr(venues, "VENUE_HOOKS", {})
    monkeypatch.setattr(venues, "_loaded", True)
    seen_hook: list[int] = []

    @venue_hook("enrich_probe")
    class Probe(VenueHook):
        def post_enrich(self, cfg, events, ctx):
            seen_hook.append(len(events))
            return [e.model_copy(update={"description": "hooked"}) for e in events]

    t1 = _next(3)
    node = {"@context": "https://schema.org", "@type": "Event", "name": "Show", "startDate": str(t1), "url": URL}
    listing = f'<script type="application/ld+json">{json.dumps(node)}</script>'
    pages = {URL: _page(_rows([(t1, "7pm")]) + "<p>£11</p>")}
    cfg = _cfg().model_copy(update={"hook": "enrich_probe"})

    async def go():
        def handle(req):
            return httpx.Response(200, text=pages[str(req.url)]) if str(req.url) in pages else httpx.Response(404)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            ctx = SourceCtx(http=http, today=TODAY, window_days=90, city=get_city("london"))
            return await parse_venue(cfg, FetchResult(FetchStatus.OK, body=listing, url=LISTING), ctx)
    (ev,), st = asyncio.run(go())
    assert ev.time == "19:00" and ev.price == "£11" and st["stats"]["enrich.pages"] == 1
    assert seen_hook == [1] and ev.description == "hooked" and st["stats"]["hook.post_enrich"] == 1


def test_crawl_sites_closes_browser(cache, monkeypatch):
    import src.fetch.browser as B
    from src.sources import html_sites as HS
    closed: list[int] = []

    async def close():
        closed.append(1)
    monkeypatch.setattr(B, "close_browser", close)
    cfg = _cfg(fetch={"mode": "browser"})
    monkeypatch.setattr(HS, "_venues", lambda p, t: (get_city("london"), [cfg]))
    frs = {cfg.name: FetchResult(FetchStatus.OK, body="<html></html>", url=LISTING)}
    asyncio.run(HS.crawl_sites(frs=frs))
    assert closed == [1]
    closed.clear()
    asyncio.run(HS.crawl_sites(frs=frs, parse_only=True))              # offline: nothing rendered
    assert closed == []


def test_follow_links_uses_shared_helper(cache):
    t1 = _next(3)
    detail = _page(f'<time datetime="{t1}T19:30">x</time><p>£7</p>', title="Gig")
    listing = '<a href="/event/gig">Gig</a><a href="/event/missing">x</a>'
    pages = {"https://venue.test/event/gig": detail}
    cfg = VenueConfig(name="Test Venue", url=LISTING, extract=["follow_links"])

    async def go():
        def handle(req):
            return httpx.Response(200, text=pages[str(req.url)]) if str(req.url) in pages else httpx.Response(404)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            ctx = SourceCtx(http=http, today=TODAY, window_days=90, city=get_city("london"))
            raws = await FL.SOURCE.records(cfg, FetchResult(FetchStatus.OK, body=listing, url=LISTING), ctx)
            return raws, ctx
    raws, ctx = asyncio.run(go())
    assert [(r.title, r.date_text, r.time_text) for r in raws] == [("Gig", t1.isoformat(), "19:30")]
    assert ctx.stats["follow_links.http_404"] == 1 and ctx.stats["follow_links.fetched"] == 1
    assert "https://venue.test/event/gig" in cache


# ---------------------------------------------------------------- digest label

def test_time_label_and_schedule_label():
    e = _ev(d=_next(3), time="10:00", times_vary=True)
    assert time_label(e) == "from 10:00" and time_label(e, "ru") == "с 10:00"
    assert schedule_label(e, TODAY) == "from 10:00"
    s = _ev(d=TODAY, kind="recurring", byday=[3], time="10:00", times_vary=True, sessions_known=True)
    assert schedule_label(s, TODAY).startswith("Thursdays from 10:00")
    assert time_label(_ev(time="19:30")) == "19:30" and time_label(_ev()) == ""
    assert Event.model_validate({"title": "x", "venue": "v", "date": str(TODAY), "url": "u"}).times_vary is False
