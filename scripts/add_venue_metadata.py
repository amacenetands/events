#!/usr/bin/env python3
"""Add map + type metadata fields to all venues in sites.yaml."""
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

import yaml

SITES_YAML = Path(__file__).parent.parent / "sites.yaml"


EXPLICIT_TYPES = {
    "Barbican": "arts-centre",
    "Southbank Centre": "arts-centre",
    "ICA": "arts-centre",
    "Battersea Arts Centre": "arts-centre",
    "Oxford House": "arts-centre",
    "Ealing Arts and Leisure": "arts-centre",
    "Somerset House": "arts-centre",
    "Discover Story Centre": "arts-centre",
    "White Cube": "gallery",
    "Estorick Collection": "gallery",
    "Pitzhanger Manor": "gallery",
    "Hogarth's House": "museum",
    "Wallace Collection": "museum",
    "Churchill War Rooms": "museum",
    "Kenwood House": "museum",
    "Ham House": "museum",
    "Fulham Palace": "museum",
    "Garden Museum": "museum",
    "Young V&A": "museum",
    "V&A East": "museum",
    "Art Play London": "aggregator",
    "London for Toddlers": "aggregator",
    "Chiswick Calendar": "aggregator",
    "Greenwich+Docklands Festival": "festival",
    "Underbelly Festival": "festival",
    "Ealing Summer Festivals": "festival",
    "Hanwell Hootie": "festival",
    "Bedford Park Festival": "festival",
    "Chiswick Festival": "festival",
    "Café Oto": "concert-hall",
    "Hackney Empire": "theatre",
    "LSE Public Events": "university",
    "Westminster Abbey": "church",
}


def infer_type(name: str, category_hint: str, pipeline) -> str:
    if name in EXPLICIT_TYPES:
        return EXPLICIT_TYPES[name]

    n = name.lower()
    pipelines = [pipeline] if isinstance(pipeline, str) else (pipeline or [])

    if any(x in n for x in ["time out", "visit london", "primary times",
                              "mumsnet", "netmums", "timeout"]):
        return "aggregator"
    if any(x in n for x in ["university", "ucl", "goldsmiths", "birkbeck", "imperial college",
                              "westminster", "queen mary", "soas", "king's college"]):
        return "university"
    if "museum" in n:
        return "museum"
    if any(x in n for x in ["opera house", "opera"]):
        return "opera-house"
    if any(x in n for x in ["theatre", "theater", "little angel"]):
        return "theatre"
    if any(x in n for x in ["gallery", "galleries", "tate", "saatchi", "courtauld",
                              "whitechapel", "serpentine", "national portrait", "royal academy",
                              "institute of contemporary"]):
        return "gallery"
    if "farm" in n:
        return "farm"
    if "garden" in n:
        return "garden"
    if "park" in n and category_hint != "concert":
        return "park"
    if any(x in n for x in ["church", "chapel", "cathedral", "st martin", "st paul",
                              "st michael", "st nicholas", "st james", "st george",
                              "st peter", "st john", "st anne"]):
        return "church"
    if any(x in n for x in ["arts centre", "arts center", "battersea arts"]):
        return "arts-centre"
    if any(x in n for x in ["concert hall", "wigmore", "cadogan", "kings place",
                              "cadogan hall", "roundhouse"]):
        return "concert-hall"
    if "hall" in n and category_hint == "concert":
        return "concert-hall"
    if category_hint == "concert":
        return "concert-hall"
    if any(x in n for x in ["cinema", "bfi", "film"]):
        return "cinema"
    if "library" in n:
        return "library"
    if "market" in n:
        return "market"
    if any(n == x or n.startswith(x + " ") or (" " + x) in n for x in ["pub", "bar", "tavern"]):
        return "pub"
    if category_hint == "theatre":
        return "theatre"
    if category_hint == "exhibition":
        return "gallery"
    if category_hint in ("kids", "other") and "museum" in pipelines:
        return "museum"
    return "venue"


def map_url(name: str) -> str:
    q = quote_plus(f"{name} London")
    return f"https://maps.google.com/?q={q}"


def patch_yaml(dry_run: bool = False) -> None:
    text = SITES_YAML.read_text()
    data = yaml.safe_load(text)
    sites = data.get("html_sites", [])

    # Build lookup: name → (map, type)
    meta: dict[str, tuple[str, str]] = {}
    for s in sites:
        name = s["name"]
        t = infer_type(name, s.get("category_hint", "other"), s.get("pipeline"))
        meta[name] = (map_url(name), t)

    lines = text.splitlines()
    output = []
    i = 0
    patched = 0

    while i < len(lines):
        line = lines[i]
        output.append(line)

        name_m = re.match(r"(\s*)- name:\s*[\"']?(.+?)[\"']?\s*$", line)
        if name_m:
            indent = name_m.group(1) + "  "
            venue_name = name_m.group(2)

            if venue_name in meta:
                # Scan forward to check if map/type already exist in this block
                j = i + 1
                block_has_map = False
                block_has_type = False
                while j < len(lines):
                    next_line = lines[j]
                    if re.match(r"\s*- name:", next_line):
                        break
                    if re.match(r"\s*map:", next_line):
                        block_has_map = True
                    if re.match(r"\s*type:", next_line):
                        block_has_type = True
                    j += 1

                m_url, m_type = meta[venue_name]
                new_lines = []
                if not block_has_map:
                    new_lines.append(f'{indent}map: "{m_url}"')
                if not block_has_type:
                    new_lines.append(f'{indent}type: "{m_type}"')

                if new_lines:
                    if dry_run:
                        print(f"\n[DRY] {venue_name}: +map={m_url} +type={m_type}")
                    else:
                        output.extend(new_lines)
                    patched += 1

        i += 1

    if not dry_run:
        SITES_YAML.write_text("\n".join(output) + "\n")
        print(f"Patched {patched} venues in sites.yaml.")
    else:
        print(f"\n[DRY-RUN] Would patch {patched} venues.")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    patch_yaml(dry_run=dry)
