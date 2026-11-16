"""/new-city path (plan §4): skill file, seed_venues, validate_venues helpers, --city routing."""
from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

import main as mainmod
from src import config

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / ".claude" / "skills" / "new-city" / "SKILL.md"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod            # dataclasses look the module up
    spec.loader.exec_module(mod)
    return mod


seed = _script("seed_venues")
vv = _script("validate_venues")


# ── skill file ───────────────────────────────────────────────────────────────

def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md needs YAML frontmatter"
    return yaml.safe_load(m.group(1))


def _commands(text: str) -> list[str]:
    """`venv/bin/python …` lines inside ``` fences (continuations joined)."""
    out, lang = [], None
    for ln in text.replace("\\\n", " ").splitlines():
        st = ln.strip()
        if st.startswith("```"):
            lang = None if lang is not None else (st[3:] or "text")
        elif lang in ("bash", "text") and st.startswith("venv/bin/python"):
            out.append(st)
    return out


def test_skill_frontmatter():
    fm = _frontmatter(SKILL.read_text())
    assert fm["name"] == "new-city"
    for trig in ("/new-city", "add city", "new city"):
        assert trig in fm["description"]


def test_skill_commands_exist():
    """Every script / module / main.py subcommand / flag the skill names exists in the repo."""
    cmds = _commands(SKILL.read_text())
    assert len(cmds) >= 8
    sub_src = {"crawl": "def cmd_crawl", "query": "def cmd_query"}
    main_src = (ROOT / "main.py").read_text()
    for c in cmds:
        parts = c.split()
        if parts[1] == "-m":
            target = ROOT / (parts[2].replace(".", "/") + ".py")
        else:
            target = ROOT / parts[1]
        assert target.exists(), c
        src = target.read_text()
        if target.name == "main.py":
            sub = parts[2]
            assert sub in ("crawl", "query", "eval"), c
            if sub == "eval":
                src = (ROOT / "src" / "eval" / "cli.py").read_text()
            else:
                i = main_src.index(sub_src[sub])
                src = main_src[i:main_src.index("\ndef ", i + 1)] + main_src[main_src.index("def _add_crawl_flags"):]
        for flag in re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", c):
            assert f'"{flag}"' in src, f"{flag} not defined for: {c}"


def test_skill_mentions_every_step_tool():
    t = SKILL.read_text()
    for s in ("cities.yaml", "seed_venues.py", "--test-site", "--adapter-check", "eval --capture",
              "eval --city", "--status", "bypass"):
        assert s in t, s


def test_check_moscow_removed():
    assert not (ROOT / "_check_moscow.py").exists()
    assert not (ROOT / "moscow_venues_staging.yaml").exists()


# ── seed_venues (canned responses, no network) ───────────────────────────────

OVERPASS = {"elements": [
    {"type": "node", "id": 1, "lat": 48.86, "lon": 2.33,
     "tags": {"amenity": "theatre", "name": "Théâtre A", "name:en": "Theatre A", "website": "https://www.theatre-a.fr/",
              "wikidata": "Q1"}},
    {"type": "way", "id": 2, "center": {"lat": 48.85, "lon": 2.35},
     "tags": {"tourism": "museum", "name": "Musée B", "wikidata": "Q2"}},
    {"type": "node", "id": 3, "lat": 48.0, "lon": 2.0,
     "tags": {"tourism": "gallery", "name": "Galerie C", "contact:website": "galerie-c.fr"}},
    {"type": "node", "id": 4, "tags": {"amenity": "cinema"}},                         # unnamed
    {"type": "node", "id": 5, "tags": {"amenity": "music_venue", "name": "Club D"}},   # no site
    {"type": "node", "id": 6, "tags": {"amenity": "theatre", "name": "Théâtre A bis",
                                       "website": "http://theatre-a.fr/salle2"}},     # same host as A
    {"type": "node", "id": 7, "tags": {"amenity": "arts_centre", "name": "Known Hall",
                                       "website": "https://en.known.fr/"}},
]}
WIKIDATA = {"entities": {
    "Q1": {"claims": {}, "sitelinks": {f"w{i}": {} for i in range(30)}},
    "Q2": {"claims": {"P856": [
        {"rank": "deprecated", "mainsnak": {"datavalue": {"value": "https://old-b.fr"}}},
        {"rank": "preferred", "mainsnak": {"datavalue": {"value": "https://musee-b.fr/"}}}]},
        "sitelinks": {f"w{i}": {} for i in range(12)}},
    "Q9": {"missing": ""},
}}


