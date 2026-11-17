"""jsonld source: node parsing, offers/eventSchedule mapping, and recorded expected outputs.
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
        "recurring", [3], "10:00", date(2027, 3, 25), "https://example.org/rhyme-time")
    plain = dict(sch, byDay="Thursday")                         # byDay as a plain string
    (r2,) = raws_from_html(_page({"@type": "Event", "name": "Lego Club", "eventSchedule": plain}), CFG)
    (e2,) = build_events([r2], CFG, _ctx())
    assert (e2.kind, e2.byday, e2.time) == ("recurring", [3], "10:00")


def test_offers_url_when_event_has_none():
    off = lambda *us: [{"@type": "Offer", "url": u, "price": "10"} for u in us]
    node = {"@type": "Event", "name": "Sharp Edges", "startDate": "2026-11-20"}
    assert _records(_page(dict(node, offers=off("https://example.org/whatson/Sharp-Edges"))))[0].url == \
        "https://example.org/whatson/Sharp-Edges"
    # same-site offer preferred over a ticket vendor; listing / generic pages never used
    got = _records(_page(dict(node, offers=off("https://tix.example.com/b/1", "/whatson/sharp"))))[0].url
    assert got == "https://example.org/whatson/sharp"
    assert _records(_page(dict(node, offers=off("https://tix.example.com/b/1"))))[0].url == "https://tix.example.com/b/1"
    assert _records(_page(dict(node, offers=off(CFG.url, "/basket"))))[0].url is None
    # an offers url shared by several nodes is a basket/checkout page, not the event's
    two = _page(dict(node, offers=off("/basket/add")), dict(node, name="Other", offers=off("/basket/add")))
    assert [r.url for r in _records(two)] == [None, None]
    # the node's own url wins
    assert _records(_page(dict(node, url="/ev/own", offers=off("/whatson/sharp"))))[0].url == "https://example.org/ev/own"


def test_undated_nodes_counted_partial():
    ctx = _ctx()
    good = {"@type": "Event", "name": "A", "startDate": "2026-11-20"}
    raws = _records(_page(good, {"@type": "Event", "name": "B"}), ctx=ctx)
    assert [r.title for r in raws] == ["A"] and ctx.stats["jsonld.partial"] == 1


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


def test_body_used_whatever_the_status():
    fr = FetchResult(FetchStatus.BLOCKED, body=(FIX / "jsonld_apollo.html").read_text())   # fallback body
    assert asyncio.run(get_source("jsonld").records(CFG, fr, _ctx()))
    assert asyncio.run(get_source("jsonld").records(CFG, FetchResult(FetchStatus.BLOCKED), _ctx())) == []
    assert asyncio.run(get_source("jsonld").records(CFG, None, _ctx())) == []


def test_listing_url_filled_from_unique_anchor():
    node = {"@type": "Event", "name": "Woolf Quartet", "startDate": "2026-11-22T18:30", "url": CFG.url}
    page = _page(node) + ('<a href="/concerts/woolf-quartet/">Woolf Quartet</a>'
                          '<a href="/concerts/other/">Other</a><a href="/whats-on/">Woolf Quartet</a>')
    (r,) = _records(page)
    assert r.url == "https://example.org/concerts/woolf-quartet/"
    twice = page + '<a href="/concerts/woolf-quartet-2/">Woolf Quartet</a>'
    assert _records(twice)[0].url == CFG.url                 # ambiguous: left alone


# ── recorded expected outputs (tests/fixtures/sources/expected_events.json) ──
# Recorded once via this path at TODAY (2026-11-15); the legacy-path parity check this
# replaces found 0 events lost (only ATG top-level arrays / control-char blocks gained).
PARITY = {
    "conway_hall": "Conway Hall", "camden_peoples": "Camden People's Theatre", "apollo": "Apollo Theatre",
    "garrick": "Garrick Theatre", "lyric": "Lyric Theatre", "bach_to_baby": "Bach to Baby Ealing",
    "aldwych": "Aldwych Theatre",
}
EXPECTED = json.loads((FIX / "expected_events.json").read_text())


def _key(e):
    return [e.title, e.date.isoformat(), e.end_date.isoformat() if e.end_date else None, e.time, e.url]


@pytest.mark.parametrize("slug", sorted(PARITY))
def test_matches_recorded_output(slug):
    assert EXPECTED["today"] == TODAY.isoformat()
    cfg = next(v for v in all_venues() if v.name == PARITY[slug])
    html = (FIX / f"jsonld_{slug}.html").read_text()
    got = sorted(_key(e) for e in build_events(_records(html, cfg), cfg, _ctx()))   # incl. anchor URL fill
    assert got == EXPECTED["jsonld"][slug]


def test_itemlist_wrapped_events():
    from src.sources.jsonld import event_nodes
    html = ('<script type="application/ld+json">{"@type":"ItemList","itemListElement":['
            '{"@type":"ListItem","position":0,"item":{"@type":"Event","name":"A","startDate":"2026-12-01T19:00:00"}}]}</script>')
    assert [n["name"] for n in event_nodes(html)] == ["A"]
