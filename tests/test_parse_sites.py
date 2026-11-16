"""parse_sites / parse_venue: ordered `extract` semantics over fake sources. No DB, no network."""
import asyncio
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.config import VenueConfig, get_city
from src.fetch.types import FetchResult, FetchStatus
from src.models import RawEvent
from src.sources import base, html_sites
from src.sources.base import SourceCtx

FIX = Path(__file__).parent / "fixtures" / "sources"
TODAY = date(2026, 11, 15)
JSONLD_PAGE = (FIX / "jsonld_apollo.html").read_text()
PLAIN_PAGE = "<html><body><main><h3>Gig</h3><p>Sat 22 Nov 2026</p></main></body></html>"
WALL = ("<html><head><title>Just a moment...</title></head><body>"
        "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1'></script></body></html>")


class Fake:
    def __init__(self, kind, titles=(), exc=None):
        self.kind, self.titles, self.exc, self.calls = kind, list(titles), exc, 0
        self.times: dict[str, str] = {}         # title -> HH:MM (default 19:30)

    async def records(self, cfg, fr, ctx):
        self.calls += 1
        if self.exc:
            raise self.exc
        day = (ctx.today + timedelta(days=7)).isoformat()      # relative: parse_sites uses the real today
        return [RawEvent(title=t, date_text=f"{day}T{self.times.get(t, '19:30')}", source_kind=self.kind)
                for t in self.titles]


@pytest.fixture
def fakes(monkeypatch):
    base.load_builtin_sources()
    made = {}

    def install(kind, *titles, exc=None):
        made[kind] = Fake(kind, titles, exc)
        monkeypatch.setitem(base.SOURCES, kind, made[kind])
        return made[kind]
    return install


def _ctx():
    return SourceCtx(http=None, today=TODAY, window_days=90, city=get_city("london"))


def _venue(cfg, fr):
    ctx = _ctx()
    evs, st = asyncio.run(html_sites.parse_venue(cfg, fr, ctx))
    return evs, st, ctx


def _ok(body):
    return FetchResult(FetchStatus.OK, body=body, url="https://ex.org/")


def test_jsonld_wins_llm_not_called(fakes):
    llm = fakes("llm", "LLM event")
    cfg = VenueConfig(name="Apollo Theatre", url="https://ex.org/")
    evs, st, _ = _venue(cfg, _ok(JSONLD_PAGE))
    assert st["source"] == "jsonld" and evs and llm.calls == 0
    assert all(e.title != "LLM event" for e in evs)


def test_jsonld_empty_falls_through_to_llm(fakes):
    llm = fakes("llm", "LLM event")
    evs, st, _ = _venue(VenueConfig(name="V", url="https://ex.org/"), _ok(PLAIN_PAGE))
    assert llm.calls == 1 and st["source"] == "llm" and [e.title for e in evs] == ["LLM event"]
    assert (evs[0].date, evs[0].time) == (date(2026, 11, 22), "19:30")


def test_order_and_adapter_needs_config(fakes):
    adapter, follow, llm = fakes("adapter", "A"), fakes("follow_links", "F"), fakes("llm", "L")
    cfg = VenueConfig(name="V", url="https://ex.org/", extract=["follow_links", "adapter", "llm"])
    _, st, _ = _venue(cfg, _ok(PLAIN_PAGE))
    assert st["source"] == "follow_links" and (adapter.calls, llm.calls) == (0, 0)
    assert html_sites.source_plan(cfg, _ok(PLAIN_PAGE)) == ["follow_links", "llm"]   # no adapter configured


def test_merge_unions_and_dedupes(fakes):
    fakes("jsonld", "Show A", "Show B").times = {"Show B": "15:00"}
    fakes("llm", "Show B", "Show C", "Show A again").times = {"Show B": "15:00", "Show C": "21:00"}
    cfg = VenueConfig(name="V", url="https://ex.org/", merge=True)
    evs, st, ctx = _venue(cfg, _ok(PLAIN_PAGE))
    assert st["source"] == "jsonld+llm" and st["raws"] == 5
    # "Show A again" shares Show A's date + time: the later source's copy is dropped
    assert sorted(e.title for e in evs) == ["Show A", "Show B", "Show C"]
    assert ctx.stats["build.merge_dropped"] == 1


SPX = {"kind": "spektrix", "client": "testclient"}


def test_blocked_no_body_skips_page_sources_but_spektrix_runs(fakes):
    jl, llm, spx = fakes("jsonld", "J"), fakes("llm", "L"), fakes("spektrix", "Spektrix show")
    fr = FetchResult(FetchStatus.BLOCKED, url="https://ex.org/", note="bot wall")
    evs, st, ctx = _venue(VenueConfig(name="V", url="https://ex.org/", adapter=SPX), fr)
    assert (jl.calls, llm.calls, spx.calls) == (0, 0, 1)
    assert st["source"] == "spektrix" and st["fetch"] == "blocked" and [e.title for e in evs] == ["Spektrix show"]
    assert not ctx.errors
    assert html_sites.source_plan(VenueConfig(name="W", url="https://ex.org/"), fr) == []