def test_parse_overpass():
    cs = seed.parse_overpass(OVERPASS, lang="en")
    names = [c.name for c in cs]
    assert "Theatre A" in names and "Musée B" in names and len(cs) == 6
    b = next(c for c in cs if c.name == "Musée B")
    assert (b.lat, b.lon, b.category_hint, b.kind, b.osm) == (48.85, 2.35, "exhibition", "tourism=museum", "way/2")
    assert next(c for c in cs if c.name == "Galerie C").url == "https://galerie-c.fr"


def test_parse_wikidata_prefers_preferred_and_skips_deprecated():
    wd = seed.parse_wikidata(WIKIDATA)
    assert wd["Q2"] == {"website": "https://musee-b.fr/", "sitelinks": 12}
    assert wd["Q1"] == {"website": None, "sitelinks": 30} and "Q9" not in wd


def test_seed_ranks_dedupes_and_skips_known_hosts():
    got = seed.seed("paris", 48.86, 2.35, 5, lang="en", overpass=OVERPASS, wikidata=WIKIDATA,
                    skip_hosts={"known.fr"}, http=lambda *a, **k: pytest.fail("network"))
    names = [c.name for c in got]
    assert names[0] == "Theatre A"                    # theatre + 30 sitelinks
    assert got[names.index("Théâtre A bis")].shared_host     # same host as A, other name: kept, flagged
    assert "Known Hall" not in names                  # already in sites/<city>.yaml
    assert names[-1] == "Club D"                      # no website: last
    assert next(c for c in got if c.name == "Musée B").url == "https://musee-b.fr/"   # P856 filled in
    assert len(seed.seed("paris", 0, 0, 1, overpass=OVERPASS, wikidata=WIKIDATA, skip_hosts=set(), cap=2)) == 2


def test_seed_cli_offline(tmp_path):
    import json
    (tmp_path / "o.json").write_text(json.dumps(OVERPASS))
    (tmp_path / "w.json").write_text(json.dumps(WIKIDATA))
    out = tmp_path / "seed.yaml"
    assert seed.main(["--city", "nowhere", "--lat", "48.8", "--lon", "2.3", "--lang", "fr",
                      "--overpass-json", str(tmp_path / "o.json"), "--wikidata-json", str(tmp_path / "w.json"),
                      "--out", str(out)]) == 0
    text = out.read_text()
    assert "agenda sorties Nowhere" in text            # local-language search checklist
    rows = yaml.safe_load(text)["candidates"]
    assert {"name", "url", "category_hint", "score"} <= set(rows[0])


def test_overpass_query_covers_kinds():
    q = seed.overpass_query(55.75, 37.62, 2.5)
    assert "around:2500,55.75,37.62" in q
    for k, v in seed.KINDS:
        assert f'["{k}"="{v}"]' in q


def test_host_normalisation():
    assert seed.host("https://en.shm.ru/shows/") == seed.host("http://www.shm.ru") == "shm.ru"
    assert seed.host("https://kremlin.ru") == "kremlin.ru" and seed.host(None) == ""


# ── validate_venues helpers (offline) ────────────────────────────────────────

LD_PAGE = ("<html><head><title>What's on</title>"
           '<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
           '<script type="application/ld+json">[' + ",".join(
               f'{{"@type":"Event","name":"E{i}","startDate":"2031-0{i}-01"}}' for i in range(1, 5))
           + "]</script></head><body>" + "<p>listing text</p>" * 200
           + '<a href="/cal.ics">ics</a></body></html>')


def test_probe_page_and_suggest():
    p = vv.probe_page(LD_PAGE, "https://venue.example/whats-on")
    assert p["jsonld_events"] == 4 and not p["blocked"]
    assert p["feeds"] == ["/feed.xml", "/cal.ics"]
    assert vv.suggest(p)["extract"] == ["jsonld", "llm"]
    thin = vv.probe_page("<html><body><div id=app></div></body></html>")
    assert vv.suggest(thin)["fetch"] == "browser"
    wall = vv.probe_page("<html><head><title>Just a moment...</title></head><body>cf-chl-x</body></html>")
    assert wall["blocked"] and vv.suggest(wall)["extract"] is None


