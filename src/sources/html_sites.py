"""
HTML site scraper with batched LLM extraction.
- js_required: false → httpx + markdownify (no browser)
- js_required: true  → crawl4ai + playwright
- Batches BATCH_SIZE sites per LLM call to reduce request count.
- JSON-LD-complete sites skip markdownify entirely.

Stages:
  fetch_sites(...)   — fetch only, persist to scrapes cache, no LLM
  parse_sites(...)   — read from scrapes cache, extract events
  fetch_html_sites(...) — thin back-compat wrapper: fetch then parse
"""
import asyncio
import os
import re
from pathlib import Path
import httpx
import yaml
from markdownify import markdownify

from ..extract import extract_events_batch_async
from .. import timing
from ..dedup import get_cached_html, save_html_cache, get_cached_html_any
from ..models import Event

SITES_YAML = Path(__file__).parent.parent.parent / "sites.yaml"
MAX_CONCURRENT_HTTPX = 8
MAX_CONCURRENT_BROWSER = 5
BROWSER_DELAY_DEFAULT = 3.0   # seconds to let JS render; per-site `delay:` / `wait_for:` in sites.yaml

# Hard per-fetch timeouts — a hung fetch is cancelled, logged, returns None
FETCH_TIMEOUT_BROWSER = 120
FETCH_TIMEOUT_SIMPLE = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


def _load_sites(pipeline: str | None = None, include_disabled: bool = False) -> list[dict]:
    """Legacy-shaped site dicts for one pipeline tag, via config.load_venues.
    pipeline=None → London default (Moscow-tagged venues excluded); a Moscow tag
    (moscow / moscow-theatre) → that city's venues carrying it; any other tag →
    London venues carrying it. {YEAR} in urls is resolved to the current year."""
    from datetime import date
    from .. import config
    year = date.today().year
    city = config.city_for_tag(pipeline)
    venues = config.load_venues(city, include_disabled=include_disabled,
                                tags=[pipeline] if pipeline else None)
    out = []
    for v in venues:
        d = v.to_legacy_dict()
        d["url"] = v.resolved_url(year)
        out.append(d)
    return out


def configured_site_names(pipeline: str | None = None, include_disabled: bool = False) -> list[str]:
    """Names of all non-skipped sites for a pipeline. include_disabled keeps
    fetch-disabled (bot-blocked) venues so their cached rows still surface."""
    return [s["name"] for s in _load_sites(pipeline=pipeline, include_disabled=include_disabled)]


# Word-boundaried so a boilerplate word can't match as a substring of a real
# content class — e.g. bare "promo" used to strip St Paul's event cards whose
# class is "node--promoted". (Compound-token false matches like "sidebar" inside
# "page-template-right-sidebar" are handled separately by the >40%-of-page guard
# in _clean_html.)
_BOILERPLATE_PATTERNS = re.compile(
    r"\b(?:cookie|consent|gdpr|banner|popup|modal|newsletter|subscribe|overlay|"
    r"sticky|announcement|alert|notification|skip-link|breadcrumb|pagination|"
    r"social-share|share-button|follow-us|sidebar|widget|advertisement|promo)\b",
    re.IGNORECASE,
)

# Event types recognised in JSON-LD — shared by _parse_jsonld_nodes, _extract_jsonld, _events_from_jsonld
_JSONLD_EVENT_TYPES = (
    "Event", "MusicEvent", "VisualArtsEvent", "ExhibitionEvent",
    "TheaterEvent", "EducationEvent", "ChildrensEvent", "ScreeningEvent", "ComedyEvent",
    "DanceEvent", "Festival", "SocialEvent", "Event",
)

_JSONLD_CATEGORY = {
    "MusicEvent": "concert", "TheaterEvent": "theatre", "DanceEvent": "theatre",
    "ExhibitionEvent": "exhibition", "VisualArtsEvent": "exhibition",
    "EducationEvent": "talk", "ScreeningEvent": "film", "ComedyEvent": "comedy",
    "Festival": "festival", "ChildrensEvent": "other",
}


