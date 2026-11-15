"""Spektrix source: fingerprint, add-on filtering, bulk + per-event instances, prices, caps,
robustness, Source contract. No network and no cache/memory.db: recorded fixtures
(scripts/record_spektrix_fixtures.py) via httpx.MockTransport, page snippets in pages/."""
import asyncio
import json
import re
from datetime import date, datetime, time
from pathlib import Path

import httpx
import pytest

from src.config import VenueConfig, get_city
from src.fetch.types import FetchResult, FetchStatus
from src.sources import spektrix as S
from src.sources.base import SOURCES, Source, SourceCtx
from src.sources.platforms import detect_platform

FIX = Path(__file__).resolve().parent / "fixtures" / "spektrix"
PAGES = FIX / "pages"
MANIFEST = json.loads((FIX / "manifest.json").read_text())
START = date.fromisoformat(MANIFEST["start"])
END = date.fromisoformat(MANIFEST["end"])
NOW = datetime.combine(START, time.min)

_PATH = re.compile(r"^/(?P<client>[^/]+)/api/v3/(?:events(?:/(?P<eid>[^/]+)/instances)?"
                   r"|instances/(?P<iid>[^/]+)/status|(?P<bulk>instances))$")


def _load(name):
    return json.loads((FIX / name).read_text())


def _json(body):
    return httpx.Response(200, json=body)


def _404():
    return httpx.Response(404, text="<html>404</html>", headers={"content-type": "text/html"})


def fixture_transport(calls=None, host="system.spektrix.com", bulk=True, override=None):
    """Serves fixtures by path; `override(req, match)` may return a Response first."""
    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(req)
        m = _PATH.match(req.url.path)
        if req.url.host != host or not m:
            return _404()
        if override and (r := override(req, m)) is not None:
            return r
        c = m["client"]
        if m["bulk"]:
            f = FIX / f"{c}_bulk.json" if bulk else None
        elif m["eid"]:
            f = FIX / f"{c}_instances_{m['eid']}.json"
        elif m["iid"]:
            f = FIX / f"{c}_status_{m['iid']}.json"
        else:
            f = FIX / f"{c}_events.json"
        if f is None or not f.exists():
            return _404()
        return httpx.Response(200, content=f.read_bytes(), headers={"content-type": "application/json"})
    return httpx.MockTransport(handler)


def run(client, transport=None, **kw):
    kw.setdefault("delay", 0)
    kw.setdefault("now", NOW)
    async def go():
        async with httpx.AsyncClient(transport=transport or fixture_transport()) as http:
            return await S.fetch_events(client, http=http, start=START, end=END, **kw)
    return asyncio.run(go())


def _page(name):
    meta = json.loads((PAGES / "index.json").read_text())[name]
    return (PAGES / meta["file"]).read_text(), meta["url"]


def _in_window(insts):
    return [i for i in insts if START.isoformat() <= i["start"][:10] <= END.isoformat()]


# ── fingerprint ──────────────────────────────────────────────────────────────

CACHED = {
    "Kings Place": "kingsplace",
    "The Other Palace": "theotherpalace",
    "Bush Theatre": "bushtheatre",
    "Royal Court Theatre": "royalcourt",
    "Park Theatre": "parktheatre",
    "Lyric Hammersmith": "lyrichammersmith",
    "Hackney Empire": "hackneyempire",
    "Camden People's Theatre": "camdenpeoplestheatre",   # only a signup <form action>
    "Criterion Theatre": "criteriontheatre",
    "Chickenshed Theatre": None,   # cookie banner + /legal/website-… link, no client
}


@pytest.mark.parametrize("name,client", CACHED.items())
def test_fingerprint_cached_pages(name, client):
    html, url = _page(name)
    assert S.fingerprint(html, url) == client


