#!/usr/bin/env python3
"""Print a summary of the most recent pipeline run from cache/runtimes.jsonl."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

LOG = Path(__file__).parent.parent / "cache" / "runtimes.jsonl"


def main():
    if not LOG.exists():
        print("No runtimes.jsonl found — run the pipeline first.")
        return

    lines = LOG.read_text().splitlines()
    if not lines:
        print("runtimes.jsonl is empty.")
        return

    # Find the last "run" record and collect all records after the previous one
    run_indices = [i for i, l in enumerate(lines) if '"stage": "run"' in l]
    if not run_indices:
        # No completed run yet — show everything
        start = 0
    else:
        # Find the second-to-last run to bound the current run's records
        prev_run = run_indices[-2] if len(run_indices) >= 2 else -1
        start = prev_run + 1

    records = []
    for line in lines[start:]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not records:
        print("No records found.")
        return

    # Separate by record type
    venue_records = [r for r in records if "venue" in r]
    stage_records = [r for r in records if "stage" in r]
    run_record = next((r for r in reversed(stage_records) if r.get("stage") == "run"), None)
    fetch_record = next((r for r in stage_records if r.get("stage") == "fetch"), None)
    parse_record = next((r for r in stage_records if r.get("stage") == "parse"), None)

    ts = records[0].get("ts", "unknown")[:19].replace("T", " ")
    print(f"=== Last run timing ({ts} UTC) ===\n")

    # Per-venue breakdown — slowest first
    if venue_records:
        print("Per-venue (slowest first):")
        for r in sorted(venue_records, key=lambda x: x.get("secs", 0), reverse=True):
            hit = " [cache]" if r.get("cache_hit") else ""
            model = r.get("model", "")
            label = f"  {r['venue']:<45} {r.get('events', '?'):>3} events  {r.get('secs', 0):>6.1f}s  [{model}]{hit}"
            print(label)
        print()

    # Stage totals
    if fetch_record:
        print(f"  fetch stage : {fetch_record.get('fetched', '?')}/{fetch_record.get('total', '?')} sites  {fetch_record.get('secs', 0):.1f}s")
    if parse_record:
        print(f"  parse stage : {parse_record.get('venues', '?')} venues → {parse_record.get('events', '?')} events  {parse_record.get('secs', 0):.1f}s")
    if run_record:
        print(f"  TOTAL       : {run_record.get('secs', 0):.1f}s  ({run_record.get('name', '')})")


if __name__ == "__main__":
    main()
