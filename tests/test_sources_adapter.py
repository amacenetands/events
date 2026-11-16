"""adapter source: direct adapters -> RawEvent; non-direct / unparseable -> [] (LLM's job).
Fixtures: tests/fixtures/sources/adapter_*.html (scripts/record_source_fixtures.py)."""
import asyncio
import datetime as _dt
import types
from datetime import date
from pathlib import Path

import pytest

from src.config import VenueConfig, all_venues, get_city
from src.fetch.types import FetchResult, FetchStatus
from src.sources.adapter_source import raws_from_html
from src.sources.base import SourceCtx, get_source
from src.sources.build import build_events

FIX = Path(__file__).parent / "fixtures" / "sources"
TODAY = date(2026, 11, 15)
VENUES = {v.name: v for v in all_venues()}


def _ctx(today: date = TODAY) -> SourceCtx:
    return SourceCtx(http=None, today=today, window_days=365, city=get_city("london"))


def _html(slug: str) -> str:
    return (FIX / f"adapter_{slug}.html").read_text()


def test_json_adapter_orange_tree():
    cfg = VENUES["Orange Tree Theatre"]
    fr = FetchResult(FetchStatus.OK, body=_html("orange_tree"))
    raws = asyncio.run(get_source("adapter").records(cfg, fr, _ctx()))
    assert len(raws) == 12 and {r.source_kind for r in raws} == {"adapter:json"}
    first = raws[0]
    assert (first.title, first.date_text) == ("Constanze at St Mary Magdalene Church", "27 November 2026 19:30")
    evs = build_events(raws, cfg, _ctx())
    e = next(e for e in evs if e.title.startswith("Constanze"))
    assert (e.date, e.time, e.kind, e.url) == (
        date(2026, 11, 27), "19:30", "one_off", "https://orangetreetheatre.co.uk/whats-on/constanze/")
    assert all(e.date >= TODAY for e in evs) and len(evs) < len(raws)   # past performances dropped


def test_orange_tree_matches_recorded_output():
    import json
    expected = json.loads((FIX / "expected_events.json").read_text())
    cfg = VENUES["Orange Tree Theatre"]
    got = sorted([e.title, e.date.isoformat()] for e in build_events(raws_from_html(_html("orange_tree"), cfg, _ctx()), cfg, _ctx()))
    assert expected["today"] == TODAY.isoformat() and got == expected["adapter"]["orange_tree"]


def test_coliseum_python_adapter(monkeypatch):
    from src.sources.adapters.venues import coliseum

    class _Date(_dt.date):
        @classmethod
        def today(cls):
            return cls(TODAY.year, TODAY.month, TODAY.day)

    monkeypatch.setattr(coliseum, "dt", types.SimpleNamespace(date=_Date))
    cfg = VENUES["London Coliseum"]
    raws = raws_from_html(_html("coliseum"), cfg, _ctx())
    tosca = [r for r in raws if r.title == "Tosca"]
    assert [(r.date_text, r.time_text, r.source_kind) for r in tosca] == [
        ("19 November 2026", "7.30pm", "adapter:coliseum"), ("21 November 2026", "6.00pm", "adapter:coliseum"),
        ("27 November 2026", "7.30pm", "adapter:coliseum"), ("30 November 2026", "5.00pm", "adapter:coliseum")]
    evs = [e for e in build_events(raws, cfg, _ctx()) if e.title == "Tosca"]
    assert [(e.kind, e.date, e.time, e.sessions_known) for e in evs] == [
        ("one_off", date(2026, 11, 19), "19:30", True), ("one_off", date(2026, 11, 21), "18:00", True),
        ("one_off", date(2026, 11, 27), "19:30", True), ("one_off", date(2026, 11, 30), "17:00", True)]
    assert {e.url for e in evs} == {"https://londoncoliseum.org/events/tosca/"}


def test_few_undated_cards_dropped_many_defer_to_llm():
    ctx = _ctx()
    raws = raws_from_html(_html("vam"), VENUES["V&A"], ctx)            # 9 of 10 cards dated
    assert len(raws) == 9 and ctx.stats["adapter.undated"] == 1 and all(r.price_text for r in raws)
    ad = {"kind": "cards", "card": "li", "title": "h2", "date": ".d"}
    html = "".join(f'<li><h2>T{i}</h2><p class="d">{d}</p></li>'
                   for i, d in enumerate(["Now open", "On-demand", f"{TODAY.day + 1} {TODAY:%B %Y}"]))
    ctx = _ctx()
    assert raws_from_html(html, VenueConfig(name="C", url="https://c.example/", adapter=ad), ctx) == []
    assert ctx.stats["adapter.unparsed_venues"] == 1