@pytest.mark.parametrize("html,client,domain", [
    ('<spektrix-login-status client-name="parktheatre" custom-domain="tickets.parktheatre.co.uk">',
     "parktheatre", "tickets.parktheatre.co.uk"),
    ("<div data-spektrix-client='gate'></div>", "gate", None),
    ('<form action="https://system.spektrix.com/camdenpeoplestheatre/website/secure/signup.aspx">',
     "camdenpeoplestheatre", None),
    ('<script src="https://webcomponents.spektrix.com/stable/webcomponents-loader.js"></script>'
     '<script src="https://tickets.bushtheatre.co.uk/bushtheatre/website/scripts/integrate.js">',
     "bushtheatre", "tickets.bushtheatre.co.uk"),
    ("spektrix_api_url = 'https://purchase.londoncoliseum.org/londoncoliseumlimited/api/v3';",
     "londoncoliseumlimited", "purchase.londoncoliseum.org"),
    ('{"spektrix":{"clientName":"stratfordeast","clientDomain":"tickets.stratfordeast.com"}}',
     "stratfordeast", "tickets.stratfordeast.com"),
    ('<script src="https://webcomponents.spektrix.com/stable/webcomponents-loader.js">', None, None),
    ("<p>no ticketing here</p>", None, None),
])
def test_fingerprint_patterns(html, client, domain):
    sc = S.detect(html)
    assert (sc.client if sc else None) == client
    if sc:
        assert sc.domain == domain


def test_fingerprint_ignores_link_to_another_venues_box_office():
    html = ('<p>Also on at our friends\' theatre: <a href="https://system.spektrix.com/othervenue/'
            'website/EventDetails.aspx?EventId=123">book</a></p>'
            '<script src="https://webcomponents.spektrix.com/stable/webcomponents-loader.js"></script>')
    assert S.fingerprint(html, "https://www.myvenue.co.uk/whats-on") is None


def test_fingerprint_widget_beats_foreign_links():
    html = ('<spektrix-basket-summary client-name="myvenue"></spektrix-basket-summary>'
            + '<img src="https://system.spektrix.com/othervenue/files/a.jpg">' * 3)
    assert S.fingerprint(html, "https://myvenue.org") == "myvenue"


def test_fingerprint_domain_comes_from_same_tag():
    html = ('<spektrix-login-status client-name="aa" custom-domain="tickets.aa.org"></spektrix-login-status>'
            '<spektrix-donate custom-domain="tickets.b.org"></spektrix-donate>')
    assert S.detect(html) == S.SpektrixClient("aa", "tickets.aa.org")


# ── filtering ────────────────────────────────────────────────────────────────

def test_addon_filtering_youngvic():
    events = _load("youngvic_events.json")
    kept, dropped = S.select_events(events, START, END, 400)
    names = {e["name"] for e in kept}
    assert not any(re.search(r"programme|playtext|headset|listening|touch tour", n, re.I) for n in names)
    assert {"GIRLS", "EUROTRASH"} <= names
    assert dropped == len(events) - len(kept) and dropped >= 5


def test_dont_include_on_whats_on_attribute():
    events = _load("theotherpalace_events.json")
    hidden = [e for e in events if e.get("attribute_DontIncludeOnWhatsOn")]
    assert hidden and all(S.is_addon(e) for e in hidden)
    assert not S.is_addon({"name": "Hamlet", "attribute_DontIncludeOnWhatsOn": False})
    assert S.is_addon({"name": "Hamlet", "attribute_DontIncludeOnWhatsOn": "1"})


def test_addon_sibling_title_rule():
    sibs = ["Hamlet", "Girls", "The Programme", "The Restaurant"]
    assert S.is_addon({"name": "Hamlet Programme"}, sibs)
    assert S.is_addon({"name": "Hamlet - Programme £5"}, sibs)
    assert S.is_addon({"name": "Girls Playtext"}, sibs)
    assert S.is_addon({"name": "Hamlet Cocktail and Programme Deal"}, sibs)
    assert not S.is_addon({"name": "Hamlet"}, sibs)
    assert not S.is_addon({"name": "Hamlet: The Musical"}, sibs)


