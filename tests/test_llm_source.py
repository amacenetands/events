"""llm source: raw-string prompt, fake LLM backend, cache, failures -> stats. No DB, no network."""
import asyncio
import datetime as _dt
import json
import types
from datetime import date

import pytest

import src.extract as ex
from src.config import VenueConfig, get_city
from src.fetch.types import FetchResult, FetchStatus
from src.sources import llm_source
from src.sources.adapters import textutil
from src.sources.base import SourceCtx, get_source
from src.sources.build import build_events

TODAY = date(2026, 11, 15)     # a Sunday
CFG = VenueConfig(name="Test Venue", url="https://ex.org/whats-on/", category_hint="concert",
                  hint="Family shows are listed under 'Little Ones'.")
PAGE = """<html><body><nav>Home</nav><main>
<div class="ev"><h3>Winter Jazz Night</h3><span>Fri 21 Nov – Sun 7 Dec 2026</span><span>7.30pm</span>
 <span>£15 / £10 conc</span><a href="/e/jazz">More</a></div>
<div class="ev"><h3>Pottery Workshop</h3><span>Sat 22 Nov 2026</span><span>2pm</span><span>Free</span>
 <a href="/e/pottery">More</a></div>
<div class="ev"><h3>Toddler Tales</h3><span>Every Saturday from 28 Nov 2026</span><span>10.30am</span>
 <a href="/e/tales">More</a></div>
</main></body></html>"""
REPLY = {"Test Venue": [
    {"title": "Winter Jazz Night", "date_text": "Fri 21 Nov – Sun 7 Dec 2026", "time_text": "7.30pm",
     "price_text": "£15 / £10 conc", "url": "/e/jazz", "desc": "x" * 500},
    {"title": "Pottery Workshop", "date_text": "Sat 22 Nov 2026", "time_text": "2pm", "price_text": "Free",
     "url": None, "category_text": "Workshop", "age_text": "8+"},
    {"title": "", "date_text": "Sat 22 Nov 2026"},          # untitled: dropped
    "not an object",                                        # dropped
]}


class _Frozen(_dt.date):
    @classmethod
    def today(cls):
        return cls(TODAY.year, TODAY.month, TODAY.day)


@pytest.fixture
def fake(monkeypatch):
    """Fake LLM + in-memory extraction cache; returns the call log."""
    state = {"calls": [], "reply": json.dumps(REPLY), "cache": {}}

    async def run_llm(prompt, backend=None, timeout=0, retries=0):
        state["calls"].append((prompt, backend))
        r = state["reply"]
        return r(prompt) if callable(r) else r

    def get_cached_parse(name, force=False, fetched_at=None, content_hash=None, model=None, **_):
        hit = state["cache"].get(name)
        if force or hit is None or hit[0] != content_hash:
            return None
        return hit[1], f"content-hash:{model}"

    def save_extraction(name, raw, content_hash=None, model=""):
        state["cache"][name] = (content_hash, raw)

    monkeypatch.setattr(ex, "run_llm", run_llm)
    monkeypatch.setattr(ex, "get_cached_parse", get_cached_parse)
    monkeypatch.setattr(ex, "save_extraction", save_extraction)
    monkeypatch.setattr(ex, "current_model_label", lambda: "deepseek")
    monkeypatch.setattr(ex, "_retry_target", lambda m: None)
    monkeypatch.setattr(ex, "BATCH_SIZE", 1)
    monkeypatch.setattr(ex.timing, "log_runtime", lambda **k: None)
    monkeypatch.setattr(textutil, "dt", types.SimpleNamespace(date=_Frozen, timedelta=_dt.timedelta,
                                                               datetime=_dt.datetime))
    return state


def _ctx(city: str = "london", **kw) -> SourceCtx:
    return SourceCtx(http=None, today=TODAY, window_days=90, city=get_city(city), **kw)


