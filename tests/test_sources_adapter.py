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


def test_parity_with_rec_to_event_orange_tree():
    from src.sources.adapters import get_adapter
    from src.sources.reduce import rec_to_event
    cfg = VENUES["Orange Tree Theatre"]
    site = cfg.to_legacy_dict()
    recs = [r for r in get_adapter(site).records(_html("orange_tree"), site) if r.title]
    old = {(e.title, e.date) for r in recs for e in rec_to_event(r, site, TODAY) if (e.end_date or e.date) >= TODAY}
    new = {(e.title, e.date) for e in build_events(raws_from_html(_html("orange_tree"), cfg, _ctx()), cfg, _ctx())}
    assert old == new


def test_coliseum_python_adapter(monkeypatch):
    from src.sources.adapters.venues import coliseum

    class _Date(_dt.date):
        @classmethod
        def today(cls):
            return cls(TODAY.year, TODAY.month, TODAY.day)

    monkeypatch.setattr(coliseum, "dt", types.SimpleNamespace(date=_Date))
    cfg = VENUES["London Coliseum"]
    raws = raws_from_html(_html("coliseum"), cfg, _ctx())
    tosca = next(r for r in raws if r.title == "Tosca")
    assert (tosca.date_text, tosca.time_text, tosca.source_kind) == (
        "19 November 2026 - 30 November 2026", "7.30pm", "adapter:coliseum")
    e = next(e for e in build_events(raws, cfg, _ctx()) if e.title == "Tosca")
    assert (e.kind, e.date, e.end_date, e.time, e.sessions_known, e.url) == (
        "run", date(2026, 11, 19), date(2026, 11, 30), "19:30", False, "https://londoncoliseum.org/events/tosca/")


def test_unparseable_dates_defer_to_llm():
    ctx = _ctx()
    assert raws_from_html(_html("vam"), VENUES["V&A"], ctx) == []     # "Now open" cards
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
    assert ctx.errors == ["Cards: adapter 'cards' found 0 records"]


def test_dedupe_and_not_ok_fetch():
    cfg = VenueConfig(name="Cards", url="https://x.org/",
                      adapter={"kind": "cards", "card": ".ev", "title": "h3", "date": ".d"})
    card = '<div class="ev"><h3>Gig</h3><span class="d">20 Nov 2026</span></div>'
    assert len(raws_from_html(card * 3, cfg, _ctx())) == 1
    fr = FetchResult(FetchStatus.ERROR, body=card)
    assert asyncio.run(get_source("adapter").records(cfg, fr, _ctx())) == []
