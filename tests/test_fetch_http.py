"""fetch_http + browser post-processing: httpx.MockTransport, fake page; no network."""
import asyncio
import shutil
from pathlib import Path

import httpx
import pytest

from src.fetch import FetchStatus
from src.fetch import browser as fb
from src.fetch import http as fh

FIX = Path(__file__).parent / "fixtures" / "fetch"
GOOD = (FIX / "sadlers_wells.html").read_text(encoding="utf-8")
WALL = (FIX / "southbank_cloudflare.html").read_text(encoding="utf-8")
URL = "https://venue.example/whats-on"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    async def _s(_):
        return None
    monkeypatch.setattr(fh, "_sleep", _s)


def run(handler, **kw):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await fh.fetch_http(URL, client=c, **kw)
    return asyncio.run(go())


def html(body, code=200, **h):
    return httpx.Response(code, text=body, headers={"content-type": "text/html; charset=utf-8", **h})


def test_ok_captures_validators():
    fr = run(lambda r: html(GOOD, etag='"v1"', **{"last-modified": "Mon, 02 Nov 2026 10:00:00 GMT"}))
    assert fr.status is FetchStatus.OK and fr.body == GOOD
    assert fr.etag == '"v1"' and fr.last_modified.startswith("Mon")
    assert fr.content_type.startswith("text/html") and fr.url == URL


def test_conditional_headers_and_304():
    seen = {}

    def h(r):
        seen.update(r.headers)
        return httpx.Response(304)
    fr = run(h, etag='"v1"', last_modified="Mon, 02 Nov 2026 10:00:00 GMT")
    assert seen["if-none-match"] == '"v1"' and "if-modified-since" in seen
    assert "Mozilla" in seen["user-agent"]
    assert fr.status is FetchStatus.NOT_MODIFIED and fr.body == ""


def test_final_url_after_redirect():
    def h(r):
        if r.url.path == "/whats-on":
            return httpx.Response(301, headers={"location": "https://venue.example/events"})
        return html(GOOD)
    assert run(h).url == "https://venue.example/events"


def test_cloudflare_403_blocked():
    fr = run(lambda r: html(WALL, 403, **{"cf-mitigated": "challenge"}))
    assert fr.status is FetchStatus.BLOCKED and fr.body == ""


def test_wall_page_with_200_blocked():
    assert run(lambda r: html(WALL)).status is FetchStatus.BLOCKED


def test_plain_403_is_error():
    fr = run(lambda r: html("<html><body>" + "x " * 2000 + "</body></html>", 403))
    assert fr.status is FetchStatus.ERROR and "403" in fr.note


def test_500_then_200_retries_once():
    calls = []

    def h(r):
        calls.append(1)
        return html("oops", 500) if len(calls) == 1 else html(GOOD)
    fr = run(h)
    assert fr.status is FetchStatus.OK and len(calls) == 2 and "retry" in fr.note


def test_500_twice_error_no_third_call():
    calls = []

    def h(r):
        calls.append(1)
        return html("oops", 502)
    fr = run(h)
    assert fr.status is FetchStatus.ERROR and len(calls) == 2


def test_timeout_retried():
    calls = []

    def h(r):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=r)
        return html(GOOD)
    assert run(h).status is FetchStatus.OK and len(calls) == 2


def test_404_not_retried():
    calls = []

    def h(r):
        calls.append(1)
        return html("nope", 404)
    assert run(h).status is FetchStatus.ERROR and len(calls) == 1


def test_empty_detection():
    fr = run(lambda r: html("<html><head><title>x</title></head><body><div id=app></div></body></html>"))
    assert fr.status is FetchStatus.EMPTY


def test_pdf_unsupported():
    fr = run(lambda r: httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}))
    assert fr.status is FetchStatus.ERROR and "content-type" in fr.note


def test_json_and_ics_skip_wall_heuristics():
    fr = run(lambda r: httpx.Response(200, text='{"msg": "access denied captcha"}',
                                      headers={"content-type": "application/json"}))
    assert fr.status is FetchStatus.OK
    fr = run(lambda r: httpx.Response(200, text="BEGIN:VCALENDAR\nSUMMARY:Enable JavaScript\nEND:VCALENDAR",
                                      headers={"content-type": "text/calendar"}))
    assert fr.status is FetchStatus.OK


def test_json_body_not_empty():
    fr = run(lambda r: httpx.Response(200, json={"events": []}))
    assert fr.status is FetchStatus.OK


