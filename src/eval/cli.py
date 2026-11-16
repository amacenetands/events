"""`eval` entrypoint (scripts/eval.py; main.py wires `eval` here): score the gold set, compare
with a baseline, record LLM replies, capture new gold pages.

  eval                                  per-field table + mismatches
  eval --only "Conway Hall" --show      also print every built event
  eval --json out.json                  full report
  eval --baseline tests/gold/baseline.json [--update-baseline]
  eval --record-llm sadlers-wells [--force]   one live LLM call on the gold page -> llm_reply.json
  eval --restamp-llm sadlers-wells      refresh a reply's text_hash (records unchanged)
  eval --capture slug --url URL [--keep CSS --items CSS --limit N --json-scripts RX] [--force]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from .drift import baseline_dict, compare, summary_line
from .run import GOLD_DIR, EvalReport, fetch_result, load_gold, offline, reply_hash, run_gold

__all__ = ["main", "record_llm", "restamp_llm", "capture"]

DEFAULT_BASELINE = GOLD_DIR / "baseline.json"


def _site(g):
    """The llm engine's site dict for a gold page (what the reply hash is computed over)."""
    from ..config import get_city
    from ..sources.base import SourceCtx
    from ..sources.html_sites import WINDOW_DAYS
    from ..sources.llm_source import site_data
    cfg = g.cfg()
    ctx = SourceCtx(http=None, today=g.today, window_days=WINDOW_DAYS, city=get_city(g.meta.get("city")))
    return cfg, ctx, site_data(cfg, fetch_result(g, cfg), ctx)


def record_llm(slug: str, force: bool = False) -> Path:
    """Run the live LLM engine once on a gold page (parse cache bypassed, nothing saved to the
    cache) and store {text_hash, records} as the venue's llm_reply.json."""
    from dotenv import load_dotenv

    from .. import extract as ex
    from ..sources.base import get_source
    from .run import _patched

    (g,) = load_gold(only=slug)
    out = g.dir / "llm_reply.json"
    if out.exists() and not force:
        raise SystemExit(f"{out} exists; --force to re-record (a live LLM call)")
    load_dotenv(GOLD_DIR.parents[1] / ".env")
    cfg, ctx, site = _site(g)
    if site is None:
        raise SystemExit(f"{slug}: page is thin, nothing for the LLM")
    ctx.force_parse = True
    captured: dict[str, list] = {}
    real = ex.extract_raw_batch_async

    async def spy(sites_data, errors=None, force_parse=False, today=None, sem=None):
        res = await real(sites_data, errors=errors, force_parse=True, today=today, sem=sem)
        captured.update(res)
        return res

    with offline(None), _patched(ex, extract_raw_batch_async=spy, save_extraction=lambda *a, **k: None,
                                 cached_records=lambda *a, **k: None):
        asyncio.run(get_source("llm").records(cfg, fetch_result(g, cfg), ctx))
    if ctx.errors:
        print("\n".join(ctx.errors), file=sys.stderr)
    recs = [r if isinstance(r, dict) else r.model_dump(exclude={"source_kind", "sold_out"}, exclude_none=True)
            for r in captured.get(cfg.name, [])]
    recs = [{k: v for k, v in r.items() if v not in (None, "")} for r in recs]
    out.write_text(json.dumps({"text_hash": reply_hash(site), "records": recs}, ensure_ascii=False, indent=1) + "\n")
    return out


def restamp_llm(slug: str) -> Path:
    """Re-stamp an existing reply's text_hash from the current page/config, records unchanged
    (only after checking that the engine input change cannot alter the reply)."""
    (g,) = load_gold(only=slug)
    out = g.dir / "llm_reply.json"
    rep = json.loads(out.read_text())
    records = rep["records"] if isinstance(rep, dict) else rep
    _, _, site = _site(g)
    out.write_text(json.dumps({"text_hash": reply_hash(site), "records": records}, ensure_ascii=False,
                              indent=1) + "\n")
    return out


