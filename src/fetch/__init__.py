"""Fetch layer (pipeline v3): fetchers registry + scrape cache + per-host politeness.

Entry points (never raise; every venue gets a FetchResult):

    frs = await fetch_all(venues, client=client, force=False, today=today)  # {cfg.name: FetchResult}
    fr  = await fetch_venue(cfg, client=client, force=False, ttl_hours=None, today=today)

Consumer wiring contract:
- Call `fetch_all(venues, client=shared_httpx_client, today=...)` once per crawl and pass
  each FetchResult straight to the sources. Never re-read bodies via the cache helpers
  (dedup.get_cached_html*): that loses final_url / status / validators.
- Parse `fr.body` whenever it is non-empty — including BLOCKED / ERROR / EMPTY results
  carrying the cached fallback body. NOT_MODIFIED always has the body present.
  The extraction content_hash cache avoids re-parsing an unchanged body.
- A venue served from a cached fallback body is stale: `fr.note` says
  "serving cached body from …" and `fr.fetched_at` is the cached body's fetch time.
- `fr.url` (final URL after redirects) is the base for resolving links.

Behaviour:
- `cfg.fetch.mode` picks the fetcher in FETCHERS (http | browser | browser_stealth | api).
  `api` returns OK with body "" and no network (status 'api' recorded for the report).
- Cache = `scrapes` table. A stored good body younger than `ttl_hours` (default
  DOWNLOAD_TTL_DAYS) is returned as NOT_MODIFIED (note "cache fresh") unless `force`.
  Otherwise the fetch is conditional (stored ETag / Last-Modified; skipped on force).
- Only OK replaces the stored body; NOT_MODIFIED refreshes fetched_at. BLOCKED / ERROR /
  EMPTY keep the last good body and serve it (status unchanged) unless it is older than
  FALLBACK_MAX_TTLS × TTL, then body "" and the note says expired.
- Status of every fetch is written to scrapes.status/status_at/note (see report.py).
- Per-host politeness: fetch starts on one host are >= `cfg.fetch.gap` (default
  HOST_GAP) apart; throttle waits happen before taking a concurrency slot.
  `cfg.fetch.delay` is only the browser post-load settle time.
- Asyncio state is per call: a throttle per fetch_all, and the shared browser is closed
  at the end of fetch_all when any browser venue ran — safe across asyncio.run calls.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from .types import FetchResult, FetchStatus, is_blocked_page, visible_text

__all__ = [
    "FetchResult", "FetchStatus", "is_blocked_page", "visible_text",
    "FETCHERS", "Fetcher", "fetch_venue", "fetch_all", "HostThrottle", "cached_result", "cached_results",
]

HOST_GAP = 1.0                     # s between fetch starts on one host (default)
VENUE_TIMEOUT = 150.0              # s hard cap on one http fetch (browser caps itself per page slot)
FALLBACK_MAX_TTLS = 4              # cached fallback bodies older than this × TTL are not served

# Fetcher(cfg, url, client=, etag=, last_modified=) -> FetchResult
Fetcher = Callable[..., Awaitable[FetchResult]]


async def _f_http(cfg, url, *, client, etag=None, last_modified=None) -> FetchResult:
    from .http import fetch_http
    return await fetch_http(url, client=client, etag=etag, last_modified=last_modified)


async def _browser(cfg, url, stealth: bool) -> FetchResult:
    from .browser import fetch_browser
    f = cfg.fetch
    return await fetch_browser(url, stealth=stealth, wait_for=f.wait_for,
                               remove_selectors=list(f.remove_selectors), delay=f.delay)


async def _f_browser(cfg, url, **_) -> FetchResult:
    return await _browser(cfg, url, False)


async def _f_browser_stealth(cfg, url, **_) -> FetchResult:
    return await _browser(cfg, url, True)


async def _f_api(cfg, url, **_) -> FetchResult:
    return FetchResult(FetchStatus.OK, body="", url=url, note="api")

_BROWSER_MODES = ("browser", "browser_stealth")


FETCHERS: dict[str, Fetcher] = {
    "http": _f_http,
    "browser": _f_browser,
    "browser_stealth": _f_browser_stealth,
    "api": _f_api,
}


class HostThrottle:
    """Per-host spacing of fetch starts. A slot is reserved under the host lock, the sleep
    happens outside it. Create one per run (holds asyncio locks). `clock`/`sleep` injectable."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep):
        self.clock, self.sleep = clock, sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._next: dict[str, float] = {}

    async def wait(self, url: str, gap: float) -> None:
        host = (urlsplit(url).hostname or "").lower()
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = self.clock()
            at = max(self._next.get(host, now), now)
            self._next[host] = at + max(gap, 0.0)
        if at > now:
            await self.sleep(at - now)