CARDS = "".join(f'<li class="ev"><h3>Show {i}</h3><span class="d">{i} December 2031</span>'
                f'<a href="/e/{i}">x</a></li>' for i in range(1, 6))


def test_adapter_check():
    today = date(2031, 11, 1)
    ok = vv.adapter_check(CARDS, {"kind": "cards", "card": "li.ev", "title": "h3", "date": ".d"}, today)
    assert ok["records"] == 5 and ok["dated"] == 5 and ok["ok"]
    bad = vv.adapter_check(CARDS, {"kind": "cards", "card": "li.ev", "title": "h3"}, today)
    assert bad["dated"] == 0 and not bad["ok"]


# ── --city routing ───────────────────────────────────────────────────────────

@pytest.fixture
def cities(tmp_path, monkeypatch):
    (tmp_path / "sites").mkdir()
    venues = {
        "london": [{"name": "L1", "url": "https://l1.example/", "tags": ["family"]},
                   {"name": "L2", "url": "https://l2.example/"}],
        "moscow": [{"name": "M1", "url": "https://m1.example/", "tags": ["moscow"]},
                   {"name": "M2", "url": "https://m2.example/", "tags": ["moscow-theatre"]}],
        "paris": [],
        "rome": [{"name": "R1", "url": "https://r1.example/"},
                 {"name": "R2", "url": "https://r2.example/", "tags": ["family"]}],
    }
    for n, vs in venues.items():
        (tmp_path / "sites" / f"{n}.yaml").write_text(yaml.safe_dump({"venues": vs}))
    c = lambda n, **kw: {"label": f"{n} Events", "display_name": n.capitalize(), "tz": "UTC",   # noqa: E731
                         "sites": f"sites/{n}.yaml", **kw}
    (tmp_path / "cities.yaml").write_text(yaml.safe_dump({
        "london": c("london"),
        "moscow": c("moscow", lang="ru", require_tags=["moscow", "moscow-theatre"],
                    parked_tags=["moscow-theatre"], crawl_tags=["moscow"]),
        "paris": c("paris", require_tags=["paris"], crawl_tags=["paris"]),
        "rome": c("rome"),                                  # no tags at all: file membership only
    }))
    monkeypatch.setattr(config, "CITIES_YAML", tmp_path / "cities.yaml")
    config.clear_cache()
    yield
    monkeypatch.undo()
    config.clear_cache()


def test_resolve_city(cities):
    assert mainmod.resolve_city(None) == "london"
    assert mainmod.resolve_city("Paris") == "paris"
    assert mainmod.resolve_city(None, "moscow-theatre") == "moscow"
    assert mainmod.resolve_city(None, "family") == "london"     # non-routing tag: default city
    with pytest.raises(SystemExit):
        mainmod.resolve_city("paris", "moscow-theatre")
    with pytest.raises(SystemExit):
        mainmod.resolve_city("atlantis")


def test_site_selection_by_city(cities):
    from src.sources.html_sites import configured_site_names as names
    assert names(city="rome") == ["R1", "R2"]                  # empty crawl_tags: file membership
    assert names(city="rome", pipeline="family") == ["R2"]     # tag only narrows within the city
    assert names(city="london") == names() == ["L1", "L2"]     # London unaffected
    assert names("family") == ["L1"]                           # tag alone: routes to the default city
    assert names(city="moscow") == ["M1"]                      # parked tag needs an explicit tag
    assert names(city="moscow", pipeline="moscow-theatre") == names("moscow-theatre") == ["M2"]


