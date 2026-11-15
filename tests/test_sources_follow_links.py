"""follow_links source: listing links -> detail pages (httpx.MockTransport, cache stubbed).
Fixtures: tests/fixtures/sources/follow_barbican_*.html (scripts/record_source_fixtures.py)."""
import asyncio
from datetime import date
from pathlib import Path

import httpx
import pytest

from src.config import VenueConfig, get_city
from src.fetch.types import FetchResult, FetchStatus
from src.sources import follow_links as FL
from src.sources.base import SourceCtx, get_source
from src.sources.build import build_events

FIX = Path(__file__).parent / "fixtures" / "sources"
PAGES = {"naza": "follow_barbican_naza.html",
         "ensemble-pygmalion-mozarts-great-mass-in-c-minor": "follow_barbican_ensemble.html"}
CFG = VenueConfig(name="Barbican", url="https://www.barbican.org.uk/whats-on", extract=["follow_links"],
                  follow={"link_pattern": r"/whats-on/\d{4}/event/"}, category_hint="concert")
LISTING = FetchResult(FetchStatus.OK, body=(FIX / "follow_barbican_listing.html").read_text())


@pytest.fixture
def cache(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr(FL, "_cache_get", lambda url, force: None if force else store.get(url))
    monkeypatch.setattr(FL, "_cache_put", lambda url, html, name: store.__setitem__(url, html))
    return store


def _handler(seen: list[str]):
    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        slug = req.url.path.rsplit("/", 1)[-1]
        if slug in PAGES:
            return httpx.Response(200, text=(FIX / PAGES[slug]).read_text())
        return httpx.Response(404)
    return handle


def _run(today: date, cfg: VenueConfig = CFG, seen: list[str] | None = None):
    seen = [] if seen is None else seen

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler(seen))) as http:
            ctx = SourceCtx(http=http, today=today, window_days=365, city=get_city("london"))
            return await get_source("follow_links").records(cfg, LISTING, ctx), ctx
    return asyncio.run(go())


def test_event_links_dedup_and_pattern():
    links = FL.event_links(LISTING.body, CFG.url, CFG.follow.link_pattern)
    assert len(links) == len(set(links)) == 18
    assert all("/whats-on/20" in u and u.startswith("https://www.barbican.org.uk/") for u in links)


def test_detail_pages_to_raw_events(cache):
    raws, ctx = _run(date(2026, 11, 1))
    by = {r.title: r for r in raws}
    naza = by["NAZA"]
    assert (naza.date_text, naza.time_text, naza.price_text, naza.url) == (
        "2026-11-18", "18:15", "£15.50", "https://www.barbican.org.uk/whats-on/2026/event/naza")
    mass = by["Ensemble Pygmalion: Mozart's Great Mass in C minor"]
    assert (mass.date_text, mass.time_text, mass.price_text) == ("2026-11-09", "20:00", "From £24")
    assert ctx.stats["follow_links.pages"] == 18 and ctx.stats["follow_links.http_404"] == 16
    assert ctx.errors == ["Barbican: follow-links 16/18 detail pages unavailable (see follow_links.* stats)"]
    assert len(cache) == 2                                   # fetched pages cached


def test_related_event_dates_ignored(cache):
    # On 15 Nov the Mass (9 Nov) is over; the 28 Nov / 15 Dec <time> tags belong to promo cards.
    raws, _ = _run(date(2026, 11, 15))
    assert [r.title for r in raws] == ["NAZA"]


def test_build_from_follow_links(cache):
    raws, ctx = _run(date(2026, 11, 1))
    evs = build_events(raws, CFG, ctx)
    e = next(e for e in evs if e.title == "NAZA")
    assert (e.date, e.time, e.price, e.kind, e.category, e.venue) == (
        date(2026, 11, 18), "18:15", "£15.50", "one_off", "concert", "Barbican")


def test_cache_hit_skips_network(cache):
    url = "https://www.barbican.org.uk/whats-on/2026/event/naza"
    cache[url] = (FIX / PAGES["naza"]).read_text()
    seen: list[str] = []
    cfg = CFG.model_copy(update={"follow": CFG.follow.model_copy(update={"link_pattern": r"/event/naza$"})})
    raws, _ = _run(date(2026, 11, 1), cfg, seen)
    assert [r.title for r in raws] == ["NAZA"] and seen == []


def test_max_links_cap(cache):
    cfg = CFG.model_copy(update={"follow": CFG.follow.model_copy(update={"max": 3})})
    seen: list[str] = []
    _run(date(2026, 11, 1), cfg, seen)
    assert len(seen) == 3


