"""Survey: which cached London venue pages run on Spektrix, and how many events the
public API returns for each client in the next N days. Hits the live API (1 req/s).

  venv/bin/python scripts/spektrix_survey.py [--days 90] [--max-clients 60]
                                            [--instances] [--only Barbican,ICA]

Prints TSV `venue | client | events | sessions | secs | base | flags`, then the coverage
wins: venues
currently `disabled: true` or `fetch.mode` browser* that have a working client.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import VenueConfig, load_venues  # noqa: E402
from src.sources import spektrix as S  # noqa: E402

DB = ROOT / "cache" / "memory.db"


def cached_clients(venues: dict[str, VenueConfig]) -> dict[str, S.SpektrixClient]:
    """venue name -> client, from the newest cached page(s) of each London venue."""
    out: dict[str, S.SpektrixClient] = {}
    rows = sqlite3.connect(f"file:{DB}?mode=ro", uri=True).execute(
        "select site_name, url, html from scrapes order by fetched_at desc")
    for name, url, html in rows:
        if name in venues and name not in out:
            sc = S.client_for_venue(venues[name], html)
            if sc:
                out[name] = sc
    return out


def flags(v: VenueConfig) -> str:
    f = []
    if v.disabled:
        f.append("disabled")
    if v.fetch.mode.startswith("browser"):
        f.append(v.fetch.mode)
    return ",".join(f)


async def count(http: httpx.AsyncClient, sc: S.SpektrixClient, start: date, end: date,
                instances: bool) -> tuple[int | None, S.SpektrixStats, float]:
    """(events kept, stats, seconds). With `instances`, stats.instances = sessions."""
    st = S.SpektrixStats()
    t0 = time.monotonic()
    evs = await S.fetch_events(sc, http=http, start=start, end=end, with_instances=instances,
                               max_events=10_000, stats=st)
    n = len(evs) if not instances else st.events_seen - st.events_dropped
    return (n if st.base else None), st, time.monotonic() - t0


async def main(days: int, max_clients: int, instances: bool, only: set[str]) -> None:
    if not DB.exists():
        sys.exit(f"{DB} missing: the survey reads cached venue pages from it")
    venues = {v.name: v for v in load_venues("london", include_disabled=True)}
    found = {k: v for k, v in cached_clients(venues).items() if not only or k in only}
    start = date.today()
    end = start + timedelta(days=days)
    results: dict[str, tuple[int | None, S.SpektrixStats, float]] = {}
    async with httpx.AsyncClient() as http:
        for sc in list(dict.fromkeys(found.values()))[:max_clients]:
            results[sc.client] = await count(http, sc, start, end, instances)
            await asyncio.sleep(1.0)
    print("venue\tclient\tevents\tsessions\tsecs\tbase\tflags")
    wins = []
    for name, sc in sorted(found.items()):
        n, st, secs = results.get(sc.client, (None, S.SpektrixStats(), 0.0))
        base = (st.base or "FAIL").replace("https://", "").replace("/api/v3", "")
        sess = st.instances if instances else "-"
        print(f"{name}\t{sc.client}\t{'-' if n is None else n}\t{sess}\t{secs:.1f}\t{base}"
              f"\t{flags(venues[name])}")
        if n and flags(venues[name]):
            wins.append(f"{name} ({sc.client}, {n} events, {flags(venues[name])})")
    print("\ncoverage wins:", "; ".join(wins) or "none")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--max-clients", type=int, default=60)
    ap.add_argument("--instances", action="store_true", help="also fetch sessions (bulk endpoint)")
    ap.add_argument("--only", default="", help="comma-separated venue names")
    a = ap.parse_args()
    asyncio.run(main(a.days, a.max_clients, a.instances, {x for x in a.only.split(",") if x}))