@pytest.mark.parametrize("name", ["The Programme", "The Restaurant", "Parking Lot", "Membership",
                                  "Livestream", "Late Night Programme",
                                  "Three Bean Salad: Ratmas 2026 Streamed event"])
def test_addon_name_false_positives(name):
    sibs = ["The Programme", "The Restaurant", "Three Bean Salad: Ratmas 2026", name]
    assert not S.is_addon({"name": name}, sibs)


def test_addon_generic_names_and_type_attributes():
    assert S.is_addon({"name": "Audio description Headset"})
    assert S.is_addon({"name": "Gift Voucher"})
    assert S.is_addon({"name": "The Other Palace Packages"})
    assert S.is_addon({"name": "Secure My Booking"})
    assert S.is_addon({"name": "Touch Tour", "attribute_WebsiteCategorisation": "Supplementary"})
    assert not S.is_addon({"name": "Touch Tour", "attribute_Festival": "Extras"})


def test_zero_instance_window_and_malformed():
    stats = S.SpektrixStats()
    evs = [{"id": "1", "name": "A", "firstInstanceDateTime": None},
           {"id": "2", "name": "B", "firstInstanceDateTime": "2000-01-01T19:30:00",
            "lastInstanceDateTime": "2000-01-02T19:30:00"},
           {"id": "3", "name": "C", "firstInstanceDateTime": START.isoformat() + "T19:30:00"},
           {"id": "4", "name": 42, "firstInstanceDateTime": START.isoformat() + "T19:30:00"},
           {"id": "5", "name": "<b></b>", "firstInstanceDateTime": START.isoformat() + "T19:30:00"},
           "not a dict"]
    kept, dropped = S.select_events(evs, START, END, 10, stats)
    assert [e["id"] for e in kept] == ["3"] and dropped == 5 and stats.events_failed == 3


def test_sort_uses_first_date_inside_window():
    long_run = {"id": "a", "name": "Long", "firstInstanceDateTime": "2000-01-01T19:30:00",
                "lastInstanceDateTime": END.isoformat() + "T19:30:00"}
    soon = {"id": "b", "name": "Soon", "firstInstanceDateTime": START.isoformat() + "T10:00:00"}
    kept, _ = S.select_events([soon, long_run], START, END, 1)
    assert len(kept) == 1     # both start at START once clamped; cap keeps one, order stable


# ── fetch_events: bulk endpoint ──────────────────────────────────────────────

def test_bulk_one_call_per_client_and_per_instance_expansion():
    calls, stats = [], S.SpektrixStats()
    out = run("youngvic", transport=fixture_transport(calls), stats=stats)
    assert stats.bulk and not stats.events_failed
    assert sum(c.url.path.endswith("/instances") for c in calls) == 1
    assert len(out) == len(_in_window(_load("youngvic_bulk.json")))   # one RawEvent per session
    r = out[0]
    assert r.source_kind == "spektrix" and r.title
    assert re.fullmatch(r"\d{4}-\d\d-\d\d", r.date_text)
    assert re.fullmatch(r"\d\d:\d\d", r.time_text)
    assert r.url == "" and r.sold_out is None              # webUrl empty -> left empty


def test_documented_filters_and_exclusive_end():
    calls = []
    run("theotherpalace", transport=fixture_transport(calls))
    nxt = date.fromordinal(END.toordinal() + 1).isoformat()
    assert dict(calls[0].url.params) == {"instanceStart_from": START.isoformat(), "instanceStart_to": nxt}
    bulk = dict(calls[1].url.params)
    assert bulk == {"startFrom": START.isoformat(), "startTo": nxt, "$expand": "priceList"}
    assert "Mozilla" in calls[0].headers["user-agent"]