def _run(cfg=CFG, body=PAGE, ctx=None):
    ctx = ctx or _ctx()
    raws = asyncio.run(get_source("llm").records(cfg, FetchResult(FetchStatus.OK, body=body, url=cfg.url), ctx))
    return raws, ctx


def test_raw_strings_to_events(fake):
    raws, ctx = _run()
    assert [r.title for r in raws] == ["Winter Jazz Night", "Pottery Workshop"]
    jazz, pottery = raws
    assert (jazz.date_text, jazz.time_text, jazz.price_text, jazz.source_kind) == (
        "Fri 21 Nov – Sun 7 Dec 2026", "7.30pm", "£15 / £10 conc", "llm")
    assert len(jazz.desc) == ex.DESC_MAX
    assert pottery.url == "https://ex.org/e/pottery"        # filled from the card's "-> /e/pottery"
    evs = {e.title: e for e in build_events(raws, CFG, ctx)}
    j, p = evs["Winter Jazz Night"], evs["Pottery Workshop"]
    assert (j.date, j.end_date, j.kind, j.time, j.price, j.url) == (
        date(2026, 11, 21), date(2026, 12, 7), "run", "19:30", "£15", "https://ex.org/e/jazz")
    assert (p.date, p.kind, p.time, p.price, p.category, p.min_age, p.audience) == (
        date(2026, 11, 22), "one_off", "14:00", "Free", "workshop", 8, "kids")
    assert ctx.stats["llm.records"] == 2 and not ctx.errors


def test_prompt_header(fake):
    _run()
    (prompt, _), = fake["calls"]
    assert "Today is Sunday 2026-11-15" in prompt
    assert "=== SITE: Test Venue | URL: https://ex.org/whats-on/ | Language: en | Category hint: concert ===" in prompt
    assert ("[Venue note — context only; never copy dates/times/prices from it: "
            "Family shows are listed under 'Little Ones'.]") in prompt
    assert "a venue web page" in prompt and "several venue" not in prompt
    assert "Winter Jazz Night" in prompt and "Home" not in prompt
    assert "COPY strings verbatim" in prompt and "date_text" in prompt and "YYYY-MM-DD" not in prompt
    assert "venue  " not in prompt                       # venue field only for aggregators


def test_moscow_lang_and_aggregator_venue(fake):
    cfg = VenueConfig(name="Тест", url="https://ex.ru/afisha/", city="moscow", must_determine_venue=True)
    fake["reply"] = json.dumps({"Тест": [{"title": "Концерт", "date_text": "19 ноября", "time_text": "19:00",
                                          "price_text": "от 500 ₽", "venue": "Зарядье"}]})
    page = PAGE.replace("Winter Jazz Night", "Концерт").replace("Fri 21 Nov – Sun 7 Dec 2026", "19 ноября 2026")
    raws, ctx = _run(cfg, page, _ctx("moscow"))
    (prompt, _), = fake["calls"]
    assert "Language: ru" in prompt and "copy those strings as-is" in prompt
    assert "venue           string" in prompt
    (e,) = build_events(raws, cfg, ctx)
    assert (e.title, e.date, e.time, e.venue) == ("Концерт", date(2026, 11, 19), "19:00", "Зарядье")


def test_cache_hit_second_call(fake):
    first, _ = _run()
    second, ctx = _run()
    assert len(fake["calls"]) == 1 and second == first
    _run(ctx=_ctx(force_parse=True))
    assert len(fake["calls"]) == 2                       # force_parse bypasses the cache


def test_prompt_change_busts_cache(fake, monkeypatch):
    _run()
    monkeypatch.setattr(ex, "_PROMPT_VERSION", "deadbeef")
    _run()
    assert len(fake["calls"]) == 2


