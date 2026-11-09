#!/usr/bin/env python3
"""Benchmark: qwen parse  vs  LLMLingua-2 compress -> qwen parse.

Uses RAW HTML already in the scrape cache (no network fetch). For each venue:
  Arm A (baseline): current pipeline prompt (clean + date_trim) -> qwen3:8b
  Arm B (lingua):   clean text -> LLMLingua-2 compress -> qwen3:8b

Reports per venue and aggregate: prompt size, compress time, parse time,
events extracted, field completeness, and title-overlap vs the cached haiku
extraction (used as an accuracy reference).

Usage:
    python scripts/benchmark_lingua.py
    python scripts/benchmark_lingua.py --venues "Wigmore Hall,Kings Place"
    python scripts/benchmark_lingua.py --rate 0.4 --ollama-model qwen3:8b
    python scripts/benchmark_lingua.py --lingua-model microsoft/llmlingua-2-xlm-roberta-large-meetingbank
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

os.environ.setdefault("LLM_BACKEND", "ollama")
os.environ.setdefault("OLLAMA_CTX", "8192")

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sources.html_sites import _load_sites, _html_to_site_data
from src.dedup import get_cached_html_any
from src.store import _conn
from src.extract import (
    _make_batch_prompt, _parse_object_strict, _build_events,
    _clean_text, _date_trim,
    _EVENT_SCHEMA, _VENUE_URL_FIELDS, _DATE_RULES, _BATCH_PROMPT,
)
from src.llm import run_llm

DEFAULT_VENUES = [
    "Wigmore Hall",
    "Kings Place",
    "St Martin-in-the-Fields",
    "Royal Academy of Music",
    "Two Temple Place",
    "Southwark Cathedral",
]

FIELDS = ("title", "date", "time", "category", "price", "description")


def est_tokens(s: str) -> int:
    """Cheap token estimate (chars/4). Consistent across arms => fair ratio."""
    return len(s) // 4


def norm_title(t) -> str:
    return re.sub(r"[^a-z0-9а-я ]", "", str(t).lower()).strip()


def title_set(events: list[dict]) -> set[str]:
    return {norm_title(e.get("title")) for e in events if norm_title(e.get("title"))}


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def field_completeness(events: list[dict]) -> float:
    hits = tot = 0
    for e in events:
        for f in FIELDS:
            tot += 1
            v = e.get(f)
            if v not in (None, "", "null"):
                hits += 1
    return hits / tot if tot else 0.0


def haiku_cached(name: str) -> list[dict]:
    row = _conn.execute(
        "SELECT events_json FROM extractions WHERE site_name=? AND model='haiku'", (name,)
    ).fetchone()
    return json.loads(row["events_json"]) if row else []


def build_prompt_with_text(site: dict, text: str, today: str) -> str:
    """Replicate _make_batch_prompt for ONE site but inject `text` verbatim
    (no date_trim) — used for the lingua arm so LLMLingua is the only compressor."""
    parts = [f"=== SITE: {site['name']} | URL: {site['url']} | Category hint: {site.get('category_hint','other')} ==="]
    if site.get("notes"):
        parts.append(f"[Note: {site['notes']}]")
    parts.append(text)
    parts.append("")
    exhibition_hint = "\n- Skip exhibitions if the site has a recent cache (noted above)."
    return _BATCH_PROMPT.format(
        schema=_EVENT_SCHEMA,
        date_rules=_DATE_RULES.format(today=today, exhibition_hint=exhibition_hint),
        sites="\n".join(parts),
    )


async def parse(prompt: str, name: str, model: str) -> tuple[list[dict], float]:
    t0 = time.time()
    raw = await run_llm(prompt, backend="ollama", model=model, timeout=420, retries=2)
    dt = time.time() - t0
    try:
        evs = _parse_object_strict(raw).get(name, [])
        if not isinstance(evs, list):
            evs = []
    except Exception as e:
        print(f"    !! parse error: {str(e)[:80]}")
        evs = []
    return evs, dt


def fmt_row(label, ptok, ctime, ptime, nev, fc, jref, jab):
    return (f"  {label:<10} prompt~{ptok:>5}tok  compress {ctime:>5.1f}s  parse {ptime:>6.1f}s  "
            f"events {nev:>3}  fields {fc*100:>3.0f}%  J(ref) {jref:.2f}  J(A,B) {jab:.2f}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venues", default=",".join(DEFAULT_VENUES))
    ap.add_argument("--rate", type=float, default=0.5, help="LLMLingua keep-rate (fraction of tokens retained)")
    ap.add_argument("--ollama-model", default=os.environ.get("OLLAMA_MODEL", "qwen3:8b"))
    ap.add_argument("--lingua-model", default="microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank")
    ap.add_argument("--max-chars", type=int, default=12000, help="cap on cleaned text fed to LLMLingua")
    ap.add_argument("--source", choices=["clean", "date_trim"], default="clean",
                    help="text fed to LLMLingua for arm B. 'clean'=full cleaned page; "
                         "'date_trim'=the SAME date-trimmed text arm A sees (equal-budget test)")
    args = ap.parse_args()

    venues = [v.strip() for v in args.venues.split(",")]
    today = date.today().isoformat()

    print(f"Loading LLMLingua-2 ({args.lingua_model}) ...", flush=True)
    from llmlingua import PromptCompressor
    t_load = time.time()
    compressor = PromptCompressor(model_name=args.lingua_model, use_llmlingua2=True, device_map="cpu")
    print(f"  loaded in {time.time()-t_load:.1f}s", flush=True)

    # warmup (excludes model-download / first-call cost from timings)
    compressor.compress_prompt("warmup text for the compressor to initialise.", rate=0.5)

    sites = {s["name"]: s for s in _load_sites()}
    agg = {"A": [], "B": []}

    for name in venues:
        site = sites.get(name)
        if not site:
            print(f"\n## {name}: not in sites.yaml — skip"); continue
        html = get_cached_html_any(site["url"])
        if not html:
            print(f"\n## {name}: no cached html — skip"); continue

        sd = _html_to_site_data(site, html)
        ref = title_set(haiku_cached(name))

        # ---- Arm A: baseline pipeline prompt (clean + date_trim) ----
        prompt_a = _make_batch_prompt([sd], today)
        evs_a, t_pa = await parse(prompt_a, name, args.ollama_model)

        # ---- Arm B: source text -> LLMLingua-2 -> qwen ----
        if args.source == "date_trim":
            cleaned = _date_trim(_clean_text(sd["text"]), max_chars=6000)
        else:
            cleaned = _clean_text(sd["text"])[: args.max_chars]
        t0 = time.time()
        comp = compressor.compress_prompt(cleaned, rate=args.rate, force_tokens=["\n", ".", ":", "-"])
        t_comp = time.time() - t0
        compressed_text = comp["compressed_prompt"]
        prompt_b = build_prompt_with_text(site, compressed_text, today)
        evs_b, t_pb = await parse(prompt_b, name, args.ollama_model)

        ta = title_set(evs_a); tb = title_set(evs_b)
        rowA = ("A/qwen", est_tokens(prompt_a), 0.0, t_pa, len(evs_a), field_completeness(evs_a), jaccard(ta, ref), jaccard(ta, tb))
        rowB = ("B/ling+qwen", est_tokens(prompt_b), t_comp, t_pb, len(evs_b), field_completeness(evs_b), jaccard(tb, ref), jaccard(ta, tb))
        agg["A"].append(rowA); agg["B"].append(rowB)

        print(f"\n## {name}   (haiku ref: {len(ref)} events, cleaned {len(cleaned):,} chars)")
        print(fmt_row(*rowA))
        print(fmt_row(*rowB))
        print(f"             LLMLingua kept {est_tokens(compressed_text)}/{est_tokens(cleaned)} tok "
              f"({100*len(compressed_text)//max(1,len(cleaned))}% chars)")

    # ---- aggregate ----
    def avg(rows, i):
        return sum(r[i] for r in rows) / len(rows) if rows else 0.0

    if agg["A"]:
        nA, nB = len(agg["A"]), len(agg["B"])
        print("\n" + "=" * 78)
        print("AGGREGATE (means over", nA, "venues)")
        for arm, rows in (("A/qwen", agg["A"]), ("B/ling+qwen", agg["B"])):
            tot_time = avg(rows, 2) + avg(rows, 3)  # compress + parse
            print(f"  {arm:<12} prompt~{avg(rows,1):>6.0f}tok  compress {avg(rows,2):>5.1f}s  "
                  f"parse {avg(rows,3):>6.1f}s  total {tot_time:>6.1f}s  "
                  f"events {avg(rows,4):>4.1f}  fields {avg(rows,5)*100:>3.0f}%  J(ref) {avg(rows,6):.2f}")
        ta_tot = avg(agg["A"], 3); tb_tot = avg(agg["B"], 2) + avg(agg["B"], 3)
        print(f"\n  speed: B/A total = {tb_tot/ta_tot:.2f}x   (>1 = lingua slower end-to-end)")
        print(f"  size:  B/A prompt = {avg(agg['B'],1)/max(1,avg(agg['A'],1)):.2f}x")
        print(f"  accuracy proxy J(A,B) mean = {avg(agg['B'],7):.2f}  (1.0 = identical titles)")


if __name__ == "__main__":
    asyncio.run(main())
