import asyncio
from datetime import date, timedelta

import pytest

from src import rank as R
from src.digest import generate_digest_v2
from src.models import Event
from src.query import Filter

TODAY = date(2026, 11, 9)


def ev(title, venue="V", cat="talk", aud="all", kind="one_off", d=None, **kw):
    return Event(title=title, venue=venue, date=d or TODAY + timedelta(days=3), category=cat, audience=aud,
                 kind=kind, url="http://x/" + title.replace(" ", ""), source=venue, **kw)


def ctx(**kw):
    return R.RankCtx(today=TODAY, now=__import__("datetime").datetime(2026, 11, 9, 9), weights={}, profile={}, **kw)


def test_audience_fit_ordering():
    f = Filter(audience="family")
    c = ctx()
    kid, al, adult = ev("a", aud="kids"), ev("b", aud="all"), ev("c", aud="adult")
    assert R.score(kid, f, c) > R.score(al, f, c) > R.score(adult, f, c)
    assert R.score(adult, f, c) < 0


def test_venue_weight():
    f = Filter()
    c = ctx(); c.weights = {"big": 3, "mid": 2}
    assert R.score(ev("a", venue="big"), f, c) - R.score(ev("a", venue="x"), f, c) == 4
    assert R.score(ev("a", venue="mid"), f, c) - R.score(ev("a", venue="x"), f, c) == 2


def test_closing_soon_boost():
    f, c = Filter(), ctx()
    soon = ev("x", kind="run", d=TODAY - timedelta(days=30), end_date=TODAY + timedelta(days=3))
    late = ev("x", kind="run", d=TODAY - timedelta(days=30), end_date=TODAY + timedelta(days=60))
    assert R.score(soon, f, c) - R.score(late, f, c) == R.W_CLOSING


def test_novelty_and_specificity():
    f = Filter(); e = ev("fresh", time="19:00")
    c = ctx(new_fps={e.fingerprint()}, seen_titles=set())
    p = R.score_parts(e, f, c)
    assert p["new"] == R.W_NEW_RECENT and p["new title"] == R.W_NEW_TITLE and p["timed"] == R.W_HAS_TIME
    assert R.score(ev("u", date_undetermined=True), f, ctx()) < R.score(ev("u"), f, ctx())


def test_diversity_caps():
    evs = [ev(f"t{i}", venue="A", cat="talk") for i in range(8)] + [ev(f"u{i}", venue=f"B{i}", cat="talk") for i in range(8)]
    picks = R.pick_diverse(evs, 12)
    assert sum(e.venue == "A" for e in picks) <= 2
    assert sum(e.category == "talk" for e in picks) <= 4
    assert len(picks) == 4


def test_nothing_hidden_in_digest():
    f = Filter()
    evs = [ev(f"event {i}", venue=f"V{i % 7}", cat=["talk", "concert", "film"][i % 3]) for i in range(80)]
    evs.append(ev("a run", kind="run", d=TODAY - timedelta(days=5), end_date=TODAY + timedelta(days=2)))
    res = asyncio.run(R.rank_events(evs, f, ctx()))
    assert 0 < len(res.picks) <= R.TOP_N
    md = generate_digest_v2(evs, f, picks=res.picks, reasons=res.reasons, scores=res.scores, today=TODAY)
    for e in evs:
        assert e.title in md, e.title
    assert "★" in md and "Top picks" in md


def test_llm_rerank_fallback(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("backend down")
    monkeypatch.setattr(R, "run_llm", boom)
    evs = [ev(f"e{i}", venue=f"V{i}", cat=["talk", "concert", "film"][i % 3]) for i in range(10)]
    res = asyncio.run(R.rank_events(evs, Filter(), ctx(), use_llm=True))
    base = asyncio.run(R.rank_events(evs, Filter(), ctx(), use_llm=False))
    assert not res.llm_used and [e.title for e in res.picks] == [e.title for e in base.picks]


def test_llm_rerank_ok(monkeypatch):
    seen = {}
    async def fake(prompt, **k):
        seen["p"] = prompt
        return "3|great fit\n1|also good\n99|bogus"
    monkeypatch.setattr(R, "run_llm", fake)
    evs = [ev(f"e{i}", venue=f"V{i}", cat=["talk", "concert", "film"][i % 3]) for i in range(6)]
    res = asyncio.run(R.rank_events(evs, Filter(), ctx(), n=2, use_llm=True))
    assert res.llm_used and len(res.picks) == 2
    assert res.reasons[res.picks[0].fingerprint()] == "great fit"


def test_closing_needs_started_and_stale_penalty():
    f = Filter(); c = ctx()
    future = ev("f", kind="run", d=TODAY + timedelta(days=2), end_date=TODAY + timedelta(days=4))
    assert "closing" not in R.score_parts(future, f, c)
    stale = ev("s", kind="run", d=TODAY - timedelta(days=90))
    fresh = ev("s", kind="run", d=TODAY - timedelta(days=10))
    assert R.score_parts(stale, f, c)["stale"] == R.W_STALE
    assert "stale" not in R.score_parts(fresh, f, c)
