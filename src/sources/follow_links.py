"""`follow_links` source: listing hides dates, so each event-detail page becomes one RawEvent."""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from urllib.parse import urldefrag, urljoin
from zoneinfo import ZoneInfo

import httpx

from ..config import LEGACY_MAX_LINKS, VenueConfig
from ..fetch.headers import HEADERS
from ..fetch.types import FetchResult, is_blocked_page
from ..models import RawEvent
from ..normalize import find_price, parse_date_specs
from .base import SourceCtx, base_url, register_source

__all__ = ["FollowLinksSource", "event_links", "raw_from_detail"]

DEFAULT_PATTERN = r"/event/"
MAX_CONCURRENT = 8
MAX_TIME_TAGS = 8           # the event's own dates come before related-event widgets
PRICE_LINES = 400           # page-text lines after the title searched for a price
TIMEOUT = 20
_HREF = re.compile(r'href=["\']([^"\']+)["\']')
_TITLE_SPLIT = re.compile(r"\s+[|–—]\s+")
_HAS_TIME = re.compile(r"\d{1,2}:\d{2}")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")


def event_links(html: str, base_url: str, pattern: str) -> list[str]:
    """De-duplicated absolute detail URLs matching `pattern`, in page order."""
    rx = re.compile(pattern)
    seen: dict[str, None] = {}
    for href in _HREF.findall(html or ""):
        if rx.search(href):
            seen.setdefault(urldefrag(urljoin(base_url, href))[0], None)
    return list(seen)


def _title(soup) -> str:
    """og:title (minus ' | Venue') else first <h1>."""
    og = soup.find("meta", attrs={"property": "og:title"})
    t = og.get("content", "") if og else ""
    if not t and soup.find("h1"):
        t = soup.find("h1").get_text(" ", strip=True)
    return _TITLE_SPLIT.split(t.strip())[0].strip()


def _parse_dt(s: str, tz: ZoneInfo, today: date) -> tuple[date, str | None] | None:
    """(local date, 'HH:MM' or None) of a <time datetime> value. ISO is read directly
    (midnight = no time); anything else goes through normalize, never a free-form parser."""
    s = (s or "").strip()
    if _ISO.match(s):
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(tz)
        has_time = bool(_HAS_TIME.search(s)) and (dt.hour, dt.minute) != (0, 0)
        return dt.date(), (dt.strftime("%H:%M") if has_time else None)
    specs = parse_date_specs(s, today, tz=tz.key)
    if not specs or specs[0].start is None:
        return None
    return specs[0].start, (specs[0].times[0] if specs[0].times else None)


def _same_page(a: str, b: str) -> bool:
    return urldefrag(a)[0].rstrip("/") == urldefrag(b)[0].rstrip("/")


def _foreign_link(tag, page_url: str, link_rx: re.Pattern) -> bool:
    """<time> inside an <a> to another event page (matches the venue's link pattern) = related card."""
    a = tag.find_parent("a", href=True)
    if a is None:
        return False
    href = urljoin(page_url, a["href"])
    return bool(link_rx.search(a["href"]) or link_rx.search(href)) and not _same_page(href, page_url)


def _first_upcoming(soup, page_url: str, link_rx: re.Pattern, ctx: SourceCtx) -> tuple[date, str | None] | None:
    """Earliest (date, time) on/after today among the page's own first <time datetime> tags."""
    tz = ZoneInfo(ctx.city.tz)
    own = [t for t in soup.find_all("time") if t.get("datetime") and not _foreign_link(t, page_url, link_rx)]
    found = [p for t in own[:MAX_TIME_TAGS] if (p := _parse_dt(t["datetime"], tz, ctx.today)) and p[0] >= ctx.today]
    return min(found, key=lambda p: (p[0], p[1] or "")) if found else None


def _price(soup, title: str) -> str | None:
    """find_price over the page text from the title on."""
    lines = soup.get_text("\n", strip=True).splitlines()
    start = next((i for i, ln in enumerate(lines) if title and title.lower() in ln.lower()), 0)
    return find_price("\n".join(lines[start:start + PRICE_LINES]))


