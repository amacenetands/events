#!/usr/bin/env python3
"""
Seed candidate venues for a new city (the /new-city skill, step 2).

OSM Overpass (theatres, arts centres, music venues, concert halls, cinemas, museums,
galleries) within a radius, enriched with Wikidata (P856 official website, sitelink count
as a notability rank). Venues whose site host is already in sites/<city>.yaml are dropped.
Writes a candidate YAML the agent reviews; nothing here touches sites/<city>.yaml.

  python scripts/seed_venues.py --city paris --lat 48.8566 --lon 2.3522 --radius-km 8
  python scripts/seed_venues.py --city paris --lat .. --lon .. --overpass-json o.json --wikidata-json w.json  # offline
  -> cache/seed/<city>.yaml (or --out)

`url` is the official site (home page): finding the what's-on page is the agent's job.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
WIKIDATA_URL = "https://www.wikidata.org/w/api.php"
UA = "london-events-seed/1.0 (venue seeding for a personal events digest)"
# Wikimedia asks bots for contact info: set SEED_CONTACT (url or email) to append it to the UA.
CAP = 80

# OSM tag value -> category_hint guess (src/config.py CategoryHint).
KINDS: dict[tuple[str, str], str] = {
    ("amenity", "theatre"): "theatre",
    ("amenity", "arts_centre"): "other",
    ("amenity", "music_venue"): "concert",
    ("amenity", "concert_hall"): "concert",
    ("amenity", "events_venue"): "other",
    ("amenity", "cinema"): "other",
    ("amenity", "library"): "lecture",
    ("amenity", "community_centre"): "other",
    ("amenity", "planetarium"): "lecture",
    ("tourism", "museum"): "exhibition",
    ("tourism", "gallery"): "gallery",
    ("tourism", "zoo"): "kids",
    ("tourism", "aquarium"): "kids",
    ("tourism", "attraction"): "other",
}
# Base score per kind: what a what's-on digest gains from a typical venue of that kind.
KIND_SCORE = {"theatre": 3, "concert": 3, "exhibition": 2, "kids": 2, "lecture": 1, "gallery": 1, "other": 1}
# Hosts that are a social profile or a shared portal, not the venue's own site: such a venue
# counts as having no website (url null + note), and never dedupes by host.
PORTAL_HOSTS = {"vk.com", "vk.ru", "facebook.com", "fb.com", "instagram.com", "t.me", "telegram.me",
                "twitter.com", "x.com", "youtube.com", "ok.ru", "linktr.ee", "mos.ru", "culture.ru",
                "paris.fr", "berlin.de", "london.gov.uk", "google.com", "sites.google.com",
                "wikipedia.org", "tripadvisor.com", "yandex.ru"}
_GOV = re.compile(r"(?:^|\.)gov(?:\.[a-z]{2})?$|(?:^|\.)gouv\.fr$")

AGGREGATOR_QUERIES = [
    '"{city}" what\'s on this week',
    '"{city}" events listings',
    '"{city}" concerts listings',
    '"{city}" family events this weekend',
    '"{city}" museums exhibitions now on',
]
LOCAL_QUERIES = {   # lang -> extra queries in the local language
    "ru": ['афиша {city}', '{city} концерты афиша', '{city} выставки сейчас', '{city} детям афиша выходные'],
    "fr": ['agenda sorties {city}', '{city} que faire ce week-end', '{city} expositions en cours'],
    "de": ['{city} veranstaltungen', '{city} was ist los', '{city} ausstellungen aktuell'],
    "es": ['agenda cultural {city}', '{city} qué hacer este fin de semana'],
    "it": ['eventi {city}', '{city} cosa fare questo weekend', '{city} mostre in corso'],
}


REVIEW_KEYS = ("note", "shared_host", "score", "kind", "wikidata", "sitelinks", "osm", "lat", "lon", "alt_names")


@dataclass
class Candidate:
    name: str
    url: str | None
    category_hint: str
    kind: str                      # "amenity=theatre"
    osm: str                       # "node/123"
    lat: float | None = None
    lon: float | None = None
    wikidata: str | None = None
    sitelinks: int = 0
    score: float = 0.0
    about: str = ""
    note: str = ""
    shared_host: bool = False
    alt_names: list[str] = field(default_factory=list)


# ── queries ──────────────────────────────────────────────────────────────────

def overpass_query(lat: float, lon: float, radius_km: float) -> str:
    r = int(radius_km * 1000)
    parts = "".join(f'nwr["{k}"="{v}"](around:{r},{lat},{lon});' for k, v in KINDS)
    return f"[out:json][timeout:90];({parts});out center tags;"


def user_agent() -> str:
    import os
    contact = os.environ.get("SEED_CONTACT", "").strip()
    return f"{UA[:-1]}; {contact})" if contact else UA


def _http_json(method: str, url: str, **kw) -> Any:
    import httpx
    r = httpx.request(method, url, headers={"User-Agent": user_agent()}, timeout=120,
                      follow_redirects=True, **kw)
    r.raise_for_status()
    return r.json()


def fetch_overpass(lat: float, lon: float, radius_km: float, http: Callable = _http_json) -> dict:
    return http("POST", OVERPASS_URL, data={"data": overpass_query(lat, lon, radius_km)})


def fetch_wikidata(qids: list[str], http: Callable = _http_json) -> dict:
    """wbgetentities in batches of 50, merged into one {"entities": {...}}."""
    ents: dict = {}
    for i in range(0, len(qids), 50):
        batch = qids[i:i + 50]
        ents.update(http("GET", WIKIDATA_URL, params={
            "action": "wbgetentities", "ids": "|".join(batch), "props": "claims|sitelinks",
            "format": "json"}).get("entities", {}))
    return {"entities": ents}


# ── parsing (pure) ───────────────────────────────────────────────────────────

def _norm_url(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip().split(";")[0].strip()
    if not re.match(r"https?://", u, re.I):
        u = "https://" + u.lstrip("/")
    return u


_HOST_PREFIX = re.compile(r"^(?:(?:www\d?|m|en|eng|ru|tickets?|afisha)\.)+")


def host(u: str | None) -> str:
    """Comparable host: 'https://en.shm.ru/shows/' and 'http://www.shm.ru' -> 'shm.ru'."""
    h = (urlsplit(u).hostname or "") if u else ""
    return _HOST_PREFIX.sub("", h.lower()) if h.count(".") > 1 else h.lower()


def parse_overpass(data: dict, lang: str = "en") -> list[Candidate]:
    """Overpass JSON -> candidates (unnamed elements dropped)."""
    out = []
    for el in data.get("elements", []):
        t = el.get("tags") or {}
        name = t.get(f"name:{lang}") or t.get("name")
        if not name:
            continue
        kind = next(((k, v) for k, v in KINDS if t.get(k) == v), None)
        if kind is None:
            continue
        c = el.get("center") or el
        alts = list(dict.fromkeys(v for k in ("name", "name:en", "official_name", "short_name")
                                  if (v := t.get(k)) and v != name))
        about = ", ".join(x for x in (t.get("addr:street"), t.get("operator"), t.get("description")) if x)
        out.append(Candidate(
            name=name, url=_norm_url(t.get("website") or t.get("contact:website") or t.get("url")),
            category_hint=KINDS[kind], kind=f"{kind[0]}={kind[1]}", osm=f"{el.get('type')}/{el.get('id')}",
            lat=c.get("lat"), lon=c.get("lon"), wikidata=t.get("wikidata"), about=about[:200],
            alt_names=alts))
    return out


def parse_wikidata(data: dict) -> dict[str, dict]:
    """{qid: {"website": P856 (first, preferred rank first), "sitelinks": n}}."""
    out = {}
    for qid, ent in (data.get("entities") or {}).items():
        if "missing" in ent:
            continue
        claims = (ent.get("claims") or {}).get("P856") or []
        claims = sorted(claims, key=lambda c: c.get("rank") != "preferred")
        site = next((c["mainsnak"]["datavalue"]["value"] for c in claims
                     if c.get("rank") != "deprecated" and c.get("mainsnak", {}).get("datavalue")), None)
        out[qid] = {"website": site, "sitelinks": len(ent.get("sitelinks") or {})}
    return out


def is_portal(u: str | None) -> bool:
    h = host(u)
    return bool(h) and (_GOV.search(h) is not None
                        or any(h == p or h.endswith("." + p) for p in PORTAL_HOSTS))


def enrich(cands: list[Candidate], wd: dict[str, dict]) -> None:
    """Wikidata sitelinks + P856 website (when OSM has none or only a portal); portal urls -> None."""
    for c in cands:
        w = wd.get(c.wikidata or "")
        if w:
            c.sitelinks = w["sitelinks"]
            if (not c.url or is_portal(c.url)) and w["website"] and not is_portal(w["website"]):
                c.url = _norm_url(w["website"])
        if is_portal(c.url):
            c.note = f"only a portal/social page: {c.url}; find the venue's own site"
            c.url = None


def score(c: Candidate) -> float:
    """Notability: kind base + wikidata sitelinks (log-ish, capped) + having a site / QID."""
    s = KIND_SCORE.get(c.category_hint, 1)
    s += min(c.sitelinks, 40) / 4
    s += 2 if c.url else -5            # no site = nothing to crawl
    s += 1 if c.wikidata else 0
    return round(s, 2)


def _norm_name(n: str) -> str:
    return re.sub(r"[\W_]+", " ", n.casefold()).strip()


def rank(cands: list[Candidate], existing_hosts: set[str] = frozenset(), cap: int = CAP) -> list[Candidate]:
    """Score, drop venues whose host is already configured, dedupe (same normalised name, or
    same host AND same name; the best-scored one wins). Different names on one host are both
    kept and flagged `shared_host` (two halls of one institution, or one shared site).
    Keep the top `cap`."""
    for c in cands:
        c.score = score(c)
    out: list[Candidate] = []
    by_host: dict[str, Candidate] = {}
    names: set[str] = set()
    for c in sorted(cands, key=lambda c: (-c.score, c.name)):
        h, n = host(c.url), _norm_name(c.name)
        if (h and h in existing_hosts) or n in names:
            continue
        if h in by_host:
            c.shared_host = by_host[h].shared_host = True
        elif h:
            by_host[h] = c
        names.add(n)
        out.append(c)
    return out[:cap]


def search_checklist(city: str, display: str, lang: str) -> list[str]:
    qs = [q.format(city=display) for q in AGGREGATOR_QUERIES]
    qs += [q.format(city=display) for q in LOCAL_QUERIES.get(lang, [])]
    return list(dict.fromkeys(qs))


def to_yaml(cands: list[Candidate], city: str, display: str, lang: str, meta: dict) -> str:
    import yaml
    head = [
        f"# Seed candidates for {city} (scripts/seed_venues.py). REVIEW, do not load: copy chosen",
        f"# entries into sites/{city}.yaml with the what's-on url, fetch and extract filled in, and",
        f"# drop the review-only keys ({', '.join(REVIEW_KEYS)}).",
        f"# {meta}",
        "# Web-search checklist (aggregators / listings sites the OSM seed cannot see):",
        *[f"#   - {q}" for q in search_checklist(city, display, lang)],
    ]
    rows = []
    for c in cands:
        d = asdict(c)
        d = {k: d[k] for k in ("name", "url", "category_hint", "about", "note", "shared_host", "score", "kind",
                               "wikidata", "sitelinks", "osm", "lat", "lon", "alt_names")}
        rows.append({k: v for k, v in d.items() if v not in (None, "", [], False)})
    return "\n".join(head) + "\n" + yaml.safe_dump({"candidates": rows}, allow_unicode=True,
                                                   sort_keys=False, width=110)


def existing_hosts(city: str) -> set[str]:
    from src.config import get_city, venues_from_file
    try:
        c = get_city(city)
    except KeyError:
        return set()
    if not c.sites_path.exists():
        return set()
    return {host(v.url) for v in venues_from_file(c.sites_path)} - {""}


def seed(city: str, lat: float, lon: float, radius_km: float, *, lang: str = "en", cap: int = CAP,
         overpass: dict | None = None, wikidata: dict | None = None, http: Callable = _http_json,
         skip_hosts: set[str] | None = None) -> list[Candidate]:
    """Candidates for `city`. Canned `overpass` / `wikidata` JSON skip the network."""
    data = overpass if overpass is not None else fetch_overpass(lat, lon, radius_km, http)
    cands = parse_overpass(data, lang)
    qids = sorted({c.wikidata for c in cands if c.wikidata and re.fullmatch(r"Q\d+", c.wikidata)})
    wd = wikidata if wikidata is not None else (fetch_wikidata(qids, http) if qids else {})
    enrich(cands, parse_wikidata(wd))
    return rank(cands, skip_hosts if skip_hosts is not None else existing_hosts(city), cap)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--city", required=True)
    p.add_argument("--lat", type=float, help="default: cities.yaml geo")
    p.add_argument("--lon", type=float)
    p.add_argument("--radius-km", type=float, default=8.0)
    p.add_argument("--lang", help="name:<lang> preferred for names (default: cities.yaml lang, else en)")
    p.add_argument("--display", help="city name for search queries (default: cities.yaml display_name)")
    p.add_argument("--cap", type=int, default=CAP)
    p.add_argument("--overpass-json", type=Path, help="canned Overpass response (offline)")
    p.add_argument("--wikidata-json", type=Path, help="canned wbgetentities response (offline)")
    p.add_argument("--out", type=Path, help="default cache/seed/<city>.yaml")
    a = p.parse_args(argv)

    from src.config import get_city
    try:
        cc = get_city(a.city)
    except KeyError:
        cc = None
    lat = a.lat if a.lat is not None else (cc.geo[0] if cc and cc.geo else None)
    lon = a.lon if a.lon is not None else (cc.geo[1] if cc and cc.geo else None)
    if lat is None or lon is None:
        p.error("--lat/--lon needed (or a cities.yaml geo for the city)")
    lang = a.lang or (cc.lang if cc else "en")
    display = a.display or (cc.display_name if cc else a.city.capitalize())
    load = lambda f: json.loads(f.read_text()) if f else None   # noqa: E731
    cands = seed(a.city, lat, lon, a.radius_km, lang=lang, cap=a.cap,
                 overpass=load(a.overpass_json), wikidata=load(a.wikidata_json))
    out = a.out or ROOT / "cache" / "seed" / f"{a.city}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_yaml(cands, a.city, display, lang,
                           {"centre": [lat, lon], "radius_km": a.radius_km, "kept": len(cands)}))
    no_url = sum(1 for c in cands if not c.url)
    print(f"{len(cands)} candidates ({no_url} without a website) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
