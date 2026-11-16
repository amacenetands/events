"""VenueConfig / cities.yaml / load_venues over sites/<city>.yaml."""
from datetime import date

import pytest
import yaml

from pydantic import ValidationError

from src import config
from src.config import LEGACY_MAX_LINKS, VenueConfig, from_legacy, load_venues
from src.sources import html_sites
from src.sources.html_sites import _load_sites, configured_site_names

PIPELINES = [None, "moscow", "moscow-theatre", "family", "theatres", "evening", "daytime", "museums"]


def _raw_entries(city: str | None = None) -> list[dict]:
    """Raw yaml entries of one city's file (None: every city, cities.yaml order)."""
    out = []
    for c in ([config.get_city(city)] if city else config.load_cities().values()):
        with open(c.sites_path) as fh:
            out += yaml.safe_load(fh)["venues"]
    return out


def test_every_entry_loads_and_round_trips():
    entries = _raw_entries()
    assert len(entries) >= 390
    bad = []
    for d in entries:
        v = from_legacy(d)
        if VenueConfig(**v.model_dump()) != v or "notes" in d or "pipeline" in d:
            bad.append(d["name"])
    assert not bad, bad[:3]


def test_to_legacy_dict_never_emits_notes():
    v = VenueConfig(name="A", url="u", about="human", hint="Flag kids events.")
    d = v.to_legacy_dict()
    assert "notes" not in d and d["hint"] == "Flag kids events." and d["about"] == "human"
    assert from_legacy({"name": "B", "url": "u", "notes": "n"}).about == "n"       # legacy input still loads


def test_unknown_key_raises_naming_venue():
    with pytest.raises(ValueError, match=r"'Foo'.*bogus"):
        from_legacy({"name": "Foo", "url": "https://foo", "bogus": 1})


def test_mapping_details():
    v = from_legacy({"name": "A", "url": "u", "js_required": True, "stealth": True, "notes": "n",
                     "pipeline": "family", "follow_event_links": True, "link_pattern": "/e/",
                     "remove_selectors": [".x"], "delay": 5, "wait_for": ".card"})
    assert v.fetch.mode == "browser_stealth" and v.fetch.delay == 5.0 and v.fetch.wait_for == ".card"
    assert v.fetch.remove_selectors == [".x"]
    assert v.about == "n" and v.hint is None and v.tags == ["family"]
    assert v.extract == ["follow_links"] and v.enrich is None          # follow != enrich
    assert v.follow.link_pattern == "/e/" and v.follow.max == LEGACY_MAX_LINKS
    assert from_legacy({"name": "B", "url": "u", "js_required": True}).fetch.mode == "browser"
    plain = from_legacy({"name": "C", "url": "u", "pipeline": ["family", "museums"]})
    assert plain.fetch.mode == "http" and plain.extract == ["jsonld", "adapter", "llm"]
    assert plain.tags == ["family", "museums"] and plain.enrich is None and plain.follow is None


@pytest.mark.parametrize("bad", [
    {"fetch": {"mode": "teleport"}},
    {"fetch": {"bogus": 1}},                 # nested extra="forbid"
    {"fix": {"date_order": "ydm"}},
    {"fix": {"nope": True}},
    {"extract": ["jsonld", "magic"]},
    {"category_hint": "opera"},
    {"weight": 0}, {"weight": 4},
])
def test_validation_rejects(bad):
    with pytest.raises(ValidationError):
        VenueConfig(name="x", url="u", **bad)
    with pytest.raises(ValueError, match="'x'"):
        from_legacy({"name": "x", "url": "u", **bad})


def test_cities():
    lon, mos = config.get_city("london"), config.get_city("moscow")
    assert lon.tz == "Europe/London" and mos.tz == "Europe/Moscow" and mos.lang == "ru"
    assert lon.api_sources == ("skiddle", "songkick") and mos.api_sources == ()
    assert mos.display_name == "Москва" and lon.songkick_metro == 24426
    assert config.get_city(None).name == "london"
    assert config.city_for_tag("moscow-theatre") == "moscow" and config.city_for_tag("family") == "london"
    assert config.legacy_pipeline_tag("london") is None and config.legacy_pipeline_tag("moscow") == "moscow"
    with pytest.raises(ValidationError):            # frozen
        lon.tz = "UTC"
    with pytest.raises(KeyError):
        config.get_city("paris")


def test_load_venues_filters():
    lon = load_venues("london")
    mos = load_venues("moscow")
    assert lon and mos and not ({v.name for v in lon} & {v.name for v in mos})
    assert all(not (set(v.tags) & {"moscow", "moscow-theatre"}) for v in lon)
    assert all(not v.skip and not v.disabled for v in lon + mos)
    assert all("moscow-theatre" not in v.tags for v in mos)                  # parked
    theatre = load_venues("moscow", tags=["moscow-theatre"])
    assert theatre and all("moscow-theatre" in v.tags for v in theatre)
    assert len(load_venues("london", include_disabled=True)) > len(lon)
    fam = load_venues("london", tags=["family"])
    assert fam and all("family" in v.tags for v in fam)
    lon[0].tags.append("mutated")                          # copies: cache unaffected
    assert "mutated" not in load_venues("london")[0].tags


