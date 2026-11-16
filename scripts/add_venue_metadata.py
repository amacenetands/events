#!/usr/bin/env python3
"""Add `type:` metadata to venues in sites/<city>.yaml (map is derived, see VenueConfig.map_url)."""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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


def patch_yaml(dry_run: bool = False) -> None:
    """Add `type:` to venues of sites/<city>.yaml that lack it. `map` is not written:
    VenueConfig.map_url() derives the Google Maps search from name + city."""
    from src.config import get_city, insert_venue_keys, venues_from_file, city_names
    patched = 0
    for city in city_names():
        path = get_city(city).sites_path
        for v in venues_from_file(path):
            if v.type:
                continue
            t = infer_type(v.name, v.category_hint, v.tags)
            if dry_run:
                print(f"[DRY] {city}: {v.name}: +type={t}")
            else:
                insert_venue_keys(path, v.name, top={"type": t})
            patched += 1
    print(("[DRY-RUN] Would patch" if dry_run else "Patched") + f" {patched} venues.")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    patch_yaml(dry_run=dry)
