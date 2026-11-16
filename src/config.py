"""City + venue configuration (pipeline v3).

`cities.yaml` names each city's tz/lang/currency and its sites file `sites/<city>.yaml`.
A venue belongs to the city whose file lists it (an explicit `city:` must agree); tags only
select within a city (`parked_tags`, `tags=`) and route a tag to its city (`city_for_tag`).
Venues are validated into `VenueConfig`; `from_legacy` also accepts the pre-v3 keys and
`to_legacy_dict` gives the plain dict older consumers read.

Nothing is read at import time; loads are lazy and cached (`clear_cache()` resets).

CLI:  python -m src.config --check [--city london]
      python -m src.config --dump "Sadler's Wells"
"""
from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ROOT = Path(__file__).resolve().parent.parent
CITIES_YAML = ROOT / "cities.yaml"
DEFAULT_CITY = "london"

LEGACY_MAX_LINKS = 120          # html_sites._follow_event_links: site.get("max_links", 120)

SourceKind = Literal["jsonld", "microdata", "time_tags", "next_data", "adapter",
                     "follow_links", "spektrix", "ics", "llm"]
# Values present in the venue files (2026-11-15) + "other". classify maps them to categories.
CategoryHint = Literal["theatre", "concert", "exhibition", "lecture", "gallery", "kids",
                       "festival", "comedy", "other"]


# ── cities ───────────────────────────────────────────────────────────────────

class CityConfig(BaseModel):
    """One city: timezone, language, sites file and venue tag rules (immutable)."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    label: str
    display_name: str                          # city name for headings ("London", "Москва")
    tz: str
    lang: str = "en"
    currency: str = "£"                        # symbol for prices given without one (JSON-LD offers)
    pricey: int = 100                          # rank: a price above this (in `currency`) is expensive
    sites: str                                 # venues file, relative to cities.yaml
    require_tags: tuple[str, ...] = ()         # tags that route to this city (city_for_tag)
    parked_tags: tuple[str, ...] = ()          # in the city, but only when asked for by tags=
    crawl_tags: tuple[str, ...] = ()           # legacy `pipeline=` for the city crawl (≤1)
    api_sources: tuple[str, ...] = ()
    songkick_metro: Optional[int] = None
    geo: Optional[tuple[float, float]] = None  # (lat, lon) centre for radius-based APIs (Skiddle)

    @field_validator("lang")
    @classmethod
    def _lang(cls, v: str) -> str:
        langs = supported_langs()
        if v not in langs:
            raise ValueError(f"lang {v!r}: src/normalize/dates.py has no month/weekday table for {v!r} "
                             f"(supported: {', '.join(langs)})")
        return v

    @field_validator("geo")
    @classmethod
    def _geo(cls, v: Optional[tuple[float, float]]) -> Optional[tuple[float, float]]:
        if v is not None and not (-90 <= v[0] <= 90 and -180 <= v[1] <= 180):
            raise ValueError(f"geo must be (lat -90..90, lon -180..180), got {v!r}")
        return v

    @property
    def sites_path(self) -> Path:
        p = Path(self.sites)
        return p if p.is_absolute() else CITIES_YAML.parent / p


def supported_langs() -> tuple[str, ...]:
    """Languages the date grammar (src/normalize/dates.py `Lang`) has month/weekday tables for."""
    from typing import get_args
    from .normalize.dates import Lang
    return get_args(Lang)


@lru_cache(maxsize=None)
def load_cities() -> dict[str, CityConfig]:
    """All cities from cities.yaml (cached)."""
    with open(CITIES_YAML) as fh:
        raw = yaml.safe_load(fh) or {}
    return {k: CityConfig(name=k, **(v or {})) for k, v in raw.items()}


def get_city(name: str | None) -> CityConfig:
    """City by name (None → DEFAULT_CITY). KeyError for unknown cities."""
    cities = load_cities()
    key = (name or DEFAULT_CITY).lower()
    if key not in cities:
        raise KeyError(f"unknown city {name!r}; known: {sorted(cities)}")
    return cities[key]


def city_names() -> list[str]:
    return list(load_cities())


def city_for_tag(tag: str | None) -> str:
    """City whose require_tags contain `tag` (e.g. moscow-theatre → moscow), else default."""
    if tag:
        for c in load_cities().values():
            if tag in c.require_tags:
                return c.name
    return DEFAULT_CITY


def legacy_pipeline_tag(city: str) -> str | None:
    """The `pipeline=` value the legacy loader uses for a city's crawl (cities.yaml crawl_tags)."""
    c = get_city(city)
    if len(c.crawl_tags) > 1:
        raise ValueError(f"city {c.name!r}: legacy crawl supports one crawl_tag, got {c.crawl_tags}")
    return c.crawl_tags[0] if c.crawl_tags else None