# ── _load_sites: the pipeline-tag rule over the per-city files ─────────────────

def _reference(pipeline, include_disabled):
    """pipeline=None: the default city's file; a tag routed to a city: that city's entries
    carrying it; any other tag: default-city entries carrying it (names, file order)."""
    city = config.city_for_tag(pipeline)
    parked = set(config.get_city(city).parked_tags) - {pipeline}
    def tags(s):
        return set(s.get("tags") or ())
    sites = [s for s in _raw_entries(city) if not s.get("skip") and (include_disabled or not s.get("disabled"))
             and not (tags(s) & parked)]
    return [s["name"] for s in sites if pipeline is None or pipeline in tags(s)]


@pytest.mark.parametrize("pipeline", PIPELINES)
@pytest.mark.parametrize("include_disabled", [False, True])
def test_load_sites_matches_tag_rule(pipeline, include_disabled):
    assert configured_site_names(pipeline, include_disabled) == _reference(pipeline, include_disabled)


def test_load_sites_shape():
    by = {s["name"]: s for s in _load_sites("family")}
    b = by["Barbican"]
    assert b["js_required"] and b["follow_event_links"] and b["max_links"] == 120
    assert b["link_pattern"] == r"/whats-on/\d{4}/event/" and b["pipeline"] == "family"


# ── synthetic cities/sites files ─────────────────────────────────────────────

@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    def make(cities: dict, sites: dict[str, list[dict]]):
        (tmp_path / "sites").mkdir(exist_ok=True)
        for name, entries in sites.items():
            (tmp_path / "sites" / f"{name}.yaml").write_text(yaml.safe_dump({"venues": entries}, allow_unicode=True))
        (tmp_path / "cities.yaml").write_text(yaml.safe_dump(cities, allow_unicode=True))
        monkeypatch.setattr(config, "CITIES_YAML", tmp_path / "cities.yaml")
        config.clear_cache()
    yield make
    monkeypatch.undo()
    config.clear_cache()


def _city(name="london", **kw):
    return {"label": "L", "display_name": "D", "tz": "UTC", "sites": f"sites/{name}.yaml", **kw}


def test_year_placeholder_resolved(tmp_config, monkeypatch):
    monkeypatch.setattr(html_sites, "city_today", lambda c: date(2031, 3, 1))
    tmp_config({"london": _city()},
               {"london": [{"name": "Fest", "url": "https://fest.example/{YEAR}/programme", "tags": ["family"]}]})
    [s] = _load_sites(None)
    assert s["url"] == "https://fest.example/2031/programme"
    assert load_venues("london")[0].url.endswith("/{YEAR}/programme")      # config keeps raw


def test_file_membership_and_tag_rules(tmp_config, capsys):
    tmp_config(
        {"london": _city(), "spb": _city("spb", require_tags=["ru", "ru-theatre"], parked_tags=["ru-theatre"],
                                         crawl_tags=["ru"])},
        {"london": [{"name": "A", "url": "u1", "tags": ["family"]},
                    {"name": "E", "url": "u5", "tags": ["ru"]}],                  # in london's file: london's
         "spb": [{"name": "B", "url": "u2", "tags": ["ru"]},
                 {"name": "D", "url": "u4", "tags": ["ru-theatre"]},             # parked
                 {"name": "F", "url": "u6", "tags": ["family"]}]},
    )
    def names(c, **kw):
        return [v.name for v in load_venues(c, **kw)]
    assert names("london") == ["A", "E"]
    assert names("spb") == ["B", "F"]
    assert names("spb", tags=["ru-theatre"]) == ["D"]
    assert names("spb", tags=["ru"]) == ["B"]
    assert config.legacy_pipeline_tag("spb") == "ru" and config.city_for_tag("ru-theatre") == "spb"
    assert config.main(["--check"]) == 0
    tmp_config({"london": _city(), "spb": _city("spb")},
               {"london": [{"name": "A", "url": "u1", "city": "spb"}, {"name": "A", "url": "u1"}], "spb": []})
    assert config.main(["--check"]) == 1
    out = capsys.readouterr().out
    assert "says city 'spb', file is 'london'" in out and "listed 2 times" in out