def _ttl_hours_default() -> float:
    from ..dedup import DOWNLOAD_TTL_DAYS
    return DOWNLOAD_TTL_DAYS * 24.0


def _parse_ts(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)   # naive → UTC


def _age_hours(iso: str) -> float:
    return (datetime.now(timezone.utc) - _parse_ts(iso)).total_seconds() / 3600


async def fetch_venue(cfg, *, client: httpx.AsyncClient | None, force: bool = False,
                      ttl_hours: float | None = None, throttle: HostThrottle | None = None,
                      today: date | None = None, _slot: asyncio.Semaphore | None = None) -> FetchResult:
    """Fetch one venue's listing page through the cache (see module docstring).
    `_slot` (fetch_all's semaphore) is taken only around the network fetch."""
    from .. import store
    mode = cfg.fetch.mode
    url = cfg.resolved_url((today or date.today()).year)
    fetcher = FETCHERS[mode]
    if mode == "api":
        fr = await fetcher(cfg, url)
        store.record_scrape_status(url, cfg.name, "api", fr.note)
        return fr

    row = store.get_scrape(url)
    good = row["html"] if row and row["html"] and not is_blocked_page(row["html"]) else ""
    ttl = _ttl_hours_default() if ttl_hours is None else ttl_hours
    if good and not force:
        age = _age_hours(row["fetched_at"])
        if age <= ttl:
            return FetchResult(
                FetchStatus.NOT_MODIFIED, body=good, url=row.get("final_url") or url,
                fetched_at=_parse_ts(row["fetched_at"]),
                etag=row.get("etag"), last_modified=row.get("last_modified"),
                note=f"cache fresh ({age:.1f}h <= {ttl:g}h)",
            )

    gap = cfg.fetch.gap if cfg.fetch.gap is not None else HOST_GAP
    await (throttle or HostThrottle()).wait(url, gap)
    cond = dict(etag=row.get("etag"), last_modified=row.get("last_modified")) if good and not force else {}

    async def do_fetch() -> FetchResult:
        if mode in _BROWSER_MODES:           # fetch_browser times itself inside its page slot
            return await fetcher(cfg, url, client=client, **cond)
        try:
            return await asyncio.wait_for(fetcher(cfg, url, client=client, **cond), VENUE_TIMEOUT)
        except asyncio.TimeoutError:
            return FetchResult(FetchStatus.ERROR, url=url, note=f"timeout {VENUE_TIMEOUT:g}s")

    if _slot is None:
        fr = await do_fetch()
    else:
        async with _slot:
            fr = await do_fetch()

    if fr.status is FetchStatus.OK:
        store.save_scrape(url, cfg.name, fr.body, status=fr.status.value, etag=fr.etag,
                          last_modified=fr.last_modified, final_url=fr.url, note=fr.note)
        return fr
    if fr.status is FetchStatus.NOT_MODIFIED and good:
        store.record_scrape_status(url, cfg.name, fr.status.value, fr.note, revalidated=True,
                                   etag=fr.etag, last_modified=fr.last_modified, final_url=fr.url)
        fr.body = good
        fr.url = fr.url or row.get("final_url") or url
        fr.etag = fr.etag or row.get("etag")
        fr.last_modified = fr.last_modified or row.get("last_modified")
        return fr
    if fr.status is FetchStatus.NOT_MODIFIED:      # 304 with nothing stored: treat as error
        fr.status, fr.note = FetchStatus.ERROR, f"{fr.note} but no cached body"
    store.record_scrape_status(url, cfg.name, fr.status.value, fr.note, final_url=fr.url or None)
    if good:
        age = _age_hours(row["fetched_at"])
        if age > FALLBACK_MAX_TTLS * ttl:
            fr.body = ""
            fr.note = f"{fr.note}; cached body from {row['fetched_at']} expired (> {FALLBACK_MAX_TTLS}x TTL)"
        else:
            fr.body = good
            fr.fetched_at = _parse_ts(row["fetched_at"])
            fr.url = row.get("final_url") or fr.url or url
            fr.note = f"{fr.note}; serving cached body from {row['fetched_at']}"
    elif fr.status is not FetchStatus.EMPTY:
        fr.body = ""
    return fr


