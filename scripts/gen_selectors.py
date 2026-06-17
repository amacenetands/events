#!/usr/bin/env python3
"""
Generate per-venue remove_selectors for sites.yaml.

Fetches each static site's HTML, extracts structural skeleton (tags + classes/ids),
asks Claude Haiku to identify non-event CSS selectors to strip, then patches sites.yaml.

Usage:
  python scripts/gen_selectors.py                     # all static sites
  python scripts/gen_selectors.py --venue "Wigmore"   # single venue (name substring match)
  python scripts/gen_selectors.py --dry-run           # print proposed changes, don't write
  python scripts/gen_selectors.py --threshold 5000    # only sites with md chars > threshold
"""
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import subprocess

import httpx
import yaml
from bs4 import BeautifulSoup
from markdownify import markdownify

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.sources.html_sites import _clean_html, _extract_jsonld, HEADERS

SITES_YAML = Path(__file__).parent.parent / "sites.yaml"
CONCURRENCY = 6
TIMEOUT = 20.0


def _structural_skeleton(html: str, max_depth: int = 4) -> str:
    """
    Extract a compact representation of the HTML tree: tag + class + id only.
    Used to give LLM structure without the full content.
    """
    soup = BeautifulSoup(html, "html.parser")
    lines = []

    def _walk(node, depth=0):
        if depth > max_depth:
            return
        if not hasattr(node, "name") or node.name is None:
            return
        cls = " ".join(node.get("class") or [])
        nid = node.get("id", "")
        role = node.get("role", "")
        text_preview = node.get_text(" ", strip=True)[:60].replace("\n", " ")
        attrs = []
        if cls:
            attrs.append(f'.{cls.replace(" ", ".")}')
        if nid:
            attrs.append(f"#{nid}")
        if role:
            attrs.append(f'[role={role}]')
        line = "  " * depth + f"<{node.name}{' ' if attrs else ''}{''.join(attrs)}>"
        if text_preview:
            line += f"  // {text_preview}"
        lines.append(line)
        for child in node.children:
            _walk(child, depth + 1)

    _walk(soup.find("body") or soup)
    return "\n".join(lines[:300])  # cap at 300 lines


SELECTOR_PROMPT = """\
You are analyzing an HTML page from a London events venue website.
Below is a structural skeleton of the page (tag names, CSS classes, IDs, and brief text previews).

Your task: identify CSS selectors for elements that are NOT event listings — things like:
- Cookie banners / GDPR popups
- Navigation menus, breadcrumbs, sidebars
- Newsletter signup, social share buttons
- Hero/banner images with no event info
- Site-wide promotions, advertisements
- Booking widgets, "plan your visit" sections unrelated to specific events

Return ONLY a JSON array of CSS selector strings (or an empty array [] if nothing to add).
These will be added to the site's `remove_selectors` field in sites.yaml.
Keep selectors simple and specific. Examples: ["div.cookie-banner", ".newsletter-signup", "#hero-promo"]

Page: {venue_name}
URL: {url}

HTML skeleton:
{skeleton}
"""


async def _fetch_site_browser(site: dict, sem: asyncio.Semaphore) -> dict | None:
    async with sem:
        try:
            import os
            from pathlib import Path as _Path
            os.environ.setdefault("CRAWL4_AI_BASE_DIRECTORY", "/tmp")
            _Path("/tmp").mkdir(parents=True, exist_ok=True)
            from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, BrowserConfig
            from crawl4ai.async_configs import CacheMode
            browser_cfg = BrowserConfig(headless=True)
            cfg = CrawlerRunConfig(
                js_code="window.scrollTo(0, document.body.scrollHeight);",
                delay_before_return_html=5.0,
                page_timeout=30000,
                cache_mode=CacheMode.BYPASS,
            )
            async with AsyncWebCrawler(config=browser_cfg, verbose=False) as crawler:
                result = await crawler.arun(url=site["url"], config=cfg)
            if not result.success or not result.html:
                print(f"  SKIP {site['name']}: browser crawl failed")
                return None
            raw = result.html
            clean = _clean_html(raw, extra_selectors=site.get("remove_selectors"))
            md = markdownify(clean, heading_style="ATX", strip=["script", "style", "nav", "footer"])
            md = "\n".join(l for l in md.splitlines() if l.strip())
            skeleton = _structural_skeleton(raw)
            return {
                "name": site["name"],
                "url": site["url"],
                "md_len": len(md),
                "raw_len": len(raw),
                "skeleton": skeleton,
                "existing_selectors": site.get("remove_selectors", []),
            }
        except Exception as e:
            print(f"  SKIP {site['name']}: {type(e).__name__}: {e}")
            return None


async def _fetch_site(client: httpx.AsyncClient, site: dict, sem: asyncio.Semaphore) -> dict | None:
    async with sem:
        try:
            r = await client.get(site["url"], headers=HEADERS, follow_redirects=True, timeout=TIMEOUT)
            r.raise_for_status()
            raw = r.text
            clean = _clean_html(raw, extra_selectors=site.get("remove_selectors"))
            md = markdownify(clean, heading_style="ATX", strip=["script", "style", "nav", "footer"])
            md = "\n".join(l for l in md.splitlines() if l.strip())
            skeleton = _structural_skeleton(raw)
            return {
                "name": site["name"],
                "url": site["url"],
                "md_len": len(md),
                "raw_len": len(raw),
                "skeleton": skeleton,
                "existing_selectors": site.get("remove_selectors", []),
            }
        except Exception as e:
            print(f"  SKIP {site['name']}: {type(e).__name__}: {e}")
            return None