def test_check_hooks(tmp_config, capsys, monkeypatch):
    from src import venues
    monkeypatch.setattr(venues, "VENUE_HOOKS", {"used_hall": venues.VenueHook(), "orphan": venues.VenueHook()})
    monkeypatch.setattr(venues, "_loaded", True)
    tmp_config({"london": _city()}, {"london": [{"name": "A", "url": "u", "hook": "used_hall"},
                                                 {"name": "B", "url": "v", "hook": "missing"}]})
    assert config.main(["--check"]) == 1
    out = capsys.readouterr().out
    assert "hook 'missing' is not registered" in out and "hook 'orphan' is registered but no venue" in out
    assert "used_hall" not in out
    with pytest.raises(ValidationError):
        VenueConfig(name="x", url="u", hook="Sadler's Wells")          # explicit ASCII slugs only


def test_legacy_pipeline_tag_rejects_multiple(tmp_config):
    tmp_config({"london": _city(crawl_tags=["a", "b"])}, {"london": []})
    with pytest.raises(ValueError):
        config.legacy_pipeline_tag("london")


# ── other sites readers route through config ─────────────────────────────────

def _all_names() -> set[str]:
    names = {v.name for v in config.all_venues()}
    for c in config.city_names():
        assert {v.name for v in load_venues(c, include_disabled=True)} <= names
    assert names == {d["name"] for d in _raw_entries()}
    return names


def test_rank_weights_via_config():
    from src.rank import load_weights
    w = load_weights()
    assert w and set(w) <= {n.lower() for n in _all_names()}
    assert w == {d["name"].lower(): int(d["weight"]) for d in _raw_entries() if d.get("weight")}


def test_classify_hints_via_config(monkeypatch):
    from src import classify
    monkeypatch.setattr(classify, "_hints", None)
    h = classify._site_hints()
    assert set(h) == {n.lower() for n in _all_names()}
    assert h == {d["name"].lower(): d.get("category_hint", "other") for d in _raw_entries()}


def test_main_readers_via_config(monkeypatch):
    import main
    from src import dedup
    from src.models import Event
    assert set(main._tags_by_source()) == _all_names()
    from datetime import datetime, timezone
    from src.fetch.types import FetchResult, FetchStatus
    old, new = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc)
    frs = {"A": FetchResult(FetchStatus.OK, fetched_at=new), "B": FetchResult(FetchStatus.OK, fetched_at=old),
           "C": FetchResult(FetchStatus.OK, fetched_at=datetime(2020, 1, 1, tzinfo=timezone.utc))}
    used = [Event(title="x", venue=n, date="2030-01-01", url="u", source=n) for n in ("A", "B")]
    assert main._oldest_fetched(frs, used) == old.isoformat()    # C produced nothing: ignored
    assert main._oldest_fetched(frs, []) is None
    asked: list[str] = []
    monkeypatch.setattr(dedup, "get_html_fetched_at", lambda u: asked.append(u), raising=False)
    evs = [Event(title="x", venue=n, date="2030-01-01", url="u", source=n) for n in ("Barbican", "Nope")]
    assert main._stale_venues(evs, 1) == []
    assert asked == [v.url for v in config.all_venues() if v.name == "Barbican"]


def test_api_sources_registry():
    from src.sources import API_SOURCE_REGISTRY, api_sources_for
    assert api_sources_for(config.get_city("london").api_sources) == list(API_SOURCE_REGISTRY.values())
    assert api_sources_for(config.get_city("moscow").api_sources) == []
    with pytest.raises(ValueError, match="eventbrite"):
        api_sources_for(["skiddle", "eventbrite"])


def test_digest_city_display():
    from src.digest import _city_display
    assert _city_display("moscow") == "Москва" and _city_display("london") == "London"
    assert _city_display("paris") == "Paris"


def test_cli_check(capsys):
    assert config.main(["--check", "--city", "london"]) == 0
    assert "london:" in capsys.readouterr().out
    assert config.main(["--dump", "Barbican"]) == 0
    assert "follow_links" in capsys.readouterr().out


def test_per_city_sites_files():
    for c in config.load_cities().values():
        assert c.sites_path.name == f"{c.name}.yaml" and c.sites_path.exists()
    lon, mos = config.get_city("london"), config.get_city("moscow")
    assert lon.geo == (51.5074, -0.1278) and (lon.pricey, mos.pricey) == (100, 10000)
    with pytest.raises(ValidationError):
        config.CityConfig(name="x", label="x", display_name="x", tz="UTC", sites="s", geo=(95, 0))
    v = from_legacy({"name": "A", "url": "u", "hook": "a_hall", "fix": {"weekly": {"WordPlay": [4]}, "byday": [6, 5]}})
    assert v.hook == "a_hall" and v.fix.weekly == {"WordPlay": [4]} and v.fix.byday == [5, 6]
    assert "hook" not in v.to_legacy_dict()
    rw = VenueConfig(name="A", url="u", fix={"range_means_weekly": {"min_days": 28, "title_re": "Club"}}).fix
    assert rw.range_means_weekly.min_days == 28
    with pytest.raises(ValidationError):
        VenueConfig(name="A", url="u", fix={"range_means_weekly": {"title_re": "("}})