def test_crawl_sites_city(cities, monkeypatch):
    from src.sources import html_sites
    seen = []

    async def fake_parse(cfg, fr, ctx):
        seen.append((cfg.name, ctx.city.name))
        return [], {"events": 0, "source": None, "raws": 0, "fetch": fr.status.value, "build.failed": 0,
                    "dropped_past": 0, "stats": {}, "errors": []}
    monkeypatch.setattr(html_sites, "parse_venue", fake_parse)
    monkeypatch.setattr(html_sites.timing, "log_runtime", lambda **kw: None)
    asyncio.run(html_sites.crawl_sites(city="rome", frs={}))
    assert seen == [("R1", "rome"), ("R2", "rome")]
    seen.clear()
    asyncio.run(html_sites.crawl_sites(frs={}))
    assert seen == [("L1", "london"), ("L2", "london")]
    seen.clear()
    asyncio.run(html_sites.crawl_sites(city="rome", test_site="r2", frs={}))
    assert seen == [("R2", "rome")]


def test_main_crawl_passes_city(cities, monkeypatch):
    got = {}

    async def fake_crawl_sites(pipeline=None, test_site=None, *, city=None, **kw):
        got.update(pipeline=pipeline, city=city)
        raise RuntimeError("stop")              # crawl() then keeps the previous table
    monkeypatch.setattr(mainmod, "crawl_sites", fake_crawl_sites)
    asyncio.run(mainmod.crawl("rome", test_site="R1"))
    assert got == {"pipeline": None, "city": "rome"}
    asyncio.run(mainmod.crawl("moscow", test_site="M2", tag="moscow-theatre"))
    assert got == {"pipeline": "moscow-theatre", "city": "moscow"}


@pytest.fixture
def captured(monkeypatch):
    got: dict = {}

    async def fake_crawl(city, **kw):
        got["crawl"] = (city, kw)
        return 1                       # >0: legacy goes on to the query

    async def fake_query(f, **kw):
        got["query"] = (f, kw)
        return []

    monkeypatch.setattr(mainmod, "crawl", fake_crawl)
    monkeypatch.setattr(mainmod, "run_query", fake_query)
    return got


def test_cmd_crawl_city_and_tag(cities, captured):
    mainmod.cmd_crawl(["--city", "paris", "--test-site", "X"])
    assert captured["crawl"][0] == "paris" and captured["crawl"][1]["test_site"] == "X"
    mainmod.cmd_crawl(["--tag", "moscow-theatre"])
    assert captured["crawl"][0] == "moscow" and captured["crawl"][1]["tag"] == "moscow-theatre"   # parked
    mainmod.cmd_crawl(["--tag", "moscow"])
    assert captured["crawl"][0] == "moscow" and captured["crawl"][1]["tag"] is None             # routing only
    mainmod.cmd_crawl(["--city", "rome", "--tag", "family"])
    assert captured["crawl"] == ("rome", captured["crawl"][1]) and captured["crawl"][1]["tag"] == "family"
    mainmod.cmd_crawl([])
    assert captured["crawl"][0] == "london"


def test_cmd_query_city_and_tag(cities, captured):
    mainmod.cmd_query(["--city", "paris"])
    f = captured["query"][0]
    assert f.city == "paris" and f.label == "paris Events"
    mainmod.cmd_query(["--tag", "moscow-theatre"])
    f = captured["query"][0]
    assert f.city == "moscow" and f.venue_tags == frozenset({"moscow-theatre"}) and f.lang == "ru"


def test_legacy_tag_and_city(cities, captured):
    mainmod.legacy(["--tag", "moscow", "--dry-run"])
    assert captured["crawl"][0] == "moscow" and captured["crawl"][1]["tag"] is None    # routing only
    assert captured["query"][0].city == "moscow" and captured["query"][1]["preset"] == "moscow"
    captured.clear()
    mainmod.legacy(["--tag", "moscow-theatre", "--dry-run"])
    assert captured["crawl"] == ("moscow", captured["crawl"][1]) and captured["crawl"][1]["tag"] == "moscow-theatre"
    assert captured["query"][0].venue_tags == frozenset({"moscow-theatre"})
    captured.clear()
    mainmod.legacy(["--city", "paris", "--dry-run"])
    f = captured["query"][0]
    assert captured["crawl"][0] == "paris" and f.city == "paris" and f.label == "Paris Events"
    captured.clear()
    mainmod.legacy(["--city", "moscow", "--family", "--dry-run"])     # flag preset wins, city retargets
    f = captured["query"][0]
    assert captured["query"][1]["preset"] == "family" and f.audience == "family"
    assert (f.city, f.label, f.lang, f.tz) == ("moscow", "Moscow Family Events", "ru", "UTC")
    with pytest.raises(SystemExit):
        mainmod.legacy(["--tag", "nonsense"])