async def fetch_all(venues: list, *, client: httpx.AsyncClient | None, force: bool = False,
                    concurrency: int = 8, ttl_hours: float | None = None,
                    throttle: HostThrottle | None = None, today: date | None = None,
                    verbose: bool = True) -> dict[str, FetchResult]:
    """fetch_venue over venues; returns {cfg.name: FetchResult} in input order. Never raises.
    Duplicate names: the first venue wins (warning printed)."""
    sem = asyncio.Semaphore(max(1, concurrency))
    th = throttle or HostThrottle()
    today = today or date.today()
    seen: set[str] = set()
    uniq = []
    for v in venues:
        if v.name in seen:
            print(f"  [fetch] duplicate venue name {v.name!r}: skipping {v.url}")
            continue
        seen.add(v.name)
        uniq.append(v)
    out: dict[str, FetchResult] = {}
    counts: dict[str, int] = {}
    total = len(uniq)
    t0 = time.monotonic()

    async def one(cfg) -> None:
        try:
            fr = await fetch_venue(cfg, client=client, force=force, ttl_hours=ttl_hours,
                                   throttle=th, today=today, _slot=sem)
        except Exception as e:
            fr = FetchResult(FetchStatus.ERROR, url=cfg.url, note=f"{type(e).__name__}: {e}"[:300])
        out[cfg.name] = fr
        counts[fr.status.value] = counts.get(fr.status.value, 0) + 1
        if verbose:
            tally = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(f"  [fetch {len(out)}/{total} {time.monotonic() - t0:.0f}s] {cfg.name}: "
                  f"{fr.status.value} ({fr.note}) | {tally}")

    try:
        await asyncio.gather(*(one(v) for v in uniq))
    finally:
        if any(v.fetch.mode in _BROWSER_MODES for v in uniq):
            from .browser import close_browser
            await close_browser()
    return {v.name: out[v.name] for v in uniq if v.name in out}


def cached_result(cfg, today: date | None = None) -> FetchResult:
    """The stored scrape as a FetchResult, no network (--parse-only): NOT_MODIFIED with the
    last good body, BLOCKED when the stored body is a bot wall, EMPTY when nothing is stored.
    `api` mode venues get OK with an empty body (API sources fetch for themselves)."""
    from .. import store
    url = cfg.resolved_url((today or date.today()).year)
    if cfg.fetch.mode == "api":
        return FetchResult(FetchStatus.OK, body="", url=url, note="api")
    row = store.get_scrape(url)
    if not row or not row.get("html"):
        return FetchResult(FetchStatus.EMPTY, url=url, note="no stored page (run crawl without --parse-only)")
    at = _parse_ts(row["fetched_at"])
    final = row.get("final_url") or url
    if is_blocked_page(row["html"]):
        return FetchResult(FetchStatus.BLOCKED, url=final, fetched_at=at, note="stored page is a bot wall")
    return FetchResult(FetchStatus.NOT_MODIFIED, body=row["html"], url=final, fetched_at=at,
                       etag=row.get("etag"), last_modified=row.get("last_modified"),
                       note=f"stored page from {row['fetched_at']}")


def cached_results(venues: list, today: date | None = None) -> dict[str, FetchResult]:
    """{cfg.name: cached_result(cfg)} for venues (first of duplicate names wins)."""
    out: dict[str, FetchResult] = {}
    for v in venues:
        if v.name not in out:
            out[v.name] = cached_result(v, today)
    return out