def _jsonld_types(node: dict) -> list[str]:
    t = node.get("@type", "")
    return [str(x) for x in (t if isinstance(t, list) else [t])]


def _jsonld_class(node: dict) -> dict:
    """schema.org @type / typicalAgeRange / eventSchedule -> category/audience/min_age/kind hints."""
    import re as _re
    types = _jsonld_types(node)
    cat = next((_JSONLD_CATEGORY[t] for t in types if t in _JSONLD_CATEGORY), "other")
    name = str(node.get("name", ""))
    if cat == "talk" and _re.search(r"workshop|class", name, _re.I):
        cat = "workshop"
    aud, min_age = "all", None
    if "ChildrensEvent" in types:
        aud = "kids"
    rng = str(node.get("typicalAgeRange") or "")
    m = _re.match(r"\s*(\d{1,2})", rng)
    if m:
        min_age = int(m.group(1))
        if min_age < 18:
            aud = "kids" if min_age <= 11 else "family"
    out = {"category": cat, "audience": aud, "min_age": min_age}
    if node.get("eventSchedule"):
        sch = node["eventSchedule"]
        sch = sch[0] if isinstance(sch, list) and sch else sch
        rec = None
        if isinstance(sch, dict):
            fr = sch.get("repeatFrequency") or ""
            dow = sch.get("byDay")
            dow = dow if isinstance(dow, str) else (", ".join(str(x).rsplit("/", 1)[-1] for x in dow) if isinstance(dow, list) else "")
            rec = (str(fr) + " " + str(dow).rsplit("/", 1)[-1]).strip() or "scheduled"
        out["kind"] = "recurring"
        out["recurrence"] = rec or "scheduled"
    return out


def _parse_jsonld_nodes(html: str) -> list[dict]:
    """Regex-extract ld+json blocks from html and return matching event nodes.

    Parses each <script type="application/ld+json"> block exactly once, walks
    @graph arrays, and returns nodes whose @type matches any known event type.
    Returns [] when no matching nodes found.
    """
    import json
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    nodes: list[dict] = []
    for block in blocks:
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        graph = data.get("@graph", [data]) if isinstance(data, dict) else [data]
        for node in graph:
            if not isinstance(node, dict):
                continue
            t = node.get("@type", "")
            if t in _JSONLD_EVENT_TYPES or "Event" in str(t):
                nodes.append(node)
    return nodes


def _clean_html(html: str, extra_selectors: list[str] | None = None) -> str:
    try:
        from bs4 import BeautifulSoup, Comment
    except ImportError:
        return html

    soup = BeautifulSoup(html, "html.parser")

    if extra_selectors:
        for sel in extra_selectors:
            try:
                for tag in soup.select(sel):
                    tag.decompose()
            except Exception:
                # malformed selector (e.g. Tailwind "md:hidden" reads as a
                # pseudo-class) — skip it rather than abort the whole page
                continue

    for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"]):
        tag.decompose()

    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    body_len = len(soup.get_text(strip=True)) or 1
    to_remove = []
    for tag in soup.find_all(True):
        try:
            attrs = " ".join([
                " ".join(tag.get("class") or []),
                tag.get("id") or "",
                tag.get("role") or "",
                tag.get("aria-label") or "",
            ])
        except Exception:
            continue
        if _BOILERPLATE_PATTERNS.search(attrs):
            # Guard against false positives: a layout wrapper whose class merely
            # contains a boilerplate word (e.g. Concrete5's
            # "page-template-right-sidebar") can hold most of the page. Only
            # strip genuinely peripheral nodes, never the main content body.
            if len(tag.get_text(strip=True)) > 0.4 * body_len:
                continue
            to_remove.append(tag)
    for tag in to_remove:
        try:
            tag.decompose()
        except Exception:
            pass

    main = soup.find("main") or soup.find("article") or soup.find(id=re.compile(r"main|content", re.I))
    # Only trust the main/content container if it actually holds most of the
    # page. Some sites put the real listing outside a tiny <main>/<article>
    # teaser (e.g. Two Temple Place: a ~100-char <article> beside 34k of content)
    # — selecting it would throw the page away. Fall back to the full document.
    if main:
        body_text = len(soup.get_text(strip=True)) or 1
        if len(main.get_text(strip=True)) >= 0.5 * body_text:
            return str(main)
    return str(soup)