# ── eval --city ──────────────────────────────────────────────────────────────

def test_eval_city_filter(tmp_path):
    from src.eval.run import load_gold
    for slug, city in (("a", "london"), ("b", "moscow"), ("c", None)):
        d = tmp_path / slug
        d.mkdir()
        meta = {"name": slug.upper(), "url": f"https://{slug}.example/", "extract": ["llm"], "today": "2031-01-01"}
        if city:
            meta["city"] = city
        (d / "meta.yaml").write_text(yaml.safe_dump(meta))
        (d / "expected.yaml").write_text("[]\n")
    assert [g.slug for g in load_gold(tmp_path, city="moscow")] == ["b"]
    assert [g.slug for g in load_gold(tmp_path, city="london")] == ["a", "c"]   # no city = london
    assert len(load_gold(tmp_path)) == 3


def test_eval_meta_stub():
    from src.eval.cli import meta_stub
    v = config.load_venues("moscow")[0]
    m = meta_stub(v.url, "moscow", date(2031, 1, 1))
    assert m["name"] == v.name and m["city"] == "moscow" and m["extract"] == list(v.extract)
    m = meta_stub(v.url, None, date(2031, 1, 1))
    assert m["city"] == "moscow"
    assert meta_stub("https://nowhere.example/", "london", date(2031, 1, 1))["name"].startswith("TODO")


def test_eval_cli_city_no_gold(capsys):
    from src.eval.cli import main as eval_main
    assert eval_main(["--city", "moscow", "--only", "no-such-slug"]) == 1
    assert "WARNING: no gold venues" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        eval_main(["--city", "atlantis"])


# ── review follow-ups ────────────────────────────────────────────────────────

def test_check_url_503_bot_wall_is_warn():
    import httpx

    wall = "<html><head><title>Just a moment...</title></head><body>cf-chl-x</body></html>"

    def handler(req):
        if req.url.host == "wall.example":
            return httpx.Response(503, text=wall)
        if req.url.host == "down.example":
            return httpx.Response(503, text="<html><body>" + "maintenance " * 400 + "</body></html>")
        return httpx.Response(200, text="<html><body>" + "listing " * 400 + "</body></html>")

    async def go(url):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as c:
            return await vv.check_url(c, "V", url, probe=True)

    r = asyncio.run(go("https://wall.example/"))
    assert r["ok"] and r["warn"] and r["bot_wall"] and r["status"] == 503
    assert r["suggest"]["fetch"] is None and "bot wall" in r["suggest"]["why"]
    r = asyncio.run(go("https://down.example/"))
    assert not r["ok"] and not r["bot_wall"]                        # plain 503: dead
    r = asyncio.run(go("https://fine.example/"))
    assert r["ok"] and not r["warn"]


def test_status_rows(monkeypatch):
    from types import SimpleNamespace
    from src import events_db
    from src.fetch import report
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    rows = [{"name": "Ok", "status": "ok", "fetched_at": now.isoformat()},
            {"name": "Empty", "status": "ok", "fetched_at": now.isoformat()},
            {"name": "Old", "status": "not_modified", "fetched_at": (now - timedelta(days=30)).isoformat()},
            {"name": "Wall", "status": "blocked", "fetched_at": None, "note": "cf"},
            {"name": "Never", "status": None}]
    monkeypatch.setattr(report, "fetch_status_report", lambda city: rows)
    evs = [SimpleNamespace(source="Ok"), SimpleNamespace(source="Old")]
    monkeypatch.setattr(events_db, "load_events", lambda city: SimpleNamespace(events=evs))
    got = {r["name"]: (r["bucket"], r["events"]) for r in vv.status_rows("x")}
    assert got == {"Ok": ("ok", 1), "Empty": ("zero_events", 0), "Old": ("stale", 1),
                   "Wall": ("blocked", 0), "Never": ("no_fetch_row", 0)}