@pytest.mark.parametrize("reply,stat", [
    ("Sorry, I cannot help with that.", "llm.failed"),          # no JSON at all -> fallback -> failed
    ('{"Test Venue": [{"title": "Winter', None),                 # truncated -> split -> chunks fail
])
def test_bad_reply_returns_empty_with_stats(fake, reply, stat):
    fake["reply"] = reply
    raws, ctx = _run()
    assert raws == []
    assert ctx.stats.get("llm.failed", 0) + ctx.stats.get("llm.partial", 0) >= 1
    if stat:
        assert ctx.stats[stat] == 1
    assert ctx.errors and all(e.startswith("Test Venue: llm") for e in ctx.errors)
    assert "Test Venue" not in fake["cache"]                    # failures are never cached


def test_thin_page_and_unusable_fetch(fake):
    raws, ctx = _run(body="<html><body><div id='app'></div></body></html>")
    assert raws == [] and ctx.stats["llm.thin"] == 1 and not fake["calls"]
    ctx = _ctx()
    assert asyncio.run(get_source("llm").records(CFG, FetchResult(FetchStatus.BLOCKED), ctx)) == []
    assert asyncio.run(get_source("llm").records(CFG, None, ctx)) == []


def test_markdown_body_reduced_as_text(fake):
    md = "# What's on\n\n* Winter Jazz Night — Fri 21 Nov 2026, 7.30pm\n* Pottery Workshop — Sat 22 Nov 2026\n"
    site = llm_source.site_data(CFG, FetchResult(FetchStatus.OK, body=md), _ctx())
    assert site is not None and "Winter Jazz Night" in site["text"]


def test_concurrent_venues_share_one_batch(fake, monkeypatch):
    monkeypatch.setattr(ex, "BATCH_SIZE", 3)
    monkeypatch.setattr(ex, "BATCH_CHAR_BUDGET", 100_000)
    monkeypatch.setattr(llm_source, "IDLE_FLUSH", 0.01)
    cfgs = [VenueConfig(name=f"V{i}", url=f"https://v{i}.org/") for i in range(3)]
    fake["reply"] = lambda p: json.dumps({c.name: [{"title": f"Show {c.name}", "date_text": "Sat 22 Nov 2026"}]
                                          for c in cfgs})

    async def go():
        fr = FetchResult(FetchStatus.OK, body=PAGE)
        return await asyncio.gather(*(get_source("llm").records(c, fr, _ctx()) for c in cfgs))

    res = asyncio.run(go())
    assert len(fake["calls"]) == 1
    assert [[r.title for r in rs] for rs in res] == [["Show V0"], ["Show V1"], ["Show V2"]]


def test_invented_and_listing_urls_rederived(fake):
    fake["reply"] = json.dumps({"Test Venue": [
        {"title": "Winter Jazz Night", "date_text": "Sat 22 Nov 2026", "url": "https://ex.org/made-up/jazz"},
        {"title": "Pottery Workshop", "date_text": "Sat 22 Nov 2026", "url": "https://ex.org/whats-on/"},
        {"title": "Ghost Show", "date_text": "Sat 22 Nov 2026", "url": "/nowhere"},
        {"title": "Toddler Tales", "date_text": "Sat 28 Nov 2026", "url": "https://ex.org/e/tales"}]})
    raws, _ = _run()
    assert [r.url for r in raws] == ["https://ex.org/e/jazz", "https://ex.org/e/pottery", None,
                                     "https://ex.org/e/tales"]


def test_venue_kept_only_for_aggregators(fake):
    fake["reply"] = json.dumps({"Test Venue": [{"title": "Winter Jazz Night", "date_text": "Sat 22 Nov 2026",
                                                "venue": "Some Hall"}]})
    raws, _ = _run()
    assert raws[0].venue is None


def test_reply_for_another_site_is_an_error_not_an_empty(fake, monkeypatch):
    cfgs = [VenueConfig(name=f"V{i}", url=f"https://v{i}.org/") for i in range(2)]
    monkeypatch.setattr(ex, "BATCH_SIZE", 2)
    monkeypatch.setattr(ex, "BATCH_CHAR_BUDGET", 100_000)
    fake["reply"] = json.dumps({"v0": [{"title": "Show A", "date_text": "Sat 22 Nov 2026"}], "Other": []})

    async def go():
        ctxs = [_ctx(), _ctx()]
        fr = FetchResult(FetchStatus.OK, body=PAGE)
        res = await asyncio.gather(*(get_source("llm").records(c, fr, x) for c, x in zip(cfgs, ctxs)))
        return res, ctxs

    (r0, r1), (c0, c1) = asyncio.run(go())
    assert [r.title for r in r0] == ["Show A"] and not c0.errors     # case-folded key match
    assert r1 == [] and c1.stats["llm.failed"] == 1 and "V1" not in fake["cache"]


