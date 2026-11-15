"""VenueConfig / cities.yaml / load_venues — incl. legacy sites.yaml round-trip."""
import pytest
import yaml

from pydantic import ValidationError

from src import config
from src.config import LEGACY_MAX_LINKS, VenueConfig, from_legacy, load_venues
from src.sources.html_sites import SITES_YAML, _load_sites, configured_site_names

PIPELINES = [None, "moscow", "moscow-theatre", "family", "theatres", "evening", "daytime", "museums"]
_LEGACY_FALSE = ("js_required", "stealth", "skip", "disabled", "must_determine_venue", "follow_event_links")


def _raw_entries() -> list[dict]:
    with open(SITES_YAML) as fh:
        return yaml.safe_load(fh)["html_sites"]


def _norm(d: dict) -> dict:
    """Legacy dict modulo key order, absent-vs-default and notes/pipeline shape."""
    d = dict(d)
    for k in _LEGACY_FALSE:
        if not d.get(k):
            d.pop(k, None)
    if d.get("weight") in (None, 1):
        d.pop("weight", None)
    if not d.get("remove_selectors"):
        d.pop("remove_selectors", None)
    if d.get("follow_event_links") and d.get("max_links") is None:
        d["max_links"] = LEGACY_MAX_LINKS
    if d.get("category_hint") is None:
        d["category_hint"] = "other"
    p = d.pop("pipeline", None)
    if p is not None:
        d["pipeline"] = [p] if isinstance(p, str) else list(p)
    if d.get("notes") is not None:
        d["notes"] = str(d["notes"])
    return {k: v for k, v in d.items() if v is not None}


def test_every_entry_loads():
    entries = _raw_entries()
    assert len(entries) >= 391
    for d in entries:
        from_legacy(d)


def test_round_trip_all_entries():
    bad = []
    for d in _raw_entries():
        got = from_legacy(d).to_legacy_dict()
        if _norm(got) != _norm(d):
            bad.append((d["name"], _norm(d), _norm(got)))
    assert not bad, bad[:3]


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


# ── _load_sites: behaviour-preserving vs the pre-v3 loader ─────────────────────

def _legacy_reference(pipeline, include_disabled):
    """The pre-v3 _load_sites membership rule, restated (names only, file order)."""
    def tags(s):
        p = s.get("pipeline")
        return set() if p is None else (set(p) if isinstance(p, list) else {p})
    sites = [s for s in _raw_entries() if not s.get("skip") and (include_disabled or not s.get("disabled"))]
    if pipeline:
        return [s["name"] for s in sites if pipeline in tags(s)]
    return [s["name"] for s in sites if not (tags(s) & {"moscow", "moscow-theatre"})]


@pytest.mark.parametrize("pipeline", PIPELINES)
@pytest.mark.parametrize("include_disabled", [False, True])
def test_load_sites_matches_legacy_rule(pipeline, include_disabled):
    assert configured_site_names(pipeline, include_disabled) == _legacy_reference(pipeline, include_disabled)


def test_load_sites_shape():
    by = {s["name"]: s for s in _load_sites("family")}
    b = by["Barbican"]
    assert b["js_required"] and b["follow_event_links"] and b["max_links"] == 120
    assert b["link_pattern"] == r"/whats-on/\d{4}/event/" and b["pipeline"] == "family"


# ── synthetic cities/sites files ─────────────────────────────────────────────

@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    def make(cities: dict, sites: list[dict]):
        (tmp_path / "sites.yaml").write_text(yaml.safe_dump({"html_sites": sites}, allow_unicode=True))
        (tmp_path / "cities.yaml").write_text(yaml.safe_dump(cities, allow_unicode=True))
        monkeypatch.setattr(config, "CITIES_YAML", tmp_path / "cities.yaml")
        config.clear_cache()
    yield make
    monkeypatch.undo()
    config.clear_cache()


def _city(**kw):
    return {"label": "L", "display_name": "D", "tz": "UTC", "sites": "sites.yaml", **kw}


def test_year_placeholder_resolved(tmp_config):
    from datetime import date
    tmp_config({"london": _city()},
               [{"name": "Fest", "url": "https://fest.example/{YEAR}/programme", "pipeline": "family"}])
    [s] = _load_sites(None)
    assert s["url"] == f"https://fest.example/{date.today().year}/programme"
    assert load_venues("london")[0].url.endswith("/{YEAR}/programme")      # config keeps raw


def test_city_override_and_tag_rules(tmp_config):
    tmp_config(
        {"london": _city(exclude_tags=["ru", "ru-theatre"]),
         "spb": _city(require_tags=["ru", "ru-theatre"], exclude_tags=["closed"],
                      parked_tags=["ru-theatre"], crawl_tags=["ru"])},
        [{"name": "A", "url": "u", "pipeline": "family"},
         {"name": "B", "url": "u", "pipeline": "ru"},
         {"name": "C", "url": "u", "pipeline": ["ru", "closed"]},                 # exclude beats require
         {"name": "D", "url": "u", "pipeline": "ru-theatre"},                     # parked
         {"name": "E", "url": "u", "pipeline": "ru", "city": "london"},           # explicit city wins
         {"name": "F", "url": "u", "pipeline": "family", "city": "spb"}],
    )
    def names(c, **kw):
        return [v.name for v in load_venues(c, **kw)]
    assert names("london") == ["A", "E"]
    assert names("spb") == ["B", "F"]
    assert names("spb", tags=["ru-theatre"]) == ["D"]
    assert names("spb", tags=["ru"]) == ["B"]
    assert config.legacy_pipeline_tag("spb") == "ru" and config.city_for_tag("ru-theatre") == "spb"


def test_legacy_pipeline_tag_rejects_multiple(tmp_config):
    tmp_config({"london": _city(crawl_tags=["a", "b"])}, [])
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
    assert h == {d["name"].lower(): d["category_hint"] for d in _raw_entries()}


def test_main_readers_via_config(monkeypatch):
    import main
    from src import dedup
    from src.models import Event
    assert set(main._tags_by_source()) == _all_names()
    seen: list[str] = []
    monkeypatch.setattr(dedup, "oldest_fetched_at", lambda urls: seen.extend(urls), raising=False)
    for city in config.city_names():
        seen.clear()
        tag = main._city_tag(city)
        main._oldest_fetched(tag)
        assert seen == [v.url for v in load_venues(city, tags=[tag] if tag else None)]
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
