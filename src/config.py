"""City + venue configuration (pipeline v3).

`cities.yaml` names each city's tz/lang/sites file and how its venues are picked
out of a shared sites file (`exclude_tags` / `require_tags` / `parked_tags`).
Venues are validated into `VenueConfig`; `from_legacy` / `to_legacy_dict` bridge
today's sites.yaml keys so the existing crawler keeps consuming plain dicts.

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
# Values present in sites.yaml (2026-11-15) + "other". classify maps them to categories.
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
    sites: str = "sites.yaml"                  # relative to cities.yaml
    exclude_tags: tuple[str, ...] = ()         # venues with any of these are not in the city
    require_tags: tuple[str, ...] = ()         # venues need one of these to be in the city
    parked_tags: tuple[str, ...] = ()          # in the city, but only when asked for by tags=
    crawl_tags: tuple[str, ...] = ()           # legacy `pipeline=` for the city crawl (≤1)
    api_sources: tuple[str, ...] = ()
    songkick_metro: Optional[int] = None

    @property
    def sites_path(self) -> Path:
        p = Path(self.sites)
        return p if p.is_absolute() else CITIES_YAML.parent / p


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


class FollowCfg(_Strict):
    """`follow_links` source: the listing hides dates, detail pages ARE the extraction."""
    link_pattern: Optional[str] = None         # None → crawler default (r"/event/")
    max: int = LEGACY_MAX_LINKS

    _re = field_validator("link_pattern")(_check_regex)


class EnrichCfg(_Strict):
    """Detail-page gap-fill pass for fields the listing lacks (step 6)."""
    fields: list[str] = []
    max: int = 20
    link_pattern: Optional[str] = None


class FixCfg(_Strict):
    """Declarative post-extraction fixes (consumed from step 5)."""
    range_means_weekly: bool = False
    time_default: Optional[str] = None
    date_order: Literal["dmy", "mdy", "ymd"] = "dmy"
    url_pattern: Optional[str] = None
    drop_title_re: Optional[str] = None

    _re = field_validator("url_pattern", "drop_title_re")(_check_regex)


class VenueConfig(_Strict):
    """One venue entry, validated."""
    name: str
    url: str                                    # may contain {YEAR}; see resolved_url()
    city: Optional[str] = None                  # explicit city; overrides tag rules
    tags: list[str] = []
    weight: int = Field(1, ge=1, le=3)
    type: Optional[str] = None
    map: Optional[str] = None
    # Human notes. Target: never sent to the LLM. Until the notes migration (step 5)
    # it is still emitted as legacy `notes`, which the extract prompt includes.
    about: Optional[str] = None
    hint: Optional[str] = None                  # LLM-facing hint (not yet consumed)
    fetch: FetchCfg = Field(default_factory=FetchCfg)
    extract: list[SourceKind] = Field(default_factory=lambda: ["jsonld", "adapter", "llm"])
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

    def resolved_url(self, year: int | str) -> str:
        return self.url.replace("{YEAR}", str(year))

    def to_legacy_dict(self) -> dict[str, Any]:
        """Inverse of from_legacy: the plain dict today's crawler consumes.
        Optional keys are emitted only when set, so `.get(k, default)` behaves as before.
        Not emitted (no legacy consumer): city, hint, enrich, fix, disabled_reason."""
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
        if self.about is not None:
            d["notes"] = self.about
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


# Every key found in sites.yaml (2026-11-15: 18 keys over 391 entries) + the three
# the crawler reads but no entry sets yet (delay, wait_for, cache_version).
LEGACY_KEYS = frozenset({
    "name", "url", "map", "type", "must_determine_venue", "category_hint", "js_required",
    "stealth", "skip", "disabled", "pipeline", "notes", "remove_selectors", "weight",
    "follow_event_links", "link_pattern", "max_links", "adapter",
    "delay", "wait_for", "cache_version",
})
# v3-native keys accepted as-is (so a partly migrated file still loads).
NATIVE_KEYS = frozenset({"city", "tags", "about", "hint", "fetch", "extract", "follow",
                         "enrich", "fix", "disabled_reason"})


def from_legacy(d: dict[str, Any]) -> VenueConfig:
    """Map a sites.yaml entry (legacy keys) to VenueConfig. Unknown key → ValueError."""
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
    return data.get("html_sites", []) if isinstance(data, dict) else list(data)


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
    if v.city is not None:
        return v.city.lower() == c.name
    t = set(v.tags)
    if t & set(c.exclude_tags):
        return False
    if c.require_tags and not (t & set(c.require_tags)):
        return False
    return True


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
        if p not in seen:
            seen.add(p)
            out.extend(v.model_copy(deep=True) for v in _load_file(p))
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────

def _check(city: str | None) -> int:
    cities = [get_city(city)] if city else list(load_cities().values())
    errors = 0
    for c in cities:
        entries = _read_entries(c.sites_path)
        ok = []
        for d in entries:
            try:
                ok.append(from_legacy(d))
            except ValueError as e:
                errors += 1
                print(f"ERROR {e}")
        active = load_venues(c.name) if not errors else []
        print(f"{c.name}: {len(entries)} entries in {c.sites}, {len(ok)} valid, "
              f"{len(active)} active (not skip/disabled/parked, in city)")
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
