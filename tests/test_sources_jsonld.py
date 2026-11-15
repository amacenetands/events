"""jsonld source: node parsing, offers/eventSchedule mapping, and parity with the old html_sites path.
Fixtures: tests/fixtures/sources/jsonld_*.html (scripts/record_source_fixtures.py, ld+json blocks only)."""
import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from src.config import VenueConfig, all_venues, get_city
from src.fetch.types import FetchResult, FetchStatus
from src.sources.base import SourceCtx, get_source
from src.sources.build import build_events
from src.sources.jsonld import event_nodes, price_text, raws_from_html, recurrence_text

FIX = Path(__file__).parent / "fixtures" / "sources"
TODAY = date(2026, 11, 15)
CFG = VenueConfig(name="Test Venue", url="https://example.org/whats-on/")


def _ctx(today: date = TODAY) -> SourceCtx:
    return SourceCtx(http=None, today=today, window_days=365, city=get_city("london"))


def _page(*nodes) -> str:
    return "".join(f'<script type="application/ld+json">{json.dumps(n)}</script>' for n in nodes)


def _records(html: str, cfg: VenueConfig = CFG, ctx: SourceCtx | None = None):
    fr = FetchResult(FetchStatus.OK, body=html)
    return asyncio.run(get_source("jsonld").records(cfg, fr, ctx or _ctx()))


def test_atg_top_level_array_dd_mm_yyyy():
    cfg = VenueConfig(name="Aldwych Theatre", url="https://www.atgtickets.com/venues/aldwych-theatre/")
    raws = _records((FIX / "jsonld_aldwych.html").read_text(), cfg)
    assert [(r.title, r.date_text, r.end_text) for r in raws] == [("Sinatra The Musical", "10-11-2026", "10-04-2027")]
    (e,) = build_events(raws, cfg, _ctx())
    assert (e.date, e.end_date, e.kind, e.sessions_known) == (date(2026, 11, 10), date(2027, 4, 10), "run", False)
    assert e.url.startswith("https://secure.lovetheatre.com/book/") and e.price == "£35"
    assert "<h3>" not in (e.description or "")


def test_offers_price_and_location_venue():
    cfg = VenueConfig(name="Bach to Baby Ealing", url="https://www.bachtobaby.com/ealing")
    (r,) = raws_from_html((FIX / "jsonld_bach_to_baby.html").read_text(), cfg)
    assert (r.price_text, r.venue, r.date_text) == ("£18", "Ealing Concerts", "2026-11-21T11:00:00")
    (e,) = build_events([r], cfg, _ctx())
    assert (e.time, e.price, e.venue) == ("11:00", "£18", "Ealing Concerts")


@pytest.mark.parametrize("offers,expect", [
    ({"price": 0, "priceCurrency": "GBP"}, "Free"),
    ({"price": "12.50", "priceCurrency": "GBP"}, "£12.50"),
    ([{"price": 10, "priceCurrency": "GBP"}, {"price": 30, "priceCurrency": "GBP"}], "£10 – £30"),
    ({"lowPrice": 8, "highPrice": 20, "priceCurrency": "EUR"}, "€8 – €20"),
    ({"price": 1500, "priceCurrency": "RUB"}, "1500 ₽"),
    ({"url": "https://x"}, ""),
])
def test_price_text(offers, expect):
    assert price_text({"offers": offers}) == expect


def test_event_schedule_recurrence():
    sch = {"@type": "Schedule", "byDay": ["https://schema.org/Thursday"], "startTime": "10:00:00",
           "startDate": "2026-09-03", "endDate": "2027-03-25", "repeatFrequency": "P1W"}
    assert recurrence_text(sch) == "Thursdays 10:00"
    assert recurrence_text({"repeatFrequency": "P1D", "startTime": "11:00"}) == "daily 11:00"
    html = _page({"@type": "ChildrensEvent", "name": "Baby Rhyme Time", "eventSchedule": sch,
                  "url": "/rhyme-time"})
    (r,) = raws_from_html(html, CFG)
    assert (r.date_text, r.end_text, r.recurrence_text) == ("2026-09-03", "2027-03-25", "Thursdays 10:00")
    (e,) = build_events([r], CFG, _ctx())
    assert (e.kind, e.byday, e.time, e.end_date, e.url) == (
        "run", [3], "10:00", date(2027, 3, 25), "https://example.org/rhyme-time")


def test_graph_types_sold_out_and_skips():
    html = _page({"@graph": [
        {"@type": "Organization", "name": "Org"},
        {"@type": "MusicEvent", "name": "Gig &amp; Co", "startDate": "2026-11-20T19:30",
         "offers": {"price": 15, "priceCurrency": "GBP", "availability": "https://schema.org/SoldOut"}},
        {"@type": "TheaterEvent", "name": "No date"},
        {"@type": ["Event", "Festival"], "name": "Fest", "startDate": "2026-12-01", "@id": "https://ex.org/fest"},
    ]}) + '<script type="application/ld+json">{broken</script>'
    raws = raws_from_html(html, CFG)
    assert [(r.title, r.sold_out, r.url) for r in raws] == [("Gig & Co", True, None), ("Fest", None, "https://ex.org/fest")]
    evs = build_events(raws, CFG, _ctx())
    assert [(e.title, e.time, e.sold_out) for e in evs] == [("Gig & Co", "19:30", True), ("Fest", None, False)]