# ── browser post-processing (fake page) ──────────────────────────────────────

class FakePage:
    """Minimal async Page: a DOM as list of (selector-ish tag, html)."""

    def __init__(self, nodes, status=200, wait_ok=True):
        self.nodes, self.status, self.wait_ok = nodes, status, wait_ok
        self.url = ""
        self.calls = []

    async def goto(self, url, **kw):
        self.url = url
        self.calls.append("goto")
        return type("R", (), {"status": self.status})()

    async def wait_for_selector(self, sel, **kw):
        self.calls.append(f"wait:{sel}")
        if not self.wait_ok:
            raise TimeoutError(sel)

    async def wait_for_load_state(self, state, **kw):
        self.calls.append(state)

    async def evaluate(self, js, arg=None):
        if arg is None:
            self.calls.append("scroll")
            return None
        n = 0
        for sel in arg:
            if ":" in sel:          # what a browser does with an invalid selector: skipped
                continue
            before = len(self.nodes)
            self.nodes = [x for x in self.nodes if x[0] != sel]
            n += before - len(self.nodes)
        self.calls.append("remove")
        return n

    async def content(self):
        return "<html><body>" + "".join(h for _, h in self.nodes) + "</body></html>"


def render(page, **kw):
    kw.setdefault("wait_for", None)
    kw.setdefault("remove_selectors", [])
    return asyncio.run(fb.render(page, URL, delay=0, timeout=5, **kw))


LISTING = "<ul>" + "".join(f"<li>Event {i} on 12 December at 7.30pm, tickets £{i}</li>" for i in range(40)) + "</ul>"


def test_render_removes_selectors_before_content():
    p = FakePage([(".cookie", "<div class=cookie>We use cookies</div>"), ("ul", LISTING),
                  ("md:hidden", "<i>keep</i>")])
    html_, code, final, note = render(p, remove_selectors=[".cookie", "md:hidden"])
    assert "cookies" not in html_ and "Event 39" in html_ and "keep" in html_
    assert p.calls.index("remove") < len(p.calls) and "removed 1 nodes" in note
    assert code == 200 and final == URL
    assert classify(html_, code, final).status is FetchStatus.OK


def classify(*a):
    return fb.classify_rendered(*a)


def test_render_wait_for_timeout_noted():
    p = FakePage([("ul", LISTING)], wait_ok=False)
    _, _, _, note = render(p, wait_for=".event-card")
    assert "wait:.event-card" in p.calls and "timed out" in note


def test_rendered_wall_blocked():
    assert classify(WALL, 200, URL).status is FetchStatus.BLOCKED
    assert classify(WALL, 403, URL).status is FetchStatus.BLOCKED


def test_rendered_empty_and_error():
    assert classify("<html><body><div id=root></div></body></html>", 200, URL).status is FetchStatus.EMPTY
    assert classify(GOOD, 404, URL).status is FetchStatus.ERROR
    assert classify(GOOD, 200, URL).status is FetchStatus.OK


def test_fetch_browser_without_patchright(monkeypatch):
    async def boom(stealth):
        raise ImportError("no patchright")
    monkeypatch.setattr(fb, "_get_browser", boom)
    fr = asyncio.run(fb.fetch_browser(URL, stealth=False))
    assert fr.status is FetchStatus.ERROR and "patchright" in fr.note


def _chromium_ok():
    try:
        import patchright  # noqa: F401
    except ImportError:
        return False
    return any((Path.home() / "Library/Caches/ms-playwright").glob("chromium*")) or \
        any((Path.home() / ".cache/ms-playwright").glob("chromium*"))


@pytest.mark.skipif(not _chromium_ok(), reason="patchright/chromium not installed")
def test_patchright_smoke_local_page(tmp_path):
    """Real Chromium on a file:// page: render + remove_selectors, no network."""
    f = tmp_path / "p.html"
    f.write_text(f"<html><body><div class='ad'>AD</div>{LISTING}</body></html>")

    async def go():
        try:
            return await fb.fetch_browser(f.as_uri(), stealth=False, remove_selectors=[".ad"],
                                          delay=0, timeout=20)
        finally:
            await fb.close_browser()
    fr = asyncio.run(go())
    assert fr.status is FetchStatus.OK, fr.note
    assert "Event 39" in fr.body and ">AD<" not in fr.body
