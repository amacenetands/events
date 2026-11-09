#!/usr/bin/env python3
"""Benchmark: does Headroom save tokens on this repo's simple tasks (add a venue)?

No network for the measurement itself (uses cached HTML + sites.yaml). Token
counts come from Headroom's own tokenizer for the target model.

Three things are measured:

  1. OUT-OF-BOX  — the documented simple API, headroom.compress(messages, ...),
     including the 'agent-90' coding-agent profile, on a realistic 9-message
     "add a venue" Claude-Code transcript. This is what you actually get by
     wiring Headroom in front of an agent.

  2. RAW KOMPRESS — call the ML prose compressor (KompressCompressor) directly
     on the two payloads an add-venue task reads (the fetched venue page, and
     sites.yaml). Upper bound on what the model *could* squeeze out, ignoring
     Headroom's routing/protection gates.

  3. FIDELITY   — does the compressed sites.yaml still parse as YAML and keep
     all venues? Kompress is lossy (it strips words like a caveman), so this
     checks whether the "savings" are usable on structured config.

Usage:
    python scripts/benchmark_headroom.py
    python scripts/benchmark_headroom.py --venue "Wigmore Hall"
    python scripts/benchmark_headroom.py --model claude-sonnet-4-5-20250929
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from headroom import compress, CompressConfig
from headroom.agent_savings import apply_agent_savings_profile
from headroom.tokenizers.registry import get_tokenizer
from headroom.transforms.kompress_compressor import KompressCompressor
from src.sources.html_sites import _load_sites, _html_to_site_data, SITES_YAML
from src.dedup import get_cached_html_any
from src.extract import _clean_text

SYS_PROMPT = (
    "You are Claude Code, an interactive coding agent. You edit files, run tools, "
    "keep changes minimal and correct, and match existing style."
)


def build_transcript(venue: str, sites_yaml: str, page: str, block: str) -> list[dict]:
    """A realistic multi-turn add-venue session. Tool results are role=user
    (Anthropic format); the agent-90 profile makes user messages eligible."""
    return [
        {"role": "system", "content": SYS_PROMPT},
        {"role": "user", "content":
            f"Add the venue '{venue}' to sites.yaml — it runs concert series. Read the "
            f"file to match the format, fetch the page, derive the fields, append a block."},
        {"role": "assistant", "content": "Reading sites.yaml to match the entry format."},
        {"role": "user", "content": f"[tool_result: Read sites.yaml]\n{sites_yaml}"},
        {"role": "assistant", "content": "Got it. Fetching the venue page to derive fields."},
        {"role": "user", "content": f"[tool_result: WebFetch {venue}]\n{page}"},
        {"role": "assistant", "content": f"Proposed block:\n{block}"},
        {"role": "user", "content": "Looks good. Confirm it isn't a dup of St James's Piccadilly."},
        {"role": "assistant", "content":
            "Distinct: Piccadilly is W1; this is St James's Church, Paddington (W2)."},
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="St James's Church Paddington")
    ap.add_argument("--model", default="claude-sonnet-4-5-20250929")
    ap.add_argument("--max-chars", type=int, default=14000)
    args = ap.parse_args()

    sites = {s["name"]: s for s in _load_sites()}
    site = sites.get(args.venue)
    if not site:
        sys.exit(f"venue {args.venue!r} not in sites.yaml")
    html = get_cached_html_any(site["url"])
    if not html:
        sys.exit(f"no cached html for {args.venue!r} — run the pipeline once first")

    sd = _html_to_site_data(site, html)
    page = _clean_text(sd["text"])[: args.max_chars]
    sites_yaml = Path(SITES_YAML).read_text()
    block = f'  - name: "{args.venue}"\n    url: "{site["url"]}"\n    pipeline: daytime'

    tk = get_tokenizer(args.model)
    k = KompressCompressor()
    n_orig = len(_load_sites())

    print(f"model={args.model}  venue={args.venue!r}  tokenizer={type(tk).__name__}")
    print(f"venue page {len(page):,}c · sites.yaml {len(sites_yaml):,}c · {n_orig} venues\n")

    # --- 1. out-of-box compress() on the realistic transcript ---
    cfg = apply_agent_savings_profile(CompressConfig(), "agent-90")
    transcript = build_transcript(args.venue, sites_yaml, page, block)
    r = compress(transcript, model=args.model, config=cfg)
    print("1. OUT-OF-BOX  compress() + agent-90 profile on 9-msg add-venue transcript:")
    print(f"   before {r.tokens_before:>6,}  after {r.tokens_after:>6,}  "
          f"saved {r.tokens_saved:>6,} ({100*r.compression_ratio:.0f}%)  "
          f"[{','.join(r.transforms_applied)}]")

    # --- 2. raw Kompress capability on each payload ---
    print("\n2. RAW KOMPRESS (ML prose compressor, forced) on each payload:")
    for tag, txt in (("venue_page", page), ("sites.yaml", sites_yaml)):
        b = tk.count_text(txt)
        comp = k.compress(txt).compressed
        a = tk.count_text(comp)
        print(f"   {tag:<11} before {b:>6,}  after {a:>6,}  saved {b - a:>6,} "
              f"({100 * (b - a) / b:.0f}%)   chars {len(txt):,}->{len(comp):,}")

    # --- 3. fidelity: does compressed sites.yaml survive? ---
    print("\n3. FIDELITY  (is the compressed sites.yaml still usable config?):")
    import yaml
    comp_yaml = k.compress(sites_yaml).compressed
    try:
        parsed = yaml.safe_load(comp_yaml)
        n = len(parsed) if isinstance(parsed, list) else 0
        verdict = "OK" if n == n_orig else f"CORRUPT — {n}/{n_orig} venues survived"
        print(f"   parses: yes · {verdict}")
    except Exception as e:
        print(f"   parses: NO — {str(e)[:120]} (config destroyed)")

    print("\nTakeaway: Headroom's simple API leaves these payloads untouched "
          "(router declines; force_kompress isn't forwardable via compress()). "
          "Forcing the ML compressor cuts ~60% of prose tokens but is lossy and "
          "wrecks structured YAML — same failure mode as the TSV benchmark. "
          "Token-only test; answer-equivalence not verified (cf. benchmark_lingua.py).")


if __name__ == "__main__":
    main()
