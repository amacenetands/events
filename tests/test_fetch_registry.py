"""fetch_venue / fetch_all: scrapes cache, TTL, wall retention, per-host throttle.
Temp sqlite db via monkeypatched store._conn — never cache/memory.db."""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import src.fetch as F
from src import store
from src.config import VenueConfig
from src.fetch import FetchStatus
from src.fetch import http as fh
from src.fetch.report import fetch_status_report

FIX = Path(__file__).parent / "fixtures" / "fetch"
GOOD = (FIX / "sadlers_wells.html").read_text(encoding="utf-8")
GOOD2 = GOOD.replace("<body", "<body data-v2", 1)
WALL = (FIX / "southbank_cloudflare.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "t.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(store._SCHEMA)
    store._migrate_scrapes(conn)
    monkeypatch.setattr(store, "_conn", conn)

    async def _s(_):
        return None
    monkeypatch.setattr(fh, "_sleep", _s)
    yield conn
    conn.close()


class Server:
    """Scripted responses; records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.reqs: list[httpx.Request] = []

    def __call__(self, req):
        self.reqs.append(req)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return r(req) if callable(r) else r


def ok(body=GOOD, **h):
    return httpx.Response(200, text=body, headers={"content-type": "text/html", **h})


def venue(name="V", url="https://v.example/on", **fetch):
    return VenueConfig(name=name, url=url, fetch=fetch or {})


class FakeThrottle(F.HostThrottle):
    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []

        async def sleep(d):
            self.slept.append(d)
            self.t += d
        super().__init__(clock=lambda: self.t, sleep=sleep)


def go(srv, cfg, **kw):
    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(srv)) as c:
            return await F.fetch_venue(cfg, client=c, throttle=kw.pop("throttle", FakeThrottle()), **kw)
    return asyncio.run(main())


def age_row(conn, hours):
    t = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn.execute("UPDATE scrapes SET fetched_at=?", (t,))
    conn.commit()


def test_ok_stored(tmp_db):
    fr = go(Server(ok(etag='"e1"')), venue())
    assert fr.status is FetchStatus.OK
    row = store.get_scrape("https://v.example/on")
    assert row["html"] == GOOD and row["etag"] == '"e1"' and row["status"] == "ok"
    assert row["site_name"] == "V" and row["status_at"]


def test_ttl_fresh_no_network(tmp_db):
    go(Server(ok()), venue())
    srv = Server(ok(GOOD2))
    fr = go(srv, venue(), ttl_hours=24)
    assert fr.status is FetchStatus.NOT_MODIFIED and fr.body == GOOD and not srv.reqs
    assert "cache fresh" in fr.note


def test_ttl_expired_conditional_304_body_from_cache(tmp_db):
    go(Server(ok(etag='"e1"', **{"last-modified": "Mon, 02 Nov 2026 10:00:00 GMT"})), venue())
    age_row(tmp_db, 48)
    srv = Server(httpx.Response(304, headers={"etag": '"e1"'}))
    fr = go(srv, venue(), ttl_hours=24)
    assert srv.reqs[0].headers["if-none-match"] == '"e1"'
    assert "if-modified-since" in srv.reqs[0].headers
    assert fr.status is FetchStatus.NOT_MODIFIED and fr.body == GOOD and fr.ok
    row = store.get_scrape("https://v.example/on")
    assert row["status"] == "not_modified" and store_age(row) < 1   # revalidated → fresh


def store_age(row):
    return F._age_hours(row["fetched_at"])


def test_force_bypasses_ttl_and_conditional(tmp_db):
    go(Server(ok(etag='"e1"')), venue())
    srv = Server(ok(GOOD2))
    fr = go(srv, venue(), force=True, ttl_hours=24)
    assert fr.status is FetchStatus.OK and fr.body == GOOD2 and len(srv.reqs) == 1
    assert "if-none-match" not in srv.reqs[0].headers
    assert store.get_scrape("https://v.example/on")["html"] == GOOD2


def test_wall_keeps_previous_body(tmp_db):
    go(Server(ok()), venue())
    fr = go(Server(httpx.Response(403, text=WALL, headers={"cf-mitigated": "challenge"})),
            venue(), force=True)
    assert fr.status is FetchStatus.BLOCKED and fr.body == GOOD and "cached body" in fr.note
    row = store.get_scrape("https://v.example/on")
    assert row["html"] == GOOD and row["status"] == "blocked"


def test_wall_without_previous_body_placeholder(tmp_db):
    fr = go(Server(ok(WALL)), venue())
    assert fr.status is FetchStatus.BLOCKED and fr.body == ""
    row = store.get_scrape("https://v.example/on")
    assert row["html"] == "" and row["status"] == "blocked"
    # next run: placeholder is not a cache hit, OK replaces it
    fr = go(Server(ok()), venue())
    assert fr.status is FetchStatus.OK and store.get_scrape("https://v.example/on")["html"] == GOOD


def test_cached_wall_never_served(tmp_db):
    store.save_scrape("https://v.example/on", "V", WALL)      # legacy poisoned row
    srv = Server(ok())
    fr = go(srv, venue(), ttl_hours=24)
    assert srv.reqs and fr.status is FetchStatus.OK and fr.body == GOOD


def test_error_and_empty_keep_body(tmp_db):
    go(Server(ok()), venue())
    fr = go(Server(httpx.Response(500, text="x")), venue(), force=True)
    assert fr.status is FetchStatus.ERROR and fr.body == GOOD
    fr = go(Server(ok("<html><body></body></html>")), venue(), force=True)
    assert fr.status is FetchStatus.EMPTY and fr.body == GOOD
    assert store.get_scrape("https://v.example/on")["html"] == GOOD


def test_retry_500_then_200_stored(tmp_db):
    srv = Server(httpx.Response(503, text="busy"), ok())
    fr = go(srv, venue())
    assert fr.status is FetchStatus.OK and len(srv.reqs) == 2


def test_api_mode_no_network(tmp_db):
    srv = Server(ok())
    fr = go(srv, venue(mode="api"))
    assert fr.status is FetchStatus.OK and fr.body == "" and fr.note == "api" and not srv.reqs
    row = store.get_scrape("https://v.example/on")
    assert row["status"] == "api" and row["html"] == ""


def test_year_template_resolved(tmp_db):
    srv = Server(ok())
    go(srv, venue(url="https://v.example/{YEAR}/on"))
    assert "{YEAR}" not in str(srv.reqs[0].url)


def test_browser_mode_dispatch(tmp_db, monkeypatch):
    seen = {}

    async def fake(url, **kw):
        seen.update(kw, url=url)
        return F.FetchResult(FetchStatus.OK, body=GOOD, url=url)
    import src.fetch.browser as fb
    monkeypatch.setattr(fb, "fetch_browser", fake)
    fr = go(Server(ok()), venue(mode="browser_stealth", wait_for=".ev", remove_selectors=[".x"], delay=2))
    assert fr.status is FetchStatus.OK and seen["stealth"] is True
    assert seen["wait_for"] == ".ev" and seen["remove_selectors"] == [".x"] and seen["delay"] == 2


def test_throttle_per_host_fake_clock():
    th = FakeThrottle()

    async def main():
        await th.wait("https://a.example/1", 3)
        await th.wait("https://a.example/2", 3)    # same host: waits 3
        await th.wait("https://b.example/1", 3)    # other host: no wait
        th.t += 10
        await th.wait("https://a.example/3", 3)    # gap already elapsed
    asyncio.run(main())
    assert th.slept == [3]


def test_fetch_venue_honours_delay_per_host(tmp_db):
    th = FakeThrottle()
    srv = Server(ok())

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(srv)) as c:
            return await F.fetch_all([venue("A", "https://h.example/a", gap=5),
                                      venue("B", "https://h.example/b", gap=5),
                                      venue("C", "https://other.example/c", gap=5)],
                                     client=c, throttle=th, verbose=False)
    res = asyncio.run(main())
    assert set(res) == {"A", "B", "C"} and all(r.status is FetchStatus.OK for r in res.values())
    assert th.slept == [5]


def test_fetch_all_never_raises(tmp_db, monkeypatch):
    async def boom(cfg, url, **kw):
        raise RuntimeError("kaput")
    monkeypatch.setitem(F.FETCHERS, "http", boom)

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(Server(ok()))) as c:
            return await F.fetch_all([venue("A"), venue("B", "https://w.example", mode="api")],
                                     client=c, throttle=FakeThrottle(), verbose=False)
    res = asyncio.run(main())
    assert res["A"].status is FetchStatus.ERROR and "kaput" in res["A"].note
    assert res["B"].status is FetchStatus.OK
    assert list(res) == ["A", "B"]


def test_status_report(tmp_db):
    go(Server(ok()), venue("A", "https://a.example"))
    go(Server(ok(WALL)), venue("B", "https://b.example"))
    rep = fetch_status_report(["A", "B", "Never"])
    assert [(r["name"], r["status"]) for r in rep] == [("A", "ok"), ("B", "blocked"), ("Never", None)]
    assert rep[0]["html_len"] == len(GOOD) and rep[1]["html_len"] == 0


def test_fallback_body_carries_cached_fetched_at(tmp_db):
    go(Server(ok()), venue())
    age_row(tmp_db, 30)
    fr = go(Server(httpx.Response(500, text="x")), venue(), ttl_hours=24)
    assert fr.status is FetchStatus.ERROR and fr.body == GOOD
    assert 29 < (datetime.now(timezone.utc) - fr.fetched_at).total_seconds() / 3600 < 31


def test_fallback_body_expired_after_4x_ttl(tmp_db):
    go(Server(ok()), venue())
    age_row(tmp_db, 24 * 5)
    fr = go(Server(ok(WALL)), venue(), ttl_hours=24)
    assert fr.status is FetchStatus.BLOCKED and fr.body == "" and "expired" in fr.note
    assert store.get_scrape("https://v.example/on")["html"] == GOOD     # still kept


def test_naive_timestamp_is_utc():
    naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    assert abs(F._age_hours(naive)) < 0.01


def test_placeholder_not_in_staleness(tmp_db, monkeypatch):
    from src import dedup
    monkeypatch.setattr(dedup, "_conn", tmp_db)
    go(Server(ok(WALL)), venue("W", "https://w.example"))
    go(Server(ok()), venue("A", "https://a.example"))
    assert dedup.get_html_fetched_at("https://w.example") is None
    assert not dedup.oldest_fetched_at(["https://w.example", "https://a.example"]).startswith("1970")
    rep = fetch_status_report(["W"])
    assert rep[0]["status"] == "blocked" and rep[0]["fetched_at"] is None


def test_today_threads_year(tmp_db):
    from datetime import date
    srv = Server(ok())

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(srv)) as c:
            await F.fetch_all([venue(url="https://v.example/{YEAR}/on")], client=c,
                              today=date(2031, 1, 5), throttle=FakeThrottle(), verbose=False)
    asyncio.run(main())
    assert str(srv.reqs[0].url) == "https://v.example/2031/on"


def test_duplicate_names_first_wins(tmp_db):
    srv = Server(ok())

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(srv)) as c:
            return await F.fetch_all([venue("A", "https://a.example/1"), venue("A", "https://a.example/2")],
                                     client=c, throttle=FakeThrottle(), verbose=False)
    res = asyncio.run(main())
    assert list(res) == ["A"] and len(srv.reqs) == 1


def test_sequential_asyncio_runs(tmp_db, monkeypatch):
    """Two asyncio.run(fetch_all) in one process: no loop-bound state survives; the
    browser module is exercised through its real loop-reset path with a fake browser."""
    import src.fetch.browser as fb
    closed = []

    class FakeBrowser:
        def is_connected(self):
            return True

        async def new_context(self):
            raise RuntimeError("no real browser in this test")

        async def close(self):
            closed.append(1)

    class FakePW:
        def __init__(self):
            self.chromium = self

        async def launch(self, **kw):
            return FakeBrowser()

        async def stop(self):
            pass

    async def fake_get(stealth):
        fb._ensure_state()
        async with fb._lock:
            if stealth not in fb._browsers:
                fb._browsers[stealth] = await FakePW().launch()
            return fb._browsers[stealth]
    monkeypatch.setattr(fb, "_get_browser", fake_get)
    vs = [venue("A", "https://a.example"), venue("B", "https://a.example/b"),
          venue("C", "https://c.example", mode="browser")]

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(Server(ok()))) as c:
            return await F.fetch_all(vs, client=c, force=True, verbose=False,
                                     throttle=F.HostThrottle(sleep=lambda d: asyncio.sleep(0)))
    for _ in range(2):
        res = asyncio.run(main())
        assert res["A"].status is FetchStatus.OK and res["B"].status is FetchStatus.OK
        assert res["C"].status is FetchStatus.ERROR and "no real browser" in res["C"].note
    assert len(closed) == 2 and fb._browsers == {}


def test_migration_rerun_on_missing_columns(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "old.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(store._SCHEMA)             # legacy scrapes: no status columns
    monkeypatch.setattr(store, "_conn", conn)
    store.save_scrape("https://x.example", "X", GOOD)
    assert store.get_scrape("https://x.example")["status"] == "ok"
