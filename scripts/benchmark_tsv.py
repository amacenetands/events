#!/usr/bin/env python3
"""Benchmark output format: qwen JSON-output  vs  qwen TSV-output.

Same venues, same date-trimmed input text, same date-rules — ONLY the output
format spec differs. Measures the real lever (emitted generation tokens) plus
parse wall-time, accuracy vs the cached haiku extraction, and — critically —
TSV parse RELIABILITY (column drift / malformed rows from an 8b model).

Usage:
    python scripts/benchmark_tsv.py
    python scripts/benchmark_tsv.py --venues "Wigmore Hall,Kings Place" --runs 2
"""
import argparse, asyncio, json, os, re, sys, time
from datetime import date
from pathlib import Path

os.environ.setdefault("LLM_BACKEND", "ollama")
os.environ.setdefault("OLLAMA_CTX", "8192")
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sources.html_sites import _load_sites, _html_to_site_data
from src.dedup import get_cached_html_any
from src.store import _conn
from src.extract import (
    _make_batch_prompt, _parse_object_strict, _clean_text, _date_trim,
    _EVENT_SCHEMA, _DATE_RULES,
)
from src.llm import run_llm

DEFAULT_VENUES = [
    "Wigmore Hall", "Kings Place", "St Martin-in-the-Fields",
    "Royal Academy of Music", "Two Temple Place", "Southwark Cathedral",
]
# schema field order — must match _EVENT_SCHEMA (date_confirmed included)
COLS = ["title", "date", "end_date", "time", "category", "is_one_off",
        "is_kids", "is_family", "date_confirmed", "description", "price"]
BOOL = {"is_one_off", "is_kids", "is_family", "date_confirmed"}
SCORE_FIELDS = ("title", "date", "time", "category", "price", "description")

_TSV_PROMPT = """\
Extract upcoming events from the venue website below. Return ONLY tab-separated values — no prose, no JSON, no markdown fences.

Output format: first a header line `# <Site Name>`, then ONE tab-separated row per event with EXACTLY these 11 columns in this order:
{cols}

Column rules:
- Separate columns with a single TAB character. Never use commas as separators.
- Leave a column EMPTY (nothing between the tabs) for null/unknown. Never write null/TBC/unknown.
- bool columns (is_one_off, is_kids, is_family, date_confirmed): write exactly true or false.
- description: one sentence, and it MUST NOT contain any tab or newline.
- No blank lines between rows; do not repeat the column-name header.
- If there are no qualifying events, output only the `# <Site Name>` line.

Field meanings:
{schema}

{date_rules}

{site}"""


def est_tok(tok, s: str) -> int:
    return len(tok(s)["input_ids"])


def norm_title(t) -> str:
    return re.sub(r"[^a-z0-9а-я ]", "", str(t).lower()).strip()


def tset(evs) -> set:
    return {norm_title(e.get("title")) for e in evs if norm_title(e.get("title"))}


def jacc(a, b) -> float:
    if not a and not b: return 1.0
    if not a or not b: return 0.0
    return len(a & b) / len(a | b)


def completeness(evs) -> float:
    h = t = 0
    for e in evs:
        for f in SCORE_FIELDS:
            t += 1
            if e.get(f) not in (None, "", "null"): h += 1
    return h / t if t else 0.0


def haiku_ref(name):
    row = _conn.execute("SELECT events_json FROM extractions WHERE site_name=? AND model='haiku'", (name,)).fetchone()
    return json.loads(row["events_json"]) if row else []


def make_tsv_prompt(site, text, today):
    block = f"=== SITE: {site['name']} | URL: {site['url']} | Category hint: {site.get('category_hint','other')} ===\n{text}"
    hint = "\n- Skip exhibitions if the site has a recent cache (noted above)."
    return _TSV_PROMPT.format(
        cols="\t".join(COLS), schema=_EVENT_SCHEMA,
        date_rules=_DATE_RULES.format(today=today, exhibition_hint=hint), site=block)


def parse_tsv(raw, name):
    """Return (events, bad_rows, ok). bad_rows = rows whose column count != 11."""
    raw = re.sub(r"```[a-z]*", "", raw).strip()
    lines = [l for l in raw.splitlines() if l.strip()]
    evs, bad = [], 0
    header_seen = False
    for l in lines:
        if l.lstrip().startswith("#"):
            header_seen = True
            continue
        # skip an echoed column-name header
        if l.split("\t")[0].strip().lower() == "title" and "date" in l.lower():
            continue
        parts = l.split("\t")
        if len(parts) != len(COLS):
            bad += 1
            if len(parts) < len(COLS):
                parts = parts + [""] * (len(COLS) - len(parts))  # best-effort pad
            else:
                parts = parts[:len(COLS)]
        d = {}
        for k, v in zip(COLS, parts):
            v = v.strip()
            if v in ("", "null"):
                d[k] = None
            elif k in BOOL:
                d[k] = True if v.lower() == "true" else (False if v.lower() == "false" else None)
            else:
                d[k] = v
        if d.get("title"):
            evs.append(d)
    return evs, bad, header_seen