# ── venues ───────────────────────────────────────────────────────────────────

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_regex(v: Optional[str]) -> Optional[str]:
    """Reject patterns that do not compile (fail at load, not mid-crawl)."""
    if v is not None:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid regex {v!r}: {e}") from e
    return v


class FetchCfg(_Strict):
    """How to fetch the listing page."""
    mode: Literal["http", "browser", "browser_stealth", "api"] = "http"
    delay: Optional[float] = None              # seconds to let JS render (browser modes)
    wait_for: Optional[str] = None             # CSS selector to await (browser modes)
    remove_selectors: list[str] = []           # stripped before reduction/extraction
    gap: Optional[float] = None                # s between fetch starts on this host (fetch layer default 1.0)


class FollowCfg(_Strict):
    """`follow_links` source: the listing hides dates, detail pages ARE the extraction."""
    link_pattern: Optional[str] = None         # None → crawler default (r"/event/")
    max: int = LEGACY_MAX_LINKS

    _re = field_validator("link_pattern")(_check_regex)


class EnrichCfg(_Strict):
    """Detail-page gap-fill pass for fields the listing lacks (src/sources/enrich.py)."""
    fields: list[Literal["time", "price", "sessions"]] = []
    max: int = Field(20, ge=0)                 # detail pages per venue per crawl
    link_pattern: Optional[str] = None         # only event URLs matching (re.search) are fetched

    _re = field_validator("link_pattern")(_check_regex)


def _check_days(v: Optional[list[int]]) -> Optional[list[int]]:
    if v is not None and (not v or any(not 0 <= d <= 6 for d in v)):
        raise ValueError(f"weekdays must be a non-empty list of 0..6 (0=Mon), got {v!r}")
    return sorted(set(v)) if v is not None else None


class RangeWeeklyCfg(_Strict):
    """Opt-in: a bare range whose two ends fall on one weekday is a weekly series on it."""
    min_days: int = Field(0, ge=0)             # only ranges at least this long
    title_re: Optional[str] = None             # only titles matching (search, case-insensitive)

    _re = field_validator("title_re")(_check_regex)


