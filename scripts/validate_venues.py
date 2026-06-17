#!/usr/bin/env python3
"""
Validate all venues in sites.yaml — check URLs are reachable and flag issues.

Usage:
  python scripts/validate_venues.py             # all venues
  python scripts/validate_venues.py --dead-only  # only show failures
  python scripts/validate_venues.py --fix-skip   # also check skip:true venues
"""
import asyncio
import argparse
import sys
from pathlib import Path

import httpx
import yaml

SITES_YAML = Path(__file__).parent.parent / "sites.yaml"
TIMEOUT = 15.0
CONCURRENCY = 8

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}


async def check_url(client: httpx.AsyncClient, name: str, url: str) -> dict:
    try:
        r = await client.head(url, headers=HEADERS, follow_redirects=True, timeout=TIMEOUT)
        # Some sites reject HEAD — fall back to GET
        if r.status_code in (405, 403, 400):
            r = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=TIMEOUT)
        # 403 = Cloudflare/bot protection — site likely fine, just blocks scrapers
        ok = r.status_code < 400 or r.status_code == 403
        return {"name": name, "url": url, "status": r.status_code, "ok": ok, "warn": r.status_code == 403}
    except httpx.TimeoutException:
        return {"name": name, "url": url, "status": "TIMEOUT", "ok": False, "warn": False}
    except httpx.TooManyRedirects:
        return {"name": name, "url": url, "status": "REDIRECT_LOOP", "ok": False, "warn": False}
    except Exception as e:
        return {"name": name, "url": url, "status": f"ERR:{type(e).__name__}", "ok": False, "warn": False}


async def run(dead_only: bool, fix_skip: bool) -> int:
    data = yaml.safe_load(SITES_YAML.read_text())
    sites = data.get("html_sites", [])

    to_check = []
    skipped_entries = []
    js_sites = []
    for s in sites:
        if s.get("skip") and not fix_skip:
            skipped_entries.append(s["name"])
            continue
        if s.get("js_required"):
            js_sites.append(s["name"])
            continue
        to_check.append(s)

    print(f"Checking {len(to_check)} venues ({len(js_sites)} js_required skipped, {len(skipped_entries)} disabled)...\n")

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        async def bounded(s):
            async with sem:
                return await check_url(client, s["name"], s["url"])

        results = await asyncio.gather(*[bounded(s) for s in to_check])

    ok = [r for r in results if r["ok"] and not r.get("warn")]
    warned = [r for r in results if r["ok"] and r.get("warn")]
    dead = [r for r in results if not r["ok"]]

    if not dead_only:
        for r in ok:
            print(f"  OK   {r['status']:>3}  {r['name']}")
        for r in warned:
            print(f"  WARN {r['status']:>3}  {r['name']}  (403 — bot-blocked, check manually)")

    if dead:
        print(f"\n{'='*50}")
        print(f"DEAD ({len(dead)}):")
        for r in dead:
            print(f"  DEAD {str(r['status']):>12}  {r['name']}")
            print(f"             {r['url']}")
    else:
        print(f"\nAll {len(ok) + len(warned)} venues reachable ({len(warned)} bot-blocked).")

    if js_sites:
        print(f"\nJS-required (validated by crawl4ai, not HTTP check):")
        for n in js_sites:
            print(f"  - {n}")

    if skipped_entries and not fix_skip:
        print(f"\nDisabled (skip:true) — run --fix-skip to check these too:")
        for n in skipped_entries:
            print(f"  - {n}")

    print(f"\nSummary: {len(ok)} OK · {len(warned)} bot-blocked · {len(dead)} dead · {len(js_sites)} js-only · {len(skipped_entries)} disabled")
    return 1 if dead else 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dead-only", action="store_true", help="Only print failures")
    p.add_argument("--fix-skip", action="store_true", help="Also check skip:true venues")
    args = p.parse_args()
    sys.exit(asyncio.run(run(args.dead_only, args.fix_skip)))


if __name__ == "__main__":
    main()
