"""
HTML site scraper with batched Claude extraction.
- js_required: false → httpx + markdownify (no browser)
- js_required: true  → crawl4ai + playwright
- Batches BATCH_SIZE sites per Claude call to reduce subprocess count.
"""
import asyncio
import os
import re
from pathlib import Path
import httpx
import yaml
from markdownify import markdownify

from ..extract import extract_events_batch_async, extract_events_from_text
from ..dedup import get_all_exhibition_ages, get_cached_html, save_html_cache
from ..models import Event

SITES_YAML = Path(__file__).parent.parent.parent / "sites.yaml"
MAX_CONCURRENT_HTTPX = 8
MAX_CONCURRENT_BROWSER = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


def _load_sites(pipeline: str | None = None) -> list[dict]:
    from datetime import date
    year = str(date.today().year)
    with open(SITES_YAML) as f:
        data = yaml.safe_load(f)
    sites = [s for s in data.get("html_sites", []) if not s.get("skip", False)]
    for s in sites:
        s["url"] = s["url"].replace("{YEAR}", year)
    if pipeline:
        def _in_pipeline(s: dict) -> bool:
            p = s.get("pipeline")
            if isinstance(p, list):
                return pipeline in p
            return p == pipeline
        sites = [s for s in sites if _in_pipeline(s)]
    return sites


_BOILERPLATE_PATTERNS = re.compile(
    r"cookie|consent|gdpr|banner|popup|modal|newsletter|subscribe|overlay|"
    r"sticky|announcement|alert|notification|skip-link|breadcrumb|pagination|"
    r"social-share|share-button|follow-us|sidebar|widget|advertisement|promo",
    re.IGNORECASE,
)


def _clean_html(html: str, extra_selectors: list[str] | None = None) -> str:
    try:
        from bs4 import BeautifulSoup, Comment
    except ImportError:
        return html

    soup = BeautifulSoup(html, "html.parser")

    if extra_selectors:
        for sel in extra_selectors:
            for tag in soup.select(sel):
                tag.decompose()

    for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"]):
        tag.decompose()

    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

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
            to_remove.append(tag)
    for tag in to_remove:
        try:
            tag.decompose()
        except Exception:
            pass

    main = soup.find("main") or soup.find("article") or soup.find(id=re.compile(r"main|content", re.I))
    return str(main) if main else str(soup)


def _extract_jsonld(html: str) -> str:
    import json
    blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
    lines = []
    for block in blocks:
        try:
            data = json.loads(block.strip())
            graph = data.get("@graph", [data]) if isinstance(data, dict) else [data]
            for node in graph:
                if not isinstance(node, dict):
                    continue
                t = node.get("@type", "")
                if t in ("Event", "MusicEvent", "VisualArtsEvent", "ExhibitionEvent", "TheaterEvent", "EducationEvent") or "Event" in str(t):
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
        except Exception:
            continue
    return "\n".join(lines)


def _events_from_jsonld(html: str, source_url: str, source_name: str, date_window: int) -> list | None:
    import json as _json
    from datetime import date as _date, timedelta
    from dateutil.parser import parse as _parse_date

    today = _date.today()
    cutoff = today + timedelta(days=date_window)

    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if not blocks:
        return None

    raw_events = []
    for block in blocks:
        try:
            data = _json.loads(block.strip())
        except Exception:
            continue
        graph = data.get("@graph", [data]) if isinstance(data, dict) else [data]
        for node in graph:
            if not isinstance(node, dict):
                continue
            t = node.get("@type", "")
            if not (t in ("Event", "MusicEvent", "VisualArtsEvent", "ExhibitionEvent", "TheaterEvent", "EducationEvent") or "Event" in str(t)):
                continue
            name = node.get("name", "").strip()
            start_str = node.get("startDate", "")
            if not name or not start_str:
                continue
            try:
                start_dt = _parse_date(start_str).date()
            except Exception:
                continue

            end_str = node.get("endDate", "")
            end_dt = None
            try:
                end_dt = _parse_date(end_str).date() if end_str else None
            except Exception:
                pass

            is_one_off = end_dt is None or end_dt == start_dt
            if is_one_off:
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
                parsed = _parse_date(start_str)
                if parsed.hour or parsed.minute:
                    time_str = parsed.strftime("%H:%M")
            except Exception:
                pass

            raw_events.append({
                "title": name,
                "venue": loc or source_name,
                "date": start_dt.isoformat(),
                "end_date": end_dt.isoformat() if end_dt and not is_one_off else None,
                "time": time_str,
                "category": "other",
                "is_one_off": is_one_off,
                "is_kids": False,
                "is_family": False,
                "date_confirmed": True,
                "url": url,
                "description": desc,
                "price": price or None,
                "source": source_name,
            })

    if not raw_events:
        return None
    return raw_events


def _html_to_site_data(site: dict, html: str) -> dict:
    """Convert raw HTML into site data dict for LLM extraction."""
    jsonld = _extract_jsonld(html)
    clean = _clean_html(html, extra_selectors=site.get("remove_selectors"))
    md = markdownify(clean, heading_style="ATX", strip=["script", "style", "nav", "footer"])
    md = "\n".join(line for line in md.splitlines() if line.strip())
    text = (jsonld + "\n\n" + md).strip()[:32000] if jsonld else md[:32000]
    return {
        "name": site["name"],
        "url": site["url"],
        "text": text,
        "category_hint": site.get("category_hint", "other"),
        "notes": site.get("notes", ""),
        "must_determine_venue": site.get("must_determine_venue", False),
        "_raw_html": html,
    }