def capture(slug: str, url: str, keep: str | None, items: str | None, limit: int | None,
            json_scripts: str | None = None, force: bool = False) -> Path:
    from .capture import MAX_BYTES, cached_html, trim_html
    d = GOLD_DIR / slug
    page = d / "page.html"
    if page.exists() and not force:
        raise SystemExit(f"{page} exists; --force to overwrite")
    d.mkdir(parents=True, exist_ok=True)
    text = trim_html(cached_html(url), keep=keep, items=items, limit=limit, json_scripts=json_scripts)
    page.write_text(text)
    if len(text.encode()) > MAX_BYTES:
        print(f"warning: {len(text.encode())} bytes > {MAX_BYTES}; narrow --keep/--limit", file=sys.stderr)
    return page


def _show(rep: EvalReport) -> None:
    for slug, evs in rep.got.items():
        print(f"## {slug} ({rep.venues[slug]['won']})")
        for e in evs:
            print(f"  {e.title[:60]} | {e.date} {e.end_date} {e.kind} byday={e.byday} t={e.time} "
                  f"p={e.price} sk={e.sessions_known} st={e.start_known} | {e.url}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="eval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="gold slug or venue name")
    ap.add_argument("--today", type=date.fromisoformat, help="override every venue's capture date")
    ap.add_argument("--llm", choices=("recorded", "live", "skip"), default="recorded")
    ap.add_argument("--json", type=Path, help="write the full report here")
    ap.add_argument("--baseline", type=Path, nargs="?", const=DEFAULT_BASELINE)
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--show", action="store_true", help="print every built event")
    ap.add_argument("--quiet", action="store_true", help="summary line only")
    ap.add_argument("--record-llm", metavar="SLUG")
    ap.add_argument("--capture", metavar="SLUG")
    ap.add_argument("--url")
    ap.add_argument("--keep")
    ap.add_argument("--items")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--json-scripts", help="regex: keep non-LD <script> blocks matching it (adapter payloads)")
    ap.add_argument("--restamp-llm", metavar="SLUG", help="refresh a reply's text_hash, records unchanged")
    ap.add_argument("--force", action="store_true", help="allow --capture / --record-llm to overwrite")
    a = ap.parse_args(argv)

    if a.record_llm:
        print(record_llm(a.record_llm, force=a.force))
        return 0
    if a.restamp_llm:
        print(restamp_llm(a.restamp_llm))
        return 0
    if a.capture:
        if not a.url:
            ap.error("--capture needs --url")
        print(capture(a.capture, a.url, a.keep, a.items, a.limit, a.json_scripts, a.force))
        return 0
    if a.today and (a.baseline or a.update_baseline):
        ap.error("--today changes every venue's window; it cannot be compared with a baseline")

    rep = run_gold(today=a.today, only=a.only, llm=a.llm)
    if a.show:
        _show(rep)
    if not a.quiet:
        print(rep.table())
        print()
        print(rep.by_source_table())
        if rep.mismatches:
            print(f"\nmismatches ({len(rep.mismatches)}): venue | title | date | field | expected | got | evidence")
            for m in rep.mismatches:
                print("  " + m.line())
        errs = [f"{s}: {e}" for s, v in rep.venues.items() for e in v["errors"]]
        if errs:
            print("\nsource errors:\n  " + "\n  ".join(errs))
    print(summary_line(rep))
    if a.json:
        a.json.write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=1, default=str) + "\n")

    path = a.baseline or (DEFAULT_BASELINE if a.update_baseline else None)
    if a.update_baseline:
        if a.only:
            ap.error("--update-baseline needs the full gold set (no --only)")
        path.write_text(json.dumps(baseline_dict(rep), ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        print(f"baseline written: {path}")
        return 0
    if path:
        if not path.exists():
            print(f"no baseline at {path}", file=sys.stderr)
            return 2
        drift = compare(rep, json.loads(path.read_text()))
        if a.only:   # partial run: only this subset's events are comparable
            drift = [d for d in drift if not d.startswith("F1 ")]
        if drift:
            print(f"\nDRIFT vs {path} ({len(drift)}):\n  " + "\n  ".join(drift))
            return 1
        print(f"no drift vs {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