def test_blocked_with_fallback_body_is_parsed_and_flagged_stale(fakes):
    fakes("llm", "Old but good")
    fr = FetchResult(FetchStatus.BLOCKED, body=PLAIN_PAGE, url="https://ex.org/",
                     note="bot wall; serving cached body from 2026-11-10")
    evs, st, ctx = _venue(VenueConfig(name="V", url="https://ex.org/"), fr)
    assert [e.title for e in evs] == ["Old but good"]
    assert ctx.errors == ["V: stale page (bot wall; serving cached body from 2026-11-10)"]


def test_api_mode_skips_page_sources_silently(fakes):
    jl, spx = fakes("jsonld", "J"), fakes("spektrix", "S")
    fr = FetchResult(FetchStatus.OK, body="", url="https://ex.org/", note="api")
    evs, st, ctx = _venue(VenueConfig(name="V", url="https://ex.org/", adapter=SPX), fr)
    assert (jl.calls, spx.calls) == (0, 1) and not ctx.errors and st["source"] == "spektrix"


def test_unlisted_spektrix_fingerprint_is_not_run(fakes):
    spx, llm = fakes("spektrix", "S"), fakes("llm", "L")
    page = PLAIN_PAGE.replace("</main>", '<spektrix-basket-summary client-name="testclient"></spektrix-basket-summary>'
                                         '<script src="https://system.spektrix.com/testclient/website/scripts/integrate.js"></script></main>')
    evs, st, ctx = _venue(VenueConfig(name="V", url="https://ex.org/"), _ok(page))
    assert spx.calls == 0 and st["source"] == "llm" and ctx.stats.get("spektrix.detected_unlisted") == 1


def test_offline_spektrix_makes_no_calls():
    from src.sources.spektrix import SpektrixSource
    ctx = _ctx()
    ctx.offline = True
    cfg = VenueConfig(name="V", url="https://ex.org/", adapter=SPX)
    assert asyncio.run(SpektrixSource().records(cfg, FetchResult(FetchStatus.OK), ctx)) == []   # http=None: would crash
    assert ctx.stats["spektrix.offline"] == 1 and not ctx.errors


def test_cached_result_from_store(monkeypatch):
    from src import fetch, store
    rows = {"https://ok.org/": {"html": PLAIN_PAGE, "fetched_at": "2026-11-10T08:00:00+00:00",
                                "final_url": "https://ok.org/whats-on/"},
            "https://wall.org/": {"html": WALL, "fetched_at": "2026-11-10T08:00:00+00:00"}}
    monkeypatch.setattr(store, "get_scrape", lambda url: rows.get(url))
    ok = fetch.cached_result(VenueConfig(name="A", url="https://ok.org/"))
    assert (ok.status, ok.body, ok.url, ok.fetched_at.day) == (FetchStatus.NOT_MODIFIED, PLAIN_PAGE,
                                                               "https://ok.org/whats-on/", 10)
    assert fetch.cached_result(VenueConfig(name="B", url="https://wall.org/")).status == FetchStatus.BLOCKED
    assert fetch.cached_result(VenueConfig(name="C", url="https://none.org/")).status == FetchStatus.EMPTY


def test_raising_source_does_not_kill_venue(fakes):
    fakes("jsonld", exc=RuntimeError("boom"))
    fakes("llm", "Still here")
    evs, st, ctx = _venue(VenueConfig(name="V", url="https://ex.org/"), _ok(PLAIN_PAGE))
    assert [e.title for e in evs] == ["Still here"] and ctx.stats["jsonld.raised"] == 1
    assert any("source jsonld raised RuntimeError" in e for e in ctx.errors)


def test_parse_sites_end_to_end(fakes, monkeypatch):
    fakes("llm", "Good show")
    fakes("jsonld", exc=ValueError("bad"))
    venues = [VenueConfig(name="Alpha", url="https://a.org/"), VenueConfig(name="Beta", url="https://b.org/"),
              VenueConfig(name="Gamma", url="https://c.org/")]
    monkeypatch.setattr(html_sites, "_venues", lambda p, t, c=None: (get_city("london"),
                        [v for v in venues if not t or v.name.lower() == t.lower()]))
    monkeypatch.setattr(html_sites.timing, "log_runtime", lambda **k: None)
    frs = {"Alpha": _ok(PLAIN_PAGE), "Beta": FetchResult(FetchStatus.EMPTY, note="none")}   # Gamma missing
    errors, summary = [], {}
    evs = asyncio.run(html_sites.parse_sites(errors=errors, summary=summary, frs=frs))
    assert {e.source for e in evs} == {"Alpha"}
    assert summary["Alpha"]["source"] == "llm" and summary["Beta"]["fetch"] == "empty"
    assert summary["Gamma"]["fetch"] == "error"
    assert errors == [summary["Alpha"]["errors"][0]] and errors[0].startswith("Alpha: source jsonld raised")
    assert asyncio.run(html_sites.parse_sites(test_site="beta", frs=frs)) == []
