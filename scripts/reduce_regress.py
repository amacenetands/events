"""Regression harness: old (head-truncate + _date_trim) vs new (deterministic reducer)
text over every row in cache/memory.db `scrapes` (read-only). Prints TSV + summary.

  venv/bin/python scripts/reduce_regress.py [--only SUBSTR] [--tsv out.tsv]
"""
import argparse
import datetime as dt
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from markdownify import markdownify
from src.extract import _clean_text, _date_trim
from src.sources.adapters.textutil import TIME, future_dates
from src.sources.html_sites import _clean_html, _extract_jsonld, _html_to_site_data

HORIZON = 419  # ~ end of next year from the reference date


def old_text(site: dict, html: str) -> str:
    """Pre-reducer pipeline: jsonld + markdown, head-truncated to 32k, then _date_trim(6000)."""
    jsonld = _extract_jsonld(html)
    clean = _clean_html(html, extra_selectors=site.get("remove_selectors"))
    md = markdownify(clean, heading_style="ATX", strip=["script", "style", "nav", "footer"])
    md = "\n".join(l for l in md.splitlines() if l.strip())
    text = (jsonld + "\n\n" + md).strip()[:32000] if jsonld else md[:32000]
    return _date_trim(_clean_text(text), max_chars=6000)


def new_text(site: dict, html: str):
    sd = _html_to_site_data(site, html if "<" in html[:2000] else "", pre_text=None if "<" in html[:2000] else html)
    if sd["events"] is not None:
        txt = "\n".join(f"{e.title} | {e.date.isoformat()}" + (f" - {e.end_date.isoformat()}" if e.end_date else "")
                        + (f" {e.time}" if e.time else "") for e in sd["events"])
        return txt, len(sd["events"]), 0, False, "direct"
    return "\n".join(sd["chunks"]), len(sd["text"].splitlines()), len(sd["chunks"]), sd["thin"], "llm"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--tsv", default="")
    a = ap.parse_args()
    cfg = {s["name"]: s for s in yaml.safe_load(open(ROOT / "sites.yaml"))["html_sites"]}
    db = sqlite3.connect(f"file:{ROOT}/cache/memory.db?mode=ro", uri=True)
    rows = db.execute("select site_name, url, html from scrapes order by site_name, url").fetchall()
    today = dt.date.today()
    hdr = "site\tmode\told_chars\tnew_chars\told_fut\tnew_fut\told_times\tnew_times\trecords\tchunks\tthin\tsecs\tverdict"
    out = [hdr]
    print(hdr, flush=True)
    imp = eq = reg = 0
    regs = []
    for name, url, html in rows:
        if a.only and a.only.lower() not in name.lower():
            continue
        site = dict(cfg.get(name) or {})
        site.setdefault("name", name)
        site["url"] = url
        t0 = time.time()
        try:
            o = old_text(site, html) if "<" in html[:2000] else _date_trim(_clean_text(html))
            n, recs, chunks, thin, mode = new_text(site, html)
        except Exception as e:
            print(f"{name}\tERROR\t{e}", file=sys.stderr)
            continue
        secs = time.time() - t0
        of, nf = len(future_dates(o, today, HORIZON)), len(future_dates(n, today, HORIZON))
        if nf > of: v = "improved"; imp += 1
        elif nf == of: v = "equal"; eq += 1
        else:
            v = "regressed"; reg += 1
            if nf < of * 0.8: regs.append(name)
        row = f"{name}\t{mode}\t{len(o)}\t{len(n)}\t{of}\t{nf}\t{len(set(TIME.findall(o)))}\t{len(set(TIME.findall(n)))}\t{recs}\t{chunks}\t{int(thin)}\t{secs:.1f}\t{v}"
        out.append(row)
        print(row, flush=True)
    summ = f"SUMMARY rows={imp+eq+reg} improved={imp} equal={eq} regressed={reg} regressed_gt20pct={len(regs)} {regs}"
    print(summ)
    if a.tsv:
        Path(a.tsv).write_text("\n".join(out + [summ]))


if __name__ == "__main__":
    main()