class FixCfg(_Strict):
    """Declarative post-build fixes, applied by src/venues/fixes.apply_fixes (date_order: in build).
    `weekly` keys are regexes matched with re.search (case-insensitive) — anchor with ^ for a
    prefix. `byday` / `weekly` touch runs and series only: a one_off is never changed."""
    # true: a run whose date text names one weekday plus a recurrence cue ("Fridays", "every",
    # "weekly") with both ends on it is weekly; else sessions_known=False. The object form
    # also accepts bare same-weekday ends (see RangeWeeklyCfg).
    range_means_weekly: bool | RangeWeeklyCfg = False
    time_default: Optional[str] = None         # "19:30" for timed events with no time
    date_order: Literal["dmy", "mdy", "ymd"] = "dmy"
    url_pattern: Optional[str] = None          # event URLs not matching → listing URL
    drop_title_re: Optional[str] = None        # drop events whose title matches (case-insensitive)
    byday: Optional[list[int]] = None          # force these weekdays (0=Mon) on every run/series
    weekly: Optional[dict[str, list[int]]] = None   # title regex → weekdays, e.g. {"WordPlay": [4]}

    _re = field_validator("url_pattern", "drop_title_re")(_check_regex)
    _days = field_validator("byday")(_check_days)

    @field_validator("time_default")
    @classmethod
    def _hhmm(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError(f"time_default must be HH:MM, got {v!r}")
        return v

    @field_validator("weekly")
    @classmethod
    def _weekly(cls, v: Optional[dict[str, list[int]]]) -> Optional[dict[str, list[int]]]:
        return None if v is None else {_check_regex(k): _check_days(d) for k, d in v.items()}


class VenueConfig(_Strict):
    """One venue entry, validated."""
    name: str
    url: str                                    # may contain {YEAR}; see resolved_url()
    city: Optional[str] = None                  # explicit city; overrides tag rules
    tags: list[str] = []
    weight: int = Field(1, ge=1, le=3)
    type: Optional[str] = None
    map: Optional[str] = None
    # Human context; never meant for the LLM (legacy `notes` loads here). The migration
    # (scripts/migrate_sites.py) moves extraction-relevant text into `hint`.
    about: Optional[str] = None
    hint: Optional[str] = None                  # LLM-facing hint: dates/times/prices/format/what to skip
    hook: Optional[str] = None                  # registered src/venues hook slug (ASCII, explicit only)
    fetch: FetchCfg = Field(default_factory=FetchCfg)
    extract: list[SourceKind] = Field(default_factory=lambda: ["jsonld", "adapter", "llm"])
    merge: bool = False                         # union every `extract` source (default: first non-empty wins)
    adapter: Optional[dict[str, Any]] = None
    follow: Optional[FollowCfg] = None
    enrich: Optional[EnrichCfg] = None
    fix: FixCfg = Field(default_factory=FixCfg)
    disabled: bool = False
    disabled_reason: Optional[str] = None
    skip: bool = False
    category_hint: CategoryHint = "other"
    must_determine_venue: bool = False
    cache_version: Optional[str | int] = None

    @field_validator("hook")
    @classmethod
    def _hook(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.fullmatch(r"[a-z][a-z0-9_]*", v):
            raise ValueError(f"hook must be an ASCII slug [a-z][a-z0-9_]*, got {v!r}")
        return v

    def resolved_url(self, year: int | str) -> str:
        return self.url.replace("{YEAR}", str(year))

    def map_url(self, city_display: str) -> str:
        """`map`, else the Google Maps search the legacy metadata script generated."""
        return self.map or derived_map(self.name, city_display)

    def to_legacy_dict(self) -> dict[str, Any]:
        """Inverse of from_legacy: the plain dict today's crawler consumes.
        Optional keys are emitted only when set, so `.get(k, default)` behaves as before.
        `hint` (LLM-facing) and `about` (human) are passed as is; `notes` is never emitted.
        Not emitted (no legacy consumer): city, hook, enrich, fix, disabled_reason."""
        d: dict[str, Any] = {"name": self.name, "url": self.url, "category_hint": self.category_hint}
        mode = self.fetch.mode
        d["js_required"] = mode in ("browser", "browser_stealth")
        if mode == "browser_stealth":
            d["stealth"] = True
        if self.fetch.remove_selectors:
            d["remove_selectors"] = list(self.fetch.remove_selectors)
        if self.fetch.delay is not None:
            d["delay"] = self.fetch.delay
        if self.fetch.wait_for is not None:
            d["wait_for"] = self.fetch.wait_for
        for k in ("hint", "about"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        if self.tags:
            d["pipeline"] = self.tags[0] if len(self.tags) == 1 else list(self.tags)
        if self.adapter is not None:
            d["adapter"] = copy.deepcopy(self.adapter)
        if "follow_links" in self.extract:
            d["follow_event_links"] = True
        if self.follow is not None:
            if self.follow.link_pattern is not None:
                d["link_pattern"] = self.follow.link_pattern
            d["max_links"] = self.follow.max
        for k in ("type", "map", "cache_version"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.weight != 1:
            d["weight"] = self.weight
        for k in ("must_determine_venue", "disabled", "skip"):
            if getattr(self, k):
                d[k] = True
        return d


def derived_map(name: str, city_display: str) -> str:
    from urllib.parse import quote_plus
    return f"https://maps.google.com/?q={quote_plus(f'{name} {city_display}')}"


# Every key found in the pre-v3 sites.yaml (2026-11-15: 18 keys over 391 entries) + the three
# the crawler reads but no entry sets yet (delay, wait_for, cache_version).
LEGACY_KEYS = frozenset({
    "name", "url", "map", "type", "must_determine_venue", "category_hint", "js_required",
    "stealth", "skip", "disabled", "pipeline", "notes", "remove_selectors", "weight",
    "follow_event_links", "link_pattern", "max_links", "adapter",
    "delay", "wait_for", "cache_version",
})
# v3-native keys accepted as-is (so a partly migrated file still loads).
NATIVE_KEYS = frozenset({"city", "tags", "about", "hint", "hook", "fetch", "extract", "merge", "follow",
                         "enrich", "fix", "disabled_reason"})


def from_legacy(d: dict[str, Any]) -> VenueConfig:
    """Map a venue entry (v3-native or pre-v3 legacy keys) to VenueConfig. Unknown key → ValueError."""
    name = d.get("name", "<unnamed>")
    unknown = set(d) - LEGACY_KEYS - NATIVE_KEYS
    if unknown:
        raise ValueError(f"venue {name!r}: unknown key(s) {sorted(unknown)}")
    out: dict[str, Any] = {k: copy.deepcopy(d[k]) for k in NATIVE_KEYS if k in d}
    for k in ("name", "url", "map", "type", "category_hint", "adapter", "cache_version", "weight"):
        if d.get(k) is not None:
            out[k] = copy.deepcopy(d[k])
    for k in ("must_determine_venue", "skip", "disabled"):
        if d.get(k) is not None:
            out[k] = bool(d[k])

    fetch = dict(out.get("fetch") or {})
    if d.get("stealth"):
        fetch["mode"] = "browser_stealth"
    elif d.get("js_required"):
        fetch["mode"] = "browser"
    for k in ("remove_selectors", "delay", "wait_for"):
        if d.get(k) is not None:
            fetch[k] = copy.deepcopy(d[k])
    if fetch:
        out["fetch"] = fetch

    if d.get("notes") is not None and "about" not in out:
        out["about"] = str(d["notes"])

    p = d.get("pipeline")
    if p is not None:
        tags = [p] if isinstance(p, str) else list(p)
        out["tags"] = list(dict.fromkeys([*out.get("tags", []), *tags]))

    if d.get("follow_event_links") or d.get("link_pattern") is not None or d.get("max_links") is not None:
        follow = dict(out.get("follow") or {})
        if d.get("link_pattern") is not None:
            follow["link_pattern"] = d["link_pattern"]
        if d.get("max_links") is not None:
            follow["max"] = int(d["max_links"])
        out["follow"] = follow
        if d.get("follow_event_links"):
            out["extract"] = ["follow_links"]

    try:
        return VenueConfig(**out)
    except ValidationError as e:
        raise ValueError(f"venue {name!r}: {e}") from e


# ── loading ──────────────────────────────────────────────────────────────────

def _read_entries(path: Path) -> list[dict[str, Any]]:
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    if isinstance(data, dict):
        return data.get("venues", data.get("html_sites", []))
    return list(data)


@lru_cache(maxsize=None)
def _load_file(path: str) -> tuple[VenueConfig, ...]:
    return tuple(from_legacy(d) for d in _read_entries(Path(path)))


def venues_from_file(path: str | Path) -> list[VenueConfig]:
    """Every entry of one sites file, unfiltered (copies)."""
    return [v.model_copy(deep=True) for v in _load_file(str(path))]


def clear_cache() -> None:
    load_cities.cache_clear()
    _load_file.cache_clear()


def _in_city(v: VenueConfig, c: CityConfig) -> bool:
    """A venue in c's file is c's venue; an explicit `city:` must agree (--check flags it)."""
    return v.city is None or v.city.lower() == c.name


def load_venues(city: str, *, include_disabled: bool = False,
                tags: list[str] | None = None) -> list[VenueConfig]:
    """Venues of `city` in file order: skip dropped, disabled dropped unless asked,
    parked-tag venues dropped unless a parked tag is in `tags`, optionally only those
    carrying any of `tags`. Returns copies (safe to mutate)."""
    c = get_city(city)
    want = set(tags or ())
    parked = set(c.parked_tags) - want
    out = []
    for v in _load_file(str(c.sites_path)):
        if v.skip or (v.disabled and not include_disabled) or not _in_city(v, c):
            continue
        if parked & set(v.tags):
            continue
        if want and not (want & set(v.tags)):
            continue
        out.append(v.model_copy(deep=True))
    return out


def all_venues() -> list[VenueConfig]:
    """Every entry of every city's sites file (skip/disabled/parked included), file order,
    each file once. For name-keyed lookups (tags, weights, hints) over cached rows."""
    seen: set[str] = set()
    out: list[VenueConfig] = []
    for c in load_cities().values():
        p = str(c.sites_path)
        if p not in seen and c.sites_path.exists():
            seen.add(p)
            out.extend(v.model_copy(deep=True) for v in _load_file(p))
    return out


def legacy_site_dicts(city: str | None = None) -> list[tuple[CityConfig, dict[str, Any]]]:
    """(city, to_legacy_dict()) for every entry of one city's file (None: every city), file order.
    For maintenance scripts that read venues; skip/disabled included."""
    cities = [get_city(city)] if city else list(load_cities().values())
    return [(c, v.to_legacy_dict()) for c in cities if c.sites_path.exists()
            for v in _load_file(str(c.sites_path))]


def insert_venue_keys(path: Path, name: str, *, top: dict[str, Any] | None = None,
                      fetch: dict[str, Any] | None = None) -> bool:
    """Add keys to venue `name` in a sites/<city>.yaml text-wise (comments survive): `top`
    after the `- name:` line, `fetch` under its `fetch:` block (created when missing).
    Existing keys are the caller's business. False when the venue is not in the file."""
    lines = Path(path).read_text().splitlines()
    want = re.compile(r"-\s+name:\s*[\"']?" + re.escape(name) + r"[\"']?\s*$")
    start = next((i for i, ln in enumerate(lines) if want.fullmatch(ln)), None)
    if start is None:
        return False
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("- ")), len(lines))

    def block(d: dict[str, Any], indent: str) -> list[str]:
        dumped = yaml.safe_dump(d, allow_unicode=True, sort_keys=False, width=110)
        return [indent + ln for ln in dumped.splitlines()]

    if fetch:
        f = next((i for i in range(start + 1, end) if lines[i] == "  fetch:"), None)
        if f is None:
            lines[end:end] = ["  fetch:"] + block(fetch, "    ")
        else:
            lines[f + 1:f + 1] = block(fetch, "    ")
    if top:
        lines[start + 1:start + 1] = block(top, "  ")
    Path(path).write_text("\n".join(lines) + "\n")
    clear_cache()
    return True


# ── CLI ──────────────────────────────────────────────────────────────────────

def _validate_file(path: Path, city: CityConfig) -> tuple[list[VenueConfig], int]:
    """(valid venues, errors) for one city's sites file."""
    if not path.exists():
        print(f"ERROR {city.name}: {city.sites} missing")
        return [], 1
    ok: list[VenueConfig] = []
    errors = 0
    for d in _read_entries(path):
        try:
            v = from_legacy(d)
        except ValueError as e:
            errors += 1
            print(f"ERROR {path.name}: {e}")
            continue
        if not _in_city(v, city):
            errors += 1
            print(f"ERROR {path.name}: venue {v.name!r} says city {v.city!r}, file is {city.name!r}")
        ok.append(v)
    return ok, errors


def _check_hooks(venues: list[VenueConfig], partial: bool) -> int:
    """cfg.hook must be registered; with every city loaded, each registered hook must be used."""
    from .venues import load_venue_hooks
    hooks = load_venue_hooks()
    errors = 0
    for v in venues:
        if v.hook and v.hook not in hooks:
            errors += 1
            print(f"ERROR venue {v.name!r}: hook {v.hook!r} is not registered (src/venues/)")
    if not partial:
        for slug in sorted(set(hooks) - {v.hook for v in venues}):
            errors += 1
            print(f"ERROR hook {slug!r} is registered but no venue sets `hook: {slug}`")
    return errors


def _check_api_sources(cities: list[CityConfig]) -> int:
    from .sources import API_SOURCE_REGISTRY
    errors = 0
    for c in cities:
        for n in c.api_sources:
            if n not in API_SOURCE_REGISTRY:
                errors += 1
                print(f"ERROR city {c.name!r}: api_sources {n!r} unknown; known: {sorted(API_SOURCE_REGISTRY)}")
    return errors


def _check_dup_names(venues: list[VenueConfig]) -> int:
    """Venue names must be unique (case-insensitive): fetch results, source contexts and the
    events table's `source` are keyed by name, so two pages under one name parse one page twice."""
    urls: dict[str, list[str]] = {}
    for v in venues:
        urls.setdefault(v.name.lower(), []).append(v.url)
    bad = {n: u for n, u in urls.items() if len(set(u)) > 1}
    for n, u in bad.items():
        print(f"ERROR venue name {n!r} used by {len(u)} urls: {', '.join(u)}")
    return len(bad)


def _check(city: str | None) -> int:
    """Validate cities.yaml, every city's sites file, venue/city agreement, duplicate urls and hooks."""
    try:
        cities = [get_city(city)] if city else list(load_cities().values())
    except ValidationError as e:
        print(f"ERROR {CITIES_YAML.name}: {e}")
        return 1
    errors = _check_api_sources(cities)
    loaded: list[VenueConfig] = []
    for c in cities:
        vs, e = _validate_file(c.sites_path, c)
        errors += e
        loaded += vs
        print(f"{c.name}: {len(vs)} entries in {c.sites}, {len(vs) - e} valid")
    seen: dict[tuple[str, str], int] = {}
    for v in loaded:
        seen[(v.name, v.url)] = seen.get((v.name, v.url), 0) + 1
    for (name, url), n in seen.items():
        if n > 1:
            errors += 1
            print(f"ERROR venue {name!r} ({url}) listed {n} times")
    errors += _check_dup_names(loaded)
    errors += _check_hooks(loaded, partial=city is not None)
    if not errors:
        for c in cities:
            print(f"{c.name}: {len(load_venues(c.name))} active (not skip/disabled/parked)")
    return 1 if errors else 0


def _dump(name: str) -> int:
    for v in all_venues():
        if v.name.lower() == name.lower():
            print(yaml.safe_dump(v.model_dump(exclude_defaults=True), allow_unicode=True,
                                 sort_keys=False), end="")
            return 0
    print(f"no venue named {name!r}")
    return 1


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m src.config")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="validate every venue entry")
    g.add_argument("--dump", metavar="NAME", help="print one venue's VenueConfig as YAML")
    p.add_argument("--city")
    a = p.parse_args(argv)
    return _check(a.city) if a.check else _dump(a.dump)


if __name__ == "__main__":
    raise SystemExit(main())