def _ask_claude(site_data: dict) -> list[str]:
    prompt = SELECTOR_PROMPT.format(
        venue_name=site_data["name"],
        url=site_data["url"],
        skeleton=site_data["skeleton"],
    )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", "claude-haiku-4-5-20251001"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"  Claude error for {site_data['name']}: {result.stderr[:100]}")
            return []
        text = result.stdout.strip()
        m = re.search(r"\[.*?\]", text, re.DOTALL)
        if m:
            selectors = json.loads(m.group())
            return [s for s in selectors if isinstance(s, str)]
        return []
    except Exception as e:
        print(f"  Claude error for {site_data['name']}: {e}")
        return []


def _patch_sites_yaml(patches: dict[str, list[str]], dry_run: bool) -> None:
    """Update sites.yaml with new remove_selectors entries."""
    text = SITES_YAML.read_text()
    lines = text.splitlines()
    output = []
    i = 0
    while i < len(lines):
        line = lines[i]
        output.append(line)
        # Detect venue name line
        name_m = re.match(r"(\s*)- name:\s*[\"']?(.+?)[\"']?\s*$", line)
        if name_m:
            indent = name_m.group(1) + "  "  # 2 extra spaces for field indent
            venue_name = name_m.group(2)
            if venue_name in patches:
                new_sels = patches[venue_name]
                # Scan forward to check if remove_selectors already exists in this block
                j = i + 1
                block_has_sel = False
                while j < len(lines):
                    next_line = lines[j]
                    if re.match(r"\s*- name:", next_line):
                        break  # next venue block
                    if "remove_selectors:" in next_line:
                        block_has_sel = True
                        break
                    j += 1
                if not block_has_sel and new_sels:
                    sel_lines = [f"{indent}remove_selectors:"]
                    for s in new_sels:
                        sel_lines.append(f"{indent}  - \"{s}\"")
                    if dry_run:
                        print(f"\n[DRY-RUN] Would add to {venue_name}:")
                        for sl in sel_lines:
                            print(f"  {sl}")
                    else:
                        output.extend(sel_lines)
        i += 1

    if not dry_run:
        SITES_YAML.write_text("\n".join(output) + "\n")
        print(f"\nPatched sites.yaml with {len(patches)} venues.")


async def run(venue_filter: str | None, dry_run: bool, threshold: int, js_sites: bool = False) -> None:
    data = yaml.safe_load(SITES_YAML.read_text())
    all_sites = data.get("html_sites", [])

    if js_sites:
        sites = [s for s in all_sites if not s.get("skip") and s.get("js_required")]
        print(f"Processing {len(sites)} JS venues (browser fetch)...")
        if venue_filter:
            sites = [s for s in sites if venue_filter.lower() in s["name"].lower()]
        sem = asyncio.Semaphore(2)  # limit concurrent browser instances
        tasks = [_fetch_site_browser(s, sem) for s in sites]
        results = await asyncio.gather(*tasks)
    else:
        sites = [s for s in all_sites if not s.get("skip") and not s.get("js_required")]
        if venue_filter:
            sites = [s for s in sites if venue_filter.lower() in s["name"].lower()]
        print(f"Processing {len(sites)} static venues...")
        sem = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient() as client:
            tasks = [_fetch_site(client, s, sem) for s in sites]
            results = await asyncio.gather(*tasks)

    fetched = [r for r in results if r is not None]
    print(f"Fetched {len(fetched)} sites. Analyzing with Claude Haiku...")

    # Filter to sites above threshold (no point fine-tuning small pages)
    to_analyze = [r for r in fetched if r["md_len"] > threshold]
    print(f"Sites above {threshold} chars md: {len(to_analyze)} (others already clean enough)")

    patches: dict[str, list[str]] = {}
    for site_data in to_analyze:
        existing = site_data["existing_selectors"]
        selectors = _ask_claude(site_data)
        # Merge with existing, deduplicate
        merged = list(dict.fromkeys(existing + selectors))
        new_only = [s for s in selectors if s not in existing]
        pct = round((1 - site_data["md_len"] / site_data["raw_len"]) * 100)
        print(
            f"  {site_data['name']}: {site_data['raw_len']:,} → md={site_data['md_len']:,} ({pct}%) "
            f"→ +{len(new_only)} selectors: {new_only}"
        )
        if new_only:
            patches[site_data["name"]] = merged

    if patches:
        _patch_sites_yaml(patches, dry_run=dry_run)
    else:
        print("\nNo new selectors needed.")

    # Summary for sites below threshold
    clean = [r for r in fetched if r["md_len"] <= threshold]
    if clean:
        print(f"\nAlready clean (md ≤ {threshold} chars): {[r['name'] for r in clean]}")


def main() -> None:
    p = argparse.ArgumentParser(description="Generate remove_selectors for sites.yaml")
    p.add_argument("--venue", metavar="NAME", help="Filter to venues matching this substring")
    p.add_argument("--dry-run", action="store_true", help="Print proposed changes, don't write")
    p.add_argument("--threshold", type=int, default=8000, help="Only analyze sites with md chars above this (default 8000)")
    p.add_argument("--js", action="store_true", help="Process JS sites via browser fetch instead of static sites")
    args = p.parse_args()
    asyncio.run(run(args.venue, args.dry_run, args.threshold, js_sites=args.js))


if __name__ == "__main__":
    main()