def test_no_links_reported(cache):
    cfg = CFG.model_copy(update={"follow": CFG.follow.model_copy(update={"link_pattern": r"/nomatch/"})})
    raws, ctx = _run(date(2026, 11, 1), cfg)
    assert raws == [] and not ctx.errors and ctx.stats["follow_links.no_links"] == 1


def test_blocked_detail_not_cached(cache):
    wall = "<html><head><title>Just a moment...</title></head><body>cf-chl-</body></html>"

    def handle(req):
        return httpx.Response(200, text=wall)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            ctx = SourceCtx(http=http, today=date(2026, 11, 1), window_days=365, city=get_city("london"))
            return await get_source("follow_links").records(CFG, LISTING, ctx), ctx
    raws, ctx = asyncio.run(go())
    assert raws == [] and cache == {} and ctx.stats["follow_links.blocked"] == 18


def test_parse_dt():
    from zoneinfo import ZoneInfo
    tz, today = ZoneInfo("Europe/London"), date(2026, 6, 1)
    assert FL._parse_dt("2026-06-23T18:45:00Z", tz, today) == (date(2026, 6, 23), "19:45")   # BST
    assert FL._parse_dt("2026-12-01", tz, today) == (date(2026, 12, 1), None)
    assert FL._parse_dt("2026-12-01T00:00:00", tz, today) == (date(2026, 12, 1), None)     # midnight = no time
    assert FL._parse_dt("Sat 20 June 2026 7.30pm", tz, today) == (date(2026, 6, 20), "19:30")
    for junk in ("19:30", "2h", "10", "not a date", "PT2H"):
        assert FL._parse_dt(junk, tz, today) is None, junk


def test_force_bypasses_cache(cache):
    url = "https://www.barbican.org.uk/whats-on/2026/event/naza"
    cache[url] = "<html>stale</html>"
    cfg = CFG.model_copy(update={"follow": CFG.follow.model_copy(update={"link_pattern": r"/event/naza$"})})
    seen: list[str] = []

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler(seen))) as http:
            ctx = SourceCtx(http=http, today=date(2026, 11, 1), window_days=365, city=get_city("london"), force=True)
            return await get_source("follow_links").records(cfg, LISTING, ctx)
    assert [r.title for r in asyncio.run(go())] == ["NAZA"] and seen == [url]


def test_bad_detail_page_does_not_kill_venue(cache, monkeypatch):
    real = FL.raw_from_detail

    def flaky(html, url, ctx, pattern):
        if "naza" in url:
            raise RuntimeError("boom")
        return real(html, url, ctx, pattern)
    monkeypatch.setattr(FL, "raw_from_detail", flaky)
    raws, ctx = _run(date(2026, 11, 1))
    assert [r.title for r in raws] == ["Ensemble Pygmalion: Mozart's Great Mass in C minor"]
    assert ctx.stats["follow_links.failed"] == 1


def test_redirected_detail_keeps_final_url(cache):
    target = "https://www.barbican.org.uk/whats-on/2026/event/naza"

    def handle(req):
        if req.url.path.endswith("/old-naza"):
            return httpx.Response(301, headers={"location": target})
        if str(req.url) == target:
            return httpx.Response(200, text=(FIX / PAGES["naza"]).read_text())
        return httpx.Response(404)
    listing = FetchResult(FetchStatus.OK, body='<a href="/whats-on/2026/event/old-naza">x</a>',
                          url="https://www.barbican.org.uk/whats-on")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            ctx = SourceCtx(http=http, today=date(2026, 11, 1), window_days=365, city=get_city("london"))
            return await get_source("follow_links").records(CFG, listing, ctx)
    (r,) = asyncio.run(go())
    assert (r.title, r.url) == ("NAZA", target)


def test_offline_reads_cached_detail_pages_only(cache):
    url = "https://www.barbican.org.uk/whats-on/2026/event/naza"
    cache[url] = (FIX / PAGES["naza"]).read_text()
    seen: list[str] = []

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler(seen))) as http:
            ctx = SourceCtx(http=http, today=date(2026, 11, 1), window_days=365, city=get_city("london"),
                            offline=True)
            return await get_source("follow_links").records(CFG, LISTING, ctx), ctx
    raws, ctx = asyncio.run(go())
    assert seen == [] and not ctx.errors
    assert [r.url for r in raws] == [url]
    assert ctx.stats["follow_links.offline_uncached"] >= 17