def test_drops_sessions_before_now():
    insts = _in_window(_load("youngvic_bulk.json"))
    late = max(datetime.fromisoformat(i["start"]) for i in insts)
    out = run("youngvic", now=late)
    assert len(out) == sum(datetime.fromisoformat(i["start"]) >= late for i in insts)


# ── fetch_events: per-event fallback ─────────────────────────────────────────

def test_per_event_fallback_when_bulk_fails():
    calls, stats = [], S.SpektrixStats()
    out = run("youngvic", transport=fixture_transport(calls, bulk=False), stats=stats)
    assert not stats.bulk and out
    kept, _ = S.select_events(_load("youngvic_events.json"), START, END, 400)
    recorded = MANIFEST["clients"]["youngvic"]
    assert stats.events_failed == len(kept) - len(recorded)   # unrecorded events 404 -> counted
    assert sum("/events/" in c.url.path for c in calls) == len(kept)
    expected = sum(len(_in_window(_load(f"youngvic_instances_{i}.json"))) for i in recorded)
    assert len(out) == expected


def test_per_event_timeout_returns_partial():
    slow = MANIFEST["clients"]["youngvic"][1]

    async def handler(req):
        if slow in req.url.path:
            await asyncio.sleep(5)
        return fixture_transport(bulk=False).handle_request(req)

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            return await handler(req)

    stats = S.SpektrixStats()
    out = run("youngvic", transport=T(), timeout=0.5, stats=stats)
    fast = MANIFEST["clients"]["youngvic"][0]
    assert len(out) == len(_in_window(_load(f"youngvic_instances_{fast}.json")))
    assert any("timeout" in e for e in stats.errors)


# ── prices / sold out ────────────────────────────────────────────────────────

def test_price_from_cheapest_band():
    out = run("theotherpalace")
    bulk = _in_window(_load("theotherpalace_bulk.json"))
    multi = [i for i in bulk if len({p["amount"] for p in i["priceList"]["prices"] if p["amount"] > 0}) > 1]
    assert multi
    lo = min(p["amount"] for p in multi[0]["priceList"]["prices"] if p["amount"] > 0)
    assert any(r.price_text == f"From £{lo:.0f}" for r in out)


def test_price_text_rules():
    P = lambda *ps: {"priceList": {"prices": list(ps)}}
    assert S.price_text(P({"amount": 0})) == "Free"
    assert S.price_text(P({"amount": 0}, {"amount": 12.5})) == "£12.50"
    assert S.price_text({"priceList": {"id": "x"}}) is None
    comp = {"amount": 0.5, "ticketType": {"name": "Essential Companion"}}
    assert S.price_text(P(comp, {"amount": 20}, {"amount": 30})) == "From £20"
    assert S.price_text(P({"amount": "12"})) is None


def test_sold_out_field():
    ev = {"name": "Show", "description": "<p>A play. More text.</p>"}
    r = S.instance_to_raw(ev, {"start": "2030-01-01T19:30:00", "isSoldOut": True})
    assert r.sold_out is True and r.desc == "A play."
    inst = {"start": "2030-01-01T19:30:00"}
    assert S.instance_to_raw(ev, inst, {"capacity": 100, "available": 0}).sold_out is True
    assert S.instance_to_raw(ev, inst, {"capacity": "100", "available": "3"}).sold_out is False
    assert S.instance_to_raw(ev, inst, {"capacity": 0, "available": 0}).sold_out is None
    assert S.instance_to_raw(ev, inst, {"capacity": "lots", "available": None}).sold_out is None
    assert S.instance_to_raw(ev, {**inst, "cancelled": True}) is None


def test_check_availability_capped():
    calls, stats = [], S.SpektrixStats()
    out = run("youngvic", transport=fixture_transport(calls), check_availability=True,
              status_cap=2, stats=stats)
    assert out and sum(c.url.path.endswith("/status") for c in calls) == 2


# ── caps / failures ──────────────────────────────────────────────────────────

