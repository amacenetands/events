"""Drift vs a stored baseline: per-field F1 drops and per-event regressions."""
from __future__ import annotations

from typing import Any

from .run import FIELDS, EvalReport

__all__ = ["F1_TOLERANCE", "baseline_dict", "compare", "summary_line"]

F1_TOLERANCE = 0.02


def baseline_dict(rep: EvalReport | dict) -> dict[str, Any]:
    """The parts of a report a baseline keeps (scores, coverage, per-event wrong fields)."""
    d = rep.to_dict() if isinstance(rep, EvalReport) else rep
    return {"fields": d["fields"], "by_source": d["by_source"], "coverage": d["coverage"], "events": d["events"],
            "extras": sorted(d.get("extras", []))}


def _f1(block: dict, f: str) -> float | None:
    c = block.get(f)
    return None if c is None else float(c["f1"])


def compare(current: EvalReport | dict, baseline: dict) -> list[str]:
    """Regression lines (empty = no drift): any overall or per-source field F1 that dropped by
    more than F1_TOLERANCE, any gold event found before and missing now, and any field of a gold
    event that was correct in the baseline and is wrong now, and any pipeline event outside the
    gold set that the baseline did not have (a new spurious event). Improvements are not reported."""
    cur = baseline_dict(current)
    out: list[str] = []
    for f in FIELDS:
        b, c = _f1(baseline.get("fields", {}), f), _f1(cur["fields"], f)
        if b is not None and c is not None and b - c > F1_TOLERANCE:
            out.append(f"F1 {f}: {b:.3f} -> {c:.3f}")
    for src, block in sorted(baseline.get("by_source", {}).items()):
        now = cur["by_source"].get(src)
        if now is None:
            continue
        for f in FIELDS:
            b, c = _f1(block, f), _f1(now, f)
            if b is not None and c is not None and b - c > F1_TOLERANCE:
                out.append(f"F1 {src}.{f}: {b:.3f} -> {c:.3f}")
    for eid, was in sorted(baseline.get("events", {}).items()):
        now = cur["events"].get(eid)
        if now is None:                       # gold event removed/renamed: not a regression
            continue
        if was["found"] and not now["found"]:
            out.append(f"event lost: {eid}")
            continue
        newly = [f for f in now["wrong"] if f not in was["wrong"]]
        if was["found"] and newly:
            out.append(f"event regressed: {eid}: {', '.join(newly)}")
    old_extra = set(baseline.get("extras", []))
    venues = {e.split("|", 1)[0] for e in cur["events"]}       # partial runs: only their venues
    for x in sorted(set(cur["extras"]) - old_extra):
        if x.split("|", 1)[0] in venues:
            out.append(f"new extra event: {x}")
    return out


def summary_line(rep: EvalReport | dict) -> str:
    """One line for the crawl log: coverage, per-field F1, mismatch count."""
    d = rep.to_dict() if isinstance(rep, EvalReport) else rep
    cov = d["coverage"]
    f1 = " ".join(f"{f}={d['fields'][f]['f1']:.2f}" for f in FIELDS)
    n_mis = len(d.get("mismatches", []))
    return (f"eval: {cov['found']}/{cov['expected']} gold events found ({cov.get('found_wrong_date', 0)} on a "
            f"wrong date), {cov['missed']} missed, {cov['extra']} extra | F1 {f1} | {n_mis} mismatches")