def test_control_chars_in_json_tolerated():
    html = '<script type="application/ld+json">{"@type": "Event", "name": "A\tB", "startDate": "2026-12-05"}</script>'
    assert [n["name"] for n in event_nodes(html)] == ["A\tB"]


def test_no_date_filtering_in_source():
    # eventSchedule with a past start and no end must reach build (open-ended weekly series)
    sch = {"byDay": "https://schema.org/Saturday", "startTime": "11:00", "repeatFrequency": "P1W"}
    html = _page({"@type": "Event", "name": "Saturday Club", "startDate": "2025-01-04", "eventSchedule": sch},
                 {"@type": "Event", "name": "Long gone", "startDate": "2025-01-04"})
    raws = _records(html)
    assert [r.title for r in raws] == ["Saturday Club", "Long gone"]
    evs = build_events(raws, CFG, _ctx())
    assert [(e.title, e.kind, e.date, e.byday, e.time) for e in evs] == [
        ("Saturday Club", "recurring", date(2026, 11, 21), [5], "11:00")]


@pytest.mark.parametrize("offers,cur,expect", [
    ({"price": "12 - 18"}, "£", "£12 - 18"),
    ({"price": "PWYC from 1", "priceCurrency": "GBP"}, "£", "PWYC from £1"),
    ({"price": "£5 / £3 conc"}, "£", "£5 / £3 conc"),
    ({"price": 700}, "₽", "700 ₽"),
])
def test_price_text_non_numeric_kept(offers, cur, expect):
    assert price_text({"offers": offers}, cur) == expect


def test_city_currency_default():
    html = _page({"@type": "Event", "name": "Концерт", "startDate": "2026-11-20", "offers": {"price": 1500}})
    ctx = SourceCtx(http=None, today=TODAY, window_days=365, city=get_city("moscow"))
    (r,) = _records(html, ctx=ctx)
    assert r.price_text == "1500 ₽"


def test_category_audience_age_texts():
    html = _page({"@type": ["ChildrensEvent", "TheaterEvent"], "name": "Gruffalo", "startDate": "2026-11-20",
                  "typicalAgeRange": "3+"},
                 {"@type": "MusicEvent", "name": "Gig", "startDate": "2026-11-21"},
                 {"@type": "Event", "name": "Plain", "startDate": "2026-11-22"})
    raws = _records(html)
    assert [(r.category_text, r.audience_text, r.age_text) for r in raws] == [
        ("theatre", "kids", "3+"), ("music", "", ""), ("", "", "")]
    evs = build_events(raws, CFG, _ctx())
    assert [(e.category, e.audience, e.min_age) for e in evs] == [
        ("theatre", "kids", 3), ("concert", "all", None), ("other", "all", None)]


def test_relative_url_resolved_against_fetch_url():
    html = _page({"@type": "Event", "name": "A", "startDate": "2026-11-20", "url": "/e/a"})
    fr = FetchResult(FetchStatus.OK, body=html, url="https://moved.example.org/new/whats-on")
    (r,) = asyncio.run(get_source("jsonld").records(CFG, fr, _ctx()))
    assert r.url == "https://moved.example.org/e/a"


def test_not_ok_fetch_returns_nothing():
    fr = FetchResult(FetchStatus.BLOCKED, body=(FIX / "jsonld_apollo.html").read_text())
    assert asyncio.run(get_source("jsonld").records(CFG, fr, _ctx())) == []
    assert asyncio.run(get_source("jsonld").records(CFG, None, _ctx())) == []


# ── parity with the legacy path (html_sites._events_from_jsonld + extract._build_events) ──
# TODAY is frozen for both paths. New-only events (by title) are listed: the old path skipped
# top-level JSON arrays (ATG) and blocks with raw control characters.
PARITY = {
    "conway_hall": ("Conway Hall", {"The Golden Fountain"}),
    "camden_peoples": ("Camden People's Theatre", set()),
    "apollo": ("Apollo Theatre", set()),
    "garrick": ("Garrick Theatre", set()),
    "lyric": ("Lyric Theatre", set()),
    "bach_to_baby": ("Bach to Baby Ealing", set()),
    "aldwych": ("Aldwych Theatre", {"Sinatra The Musical"}),
}


class _FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(TODAY.year, TODAY.month, TODAY.day)


def _key(e):
    return (e.title, e.date, e.end_date, e.time, e.url)


@pytest.mark.parametrize("slug", sorted(PARITY))
def test_parity_with_legacy_jsonld(slug, monkeypatch):
    import datetime as datetime_mod
    from src import extract
    from src.sources.html_sites import _events_from_jsonld
    name, new_only = PARITY[slug]
    cfg = next(v for v in all_venues() if v.name == name)
    html = (FIX / f"jsonld_{slug}.html").read_text()
    with monkeypatch.context() as m:       # legacy code calls date.today() internally
        m.setattr(datetime_mod, "date", _FrozenDate)
        m.setattr(extract, "date", _FrozenDate)
        old = {_key(e) for e in extract._build_events(_events_from_jsonld(html, cfg.url, cfg.name, 365) or [],
                                                      cfg.url, cfg.name)}
    new = {_key(e) for e in build_events(raws_from_html(html, cfg, cfg.url), cfg, _ctx())}
    assert old <= new, f"lost vs legacy: {sorted(old - new)}"
    assert {k[0] for k in new - old} <= new_only, f"unexpected new: {sorted(new - old)}"
