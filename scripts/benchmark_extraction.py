#!/usr/bin/env python3
"""Benchmark Claude Haiku vs Ollama qwen3:8b on real event extraction.

Usage:
    python scripts/benchmark_extraction.py
    python scripts/benchmark_extraction.py --sites "Wigmore Hall,Barbican"
    python scripts/benchmark_extraction.py --ollama-model qwen3:14b
"""
import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sources.html_sites import _fetch_markdown_simple, _load_sites
from src.extract import _make_batch_prompt, _parse_object, _build_events, _clean_text

DEFAULT_SITES = [
    "Wigmore Hall",
    "Barbican",
    "Cadogan Hall",
    "National Gallery",
    "Royal Academy",
]

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
OLLAMA_TIMEOUT = 900.0


async def fetch_sites(site_names: list[str]) -> list[dict]:
    all_sites = _load_sites()
    targets = [s for s in all_sites if s["name"] in site_names and not s.get("js_required")]
    if not targets:
        print(f"No non-JS sites found matching: {site_names}")
        sys.exit(1)
    sem = asyncio.Semaphore(5)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[
            _fetch_markdown_simple(client, s, sem) for s in targets
        ])
    fetched = [r for r in results if r]
    print(f"Fetched {len(fetched)}/{len(targets)} sites")
    return fetched


def run_claude(prompt: str) -> tuple[str, float]:
    t0 = time.time()
    result = subprocess.run(
        ["claude", "-p", prompt, "--model", CLAUDE_MODEL],
        capture_output=True, text=True, timeout=300,
    )
    return result.stdout.strip(), time.time() - t0


async def run_ollama(prompt: str, model: str, url: str = "http://localhost:11434",
                     num_ctx: int = 32768) -> tuple[str, float]:
    t0 = time.time()
    payload = {
        "model": model,
        "prompt": "/no_think\n" + prompt,
        "stream": True,
        "options": {"temperature": 0, "num_ctx": num_ctx},
    }
    chunks: list[str] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=900.0)) as client:
        async with client.stream("POST", f"{url}/api/generate", json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if line:
                    try:
                        chunks.append(json.loads(line).get("response", ""))
                    except json.JSONDecodeError:
                        pass
    raw = "".join(chunks)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    return raw, time.time() - t0


def score(raw: str, site_names: list[str]) -> dict:
    parsed = _parse_object(raw)
    events_per_site = {n: parsed.get(n, []) for n in site_names}
    total = sum(len(v) for v in events_per_site.values() if isinstance(v, list))
    sites_hit = sum(1 for v in events_per_site.values() if isinstance(v, list) and v)
    field_hits = 0
    field_total = 0
    for evts in events_per_site.values():
        for e in (evts if isinstance(evts, list) else []):
            for f in ("title", "venue", "date", "url"):
                field_total += 1
                if e.get(f):
                    field_hits += 1
    field_pct = f"{100*field_hits//field_total}%" if field_total else "n/a"
    return {
        "json_valid": bool(parsed),
        "sites_with_events": f"{sites_hit}/{len(site_names)}",
        "total_events": total,
        "field_completeness": field_pct,
    }


def print_event_sample(label: str, parsed: dict, site_names: list[str], n: int = 3) -> None:
    print(f"\n  {label}:")
    for name in site_names:
        evts = parsed.get(name, [])
        if not isinstance(evts, list):
            evts = []
        sample = [e.get("title", "?")[:50] for e in evts[:n]]
        print(f"    {name} ({len(evts)} events): {sample}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sites", default=",".join(DEFAULT_SITES),
                        help="Comma-separated site names to test")
    parser.add_argument("--ollama-model", default="qwen3:8b")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--skip-claude", action="store_true")
    parser.add_argument("--skip-ollama", action="store_true")
    args = parser.parse_args()

    site_names = [s.strip() for s in args.sites.split(",")]

    print(f"Fetching {len(site_names)} sites...")
    sites_data = await fetch_sites(site_names)
    actual_names = [s["name"] for s in sites_data]

    prompt = _make_batch_prompt(sites_data, date.today().isoformat())
    prompt_chars = len(prompt)
    prompt_tokens_est = prompt_chars // 4
    print(f"Prompt: {prompt_chars:,} chars (~{prompt_tokens_est:,} tokens)")
    print(f"Sites:  {actual_names}")

    results = {}

    # Run sequentially: Ollama first (avoids resource contention with Claude subprocess)
    if not args.skip_ollama:
        print(f"\n[1] Ollama {args.ollama_model}...")
        try:
            raw_o, t_o = await run_ollama(prompt, args.ollama_model, args.ollama_url)
            so = score(raw_o, actual_names)
            results["ollama"] = {"raw": raw_o, "time": t_o, "score": so}
            print(f"  Time:  {t_o:.1f}s")
            print(f"  Score: {so}")
        except Exception as e:
            import traceback
            print(f"  ERROR: {e}")
            traceback.print_exc()

    if not args.skip_claude:
        print(f"\n[2] Claude Haiku ({CLAUDE_MODEL})...")
        raw_c, t_c = run_claude(prompt)
        sc = score(raw_c, actual_names)
        results["claude"] = {"raw": raw_c, "time": t_c, "score": sc}
        print(f"  Time:  {t_c:.1f}s")
        print(f"  Score: {sc}")

    print("\n=== Event sample comparison ===")
    for label, res in results.items():
        parsed = _parse_object(res["raw"])
        print_event_sample(label, parsed, actual_names)

    if len(results) == 2:
        print("\n=== Summary ===")
        c_s = results["claude"]["score"]
        o_s = results["ollama"]["score"]
        ratio = results["ollama"]["time"] / results["claude"]["time"] if results["claude"]["time"] else 0
        print(f"  Claude Haiku ({CLAUDE_MODEL}): {results['claude']['time']:.1f}s  {c_s}")
        print(f"  Ollama {args.ollama_model}: {results['ollama']['time']:.1f}s  {o_s}  ({ratio:.1f}x time)")


if __name__ == "__main__":
    asyncio.run(main())
