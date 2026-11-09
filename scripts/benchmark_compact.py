#!/usr/bin/env python3
"""Benchmark: qwen current JSON output  vs  qwen minified-JSON output.

Same venues, same date-trimmed input, same date-rules. Arm B adds a 'minified
JSON, no whitespace' instruction. Measures emitted output tokens, parse time,
accuracy vs cached haiku, parse reliability, and whether qwen actually HONORS
compact (whitespace ratio of the raw output). Parser (_parse_object_strict /
json.loads) is identical for both — compact needs no code change.

Usage: python scripts/benchmark_compact.py [--venues "A,B"] [--runs 2]
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
from src.extract import _make_batch_prompt, _parse_object_strict, _clean_text, _date_trim
from src.llm import run_llm

DEFAULT_VENUES = ["Wigmore Hall", "Kings Place", "St Martin-in-the-Fields",
                  "Royal Academy of Music", "Two Temple Place", "Southwark Cathedral"]
SCORE_FIELDS = ("title", "date", "time", "category", "price", "description")
MINIFY = ("\n\nOUTPUT FORMAT: minified JSON only — no spaces after : or , no "
          "newlines, no indentation. One single line. Still valid JSON.")


def est_tok(tok, s): return len(tok(s)["input_ids"])
def norm(t): return re.sub(r"[^a-z0-9а-я ]", "", str(t).lower()).strip()
def tset(evs): return {norm(e.get("title")) for e in evs if norm(e.get("title"))}
def jacc(a, b):
    if not a and not b: return 1.0
    if not a or not b: return 0.0
    return len(a & b) / len(a | b)
def comp(evs):
    h = t = 0
    for e in evs:
        for f in SCORE_FIELDS:
            t += 1
            if e.get(f) not in (None, "", "null"): h += 1
    return h / t if t else 0.0
def ws_ratio(s): return (s.count(" ") + s.count("\n")) / len(s) if s else 0.0
def href(name):
    row = _conn.execute("SELECT events_json FROM extractions WHERE site_name=? AND model='haiku'", (name,)).fetchone()
    return json.loads(row["events_json"]) if row else []


async def call(prompt, model):
    t0 = time.time()
    raw = await run_llm(prompt, backend="ollama", model=model, timeout=420, retries=2)
    return raw, time.time() - t0


def evparse(raw, name):
    try:
        evs = _parse_object_strict(raw).get(name, []) or []
        return (evs if isinstance(evs, list) else []), True
    except Exception:
        return [], False


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venues", default=",".join(DEFAULT_VENUES))
    ap.add_argument("--ollama-model", default=os.environ.get("OLLAMA_MODEL", "qwen3:8b"))
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()
    venues = [v.strip() for v in args.venues.split(",")]
    today = date.today().isoformat()
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained("Qwen/Qwen3-8B")
    sites = {s["name"]: s for s in _load_sites()}
    rows = {"PRETTY": [], "COMPACT": []}
    fails = {"PRETTY": 0, "COMPACT": 0}

    for name in venues:
        site = sites.get(name)
        html = site and get_cached_html_any(site["url"])
        if not html:
            print(f"\n## {name}: no cached html — skip"); continue
        sd = _html_to_site_data(site, html)
        text = _date_trim(_clean_text(sd["text"]), max_chars=6000)
        ref = tset(href(name))
        base = _make_batch_prompt([{**sd, "text": text}], today)
        prompts = {"PRETTY": base, "COMPACT": base + MINIFY}

        for r in range(args.runs):
            res = {}
            for arm in ("PRETTY", "COMPACT"):
                raw, dt = await call(prompts[arm], args.ollama_model)
                evs, ok = evparse(raw, name)
                if not ok: fails[arm] += 1
                res[arm] = (raw, dt, evs, ok)
            ep, ec = tset(res["PRETTY"][2]), tset(res["COMPACT"][2])
            for arm in ("PRETTY", "COMPACT"):
                raw, dt, evs, ok = res[arm]
                rows[arm].append((est_tok(tok, raw), dt, len(evs), comp(evs),
                                  jacc(tset(evs), ref), jacc(ep, ec), int(ok), ws_ratio(raw)))
            print(f"\n## {name}" + (f" run{r+1}" if args.runs > 1 else "") + f"   (haiku ref {len(ref)})")
            for arm in ("PRETTY", "COMPACT"):
                t, dt, ev, ok = rows[arm][-1][0], rows[arm][-1][1], rows[arm][-1][2], rows[arm][-1][6]
                fc, jr, wr = rows[arm][-1][3], rows[arm][-1][4], rows[arm][-1][7]
                print(f"  {arm:<8} out {t:>5}tok  {dt:>6.1f}s  events {ev:>3}  fields {fc*100:>3.0f}%  J(ref) {jr:.2f}  ws {wr*100:>2.0f}%  ok={bool(ok)}")

    def avg(a, i): return sum(r[i] for r in a) / len(a) if a else 0.0
    if rows["PRETTY"]:
        n = len(rows["PRETTY"])
        print("\n" + "=" * 74 + f"\nAGGREGATE (means over {n} runs)")
        for arm in ("PRETTY", "COMPACT"):
            a = rows[arm]
            print(f"  {arm:<8} out {avg(a,0):>6.0f}tok  parse {avg(a,1):>6.1f}s  events {avg(a,2):>4.1f}  "
                  f"fields {avg(a,3)*100:>3.0f}%  J(ref) {avg(a,4):.2f}  whitespace {avg(a,7)*100:>2.0f}%")
        op, oc = avg(rows["PRETTY"], 0), avg(rows["COMPACT"], 0)
        pp, pc = avg(rows["PRETTY"], 1), avg(rows["COMPACT"], 1)
        print(f"\n  output tokens COMPACT/PRETTY = {oc/op:.2f}x   ({100-100*oc/op:.0f}% fewer)")
        print(f"  parse time    COMPACT/PRETTY = {pc/pp:.2f}x")
        print(f"  agreement J(PRETTY,COMPACT) mean = {avg(rows['COMPACT'],5):.2f}")
        print(f"  reliability: PRETTY parse-fails {fails['PRETTY']}/{n}, COMPACT parse-fails {fails['COMPACT']}/{n}")


if __name__ == "__main__":
    asyncio.run(main())
