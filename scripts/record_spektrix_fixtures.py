"""Record Spektrix fixtures for tests/test_spektrix.py (hits the network, run rarely).

  venv/bin/python scripts/record_spektrix_fixtures.py [--days 90] [--pages-only]

Writes tests/fixtures/spektrix/ (trimmed to stay small, < 60 KB):
  <client>_events.json                  events list (≤ 20, heavy fields stripped)
  <client>_bulk.json                    /instances?$expand=priceList, recorded events only
  <client>_instances_<eventId>.json     per-event fallback, 2 events per client
  <client>_status_<instanceId>.json     one /status response (first client)
  manifest.json                         {recorded_on, start, end, clients, status}
  pages/<slug>.html                     ~2 KB snippets of cached venue pages (cache/memory.db)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.sources import spektrix as S  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "spektrix"
DB = ROOT / "cache" / "memory.db"
CLIENTS = ("kingsplace", "youngvic", "theotherpalace")
MAX_EVENTS, MAX_INSTANCES = 20, 4
# Cached venue pages the fingerprint / platform tests use.
PAGES = ("Kings Place", "The Other Palace", "Bush Theatre", "Royal Court Theatre", "Park Theatre",
         "Lyric Hammersmith", "Hackney Empire", "Camden People's Theatre", "Criterion Theatre",
         "Chickenshed Theatre", "OSO Arts Centre", "Watermans Arts Centre", "Conway Hall",
         "Golden Goose Theatre", "London Transport Museum")
_SNIP = re.compile(r"spektrix|ticketsolve|eventbrite\.[a-z.]+/o/|primary_organizer_id", re.I)
_EVENT_KEYS = ("id", "name", "description", "webUrl", "webEventId", "isOnSale", "instanceDates",
               "firstInstanceDateTime", "lastInstanceDateTime")


# ── trimming ─────────────────────────────────────────────────────────────────

def trim_event(e: dict[str, Any]) -> dict[str, Any]:
    out = {k: e.get(k) for k in _EVENT_KEYS}
    out["description"] = (out["description"] or "")[:300]
    out |= {k: v for k, v in e.items()
            if k.startswith("attribute_") and v not in ("", None, False) and len(str(v)) < 80}
    return out


def trim_instance(i: dict[str, Any]) -> dict[str, Any]:
    prices = ((i.get("priceList") or {}).get("prices")) or []
    return {
        "id": i.get("id"), "event": {"id": (i.get("event") or {}).get("id")},
        "start": i.get("start"), "isOnSale": i.get("isOnSale"), "cancelled": i.get("cancelled"),
        "priceList": {"prices": [{"amount": p.get("amount"),
                                  "ticketType": {"name": (p.get("ticketType") or {}).get("name")},
                                  "priceBand": {"name": (p.get("priceBand") or {}).get("name")}}
                                 for p in prices]},
    }


def _keep_events(events: list[dict], picked: list[dict], start: date, end: date) -> list[dict]:
    """Picked + every add-on + others up to MAX_EVENTS, original order."""
    names = [e.get("name") or "" for e in events]
    must = {e["id"] for e in picked} | {e["id"] for e in events if S.is_addon(e, names)}
    keep, room = [], MAX_EVENTS - len(must)
    for e in events:
        if e["id"] in must:
            keep.append(e)
        elif room > 0:
            keep.append(e)
            room -= 1
    return [trim_event(e) for e in keep]


# ── recording ────────────────────────────────────────────────────────────────

def _dump(name: str, data: object) -> None:
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=0))


async def _get(http: httpx.AsyncClient, url: str, params: dict[str, str] | None = None,
               timeout: float = S.REQUEST_TIMEOUT) -> Any:
    await asyncio.sleep(1.0)
    return await S._get_json(http, url, params, timeout=timeout)


def _pick(events: list[dict], start: date, end: date) -> list[dict]:
    """Two real (non add-on) events, preferring runs with several sessions."""
    kept, _ = S.select_events(events, start, end, 10_000)
    kept.sort(key=lambda e: e.get("firstInstanceDateTime") == e.get("lastInstanceDateTime"))
    return kept[:2]


async def record_client(http: httpx.AsyncClient, client: str, start: date, end: date,
                        manifest: dict) -> None:
    base = S.SpektrixClient(client).bases[0]
    events = await _get(http, f"{base}/events",
                        S._window(start, end, "instanceStart_from", "instanceStart_to"))
    picked = _pick(events, start, end)
    _dump(f"{client}_events.json", _keep_events(events, picked, start, end))
    manifest["clients"][client] = [e["id"] for e in picked]
    ids = set(manifest["clients"][client])
    bulk = await _get(http, f"{base}/instances",
                      S._window(start, end, "startFrom", "startTo") | {"$expand": "priceList"},
                      timeout=S.BULK_TIMEOUT)
    per: dict[str, int] = {}
    kept = []
    for i in bulk:
        eid = (i.get("event") or {}).get("id")
        if eid in ids and per.get(eid, 0) < MAX_INSTANCES:
            per[eid] = per.get(eid, 0) + 1
            kept.append(trim_instance(i))
    _dump(f"{client}_bulk.json", kept)
    for ev in picked:
        params = S._window(start, end, "start_from", "start_to") | {"$expand": "priceList"}
        insts = await _get(http, f"{base}/events/{ev['id']}/instances", params)
        _dump(f"{client}_instances_{ev['id']}.json", [trim_instance(i) for i in insts[:MAX_INSTANCES]])
        if insts and "status" not in manifest:
            iid = insts[0]["id"]
            _dump(f"{client}_status_{iid}.json", await _get(http, f"{base}/instances/{iid}/status"))
            manifest["status"] = {client: iid}
    print(f"{client}: {len(events)} events, {len(bulk)} bulk instances; kept {len(kept)}")


async def record(days: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.json"):
        f.unlink()
    start = date.today()
    end = start + timedelta(days=days)
    manifest: dict = {"recorded_on": start.isoformat(), "start": start.isoformat(),
                      "end": end.isoformat(), "clients": {}}
    async with httpx.AsyncClient() as http:
        for client in CLIENTS:
            await record_client(http, client, start, end, manifest)
    _dump("manifest.json", manifest)


# ── page snippets ────────────────────────────────────────────────────────────

def _marker_spans(html: str, pad: int) -> list[tuple[int, int]]:
    """Detector matches first (what the tests rely on), then generic platform markers."""
    from src.sources import platforms as P
    rxs = [*S._STRONG, S._URL, P._TS_HOST, P._EB_ORG, _SNIP]
    spans = []
    for rx in rxs:
        for m in rx.finditer(html):
            spans.append((max(0, m.start() - pad), min(len(html), m.end() + pad)))
    return spans


def snippet(html: str, budget: int = 3000, pad: int = 120) -> str:
    """Windows around detector matches (priority order), merged, capped at `budget` chars."""
    chosen: list[tuple[int, int]] = []
    used = 0
    for a, b in _marker_spans(html, pad):
        if used >= budget:
            break
        if any(a < y and b > x for x, y in chosen):
            continue
        chosen.append((a, b))
        used += b - a
    return "\n<!-- … -->\n".join(html[a:b] for a, b in sorted(chosen))


def record_pages() -> None:
    if not DB.exists():
        sys.exit(f"{DB} missing: page snippets need the local scrape cache")
    (OUT / "pages").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    index = {}
    for name in PAGES:
        row = db.execute("select url, html from scrapes where site_name=? order by fetched_at desc",
                         (name,)).fetchone()
        if not row:
            print(f"skip {name}: not cached")
            continue
        slug = re.sub(r"\W+", "_", name.lower()).strip("_")
        (OUT / "pages" / f"{slug}.html").write_text(snippet(row[1]))
        index[name] = {"file": f"{slug}.html", "url": row[0]}
    (OUT / "pages" / "index.json").write_text(json.dumps(index, indent=1))
    print(f"pages: {len(index)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--pages-only", action="store_true")
    a = ap.parse_args()
    if not a.pages_only:
        asyncio.run(record(a.days))
    record_pages()
