"""Patchright (stealth-patched Playwright) fetcher for JS-rendered listings.

One shared headless Chromium per process per mode (lazy; `close_browser()` at
shutdown), a fresh context per fetch. Non-stealth contexts block images/fonts/media.
`stealth=True` only means patchright's own launch defaults on a separate browser with no
request interception — a normal browser render, no captcha solving or wall bypassing.
Both modes are patchright defaults; stealth differs only in doing no request blocking.
Patchright is imported lazily: without it every fetch returns ERROR with a note.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .types import FetchResult, FetchStatus, is_blocked_page, visible_text

EMPTY_MIN_TEXT = 500
DEFAULT_SETTLE = 3.0          # s after load (cfg.fetch.delay overrides); = legacy BROWSER_DELAY_DEFAULT
NETWORKIDLE_CAP = 10.0        # s max wait for networkidle
WAIT_FOR_CAP = 15.0           # s max wait for cfg.fetch.wait_for
MAX_PAGES = 4                 # concurrent pages across the process
_BLOCK_TYPES = {"image", "font", "media"}

# Per-event-loop state: created lazily inside the running loop, dropped when a new loop
# shows up (asyncio primitives and the Playwright driver are bound to their loop).
_pw = None
_browsers: dict[bool, Any] = {}
_lock: asyncio.Lock | None = None
_pages: asyncio.Semaphore | None = None
_loop: asyncio.AbstractEventLoop | None = None


def _ensure_state() -> None:
    global _pw, _lock, _pages, _loop
    loop = asyncio.get_running_loop()
    if _loop is not loop:
        _browsers.clear()            # bound to a dead loop: cannot be closed from here
        _pw, _loop = None, loop
        _lock, _pages = asyncio.Lock(), asyncio.Semaphore(MAX_PAGES)

# Scroll to the bottom twice so lazy-loaded listings (Drupal Views, React) render.
_SCROLL_JS = """async () => {
  for (let i = 0; i < 2; i++) {
    window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
    await new Promise(r => setTimeout(r, 700));
  }
}"""
# Remove selectors in-page; a bad selector (e.g. Tailwind "md:hidden") is skipped.
_REMOVE_JS = """(sels) => {
  let n = 0;
  for (const s of sels) {
    try { document.querySelectorAll(s).forEach(e => { e.remove(); n++; }); } catch (e) {}
  }
  return n;
}"""


async def _get_browser(stealth: bool):
    global _pw
    _ensure_state()
    assert _lock is not None
    async with _lock:
        b = _browsers.get(stealth)
        if b is not None and b.is_connected():
            return b
        from patchright.async_api import async_playwright  # lazy: optional dependency
        if _pw is None:
            _pw = await async_playwright().start()
        b = await _pw.chromium.launch(headless=True)
        _browsers[stealth] = b
        return b


async def close_browser() -> None:
    """Close shared browsers and the Playwright driver (idempotent)."""
    global _pw, _lock, _pages, _loop
    if _loop is not None and _loop is not asyncio.get_running_loop():
        _browsers.clear()
        _pw, _lock, _pages, _loop = None, None, None, None
        return
    bs = list(_browsers.values())
    _browsers.clear()
    for b in bs:
        try:
            await b.close()
        except Exception:
            pass
    if _pw is not None:
        try:
            await _pw.stop()
        except Exception:
            pass
    _pw, _lock, _pages, _loop = None, None, None, None


async def _block_heavy(route) -> None:
    if route.request.resource_type in _BLOCK_TYPES:
        await route.abort()
    else:
        await route.continue_()


async def render(page, url: str, *, wait_for: str | None, remove_selectors: list[str],
                 delay: float | None, timeout: float) -> tuple[str, int | None, str, str]:
    """Drive one page: goto → optional selector → capped networkidle → scroll → settle →
    remove selectors → content. Returns (html, http_status, final_url, note)."""
    notes: list[str] = []
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    code = resp.status if resp is not None else None
    if wait_for:
        try:
            await page.wait_for_selector(wait_for, timeout=min(WAIT_FOR_CAP, timeout) * 1000)
        except Exception:
            notes.append(f"wait_for {wait_for!r} timed out")
    try:
        await page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_CAP * 1000)
    except Exception:
        notes.append("networkidle cap hit")
    try:
        await page.evaluate(_SCROLL_JS)
    except Exception:
        pass
    settle = DEFAULT_SETTLE if delay is None else delay
    if settle > 0:
        await asyncio.sleep(settle)
    if remove_selectors:
        try:
            n = await page.evaluate(_REMOVE_JS, list(remove_selectors))
            notes.append(f"removed {n} nodes")
        except Exception as e:
            notes.append(f"remove_selectors failed: {type(e).__name__}")
    html = await page.content()
    return html, code, page.url, "; ".join(notes)


def classify_rendered(html: str, code: int | None, final_url: str, note: str = "") -> FetchResult:
    """Status for a rendered page: walls first (any status), then HTTP errors, then EMPTY."""
    pre = f"browser HTTP {code}" if code is not None else "browser"
    note = f"{pre}; {note}" if note else pre
    base = dict(url=final_url, content_type="text/html", fetched_at=datetime.now(timezone.utc))
    if is_blocked_page(html):
        return FetchResult(FetchStatus.BLOCKED, note=f"{note}; bot wall", **base)
    if code is not None and code >= 400:
        return FetchResult(FetchStatus.ERROR, note=note, **base)
    if len(visible_text(html)) < EMPTY_MIN_TEXT:
        return FetchResult(FetchStatus.EMPTY, body=html, note=note, **base)
    return FetchResult(FetchStatus.OK, body=html, note=note, **base)


async def fetch_browser(url: str, *, stealth: bool = False, wait_for: str | None = None,
                        remove_selectors: list[str] | None = None, delay: float | None = None,
                        timeout: float = 45) -> FetchResult:
    """Render url in headless Chromium (patchright). Never raises."""
    try:
        browser = await _get_browser(stealth)
    except ImportError:
        return FetchResult(FetchStatus.ERROR, url=url, note="patchright not installed")
    except Exception as e:
        return FetchResult(FetchStatus.ERROR, url=url,
                           note=f"browser launch failed: {type(e).__name__}: {e}"[:300])
    _ensure_state()
    assert _pages is not None
    async with _pages:
        ctx = None
        try:
            ctx = await browser.new_context()
            if not stealth:
                await ctx.route("**/*", _block_heavy)
            page = await ctx.new_page()
            html, code, final, note = await asyncio.wait_for(
                render(page, url, wait_for=wait_for, remove_selectors=remove_selectors or [],
                       delay=delay, timeout=timeout),
                timeout=timeout + NETWORKIDLE_CAP + WAIT_FOR_CAP + 10,
            )
            return classify_rendered(html, code, final, note)
        except Exception as e:
            return FetchResult(FetchStatus.ERROR, url=url,
                               note=f"browser: {type(e).__name__}: {e}"[:300])
        finally:
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:
                    pass