def test_adapter_check_order_and_bad_kind():
    html = '<li class="ev"><h3>A</h3><span class="d">12/03/2031</span></li>' * 3
    cfg = {"kind": "cards", "card": "li.ev", "title": "h3", "date": ".d"}
    assert vv.adapter_check(html, cfg, date(2031, 1, 1), order="mdy")["dated"] == 3
    with pytest.raises(KeyError):
        vv.adapter_check(html, {"kind": "nope"}, date(2031, 1, 1))
    with pytest.raises(KeyError):
        vv.adapter_check(html, {"kind": "cards"}, date(2031, 1, 1))


def test_seed_portals_and_shared_hosts():
    data = {"elements": [
        {"type": "node", "id": 1, "tags": {"amenity": "theatre", "name": "Hall One", "website": "https://vk.com/hall1"}},
        {"type": "node", "id": 2, "tags": {"tourism": "museum", "name": "City Museum",
                                           "website": "https://museum.mos.ru/"}},
        {"type": "node", "id": 3, "tags": {"amenity": "theatre", "name": "Big Theatre Main Stage",
                                           "website": "https://bigtheatre.example/"}},
        {"type": "node", "id": 4, "tags": {"amenity": "theatre", "name": "Big Theatre New Stage",
                                           "website": "https://www.bigtheatre.example/new"}},
        {"type": "node", "id": 5, "tags": {"amenity": "theatre", "name": "Big Theatre — Main Stage",
                                           "website": "https://bigtheatre.example/"}},
        {"type": "node", "id": 6, "tags": {"amenity": "library", "name": "Ministry Library",
                                           "website": "https://library.culture.gov.uk/"}},
    ]}
    got = {c.name: c for c in seed.seed("x", 0, 0, 1, overpass=data, wikidata={}, skip_hosts=set())}
    assert got["Hall One"].url is None and "vk.com" in got["Hall One"].note
    assert got["City Museum"].url is None and got["Ministry Library"].url is None
    assert "Big Theatre — Main Stage" not in got                     # same name + host: one entry
    assert got["Big Theatre Main Stage"].shared_host and got["Big Theatre New Stage"].shared_host
    assert got["Ministry Library"].category_hint == "lecture"


def test_seed_yaml_entries_load_as_venues(tmp_path):
    import json
    from src.config import VenueConfig
    (tmp_path / "o.json").write_text(json.dumps(OVERPASS))
    (tmp_path / "w.json").write_text(json.dumps(WIKIDATA))
    out = tmp_path / "s.yaml"
    seed.main(["--city", "nowhere", "--lat", "1", "--lon", "1", "--overpass-json", str(tmp_path / "o.json"),
               "--wikidata-json", str(tmp_path / "w.json"), "--out", str(out)])
    rows = yaml.safe_load(out.read_text())["candidates"]
    for r in rows:
        if r.get("url"):
            VenueConfig(**{k: v for k, v in r.items() if k not in seed.REVIEW_KEYS})


def test_seed_user_agent(monkeypatch):
    monkeypatch.delenv("SEED_CONTACT", raising=False)
    assert seed.user_agent() == seed.UA
    monkeypatch.setenv("SEED_CONTACT", "https://example.org/contact")
    assert seed.user_agent().endswith("; https://example.org/contact)")


def test_city_lang_and_api_sources_checked(tmp_path, monkeypatch, capsys):
    (tmp_path / "sites").mkdir()
    (tmp_path / "sites" / "b.yaml").write_text("venues: []\n")
    city = {"label": "B", "display_name": "B", "tz": "UTC", "sites": "sites/b.yaml"}
    monkeypatch.setattr(config, "CITIES_YAML", tmp_path / "cities.yaml")
    try:
        (tmp_path / "cities.yaml").write_text(yaml.safe_dump({"b": {**city, "lang": "de"}}))
        config.clear_cache()
        assert config.main(["--check"]) == 1
        assert "dates.py has no month/weekday table for 'de'" in capsys.readouterr().out
        (tmp_path / "cities.yaml").write_text(yaml.safe_dump({"b": {**city, "api_sources": ["nope"]}}))
        config.clear_cache()
        assert config.main(["--check"]) == 1
        assert "api_sources 'nope' unknown" in capsys.readouterr().out
        assert "ru" in config.supported_langs()
    finally:
        monkeypatch.undo()
        config.clear_cache()