async def call(prompt, model):
    t0 = time.time()
    raw = await run_llm(prompt, backend="ollama", model=model, timeout=420, retries=2)
    return raw, time.time() - t0


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venues", default=",".join(DEFAULT_VENUES))
    ap.add_argument("--ollama-model", default=os.environ.get("OLLAMA_MODEL", "qwen3:8b"))
    ap.add_argument("--runs", type=int, default=1, help="repeat per venue (reliability sampling)")
    args = ap.parse_args()
    venues = [v.strip() for v in args.venues.split(",")]
    today = date.today().isoformat()

    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained("Qwen/Qwen3-8B")

    sites = {s["name"]: s for s in _load_sites()}
    rows = {"JSON": [], "TSV": []}
    json_fail = tsv_fail = tsv_badrows = 0

    for name in venues:
        site = sites.get(name)
        html = site and get_cached_html_any(site["url"])
        if not html:
            print(f"\n## {name}: no cached html — skip"); continue
        sd = _html_to_site_data(site, html)
        text = _date_trim(_clean_text(sd["text"]), max_chars=6000)  # identical input both arms
        ref = tset(haiku_ref(name))

        for r in range(args.runs):
            # JSON arm
            pj = _make_batch_prompt([{**sd, "text": text}], today)
            rawj, tj = await call(pj, args.ollama_model)
            try:
                evj = _parse_object_strict(rawj).get(name, []) or []
                if not isinstance(evj, list): evj = []
                okj = True
            except Exception:
                evj, okj = [], False; json_fail += 1
            otj = est_tok(tok, rawj)

            # TSV arm
            pt = make_tsv_prompt(site, text, today)
            rawt, tt = await call(pt, args.ollama_model)
            evt, bad, okt = parse_tsv(rawt, name)
            if not okt: tsv_fail += 1
            tsv_badrows += bad
            ott = est_tok(tok, rawt)

            tj_set, tt_set = tset(evj), tset(evt)
            rows["JSON"].append((otj, tj, len(evj), completeness(evj), jacc(tj_set, ref), jacc(tj_set, tt_set), int(okj), 0))
            rows["TSV"].append((ott, tt, len(evt), completeness(evt), jacc(tt_set, ref), jacc(tj_set, tt_set), int(okt), bad))

            print(f"\n## {name}" + (f" run{r+1}" if args.runs > 1 else "") + f"   (haiku ref {len(ref)})")
            print(f"  JSON  out {otj:>5}tok  {tj:>6.1f}s  events {len(evj):>3}  fields {completeness(evj)*100:>3.0f}%  J(ref) {jacc(tj_set,ref):.2f}  ok={okj}")
            print(f"  TSV   out {ott:>5}tok  {tt:>6.1f}s  events {len(evt):>3}  fields {completeness(evt)*100:>3.0f}%  J(ref) {jacc(tt_set,ref):.2f}  ok={okt}  badrows={bad}")

    def avg(a, i): return sum(r[i] for r in a) / len(a) if a else 0.0
    if rows["JSON"]:
        n = len(rows["JSON"])
        print("\n" + "=" * 74 + f"\nAGGREGATE (means over {n} runs)")
        for arm in ("JSON", "TSV"):
            a = rows[arm]
            print(f"  {arm:<5} out {avg(a,0):>6.0f}tok  parse {avg(a,1):>6.1f}s  events {avg(a,2):>4.1f}  "
                  f"fields {avg(a,3)*100:>3.0f}%  J(ref) {avg(a,4):.2f}")
        ot_j, ot_t = avg(rows["JSON"], 0), avg(rows["TSV"], 0)
        pt_j, pt_t = avg(rows["JSON"], 1), avg(rows["TSV"], 1)
        print(f"\n  output tokens TSV/JSON = {ot_t/ot_j:.2f}x   ({100-100*ot_t/ot_j:.0f}% fewer)")
        print(f"  parse time    TSV/JSON = {pt_t/pt_j:.2f}x")
        print(f"  agreement J(JSON,TSV) mean = {avg(rows['TSV'],5):.2f}")
        print(f"  reliability: JSON parse-fails {json_fail}/{n}, TSV parse-fails {tsv_fail}/{n}, TSV bad rows total {tsv_badrows}")


if __name__ == "__main__":
    asyncio.run(main())
