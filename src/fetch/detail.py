"""Event-detail page fetch shared by `follow_links` (source) and `enrich` (gap-fill pass).

Page cache by URL (sqlite `html_cache` via dedup), offline-aware, one semaphore per caller,
optional per-host throttle, blocked-page detection. Failures only bump `<prefix>.*` stats:
`offline_uncached`, `fetch_error`, `http_NNN`, `blocked`; plus `cache_hit` / `fetched`.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable, Optional

import httpx

from .headers import HEADERS
from .types import is_blocked_page

if TYPE_CHECKING:
    from ..config import VenueConfig
    from . import HostThrottle

__all__ = ["fetch_detail", "cache_get", "cache_put", "TIMEOUT"]

TIMEOUT = 20

CacheGet = Callable[[str, bool], Optional[str]]
CachePut = Callable[[str, str, str], None]


def cache_get(url: str, force: bool) -> str | None:
    from ..dedup import get_cached_html          # lazy: opens the sqlite cache
    return get_cached_html(url, force=force)


def cache_put(url: str, html: str, site_name: str) -> None:
    from ..dedup import save_html_cache
    save_html_cache(url, html, site_name=site_name)


def _bump(ctx, key: str) -> None:
    ctx.stats[key] = ctx.stats.get(key, 0) + 1


async def fetch_detail(url: str, cfg: "VenueConfig", ctx, sem: asyncio.Semaphore, *, prefix: str,
                       get: CacheGet | None = None, put: CachePut | None = None,
                       throttle: "HostThrottle | None" = None, gap: float = 0.0,
                       browser: str | None = None) -> tuple[str, str] | None:
    """(html, final url) from the page cache, else fetched via `ctx.http` — or rendered when
    `browser` is "browser"/"browser_stealth" — and cached when it is a real page; None on
    any failure. `ctx.force` re-downloads unless offline. The caller closes the browser."""
    get, put = get or cache_get, put or cache_put
    cached = get(url, ctx.force and not ctx.offline)
    if cached:
        _bump(ctx, f"{prefix}.cache_hit")
        return cached, url
    if ctx.offline:
        _bump(ctx, f"{prefix}.offline_uncached")
        return None
    if throttle is not None:
        await throttle.wait(url, gap)
    if browser:
        return await _rendered(url, cfg, ctx, sem, prefix, put, browser == "browser_stealth")
    async with sem:
        try:
            r = await ctx.http.get(url, headers=HEADERS, follow_redirects=True, timeout=TIMEOUT)
        except httpx.HTTPError:
            _bump(ctx, f"{prefix}.fetch_error")
            return None
    if r.status_code >= 400:
        _bump(ctx, f"{prefix}.http_{r.status_code}")
        return None
    if is_blocked_page(r.text):
        _bump(ctx, f"{prefix}.blocked")
        return None
    _bump(ctx, f"{prefix}.fetched")
    put(url, r.text, cfg.name)
    return r.text, str(r.url)


async def _rendered(url, cfg, ctx, sem, prefix: str, put: CachePut, stealth: bool) -> tuple[str, str] | None:
    from .browser import fetch_browser
    from .types import FetchStatus
    async with sem:
        f = cfg.fetch
        fr = await fetch_browser(url, stealth=stealth, wait_for=f.wait_for,
                                 remove_selectors=list(f.remove_selectors), delay=f.delay)
    if fr.status is FetchStatus.BLOCKED:
        _bump(ctx, f"{prefix}.blocked")
        return None
    if fr.status is not FetchStatus.OK or not fr.body:
        _bump(ctx, f"{prefix}.fetch_error")
        return None
    _bump(ctx, f"{prefix}.fetched")
    put(url, fr.body, cfg.name)
    return fr.body, fr.url or url