def test_aliases_and_dropped_keys(fake):
    fake["reply"] = json.dumps({"Test Venue": [{"name": "Winter Jazz Night", "date": "Sat 22 Nov 2026",
                                                "start_time": "7pm", "ticket_url": "/e/jazz", "sold_out": True,
                                                "price": {"min": 5}}]})
    raws, ctx = _run()
    (r,) = raws
    assert (r.title, r.date_text, r.time_text, r.url, r.sold_out, r.price_text) == (
        "Winter Jazz Night", "Sat 22 Nov 2026", "7pm", "https://ex.org/e/jazz", True, None)
    assert ctx.stats["llm.dropped_keys"] == 1


def test_header_change_busts_cache_but_about_is_never_sent(fake):
    _run()
    _run(cfg=CFG.model_copy(update={"about": "Human notes only"}))
    assert len(fake["calls"]) == 1 and "Human notes" not in fake["calls"][0][0]
    _run(cfg=CFG.model_copy(update={"hint": "Different hint"}))
    assert len(fake["calls"]) == 2


# ── price post-check: a price copied from the neighbouring card is dropped ───

def test_verify_prices_drops_bleed_keeps_own_and_unverifiable():
    from src.models import RawEvent
    text = ("[C] Sat 14 Nov, 7.30pm | Maxine Funke | £19 | £17 Advance | £12 MEMBERS -> /events/maxine/\n"
            "[C] 20–21 Nov | JIMI TENOR BAND – Two-Day Residency | MORE INFO & TICKETS -> /events/jimi/\n"
            "£19\n£17 Advance\n"
            "Plain text gig on 22 Nov £5\n")
    raws = [RawEvent(title="Maxine Funke", price_text="£19 £17 Advance"),
            RawEvent(title="JIMI TENOR BAND – Two-Day Residency", price_text="£19 £17 Advance"),
            RawEvent(title="Plain text gig", price_text="£5"),
            RawEvent(title="Maxine Funke", price_text="£17 | £19")]          # reordered: amounts on card
    ctx = _ctx()
    out = llm_source.verify_prices(raws, text, ctx)
    assert [r.price_text for r in out] == ["£19 £17 Advance", None, "£5", "£17 | £19"]
    assert ctx.stats["llm.price_dropped"] == 1 and ctx.stats["llm.price_unverified"] == 1


def test_verify_prices_jsonld_line_is_not_a_card_and_amounts_normalised():
    from src.models import RawEvent
    text = ("[J] Winter Gala | 2026-12-01 -> /e/gala\n"
            "[C] Other Gig | 1 Dec | £12 -> /e/other\n"
            "[C] Jazz Brunch | 13 Сент | от 750 руб. -> /e/brunch\n"
            "[C] Quartet | 2 Dec | £10.00 -> /e/q\n")
    raws = [RawEvent(title="Winter Gala", price_text="£12"),      # only a [J] line: not its own card -> kept
            RawEvent(title="Jazz Brunch", price_text="From 750 ₽"),  # reformatted RU amount: on its card
            RawEvent(title="Quartet", price_text="£10"),           # '£10' == '£10.00'
            RawEvent(title="Quartet", price_text="£12")]           # on another card, not its own: dropped
    ctx = _ctx()
    out = llm_source.verify_prices(raws, text, ctx)
    assert [r.price_text for r in out] == ["£12", "From 750 ₽", "£10", None]
    assert ctx.stats == {"llm.price_unverified": 1, "llm.price_dropped": 1}