def raw_from_detail(html: str, url: str, ctx: SourceCtx, link_pattern: str = DEFAULT_PATTERN) -> RawEvent | None:
    """One RawEvent from a detail page (og:title + first upcoming <time datetime>); `url` is post-redirect."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    title = _title(soup)
    when = _first_upcoming(soup, url, re.compile(link_pattern), ctx) if title else None
    if not when:
        return None
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return RawEvent(title=title, date_text=when[0].isoformat(), time_text=when[1], url=url,
                    price_text=_price(soup, title), source_kind="follow_links")


def _cache_get(url: str, force: bool) -> str | None:
    from ..dedup import get_cached_html          # lazy: opens the sqlite cache
    return get_cached_html(url, force=force)


def _cache_put(url: str, html: str, site_name: str) -> None:
    from ..dedup import save_html_cache
    save_html_cache(url, html, site_name=site_name)


def _bump(ctx: SourceCtx, key: str) -> None:
    ctx.stats[key] = ctx.stats.get(key, 0) + 1


async def _detail_html(url: str, cfg: VenueConfig, ctx: SourceCtx,
                       sem: asyncio.Semaphore) -> tuple[str, str] | None:
    """(html, final url) from cache, else fetched (and cached when it is a real page).
    Failures are counted in stats only (`follow_links.http_404`, `.fetch_error`, `.blocked`)."""
    cached = _cache_get(url, ctx.force and not ctx.offline)
    if cached:
        return cached, url
    if ctx.offline:
        _bump(ctx, "follow_links.offline_uncached")
        return None
    async with sem:
        try:
            r = await ctx.http.get(url, headers=HEADERS, follow_redirects=True, timeout=TIMEOUT)
        except httpx.HTTPError:
            _bump(ctx, "follow_links.fetch_error")
            return None
    if r.status_code >= 400:
        _bump(ctx, f"follow_links.http_{r.status_code}")
        return None
    if is_blocked_page(r.text):
        _bump(ctx, "follow_links.blocked")
        return None
    _cache_put(url, r.text, cfg.name)
    return r.text, str(r.url)


class FollowLinksSource:
    """Follows the listing's event links (network via ctx.http, detail pages cached)."""
    kind = "follow_links"

    async def records(self, cfg: VenueConfig, fr: FetchResult | None, ctx: SourceCtx) -> list[RawEvent]:
        if fr is None or not fr.body:
            return []
        pattern = (cfg.follow.link_pattern if cfg.follow else None) or DEFAULT_PATTERN
        cap = cfg.follow.max if cfg.follow else LEGACY_MAX_LINKS
        links = event_links(fr.body, base_url(cfg, fr, ctx), pattern)[:cap]
        if not links:
            _bump(ctx, "follow_links.no_links")
            return []
        sem = asyncio.Semaphore(MAX_CONCURRENT)
        missing: list[str] = []

        async def one(url: str) -> RawEvent | None:
            try:
                got = await _detail_html(url, cfg, ctx, sem)
                if got is None:
                    missing.append(url)
                    return None
                return raw_from_detail(got[0], got[1], ctx, pattern)
            except Exception:   # noqa: BLE001 — one bad detail page never kills the venue
                _bump(ctx, "follow_links.failed")
                return None

        out = [r for r in await asyncio.gather(*(one(u) for u in links)) if r is not None]
        ctx.stats["follow_links.pages"] = ctx.stats.get("follow_links.pages", 0) + len(links)
        ctx.stats["follow_links.records"] = ctx.stats.get("follow_links.records", 0) + len(out)
        if missing and not ctx.offline:
            ctx.errors.append(f"{cfg.name}: follow-links {len(missing)}/{len(links)} detail pages unavailable "
                              f"(see follow_links.* stats)")
        return out


SOURCE = register_source(FollowLinksSource())
