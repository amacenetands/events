"""Live fill-rate check (pipeline v3 §1.7): one JSON line per venue per crawl in cache/eval.jsonl.

  rows = venue_rows(city, summary, events, listing_urls, prev)   # after every `main.py crawl`
  append(rows)                                                    # cache/eval.jsonl
  latest(city) / flagged(rows) / report(rows)                     # `main.py eval --live`, digest footer

Row: {ts, city, venue, fetch, source, events, fill: {time, price, url_own, sessions_known},
prev_events, delta_pct, flag}. `prev_events` is the venue's previous line (else the row count the
events table held for it before this crawl). A venue is flagged when it drops to 0 from >0, or
by >= DROP_PCT % against the previous count.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..models import Event

EVAL_LOG = Path(__file__).resolve().parents[2] / "cache" / "eval.jsonl"
DROP_PCT = 50
FILL_FIELDS = ("time", "price", "url_own", "sessions_known")


def _norm_url(u: str | None) -> str:
    return (u or "").strip().split("#")[0].rstrip("/").lower()


def fill_rates(events: list[Event], listing_url: str | None) -> dict[str, float | None]:
    """Share of `events` with a time, a price, a url other than the listing page, and known
    sessions (rounded 0..1). None when there are no events."""
    n = len(events)
    if not n:
        return {k: None for k in FILL_FIELDS}
    lst = _norm_url(listing_url)
    got = {
        "time": sum(1 for e in events if e.time),
        "price": sum(1 for e in events if e.price),
        "url_own": sum(1 for e in events if e.url and _norm_url(e.url) != lst),
        "sessions_known": sum(1 for e in events if e.sessions_known),
    }
    return {k: round(v / n, 3) for k, v in got.items()}


def flag_reason(events: int, prev: int | None) -> str | None:
    """'0 (was N)' / '-P% (N→M)' / None."""
    if not prev:
        return None
    if events == 0:
        return f"0 (was {prev})"
    pct = (events - prev) * 100 / prev
    if pct <= -DROP_PCT:
        return f"{pct:.0f}% ({prev}→{events})"
    return None


def load_prev(city: str, path: Path | None = None) -> dict[str, int]:
    """{venue: events} from each venue's last line for `city`."""
    out: dict[str, int] = {}
    for r in _read(path):
        if r.get("city") == city:
            out[r["venue"]] = r.get("events", 0)
    return out


def venue_rows(city: str, summary: dict[str, dict], events: Iterable[Event],
               listing_urls: dict[str, str], prev: dict[str, int],
               prev_db: dict[str, int] | None = None, ts: str | None = None) -> list[dict]:
    """One row per venue in `summary` (crawl_sites' {venue: stats}) plus any other event source
    (API feeds: fetch='api'). `events` are this crawl's stored events; `prev` the previous line's
    counts, `prev_db` the events table counts before the save (fallback for a first line)."""
    ts = ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
    by_src: dict[str, list[Event]] = {}
    for e in events:
        by_src.setdefault(e.source, []).append(e)
    names = list(summary) + sorted(s for s in by_src if s not in summary)
    rows = []
    for n in names:
        st = summary.get(n, {"fetch": "api", "source": "api"})
        evs = by_src.get(n, [])
        p = prev.get(n, (prev_db or {}).get(n))
        delta = round((len(evs) - p) * 100 / p, 1) if p else None
        rows.append({"ts": ts, "city": city, "venue": n, "fetch": st.get("fetch"),
                     "source": st.get("source"), "events": len(evs),
                     "fill": fill_rates(evs, listing_urls.get(n)),
                     "prev_events": p, "delta_pct": delta, "flag": flag_reason(len(evs), p)})
    return rows


def append(rows: list[dict], path: Path | None = None) -> None:
    path = path or EVAL_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _read(path: Path | None) -> list[dict]:
    path = path or EVAL_LOG
    if not path.exists():
        return []
    out = []
    for ln in path.read_text().splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def latest(city: str, path: Path | None = None) -> list[dict]:
    """Rows of the city's most recent crawl (same ts)."""
    rows = [r for r in _read(path) if r.get("city") == city]
    if not rows:
        return []
    last = max(r["ts"] for r in rows)
    return [r for r in rows if r["ts"] == last]


def flagged(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("flag")]


def _pct(x) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def city_fill(rows: list[dict]) -> dict:
    """Event-weighted fill rates over the rows + venue counts."""
    n = sum(r["events"] for r in rows)
    out = {"venues": len(rows), "with_events": sum(1 for r in rows if r["events"]), "events": n}
    for k in FILL_FIELDS:
        out[k] = (round(sum((r["fill"].get(k) or 0) * r["events"] for r in rows) / n, 3) if n else None)
    return out


def report(rows: list[dict]) -> str:
    """Per-city fill table + flagged venues (text)."""
    if not rows:
        return "no live eval rows (run `main.py crawl` first)"
    lines = ["city | crawl | venues | with events | events | " + " | ".join(FILL_FIELDS)]
    by_city: dict[str, list[dict]] = {}
    for r in rows:
        by_city.setdefault(r["city"], []).append(r)
    for c, rs in by_city.items():
        f = city_fill(rs)
        lines.append(f"{c} | {rs[0]['ts'][:16]} | {f['venues']} | {f['with_events']} | {f['events']} | "
                     + " | ".join(_pct(f[k]) for k in FILL_FIELDS))
    fl = flagged(rows)
    lines.append(f"\nflagged ({len(fl)}): venue | fetch | source | flag")
    for r in sorted(fl, key=lambda r: (r["city"], r["venue"].lower())):
        lines.append(f"  {r['venue']} | {r['fetch']} | {r['source'] or '-'} | {r['flag']}")
    return "\n".join(lines)