def test_max_events_cap():
    calls = []
    out = run("kingsplace", transport=fixture_transport(calls), with_instances=False, max_events=5)
    assert len(out) == 5 and len(calls) == 1
    assert all(r.date_text and r.source_kind == "spektrix" for r in out)
    calls.clear()
    run("kingsplace", transport=fixture_transport(calls, bulk=False), max_events=3)
    assert sum("/events/" in c.url.path for c in calls) == 3


def test_without_instances_uses_run_dates():
    out = run("youngvic", with_instances=False)
    assert out and all(r.time_text is None for r in out)
    assert any(r.end_text and r.end_text > r.date_text for r in out)


def test_dead_client_403_returns_empty():
    t = httpx.MockTransport(lambda r: httpx.Response(403, text="<html>Just a moment</html>",
                                                     headers={"content-type": "text/html"}))
    stats = S.SpektrixStats()
    assert run("kingsplace", transport=t, stats=stats) == [] and "403" in stats.errors[0]


def test_dict_payloads_are_rejected():
    stats = S.SpektrixStats()
    t = fixture_transport(override=lambda req, m: _json({"error": "x"}) if not m["bulk"] and not m["eid"] else None)
    assert run("youngvic", transport=t, stats=stats) == [] and "dict" in stats.errors[0]
    stats = S.SpektrixStats()
    t = fixture_transport(override=lambda req, m: _json({"oops": 1}) if (m["bulk"] or m["eid"]) else None)
    assert run("youngvic", transport=t, stats=stats) == []
    assert not stats.bulk and stats.events_failed > 0


def test_malformed_instances_are_skipped():
    eid = MANIFEST["clients"]["youngvic"][0]
    good = _in_window(_load("youngvic_bulk.json"))
    bad = [{"event": {"id": eid}, "start": 12345}, {"event": {"id": eid}, "start": "garbage"},
           {"event": {"id": eid}, "start": START.isoformat() + "T19:00:00", "priceList": "nope"},
           "not a dict", {"event": "x"}]
    t = fixture_transport(override=lambda req, m: _json(good + bad) if m["bulk"] else None)
    out = run("youngvic", transport=t)
    assert len(out) == len(good) + 1           # priceList "nope" -> still a session, no price
    assert any(r.price_text is None for r in out)


def test_custom_domain_fallback():
    sc = S.SpektrixClient("youngvic", "tickets.youngvic.org")
    out = run(sc, transport=fixture_transport(host="tickets.youngvic.org"), with_instances=False)
    assert out


# ── Source contract ──────────────────────────────────────────────────────────

def _venue(**kw):
    return VenueConfig(name="Young Vic", url="https://www.youngvic.org/whats-on", **kw)


def _records(cfg, fr, transport=None):
    async def go():
        async with httpx.AsyncClient(transport=transport or fixture_transport()) as http:
            ctx = SourceCtx(http=http, today=START, window_days=(END - START).days,
                            city=get_city("london"))
            out = await S.SpektrixSource(delay=0, now=NOW).records(cfg, fr, ctx)
            return out, ctx
    return asyncio.run(go())


def _fr(body):
    return FetchResult(status=FetchStatus.OK, body=body, url="https://www.youngvic.org/whats-on")


def test_source_registered_and_protocol():
    assert isinstance(SOURCES["spektrix"], Source) and SOURCES["spektrix"].kind == "spektrix"


def test_source_adapter_config():
    out, ctx = _records(_venue(adapter={"kind": "spektrix", "client": "youngvic"}), None)
    assert out and all(r.url == "" for r in out)
    assert ctx.stats["spektrix.instances"] == len(out) and ctx.stats["spektrix.events_seen"] > 0


def test_source_fingerprint_and_no_client():
    html = '<spektrix-basket-summary client-name="theotherpalace"></spektrix-basket-summary>'
    assert _records(_venue(), _fr(html))[0]
    assert _records(_venue(), _fr("<html>plain</html>"))[0] == []
    assert _records(_venue(), None)[0] == []


