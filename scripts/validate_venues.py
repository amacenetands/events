#!/usr/bin/env python3
"""
Venue checks for sites/<city>.yaml: live reachability + page fingerprint, or the offline
fetch/coverage status of the last crawl.

Usage:
  python scripts/validate_venues.py --city moscow            # reachability (HTTP GET, 8 concurrent)
  python scripts/validate_venues.py --city moscow --dead-only
  python scripts/validate_venues.py --venue "Barbican"       # one venue (name substring), any city
  python scripts/validate_venues.py --city moscow --probe    # + platform / JSON-LD / feeds / suggestion
  python scripts/validate_venues.py --url https://x/afisha/ --city moscow   # probe a URL not yet in YAML
  python scripts/validate_venues.py --city moscow --status   # offline: last fetch status + events per venue
  python scripts/validate_venues.py --city moscow --adapter-check "Venue" \
      --adapter '{kind: cards, card: li.event, title: h3, date: .when}'   # offline, on the stored page
  python scripts/validate_venues.py --fix-skip               # also check skip:true / disabled venues

Browser-mode venues are fetched over plain HTTP too: a 200 there only proves the host is up.
Exit 1 when any checked venue is dead (or, with --status, never fetched / blocked / 0 events).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TIMEOUT = 20.0
CONCURRENCY = 8
THIN_TEXT = 1500          # visible chars below which a listing is "thin" (JS shell / needs browser)
STALE_DAYS = 7

_FEED = re.compile(r"""<link[^>]+type=["'](application/(?:rss|atom)\+xml|text/calendar)["'][^>]*>""", re.I)
_ICS_HREF = re.compile(r"""href=["']([^"']+?(?:\.ics|[?&]ical=1|/ical/?|webcal:[^"']+))["']""", re.I)
_HREF = re.compile(r"""href=["']([^"']+)["']""", re.I)
_NEXT = re.compile(r"""<script[^>]+id=["']__NEXT_DATA__["']|window\.__NUXT__""", re.I)
_TIME_TAG = re.compile(r"<time[^>]+datetime=", re.I)


# ── pure page fingerprint (tested offline) ───────────────────────────────────

def probe_page(html: str, url: str = "") -> dict:
    """What a listing page offers the pipeline: platform, JSON-LD events, feeds, SSR data."""
    from src.fetch.types import is_blocked_page, visible_text
    from src.sources.jsonld import event_nodes
    from src.sources.platforms import detect_platform
    feeds = []
    for m in _FEED.finditer(html):
        h = _HREF.search(m.group(0))
        if h:
            feeds.append(h.group(1))
    feeds += [h for h in _ICS_HREF.findall(html) if h not in feeds]
    try:
        n_ld = len(event_nodes(html))
    except Exception:   # noqa: BLE001 — malformed LD must not sink a probe
        n_ld = 0
    plat = detect_platform(html, url)
    return {
        "blocked": is_blocked_page(html),
        "text_len": len(visible_text(html)),
        "platform": f"{plat[0]}:{plat[1]}" if plat else None,
        "jsonld_events": n_ld,
        "time_tags": len(_TIME_TAG.findall(html)),
        "ssr_data": bool(_NEXT.search(html)),
        "feeds": feeds[:5],
    }


def suggest(p: dict) -> dict:
    """First guess at `fetch` / `extract` for a probed page; the agent confirms with --test-site."""
    if p["blocked"]:
        return {"fetch": None, "extract": None,
                "why": "bot wall: find a feed/ICS/platform API/aggregator, else disable"}
    if p["platform"] and p["platform"].startswith("spektrix"):
        return {"fetch": "http", "extract": ["spektrix", "jsonld", "llm"], "why": p["platform"]}
    if p["jsonld_events"] >= 3:
        return {"fetch": "http", "extract": ["jsonld", "llm"], "why": f"{p['jsonld_events']} JSON-LD events"}
    if p["text_len"] < THIN_TEXT:
        return {"fetch": "browser", "extract": ["jsonld", "adapter", "llm"],
                "why": f"thin page ({p['text_len']} chars): JS-rendered, or propose an adapter"}
    return {"fetch": "http", "extract": ["jsonld", "adapter", "llm"], "why": "server-rendered listing"}


def adapter_check(html: str, adapter: dict, today, lang: str = "en", min_records: int = 3,
                  order: str = "dmy") -> dict:
    """Run an adapter config over a page: records, how many carry a parseable date, verdict.
    ok = ≥ min_records records and ≥ 80% of them dated (what `direct` adapters need)."""
    from src.normalize.dates import parse_date_text
    from src.sources.adapters import get_adapter
    recs = get_adapter({"adapter": adapter}).records(html, {"adapter": adapter}, today=today)
    dated = 0
    for r in recs:
        try:
            if r.date and parse_date_text(r.date, today, lang=lang, order=order) is not None:
                dated += 1
        except Exception:   # noqa: BLE001
            pass
    ok = len(recs) >= min_records and dated >= 0.8 * len(recs)
    return {"records": len(recs), "dated": dated, "ok": ok,
            "sample": [r.line()[:140] for r in recs[:5]]}


def run_adapter_check(city: str | None, venue: str, adapter_yaml: str | None, min_records: int) -> int:
    import yaml
    from src.config import get_city, venues_from_file
    from src.fetch import cached_result
    from src.query import city_today, Filter
    c = get_city(city)
    cfg = next((v for v in venues_from_file(c.sites_path) if v.name.lower() == venue.lower()), None)
    if cfg is None:
        print(f"no venue {venue!r} in {c.sites}")
        return 1
    adapter = yaml.safe_load(adapter_yaml) if adapter_yaml else cfg.adapter
    if not isinstance(adapter, dict) or "kind" not in adapter:
        print("--adapter must be a YAML mapping with `kind` (or the venue must have one)")
        return 1
    today = city_today(Filter(city=c.name))
    fr = cached_result(cfg, today)
    if not fr.body:
        print(f"{cfg.name}: {fr.status.value} — {fr.note}. Fetch it first: "
              f"main.py crawl --city {c.name} --test-site \"{cfg.name}\"")
        return 1
    try:
        r = adapter_check(fr.body, adapter, today, c.lang, min_records, cfg.fix.date_order)
    except KeyError as e:          # unknown kind, or a required key (cards: `card`) missing
        print(f"bad adapter {adapter}: {e}")
        return 1
    print(f"{cfg.name}: {r['records']} records, {r['dated']} with a parseable date -> "
          f"{'OK' if r['ok'] else 'NOT OK'} (adapter {adapter})")
    for line in r["sample"]:
        print("  " + line)
    return 0 if r["ok"] else 1


# ── live checks ──────────────────────────────────────────────────────────────

def _headers(lang: str) -> dict:
    from src.fetch.headers import HEADERS
    al = {"en": "en-GB,en;q=0.9"}.get(lang, f"{lang},en;q=0.7")
    return {**HEADERS, "Accept-Language": al}


async def check_url(client: httpx.AsyncClient, name: str, url: str, mode: str = "http",
                    probe: bool = False) -> dict:
    """GET (redirects followed). ok = reachable; warn = 403/429/503 or a bot wall (likely fine
    in a browser, but the http fetcher will be blocked); ssl = only works with verify off."""
    r0 = {"name": name, "url": url, "mode": mode, "ok": False, "warn": False, "ssl": False, "final": None,
          "bot_wall": False}
    try:
        try:
            r = await client.get(url, timeout=TIMEOUT)
        except httpx.ConnectError as e:
            if "CERTIFICATE" not in str(e).upper() and "SSL" not in str(e).upper():
                raise
            async with httpx.AsyncClient(headers=client.headers, follow_redirects=True, verify=False) as c2:
                r = await c2.get(url, timeout=TIMEOUT)
            r0["ssl"] = True
    except httpx.TimeoutException:
        return {**r0, "status": "TIMEOUT"}
    except httpx.TooManyRedirects:
        return {**r0, "status": "REDIRECT_LOOP"}
    except Exception as e:   # noqa: BLE001
        return {**r0, "status": f"ERR:{type(e).__name__}"}
    final = str(r.url)
    out = {**r0, "status": r.status_code,
           "final": final if final.rstrip("/") != url.rstrip("/") else None}
    body = r.text if r.status_code < 500 or r.status_code == 503 else ""   # walls are often 503
    p = probe_page(body, final) if body else None
    blocked = bool(p and p["blocked"])
    out["bot_wall"] = blocked
    out["ok"] = r.status_code < 400 or r.status_code in (403, 429)
    out["warn"] = r.status_code in (403, 429, 503) or blocked or out["ssl"]
    if r.status_code == 503 and blocked:
        out["ok"] = True
    if probe and p:
        out["probe"] = p
        out["suggest"] = suggest(p)
    return out


def _select(city: str | None, venue: str | None, fix_skip: bool):
    from src.config import get_city, load_cities, venues_from_file
    cities = [get_city(city)] if city else list(load_cities().values())
    out, skipped = [], []
    for c in cities:
        if not c.sites_path.exists():
            continue
        for v in venues_from_file(c.sites_path):
            if venue and venue.lower() not in v.name.lower():
                continue
            if (v.skip or v.disabled) and not fix_skip:
                skipped.append(v.name)
                continue
            out.append((c, v))
    return out, skipped


async def run(city: str | None, venue: str | None, dead_only: bool, fix_skip: bool, probe: bool,
              as_json: bool) -> int:
    from datetime import date
    pairs, skipped = _select(city, venue, fix_skip)
    if not pairs:
        print("no venues matched")
        return 1
    lang = pairs[0][0].lang
    print(f"Checking {len(pairs)} venues ({len(skipped)} skip/disabled not checked)...\n", flush=True)
    sem = asyncio.Semaphore(CONCURRENCY)
    year = date.today().year
    async with httpx.AsyncClient(headers=_headers(lang), follow_redirects=True,
                                 limits=httpx.Limits(max_connections=2 * CONCURRENCY)) as client:
        async def one(v):
            async with sem:
                return await check_url(client, v.name, v.resolved_url(year), v.fetch.mode, probe)
        results = await asyncio.gather(*(one(v) for _, v in pairs))

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return 1 if any(not r["ok"] for r in results) else 0
    ok = [r for r in results if r["ok"] and not r["warn"]]
    warned = [r for r in results if r["ok"] and r["warn"]]
    dead = [r for r in results if not r["ok"]]

    def line(tag: str, r: dict) -> str:
        s = f"  {tag:<4} {str(r['status']):>13}  {r['name']}"
        if r["mode"] != "http":
            s += f"  [{r['mode']}]"
        if r["ssl"]:
            s += "  (bad TLS cert: works only with verify off)"
        if r.get("bot_wall"):
            s += "  (bot wall)"
        if r["final"]:
            s += f"\n{'':>21}→ {r['final']}"
        if "probe" in r:
            p, sg = r["probe"], r["suggest"]
            s += (f"\n{'':>21}platform={p['platform']} jsonld={p['jsonld_events']} time_tags={p['time_tags']}"
                  f" ssr={p['ssr_data']} text={p['text_len']} feeds={p['feeds'] or '-'}"
                  f"\n{'':>21}suggest: fetch={sg['fetch']} extract={sg['extract']} ({sg['why']})")
        return s

    if not dead_only:
        for r in ok:
            print(line("OK", r))
        for r in warned:
            print(line("WARN", r))
    if dead:
        print(f"\n{'=' * 50}\nDEAD ({len(dead)}):")
        for r in dead:
            print(line("DEAD", r))
            print(f"{'':>21}{r['url']}")
    print(f"\nSummary: {len(ok)} OK · {len(warned)} warn (403/429/503, bot wall or bad cert) · "
          f"{len(dead)} dead · {len(skipped)} skip/disabled not checked")
    return 1 if dead else 0


async def run_url(url: str, city: str | None) -> int:
    from src.config import get_city
    async with httpx.AsyncClient(headers=_headers(get_city(city).lang), follow_redirects=True) as client:
        r = await check_url(client, url, url, probe=True)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r["ok"] else 1


# ── offline status (last crawl) ──────────────────────────────────────────────

def status_rows(city: str) -> list[dict]:
    """One row per active venue of `city`: last fetch status/age + events in the table."""
    from src import events_db
    from src.fetch.report import fetch_status_report
    counts: dict[str, int] = {}
    for e in events_db.load_events(city).events:
        counts[e.source] = counts.get(e.source, 0) + 1
    now = datetime.now(timezone.utc)
    out = []
    for r in fetch_status_report(city):
        age = None
        if r.get("fetched_at"):
            d = datetime.fromisoformat(r["fetched_at"])
            age = (now - (d if d.tzinfo else d.replace(tzinfo=timezone.utc))).total_seconds() / 86400
        st = r.get("status")
        bucket = ("no_fetch_row" if st is None and not r.get("fetched_at")
                  else "blocked" if st == "blocked"
                  else "error" if st in ("error", "empty")
                  else "stale" if age is not None and age > STALE_DAYS
                  else "zero_events" if not counts.get(r["name"])
                  else "ok")
        out.append({"name": r["name"], "bucket": bucket, "status": st, "age_days": age,
                    "events": counts.get(r["name"], 0), "note": r.get("note")})
    return out


def run_status(city: str) -> int:
    rows = status_rows(city)
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["bucket"], []).append(r)
    for b in ("no_fetch_row", "blocked", "error", "stale", "zero_events", "ok"):
        rs = by.get(b, [])
        if not rs:
            continue
        print(f"\n{b} ({len(rs)}):")
        for r in sorted(rs, key=lambda r: -r["events"]):
            age = f"{r['age_days']:.1f}d" if r["age_days"] is not None else "-"
            note = f"  {r['note'][:70]}" if r.get("note") and b != "ok" else ""
            print(f"  {r['events']:>4} ev  {age:>6}  {r['name']}{note}")
    print("\nSummary: " + " · ".join(f"{len(by.get(b, []))} {b}" for b in
                                     ("ok", "zero_events", "stale", "error", "blocked", "no_fetch_row")))
    return 0 if len(by.get("ok", [])) == len(rows) else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--city", help="cities.yaml name (default: every city; --status/--url: london)")
    p.add_argument("--venue", metavar="NAME", help="only venues whose name contains this")
    p.add_argument("--dead-only", action="store_true", help="only print failures")
    p.add_argument("--fix-skip", action="store_true", help="also check skip:true / disabled venues")
    p.add_argument("--probe", action="store_true", help="fingerprint each page and suggest fetch/extract")
    p.add_argument("--url", help="probe one URL (not yet in sites/<city>.yaml)")
    p.add_argument("--status", action="store_true", help="offline: last fetch status + events per venue")
    p.add_argument("--json", action="store_true", help="machine-readable results")
    p.add_argument("--adapter-check", metavar="NAME", help="offline: run an adapter on NAME's stored page")
    p.add_argument("--adapter", help="YAML mapping for --adapter-check (default: the venue's own adapter:)")
    p.add_argument("--min-records", type=int, default=3)
    a = p.parse_args(argv)
    if a.city:
        from src.config import city_names
        if a.city.lower() not in city_names():
            p.error(f"unknown city {a.city!r}; known: {', '.join(city_names())}")
        a.city = a.city.lower()
    if a.status:
        return run_status(a.city or "london")
    if a.adapter_check:
        return run_adapter_check(a.city, a.adapter_check, a.adapter, a.min_records)
    if a.url:
        return asyncio.run(run_url(a.url, a.city))
    return asyncio.run(run(a.city, a.venue, a.dead_only, a.fix_skip, a.probe, a.json))


if __name__ == "__main__":
    sys.exit(main())
