#!/usr/bin/env python3
"""sites.yaml -> sites/<city>.yaml (pipeline v3, step 5). Deterministic, no LLM.

- Entries are routed to the one city whose tag rules (cities.yaml exclude/require_tags,
  explicit `city:`) take them; parked venues stay in their city's file.
- Legacy keys become v3-native ones (VenueConfig dump, defaults dropped). `map` is dropped
  where it is the derivable Google Maps search (`config.derived_map`).
- `notes:` is split into `about:` (human context, never sent to the LLM) and `hint:` (what
  the extractor needs: dates/times/prices/format/what to skip) by the rules below. Pieces
  that match both rule sets, or neither cleanly, are printed for manual review.
- `# DISABLED: <reason>` comments become `disabled_reason`; any other comment is flagged.

    python scripts/migrate_sites.py [--src sites.yaml] [--dry-run] [--force]

Kept for history: sites.yaml was deleted after the migration (sites/<city>.yaml is the source).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.config import CityConfig, VenueConfig, derived_map, from_legacy  # noqa: E402

# Default is `about`. `hint` only for imperative / page-format text the extractor acts on
# ("flag family events", "skip ...", "is_kids=true", "doors 18:00", "listing hides dates —
# follow detail links", "times on the detail page", "extract each date as a separate event").
HINT_RE = re.compile(
    r"\b(?:flag|skip|ignore|exclude|emit|extract|is_\w+|always|never)\b"
    r"|\bdoors\s*\d{1,2}(?:[:.]\d{2})?"
    r"|\b(?:dates?|times?|prices?)\s+(?:are\s+)?(?:only\s+)?on\s+(?:the\s+)?(?:detail|event|individual)"
    r"|\blisting hides\b|\bfollow detail links?\b|\bseparate (?:event|talks?|page)",
    re.I)
# Crawl-recon notes (how the page is built) — for humans/fetch config, not the extractor.
RECON_RE = re.compile(
    r"\b(?:server[- ]rendered|static(?: html| fetch)?|spa|shell|js|javascript|browser|bitrix|tilda|"
    r"nuxt|next\.js|wix|ajax|xhr|cms|cloudflare|scrapers?|blocks?|html|epoch|__\w+__|timepad|"
    r"verify js|confirmed)\b|\b(?:titles?|dates?|prices?|times?|tickets?|rooms?)\s*\+|\bw/\s|"
    r"\b(?:RU|EN|RU/EN|EN/RU)\b(?:\s*\(|\.|$)",
    re.I)
TEST_MARK = re.compile(r"\s*\[test:\s*\w+\]\s*$")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[А-ЯЁ\"'(])")
_DISABLED = re.compile(r"^\s*#\s*DISABLED:\s*(?P<r>.+?)\s*$")
_INLINE_COMMENT = re.compile(r"""^[^'"#]*\s#\s""")

KEY_ORDER = ("name", "url", "city", "tags", "weight", "type", "map", "category_hint", "must_determine_venue",
             "fetch", "extract", "merge", "follow", "adapter", "enrich", "fix", "hook", "cache_version",
             "hint", "about", "skip", "disabled", "disabled_reason")


def _pieces(notes: str) -> list[list[str]]:
    """Sentences; a leading 'Place — description' sentence is [head, tail] (split at the dash)."""
    out: list[list[str]] = []
    for i, sent in enumerate(s.strip() for s in _SENT.split(notes.strip()) if s.strip()):
        if i == 0 and " — " in sent:
            out.append([p.strip() for p in sent.split(" — ", 1)])
        else:
            out.append([sent])
    return out


def _kind(p: str) -> str:
    h, r = bool(HINT_RE.search(p)), bool(RECON_RE.search(p))
    return "hint" if h and not r else "both" if h else "about"


def split_notes(notes: str | None) -> tuple[str | None, str | None, list[str]]:
    """(about, hint, review flags). Every word of `notes` lands in exactly one of about/hint."""
    if not notes:
        return None, None, []
    text = str(notes)
    m = TEST_MARK.search(text)
    mark = m.group(0).strip() if m else ""
    if m:
        text = text[:m.start()]
    about: list[str] = []
    hint: list[str] = []
    flags: list[str] = []
    for parts in _pieces(text):
        kinds = [_kind(p) for p in parts]
        if "hint" not in kinds:                       # keep the sentence whole
            parts, kinds = [" — ".join(parts)], ["both" if "both" in kinds else "about"]
        for p, k in zip(parts, kinds):
            (hint if k == "hint" else about).append(p)
            if k == "both":
                flags.append(f"recon+hint→about: {p!r}")
    if mark:
        about.append(mark)
    return " ".join(about) or None, " ".join(hint) or None, flags


def comments_by_entry(text: str) -> tuple[dict[int, str], list[tuple[int, str]]]:
    """({entry index: DISABLED reason}, [(entry index, other comment line)]) from the raw file."""
    reasons: dict[int, str] = {}
    other: list[tuple[int, str]] = []
    idx = -1
    for line in text.splitlines():
        if line.startswith("- name:") or line.startswith("- {"):
            idx += 1
        m = _DISABLED.match(line)
        if m and idx >= 0:
            reasons[idx] = m.group("r")
        elif idx >= 0 and (line.lstrip().startswith("#") or _INLINE_COMMENT.match(line)):
            other.append((idx, line.strip()))
    return reasons, other


def _city_of(v: VenueConfig, cities: list[CityConfig]) -> CityConfig:
    """The legacy shared-file rule: explicit city, else the city whose require_tags the
    venue carries, else the default city."""
    if v.city is not None:
        hits = [c for c in cities if c.name == v.city.lower()]
    else:
        hits = [c for c in cities if set(v.tags) & set(c.require_tags)] or \
            [c for c in cities if c.name == config.DEFAULT_CITY]
    if len(hits) != 1:
        raise ValueError(f"venue {v.name!r} (tags {v.tags}) matches {len(hits)} cities: {[c.name for c in hits]}")
    return hits[0]


def native_entry(v: VenueConfig, city: CityConfig, reason: str | None) -> tuple[dict[str, Any], list[str]]:
    d = v.model_dump(exclude_defaults=True, exclude_none=True)
    about, hint, flags = split_notes(v.about)
    d.pop("about", None)
    d.pop("hint", None)
    if v.hint:                       # already native: keep, append any split-off hint
        hint = " ".join(filter(None, (v.hint, hint)))
    if hint:
        d["hint"] = hint
    if about:
        d["about"] = about
    if d.get("map") and d["map"] == derived_map(v.name, city.display_name):
        del d["map"]
    if reason and v.disabled and not v.disabled_reason:
        d["disabled_reason"] = reason
    order = {k: i for i, k in enumerate(KEY_ORDER)}
    return dict(sorted(d.items(), key=lambda kv: order.get(kv[0], len(order)))), flags


def migrate(src: Path) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """({city: native entries in file order}, review flags)."""
    raw = src.read_text()
    entries = config._read_entries(src)
    reasons, other = comments_by_entry(raw)
    cities = list(config.load_cities().values())
    out: dict[str, list[dict[str, Any]]] = {c.name: [] for c in cities}
    flags: list[str] = []
    for i, d in enumerate(entries):
        v = from_legacy(d)
        c = _city_of(v, cities)
        e, f = native_entry(v, c, reasons.get(i))
        out[c.name].append(e)
        flags += [f"{v.name}: {x}" for x in f]
        if reasons.get(i) and not v.disabled:
            flags.append(f"{v.name}: DISABLED comment on an enabled venue (dropped): {reasons[i]!r}")
    by_name = {e["name"]: e for es in out.values() for e in es}
    for i, line in other:                         # inline comments -> appended to about
        note = line.split("#", 1)[1].strip()
        e = by_name[entries[i]["name"]]
        e["about"] = " ".join(filter(None, (e.get("about"), f"({note})")))
        flags.append(f"{entries[i].get('name')}: comment moved to about: {note!r}")
    return {k: v for k, v in out.items() if v}, flags


def dump(entries: list[dict[str, Any]], city: CityConfig) -> str:
    head = (f"# {city.label} venues (pipeline v3). Generated from sites.yaml by scripts/migrate_sites.py;\n"
            f"# edit here from now on. Schema: src/config.py VenueConfig. `hint` is sent to the LLM, `about` never.\n")
    body = yaml.safe_dump({"venues": entries}, allow_unicode=True, sort_keys=False, width=110)
    return head + body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, default=config.ROOT / "sites.yaml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="overwrite existing sites/<city>.yaml")
    a = ap.parse_args(argv)
    out, flags = migrate(a.src)
    exists = [n for n in out if config.get_city(n).sites_path.exists()]
    if exists and not a.dry_run and not a.force:
        print(f"refusing to overwrite {[str(config.get_city(n).sites) for n in exists]} (use --force)")
        return 1
    for name, entries in out.items():
        c = config.get_city(name)
        path = c.sites_path
        n_hint = sum(1 for e in entries if e.get("hint"))
        print(f"{name}: {len(entries)} venues -> {c.sites} ({n_hint} with hint)")
        if not a.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dump(entries, c))
    print(f"\n{len(flags)} flag(s) for manual review:")
    for f in flags:
        print(f"  {f}")
    config.clear_cache()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