async def _fetch_markdown_simple(
    client: httpx.AsyncClient,
    site: dict,
    sem: asyncio.Semaphore,
    force_download: bool = False,
) -> dict | None:
    async with sem:
        cached_html = get_cached_html(site["url"], force=force_download)
        if cached_html:
            print(f"  [HTML] {site['name']}: download cache hit")
            return _html_to_site_data(site, cached_html)
        try:
            r = await client.get(site["url"], headers=HEADERS, follow_redirects=True, timeout=20)
            r.raise_for_status()
            html = r.text
            save_html_cache(site["url"], html, site_name=site["name"])
            return _html_to_site_data(site, html)
        except Exception as exc:
            print(f"  [HTML] {site['name']}: fetch failed — {exc}")
            return None


async def _fetch_markdown_browser(
    site: dict,
    sem: asyncio.Semaphore,
    force_download: bool = False,
) -> dict | None:
    async with sem:
        cached_html = get_cached_html(site["url"], force=force_download)
        if cached_html:
            print(f"  [HTML] {site['name']}: download cache hit")
            return _html_to_site_data(site, cached_html)
        try:
            _cache = os.environ.get("CRAWL4_AI_BASE_DIRECTORY", "/tmp")
            Path(_cache).mkdir(parents=True, exist_ok=True)
            from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
            from crawl4ai.async_configs import CacheMode
            stealth = site.get("stealth", False)
            from crawl4ai import BrowserConfig
            browser_cfg = BrowserConfig(
                headless=True,
                enable_stealth=stealth,
                extra_args=["--disable-blink-features=AutomationControlled"] if stealth else [],
            )
            cfg = CrawlerRunConfig(
                js_code="window.scrollTo(0, document.body.scrollHeight);",
                delay_before_return_html=5.0,
                page_timeout=30000,
                cache_mode=CacheMode.BYPASS,
                magic=stealth,
                simulate_user=stealth,
            )
            async with AsyncWebCrawler(config=browser_cfg, verbose=False) as crawler:
                result = await crawler.arun(url=site["url"], config=cfg)
            if not result.success:
                print(f"  [HTML/browser] {site['name']}: crawl failed")
                return None
            raw_html = result.html or ""
            if raw_html:
                save_html_cache(site["url"], raw_html, site_name=site["name"])
                return _html_to_site_data(site, raw_html)
            else:
                content = str(result.markdown) or ""
                return {
                    "name": site["name"],
                    "url": site["url"],
                    "text": content[:32000],
                    "category_hint": site.get("category_hint", "other"),
                    "notes": site.get("notes", ""),
                    "must_determine_venue": site.get("must_determine_venue", False),
                    "_raw_html": "",
                }
        except Exception as exc:
            print(f"  [HTML/browser] {site['name']}: failed — {exc}")
            return None


async def fetch_html_sites(
    test_site: str | None = None,
    pipeline: str | None = None,
    errors: list[str] | None = None,
    force_download: bool = False,
    force_parse: bool = False,
) -> list[Event]:
    sites = _load_sites(pipeline=pipeline)
    if test_site:
        sites = [s for s in sites if s["name"].lower() == test_site.lower()]
        if not sites:
            print(f"  [HTML] Site '{test_site}' not found in sites.yaml")
            return []
        site = sites[0]
        if site.get("js_required"):
            data = await _fetch_markdown_browser(site, asyncio.Semaphore(1), force_download=force_download)
        else:
            async with httpx.AsyncClient() as client:
                data = await _fetch_markdown_simple(client, site, asyncio.Semaphore(1), force_download=force_download)
        if not data:
            return []
        return extract_events_from_text(data["text"], data["url"], data["name"])

    _DATE_WINDOW_JSONLD = 365  # wide window; pipeline-level Python filter narrows it

    simple_sites = [s for s in sites if not s.get("js_required", False)]
    browser_sites = [s for s in sites if s.get("js_required", False)]

    sem_http = asyncio.Semaphore(MAX_CONCURRENT_HTTPX)
    sem_br = asyncio.Semaphore(MAX_CONCURRENT_BROWSER)

    async with httpx.AsyncClient() as client:
        http_tasks = [_fetch_markdown_simple(client, s, sem_http, force_download=force_download) for s in simple_sites]
        br_tasks = [_fetch_markdown_browser(s, sem_br, force_download=force_download) for s in browser_sites]
        results = await asyncio.gather(*http_tasks, *br_tasks, return_exceptions=True)

    sites_data = [r for r in results if isinstance(r, dict)]
    print(f"  [HTML] Fetched {len(sites_data)}/{len(sites)} sites — batching into Claude calls")

    from ..extract import _build_events as _be
    jsonld_events: list[Event] = []
    llm_sites: list[dict] = []
    for s in sites_data:
        raw_html = s.pop("_raw_html", None)
        raw_list = _events_from_jsonld(raw_html, s["url"], s["name"], _DATE_WINDOW_JSONLD) if raw_html else None
        if raw_list:
            evts = _be(raw_list, s["url"], s["name"])
            jsonld_events.extend(evts)
            print(f"  [HTML] {s['name']}: {len(evts)} events [jsonld]")
        else:
            llm_sites.append(s)

    ages = get_all_exhibition_ages([s["name"] for s in llm_sites])
    for s in llm_sites:
        if s["name"] in ages:
            s["exhibition_cache_age"] = ages[s["name"]]

    llm_events = await extract_events_batch_async(
        llm_sites, errors=errors, force_parse=force_parse,
    )
    return jsonld_events + llm_events
