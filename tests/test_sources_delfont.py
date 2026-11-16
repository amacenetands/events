"""Delfont Mackintosh theatres: listing has no dates; follow_links + `detail: delfont` reads the show pages
('Booking until' / date range header + Performance Schedule). Fixtures: tests/fixtures/sources/follow_delfont_*.html."""
import asyncio
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from src.config import VenueConfig, get_city
from src.fetch.types import FetchResult, FetchStatus
from src.sources import follow_links as FL
from src.sources.base import SourceCtx, get_source
from src.sources.build import build_events

FIX = Path(__file__).parent / "fixtures" / "sources"
PAGES = {"hay-fever": "follow_delfont_hay-fever.html",
         "christmas-carol-goes-wrong": "follow_delfont_christmas-carol-goes-wrong.html"}
CFG = VenueConfig(name="Wyndham's Theatre", url="https://www.delfontmackintosh.co.uk/theatres/wyndhams-theatre",
                  extract=["follow_links"], category_hint="theatre",
                  follow={"link_pattern": r"/whats-on/[a-z0-9-]+$", "detail": "delfont"})
LISTING = FetchResult(FetchStatus.OK, body=(FIX / "follow_delfont_listing.html").read_text())


@pytest.fixture(autouse=True)
def cache(monkeypatch):
    monkeypatch.setattr(FL, "_cache_get", lambda url, force: None)
    monkeypatch.setattr(FL, "_cache_put", lambda url, html, name: None)


def _handle(req: httpx.Request) -> httpx.Response:
    slug = req.url.path.rsplit("/", 1)[-1]
    return httpx.Response(200, text=(FIX / PAGES[slug]).read_text()) if slug in PAGES else httpx.Response(404)


def _run(today: date):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handle)) as http:
            ctx = SourceCtx(http=http, today=today, window_days=365, city=get_city("london"))
            return await get_source("follow_links").records(CFG, LISTING, ctx), ctx
    return asyncio.run(go())


def _first_end(raw) -> date:
    return date.fromisoformat(raw.date_text.split(" – ")[1])


def test_open_run_and_future_run():
    raws, _ = _run(date(2026, 11, 16))
    by = {r.title: r for r in raws}
    hay = by["Hay Fever"]
    assert hay.date_text == "2026-11-16 – 2026-12-12"            # open run starts today, 'Booking until 12 December 2026'
    assert hay.time_text == "Monday - Saturday 7.30pm; Wednesday & Saturday 2.30pm"
    carol = by["Christmas Carol Goes Wrong"]
    assert carol.date_text == "2026-12-18 – 2027-01-23"          # '18 December 2026 - 23 January 2027'
    assert carol.time_text == "Tuesday - Saturday 7.30pm; Saturday & Sunday 2.30pm; Sunday 7pm"
    assert hay.url == "https://www.delfontmackintosh.co.uk/whats-on/hay-fever"


def test_builds_one_run_per_show_and_drops_ended():
    raws, ctx = _run(date(2026, 11, 16))
    evs = build_events(raws, CFG, ctx)
    assert {e.title: e.kind for e in evs} == {"Hay Fever": "run", "Christmas Carol Goes Wrong": "run"}
    assert all(e.time for e in evs)
    raws, _ = _run(date(2026, 12, 13))                           # Hay Fever closed the day before
    assert [r.title for r in raws] == ["Christmas Carol Goes Wrong"]


def test_unknown_detail_parser_rejected():
    with pytest.raises(Exception):
        VenueConfig(name="x", url="https://x.org", follow={"detail": "nope"})