def test_source_errors_go_to_ctx():
    t = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    out, ctx = _records(_venue(adapter={"kind": "spektrix", "client": "youngvic"}), None, t)
    assert out == [] and ctx.stats["spektrix.dead_clients"] == 1
    assert ctx.errors and ctx.errors[0].startswith("Young Vic: spektrix")


# ── platforms ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,want", [
    ("Kings Place", ("spektrix", "kingsplace")),
    ("OSO Arts Centre", ("ticketsolve", "osoarts")),
    ("Watermans Arts Centre", ("ticketsolve", "watermans")),
    ("Conway Hall", ("ticketsolve", "conwayhall")),
    ("Golden Goose Theatre", None),      # one link to another venue's Ticketsolve
    ("London Transport Museum", None),   # Eventbrite search page, many organisers
])
def test_detect_platform_cached(name, want):
    html, url = _page(name)
    assert detect_platform(html, url) == want


def test_ticketsolve_foreign_link_not_detected():
    html = '<a href="https://othervenue.ticketsolve.com/ticketbooth/shows/1">tickets</a>'
    assert detect_platform(html, "https://www.myvenue.org.uk/") is None
    html2 = html * 2 + '<a href="https://myvenue.ticketsolve.com/shows">x</a>'
    assert detect_platform(html2, "https://www.myvenue.org.uk/") == ("ticketsolve", "myvenue")


def test_detectors_are_scored_not_ordered():
    html = ('<a href="https://system.spektrix.com/other/website/EventDetails.aspx?x">a</a>'
            '<a href="https://myvenue.ticketsolve.com/shows">b</a>')
    assert detect_platform(html, "https://myvenue.co.uk") == ("ticketsolve", "myvenue")


def test_eventbrite_org_from_url_and_dominant_links():
    assert detect_platform("", "https://www.eventbrite.co.uk/o/london-transport-museum-1234567890") \
        == ("eventbrite_org", "1234567890")
    html = ('<a href="https://www.eventbrite.co.uk/o/foo-11111111">x</a>' * 3
            + '<a href="https://www.eventbrite.co.uk/o/bar-22222222">y</a>')
    assert detect_platform(html) == ("eventbrite_org", "11111111")


# ── timed-entry collapse / add-ons (2a-ii review) ────────────────────────────

def _slot(day: str, t: str, price: str | None = "From £18", so=None):
    from src.models import RawEvent as _R
    return _R(title="Ghost Tour", date_text=day, time_text=t, price_text=price, sold_out=so, source_kind="spektrix")


def test_collapse_timed_entry_slots():
    from datetime import date as _d, timedelta as _td
    days = [(_d(2026, 11, 16) + _td(days=i)).isoformat() for i in range(0, 21) if i % 7 in (0, 2)]   # Mon + Wed
    slots = [_slot(d, t, "From £10" if d == days[1] and t == "10:00" else "From £18") for d in days
             for t in ("10:00", "12:00", "14:00")]
    (one,) = S.collapse_slots(slots)
    assert (one.date_text, one.end_text, one.recurrence_text, one.time_text, one.price_text) == (
        days[0], days[-1], "Mondays, Wednesdays", "", "From £10")
    assert S.collapse_slots([_slot("2026-11-16", "19:30"), _slot("2026-11-17", "19:30")]) is None   # real shows
    daily = [_slot((_d(2026, 11, 16) + _td(days=i)).isoformat(), "11:00", so=True) for i in range(15)]
    (d,) = S.collapse_slots(daily)
    assert d.recurrence_text is None and d.sold_out is True


def test_parking_addons():
    assert S.is_addon({"name": "Blue Badge Parking"}) and S.is_addon({"name": "Car Park"})
    assert S.is_addon({"name": "Parking"}) and not S.is_addon({"name": "Parking Lot"})