def _extract_jsonld(html: str) -> str:
    """Build [STRUCTURED EVENT] text lines from JSON-LD blocks.

    Delegates node extraction to _parse_jsonld_nodes so the ld+json blocks are
    parsed only once.
    """
    lines = []
    for node in _parse_jsonld_nodes(html):
        name = node.get("name", "")
        start = node.get("startDate", "")
        end = node.get("endDate", "")
        desc = node.get("description", "")
        loc = ""
        if isinstance(node.get("location"), dict):
            loc = node["location"].get("name", "")
        url = node.get("url", "")
        price = ""
        if isinstance(node.get("offers"), dict):
            price = node["offers"].get("price", "")
        parts = [f"[STRUCTURED EVENT] {name}"]
        if start:
            parts.append(f"startDate: {start}")
        if end:
            parts.append(f"endDate: {end}")
        if loc:
            parts.append(f"location: {loc}")
        if desc:
            parts.append(f"description: {desc[:200]}")
        if url:
            parts.append(f"url: {url}")
        if price:
            parts.append(f"price: {price}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _events_from_jsonld(html: str, source_url: str, source_name: str, date_window: int) -> list | None:
    from datetime import date as _date, timedelta
    from dateutil.parser import parse as _parse_date

    today = _date.today()
    cutoff = today + timedelta(days=date_window)

    nodes = _parse_jsonld_nodes(html)
    if not nodes:
        return None

    raw_events = []
    for node in nodes:
        name = node.get("name", "").strip()
        start_str = node.get("startDate", "")
        if not name or not start_str:
            continue
        try:
            start_dt = _parse_dt(start_str).date()
        except Exception:
            continue

        end_str = node.get("endDate", "")
        end_dt = None
        try:
            end_dt = _parse_dt(end_str).date() if end_str else None
        except Exception:
            pass

        sch = node.get("eventSchedule")
        sch = sch[0] if isinstance(sch, list) and sch else sch
        if sch and isinstance(sch, dict):
            # Recurring series: startDate may be long past; keep while the schedule is live.
            sch_end = None
            if sch.get("endDate"):
                try:
                    sch_end = _parse_dt(str(sch["endDate"])).date()
                except Exception:
                    pass
            end_dt = sch_end or end_dt
            if end_dt and end_dt < today:
                continue
            if start_dt < today:
                start_dt = today
            if start_dt > cutoff:
                continue
            if end_dt and end_dt <= start_dt:
                end_dt = None
            is_one_off = False
        else:
            is_one_off = end_dt is None or end_dt == start_dt
        if sch and isinstance(sch, dict):
            pass
        elif is_one_off:
            if start_dt < today or start_dt > cutoff:
                continue
        else:
            if end_dt and end_dt < today:
                continue
            if start_dt > cutoff:
                continue

        loc = ""
        if isinstance(node.get("location"), dict):
            loc = node["location"].get("name", "") or ""
        url = node.get("url", source_url) or source_url
        price = ""
        offers = node.get("offers")
        if isinstance(offers, dict):
            p = offers.get("price", "")
            currency = offers.get("priceCurrency", "")
            price = f"{currency}{p}" if p else ""
        desc = (node.get("description") or "")[:150] or None
        time_str = None
        try:
            from datetime import datetime as _dt
            parsed = _parse_dt(start_str)
            if parsed.hour or parsed.minute:
                time_str = parsed.strftime("%H:%M")
        except Exception:
            pass

        cls = _jsonld_class(node)
        raw_events.append({
            "title": name,
            "venue": loc or source_name,
            "date": start_dt.isoformat(),
            "end_date": end_dt.isoformat() if end_dt and not is_one_off else None,
            "time": time_str,
            "category": cls["category"],
            "kind": cls.get("kind") or ("one_off" if is_one_off else "run"),
            "recurrence": cls.get("recurrence"),
            "audience": cls["audience"],
            "min_age": cls["min_age"],
            "date_confirmed": True,
            "url": url,
            "description": desc,
            "price": price or None,
            "source": source_name,
        })

    if not raw_events:
        return None
    return raw_events


_HTML_TAG_RE = re.compile(r"<\s*(?:!doctype|html|head|body|div|span|a|p|ul|ol|li|script|article|section|main|h[1-6]|meta|link|img|table|nav|header|footer)\b", re.I)


def _is_markdown_only(text: str) -> bool:
    """True when a cached 'html' row is really browser markdown (no HTML tags)."""
    return not _HTML_TAG_RE.search(text or "")


def _parse_dt(s: str):
    """Parse a JSON-LD date: ISO unambiguous (no dayfirst); dayfirst only for non-ISO."""
    from dateutil.parser import parse as _p, isoparse
    s = (s or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        try:
            return isoparse(s)
        except Exception:
            return _p(s, yearfirst=True, dayfirst=False)
    return _p(s, dayfirst=True)


def _html_to_site_data(site: dict, html: str, pre_text: str | None = None) -> dict:
    """Deterministically reduce raw HTML (or pre-built markdown) for LLM extraction.

    Returns the site dict with `text` (all records), `chunks` (<=6000 chars each,
    split on record boundaries; each is sent as its own LLM unit), `thin`, and
    `events` (not None => adapter produced fully structured events, skip LLM).
    """
    from .reduce import reduce_site, chunk_lines
    jsonld = _extract_jsonld(html) if html else ""
    red = reduce_site(html, site, pre_text=pre_text)
    lines, chunks = red.lines, red.chunks
    thin = red.thin
    if jsonld and red.events is None:
        lines = jsonld.splitlines() + lines
        chunks = chunk_lines(lines)[:8]
        thin = False
    return {
        "name": site["name"],
        "url": site["url"],
        "text": "\n".join(lines),
        "chunks": chunks,
        "thin": thin,
        "events": red.events,
        "category_hint": site.get("category_hint", "other"),
        "notes": site.get("notes", ""),
        "must_determine_venue": site.get("must_determine_venue", False),
    }


# ── fetch helpers — return raw html paired with site dict ────────────────────

async def _fetch_markdown_simple(
    client: httpx.AsyncClient,
    site: dict,
    force_download: bool = False,
) -> dict | None:
    """Fetch html for a non-JS site. Returns {"site": site, "html": raw_html}."""
    cached_html = get_cached_html(site["url"], force=force_download)
    if cached_html:
        print(f"  [HTML] {site['name']}: download cache hit")
        return {"site": site, "html": cached_html}
    try:
        r = await client.get(site["url"], headers=HEADERS, follow_redirects=True, timeout=20)
        r.raise_for_status()
        html = r.text
        save_html_cache(site["url"], html, site_name=site["name"])
        return {"site": site, "html": html}
    except Exception as exc:
        print(f"  [HTML] {site['name']}: fetch failed — {exc!r}")
        return None


_shared_crawler = None
_shared_lock: asyncio.Lock | None = None


async def _get_shared_crawler():
    """One AsyncWebCrawler (single browser) shared by all non-stealth browser sites."""
    global _shared_crawler, _shared_lock
    from crawl4ai import AsyncWebCrawler, BrowserConfig
    if _shared_lock is None:
        _shared_lock = asyncio.Lock()
    async with _shared_lock:
        if _shared_crawler is None:
            c = AsyncWebCrawler(config=BrowserConfig(headless=True), verbose=False)
            await c.start()
            _shared_crawler = c
    return _shared_crawler


async def close_shared_crawler() -> None:
    global _shared_crawler, _shared_lock
    c, _shared_crawler, _shared_lock = _shared_crawler, None, None
    if c is not None:
        try:
            await c.close()
        except Exception:
            pass


async def _fetch_markdown_browser(
    site: dict,
    force_download: bool = False,
) -> dict | None:
    """Fetch html for a JS-required site. Returns {"site": site, "html": raw_html}.

    When crawl4ai yields no raw HTML (result.html empty), falls back to the
    pre-built markdown string from result.markdown carried as a sentinel with
    html="". Callers detect this via html="" and skip _html_to_site_data,
    using the pre_text field instead.
    """
    cached_html = get_cached_html(site["url"], force=force_download)
    if cached_html:
        print(f"  [HTML] {site['name']}: download cache hit")
        return {"site": site, "html": cached_html}
    try:
        _cache = os.environ.get("CRAWL4_AI_BASE_DIRECTORY", "/tmp")
        Path(_cache).mkdir(parents=True, exist_ok=True)
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
        from crawl4ai.async_configs import CacheMode
        stealth = site.get("stealth", False)
        wait_for = site.get("wait_for")
        delay = float(site.get("delay", BROWSER_DELAY_DEFAULT))
        cfg = CrawlerRunConfig(
            # Auto-scroll the full page + a second delayed scroll so lazy-loaded
            # / AJAX listings (Drupal Views, Isotope, React) render.
            js_code=[
                "window.scrollTo(0, document.body.scrollHeight);",
                "await new Promise(r => setTimeout(r, 1500)); window.scrollTo(0, document.body.scrollHeight);",
            ],
            delay_before_return_html=delay,
            wait_for=f"css:{wait_for}" if wait_for else None,
            page_timeout=60000,
            scan_full_page=True,
            cache_mode=CacheMode.BYPASS,
            magic=stealth,
            simulate_user=stealth,
        )
        if stealth:
            from crawl4ai import BrowserConfig
            browser_cfg = BrowserConfig(
                headless=True, enable_stealth=True,
                extra_args=["--disable-blink-features=AutomationControlled"],
            )
            async with AsyncWebCrawler(config=browser_cfg, verbose=False) as crawler:
                result = await crawler.arun(url=site["url"], config=cfg)
        else:
            crawler = await _get_shared_crawler()
            result = await crawler.arun(url=site["url"], config=cfg)
        if not result.success:
            err = (getattr(result, "error_message", "") or "").strip().replace("\n", " ")[:300]
            code = getattr(result, "status_code", None)
            print(f"  [HTML/browser] {site['name']}: crawl failed (status={code}) — {err or 'no error_message'}")
            # Bot-challenge / block pages: respect them — never cache or parse the interstitial.
            if (not (result.html or "").strip() or code in (401, 403, 429)
                    or re.search(r"anti-bot|blocked|challenge", err, re.I)):
                return None
            print(f"  [HTML/browser] {site['name']}: using partial html ({len(result.html)} chars)")
        raw_html = result.html or ""
        if raw_html:
            save_html_cache(site["url"], raw_html, site_name=site["name"])
            return {"site": site, "html": raw_html}
        else:
            # No raw HTML available — carry pre-built markdown as fallback.
            # Also persist pre_text so parse stage can read it from cache.
            content = str(result.markdown) or ""
            save_html_cache(site["url"], content[:2_000_000], site_name=site["name"])
            return {"site": site, "html": "", "pre_text": content[:2_000_000]}
    except Exception as exc:
        print(f"  [HTML/browser] {site['name']}: failed — {exc!r}")
        return None


# ── follow-event-links mode — for listing pages that hide per-event dates ─────
# Some venues (e.g. Barbican) render their what's-on listing as a JS app whose
# cards carry no per-event date; the date only appears on each event's detail
# page. For these, `follow_event_links: true` in sites.yaml makes us collect the
# detail-page URLs from the listing and read each page's date deterministically
# from its <time datetime="…"> tags — no per-page LLM call.

def _parse_event_links(html: str, base_url: str, pattern: str) -> list[str]:
    """Return de-duplicated absolute event-detail URLs matching `pattern`."""
    from urllib.parse import urljoin, urldefrag
    rx = re.compile(pattern)
    seen: dict[str, None] = {}
    for href in re.findall(r'href=["\']([^"\']+)["\']', html):
        if rx.search(href):
            absurl = urldefrag(urljoin(base_url, href))[0]
            seen.setdefault(absurl, None)
    return list(seen)


def _event_from_detail(
    html: str, url: str, source_name: str, category_hint: str, today, cutoff
) -> Event | None:
    """Build one Event from an event detail page using og:title + <time> tags."""
    from dateutil.parser import parse as _parse_date
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # title — prefer og:title, strip trailing " | Venue" suffix
    title = ""
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        title = og["content"]
    elif soup.find("h1"):
        title = soup.find("h1").get_text(" ", strip=True)
    title = re.split(r"\s+[|–—]\s+", title.strip())[0].strip()
    if not title:
        return None

    # dates — scan the first few <time datetime> tags (the event's own
    # performance dates appear before any related-event widgets). Barbican lists
    # discrete performances/screenings rather than continuous runs, so each
    # detail page becomes a one-off on its earliest upcoming date — correct for
    # concerts, films and talks (the events users care about).
    seen_dates: list = []
    for t in soup.find_all("time")[:8]:
        dt = t.get("datetime")
        if not dt:
            continue
        try:
            d = _parse_date(dt).date()
        except Exception:
            continue
        if d not in seen_dates:
            seen_dates.append(d)
    upcoming = sorted(d for d in seen_dates if today <= d <= cutoff)
    if not upcoming:
        return None
    start = upcoming[0]

    cat = category_hint or "other"
    try:
        return Event(
            title=title,
            venue=source_name,
            date=start.isoformat(),
            end_date=None,
            time=None,
            category="other" if cat == "kids" else cat,
            audience="kids" if cat == "kids" else "all",
            kind="one_off",
            url=url,
            source=source_name,
        )
    except Exception:
        return None


async def _expand_event_links(
    site: dict,
    listing_html: str,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    today,
    cutoff,
    force_download: bool = False,
) -> list[Event]:
    """Follow a listing's event-detail links and build dated Events from each."""
    pattern = site.get("link_pattern", r"/event/")
    links = _parse_event_links(listing_html, site["url"], pattern)
    cap = int(site.get("max_links", 120))
    links = links[:cap]
    if not links:
        print(f"  [HTML] {site['name']}: follow-links found 0 detail URLs")
        return []

    async def _one(link: str) -> Event | None:
        async with sem:
            cached = get_cached_html(link, force=force_download)
            if cached:
                html = cached
            else:
                try:
                    r = await client.get(link, headers=HEADERS, follow_redirects=True, timeout=20)
                    r.raise_for_status()
                    html = r.text
                    save_html_cache(link, html, site_name=site["name"])
                except Exception:
                    return None
        return _event_from_detail(
            html, link, site["name"], site.get("category_hint", "other"), today, cutoff
        )

    results = await asyncio.gather(*[_one(l) for l in links])
    events = [e for e in results if e is not None]
    print(f"  [HTML] {site['name']}: {len(events)} events [follow-links, {len(links)} pages]")
    return events


# ── Stage 1: fetch only ───────────────────────────────────────────────────────

async def fetch_sites(
    pipeline: str | None = None,
    test_site: str | None = None,
    force_download: bool = False,
) -> dict:
    """Fetch all sites and persist html to scrapes cache. No parsing, no LLM.

    Returns {"total": N, "fetched": ok, "failed": [names]}.
    """
    _t0 = timing.now()
    sites = _load_sites(pipeline=pipeline)
    if test_site:
        sites = [s for s in sites if s["name"].lower() == test_site.lower()]
        if not sites:
            print(f"  [fetch] Site '{test_site}' not found in sites.yaml")
            return {"total": 0, "fetched": 0, "failed": []}

    simple_sites = [s for s in sites if not s.get("js_required", False)]
    browser_sites = [s for s in sites if s.get("js_required", False)]

    sem_http = asyncio.Semaphore(MAX_CONCURRENT_HTTPX)
    sem_br = asyncio.Semaphore(MAX_CONCURRENT_BROWSER)

    async def _timed_simple(client, site, sem):
        async with sem:
            try:
                return await asyncio.wait_for(
                    _fetch_markdown_simple(client, site, force_download=force_download),
                    timeout=FETCH_TIMEOUT_SIMPLE,
                )
            except asyncio.TimeoutError:
                print(f"  [fetch] {site['name']}: timed out after {FETCH_TIMEOUT_SIMPLE}s — skipped")
                return None

    async def _timed_browser(site, sem):
        async with sem:
            try:
                return await asyncio.wait_for(
                    _fetch_markdown_browser(site, force_download=force_download),
                    timeout=FETCH_TIMEOUT_BROWSER,
                )
            except asyncio.TimeoutError:
                print(f"  [fetch] {site['name']}: browser timed out after {FETCH_TIMEOUT_BROWSER}s — skipped")
                return None

    try:
        async with httpx.AsyncClient() as client:
            http_tasks = [_timed_simple(client, s, sem_http) for s in simple_sites]
            br_tasks = [_timed_browser(s, sem_br) for s in browser_sites]
            results = await asyncio.gather(*http_tasks, *br_tasks, return_exceptions=True)
    finally:
        await close_shared_crawler()

    failed = []
    ok = 0
    for site, r in zip(simple_sites + browser_sites, results):
        if isinstance(r, Exception):
            print(f"  [fetch] {site['name']}: error — {r}")
            failed.append(site["name"])
        elif r is None:
            failed.append(site["name"])
        else:
            ok += 1

    N = len(sites)
    dt = timing.now() - _t0
    summary = {"total": N, "fetched": ok, "failed": failed}
    print(f"  [fetch] done — {ok}/{N} fetched, {len(failed)} failed")
    print(f"  [fetch] {ok}/{N} sites in {dt:.0f}s", flush=True)
    timing.log_runtime(stage="fetch", pipeline=pipeline, total=N, fetched=ok, failed=len(failed), secs=round(dt, 1))
    return summary


# ── Stage 2: parse only ───────────────────────────────────────────────────────

async def parse_sites(
    pipeline: str | None = None,
    test_site: str | None = None,
    force_parse: bool = False,
    compare: bool = False,
    errors: list[str] | None = None,
) -> list[Event]:
    """Read html from scrapes cache and extract events. Never re-fetches."""
    _t0 = timing.now()
    sites = _load_sites(pipeline=pipeline)
    if test_site:
        sites = [s for s in sites if s["name"].lower() == test_site.lower()]
        if not sites:
            print(f"  [parse] Site '{test_site}' not found in sites.yaml")
            return []

    _DATE_WINDOW_JSONLD = 365

    from ..extract import _build_events as _be
    jsonld_events: list[Event] = []
    llm_sites: list[dict] = []
    adapter_events: list[Event] = []
    follow_events: list[Event] = []

    # Collect sites that need follow-links expansion
    follow_list: list[tuple[dict, str]] = []

    for site in sites:
        cached = get_cached_html_any(site["url"])
        if cached is None:
            print(f"  [parse] {site['name']}: no cached html — run --fetch-only first")
            continue

        # Browser-markdown fallback rows are stored in the html column; route as pre_text.
        md_only = bool(cached) and _is_markdown_only(cached)
        html = "" if md_only else cached

        if site.get("follow_event_links") and html:
            follow_list.append((site, html))
            continue

        try:
            if html:
                raw_list = _events_from_jsonld(html, site["url"], site["name"], _DATE_WINDOW_JSONLD)
                if raw_list:
                    evts = _be(raw_list, site["url"], site["name"])
                    jsonld_events.extend(evts)
                    print(f"  [HTML] {site['name']}: {len(evts)} events [jsonld]")
                    continue
            # html col holds pre_text for browser-markdown-only sites (html == "" case
            # is handled by passing the cached text as pre_text)
            site_data = _html_to_site_data(site, html if html else "", pre_text=cached if md_only else None)
            if site_data["events"] is not None:
                adapter_events.extend(site_data["events"])
                continue
            if site_data["thin"]:
                if errors is not None:
                    errors.append(f"{site['name']} (thin page — no events found in HTML)")
                continue

            llm_sites.append(site_data)
        except Exception as exc:
            print(f"  [HTML] {site['name']}: parse failed — {exc}")
            if errors is not None:
                errors.append(site["name"])
            continue

    # Follow-event-links expansion
    if follow_list:
        from datetime import date as _date, timedelta as _td
        _today = _date.today()
        _cut = _today + _td(days=_DATE_WINDOW_JSONLD)
        sem_d = asyncio.Semaphore(MAX_CONCURRENT_HTTPX)
        async with httpx.AsyncClient() as dclient:
            tasks = [
                _expand_event_links(site, html, dclient, sem_d, _today, _cut)
                for site, html in follow_list
            ]
            for evs in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(evs, list):
                    follow_events.extend(evs)

    llm_events = await extract_events_batch_async(
        llm_sites, errors=errors, force_parse=force_parse, compare=compare,
    )
    events = jsonld_events + adapter_events + llm_events + follow_events
    N = len(sites)
    dt = timing.now() - _t0
    print(f"  [parse] {N} venues → {len(events)} events in {dt:.0f}s", flush=True)
    timing.log_runtime(stage="parse", pipeline=pipeline, venues=N, events=len(events), secs=round(dt, 1))
    return events


# ── Back-compat wrapper ───────────────────────────────────────────────────────

async def fetch_html_sites(
    test_site: str | None = None,
    pipeline: str | None = None,
    errors: list[str] | None = None,
    force_download: bool = False,
    force_parse: bool = False,
    compare: bool = False,
) -> list[Event]:
    """Back-compat: fetch then parse. Existing callers unchanged."""
    sites = _load_sites(pipeline=pipeline)
    if test_site:
        sites = [s for s in sites if s["name"].lower() == test_site.lower()]
        if not sites:
            print(f"  [HTML] Site '{test_site}' not found in sites.yaml")
            return []
        site = sites[0]
        if site.get("js_required"):
            sem = asyncio.Semaphore(1)
            async with sem:
                try:
                    fetched = await asyncio.wait_for(
                        _fetch_markdown_browser(site, force_download=force_download),
                        timeout=FETCH_TIMEOUT_BROWSER,
                    )
                except asyncio.TimeoutError:
                    print(f"  [HTML] {site['name']}: browser timed out after {FETCH_TIMEOUT_BROWSER}s")
                    fetched = None
                finally:
                    await close_shared_crawler()
        else:
            async with httpx.AsyncClient() as client:
                sem = asyncio.Semaphore(1)
                async with sem:
                    try:
                        fetched = await asyncio.wait_for(
                            _fetch_markdown_simple(client, site, force_download=force_download),
                            timeout=FETCH_TIMEOUT_SIMPLE,
                        )
                    except asyncio.TimeoutError:
                        print(f"  [HTML] {site['name']}: fetch timed out after {FETCH_TIMEOUT_SIMPLE}s")
                        fetched = None
        if not fetched:
            return []
        # JSON-LD first, fall through to LLM batch path
        html = fetched.get("html", "")
        if site.get("follow_event_links") and html:
            from datetime import date as _date, timedelta as _td
            _today = _date.today()
            async with httpx.AsyncClient() as dclient:
                return await _expand_event_links(
                    site, html, dclient, asyncio.Semaphore(MAX_CONCURRENT_HTTPX),
                    _today, _today + _td(days=365), force_download=force_download,
                )
        if html:
            jsonld_events_raw = _events_from_jsonld(html, site["url"], site["name"], 365)
            if jsonld_events_raw:
                from ..extract import _build_events as _be
                return _be(jsonld_events_raw, site["url"], site["name"])
        site_data = _html_to_site_data(site, html or "", pre_text=None if html else fetched.get("pre_text", ""))
        if site_data["events"] is not None:
            return site_data["events"]
        if site_data["thin"]:
            print(f"  [HTML] {site['name']}: thin page — no events found")
            return []
        return await extract_events_batch_async([site_data], force_parse=force_parse, compare=compare)

    # Full pipeline: fetch then parse
    await fetch_sites(pipeline=pipeline, force_download=force_download)
    return await parse_sites(
        pipeline=pipeline,
        force_parse=force_parse,
        compare=compare,
        errors=errors,
    )