@pytest.mark.parametrize("adapter", [None, {"kind": "json", "script": "script", "title": "t", "date": "d", "llm": True},
                                     {"kind": "spektrix", "client": "x"}])
def test_non_direct_returns_nothing(adapter):
    cfg = VenueConfig(name="X", url="https://x.org/", adapter=adapter)
    assert raws_from_html(_html("orange_tree"), cfg, _ctx()) == []


def test_zero_records_reported():
    cfg = VenueConfig(name="Cards", url="https://x.org/", adapter={"kind": "cards", "card": ".nope"})
    ctx = _ctx()
    assert raws_from_html("<html><body></body></html>", cfg, ctx) == []
    assert not ctx.errors and ctx.stats["adapter.no_records"] == 1     # benign: next source runs


def test_dedupe_and_not_ok_fetch():
    cfg = VenueConfig(name="Cards", url="https://x.org/",
                      adapter={"kind": "cards", "card": ".ev", "title": "h3", "date": ".d"})
    card = '<div class="ev"><h3>Gig</h3><span class="d">20 Nov 2026</span></div>'
    assert len(raws_from_html(card * 3, cfg, _ctx())) == 1
    fr = FetchResult(FetchStatus.ERROR, body=card)          # fallback body is parsed (flagged stale upstream)
    assert len(asyncio.run(get_source("adapter").records(cfg, fr, _ctx()))) == 1
    assert asyncio.run(get_source("adapter").records(cfg, FetchResult(FetchStatus.ERROR), _ctx())) == []


# ── cards: price selector / card-text fallback, time from the title ─────────

_CARDS = ('<ul>'
          '<li class="c"><h2>Cloak Talk</h2><p class="d">{d1}</p><p class="t">Talk</p>'
          '<div class="tk"><p>£15.00 - £20.00</p></div><a href="/event/cloak">More</a></li>'
          '<li class="c"><h2>Library talks Monday 11.30</h2><p class="d">Every Monday until {end}</p>'
          '<div class="tk"><p>Free</p></div><a href="/event/nal">More</a></li>'
          '<li class="c"><h2>Free Your Mind</h2><p class="d">{d1}</p><a href="/event/fym">More</a></li>'
          '</ul>')


def _cards_html() -> tuple[str, date, date]:
    d1 = TODAY + _dt.timedelta(days=3)
    end = TODAY + _dt.timedelta(days=50)
    return _CARDS.format(d1=f"{d1.day} {d1:%B %Y}", end=f"{end.day} {end:%B %Y}"), d1, end


@pytest.mark.parametrize("price_sel", [".tk p", None])
def test_cards_price_and_title_time(price_sel):
    from src.sources.adapters import get_adapter
    html, d1, end = _cards_html()
    ad = {"kind": "cards", "card": "li.c", "title": "h2", "date": ".d", "desc": ".t"}
    if price_sel:
        ad["price"] = price_sel
    recs = get_adapter({"adapter": ad}).records(html, {"adapter": ad}, today=TODAY)
    from src.normalize import parse_price_text
    assert [(r.title, parse_price_text(r.price) or "", r.time) for r in recs] == [
        ("Cloak Talk", "£15–£20", ""), ("Library talks Monday 11.30", "Free", "11:30"),
        ("Free Your Mind", "", "")]                     # a title word is not a price
    cfg = VenueConfig(name="Cards", url="https://cards.example.org/whatson", adapter=ad)
    evs = {e.title: e for e in build_events(raws_from_html(html, cfg, _ctx()), cfg, _ctx())}
    assert evs["Cloak Talk"].price == "£15–£20"
    nal = evs["Library talks"]                     # "Monday 11.30" moved into weekday + time
    assert (nal.kind, nal.byday, nal.time, nal.end_date, nal.start_known, nal.price) == (
        "recurring", [0], "11:30", end, False, "Free")
    assert nal.date >= TODAY and nal.date.weekday() == 0


@pytest.mark.parametrize("title,want", [
    ("Talks Monday 11.30", "11:30"), ("Late 19:30", "19:30"), ("Gig 7pm", "19:00"), ("Tour 7.30am", "07:30"),
    ("2:22 A Ghost Story", ""), ("After Midnight", ""), ("Concert 2.50", ""), ("Room 101", ""), ("Plain title", ""),
])
def test_cards_title_time(title, want):
    from src.sources.adapters.cards import _title_time
    assert _title_time(title) == want
