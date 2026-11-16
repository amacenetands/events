"""`follow_links` source: listing hides dates, so each event-detail page becomes one RawEvent."""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from urllib.parse import urldefrag, urljoin
from zoneinfo import ZoneInfo

from ..config import LEGACY_MAX_LINKS, VenueConfig
from ..fetch.detail import cache_get, cache_put, fetch_detail
from ..fetch.types import FetchResult
from ..models import RawEvent
from ..normalize import find_price, parse_date_specs
from .base import SourceCtx, base_url, register_source

__all__ = ["FollowLinksSource", "event_links", "raw_from_detail"]

DEFAULT_PATTERN = r"/event/"
MAX_CONCURRENT = 8
MAX_TIME_TAGS = 8           # the event's own dates come before related-event widgets
PRICE_LINES = 400           # page-text lines after the title searched for a price
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


def _first_upcoming(soup, page_url: str, link_rx: re.Pattern, ctx: SourceCtx,
                    skip: tuple = ()) -> tuple[date, str | None] | None:
    """Earliest (date, time) on/after today among the page's own first <time datetime> tags
    (minus `skip`: the byline range's own tags)."""
    tz = ZoneInfo(ctx.city.tz)
    own = [t for t in soup.find_all("time") if t.get("datetime") and not _foreign_link(t, page_url, link_rx)
           and not any(t is x for x in skip)]
    found = [p for t in own[:MAX_TIME_TAGS] if (p := _parse_dt(t["datetime"], tz, ctx.today)) and p[0] >= ctx.today]
    return min(found, key=lambda p: (p[0], p[1] or "")) if found else None


_RANGE_DASH = re.compile(r"^\s*(?:[–—−-]|to|until)\s*$", re.I)


def _byline_range(soup, page_url: str, link_rx: re.Pattern, ctx: SourceCtx) -> tuple[date, date, tuple] | None:
    """(start, end, (tag, tag)) when the page's first own date element is a range of two
    <time datetime> tags joined by a dash ('Fri 16 Oct — Wed 18 Nov 2026', a film run) ending
    on/after today."""
    from bs4 import NavigableString
    tz = ZoneInfo(ctx.city.tz)
    own = [t for t in soup.find_all("time") if t.get("datetime") and not _foreign_link(t, page_url, link_rx)]
    if not own:
        return None
    first = own[0]
    nxt = first.find_next_sibling("time")
    if nxt is None or not nxt.get("datetime"):
        return None
    between = []
    for x in first.next_siblings:
        if x is nxt:
            break
        between.append(str(x) if isinstance(x, NavigableString) else x.get_text(" "))
    if not _RANGE_DASH.match("".join(between)):
        return None
    a, b = _parse_dt(first["datetime"], tz, ctx.today), _parse_dt(nxt["datetime"], tz, ctx.today)
    if a is None or b is None or b[0] <= a[0] or b[0] < ctx.today:
        return None
    return a[0], b[0], (first, nxt)


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
    if not title:
        return None
    link_rx = re.compile(link_pattern)
    run = _byline_range(soup, url, link_rx, ctx)
    when = _first_upcoming(soup, url, link_rx, ctx, skip=run[2] if run else ())
    if when and when[1]:
        run = None              # a timed upcoming session beats the byline range
    elif run:
        when = None
    if not run and not when:
        return None
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    if run:                     # a run: no timed session listed besides the byline
        date_text, time_text = f"{run[0].isoformat()} – {run[1].isoformat()}", None
    else:
        date_text, time_text = when[0].isoformat(), when[1]
    return RawEvent(title=title, date_text=date_text, time_text=time_text, url=url,
                    price_text=_price(soup, title), source_kind="follow_links")


def _cache_get(url: str, force: bool) -> str | None:
    return cache_get(url, force)


def _cache_put(url: str, html: str, site_name: str) -> None:
    cache_put(url, html, site_name)


def _bump(ctx: SourceCtx, key: str) -> None:
    ctx.stats[key] = ctx.stats.get(key, 0) + 1


async def _detail_html(url: str, cfg: VenueConfig, ctx: SourceCtx,
                       sem: asyncio.Semaphore) -> tuple[str, str] | None:
    """(html, final url) via fetch.detail; failures only in `follow_links.*` stats."""
    # module-level lookups at call time so tests can stub _cache_get/_cache_put
    return await fetch_detail(url, cfg, ctx, sem, prefix="follow_links",
                              get=lambda u, f: _cache_get(u, f), put=lambda u, h, n: _cache_put(u, h, n))


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
