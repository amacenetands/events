"""src/venues: hook registry, explicit binding, identity defaults, isolation, crawl wiring."""
import asyncio
from datetime import date

import pytest

from src import venues
from src.config import VenueConfig, get_city
from src.fetch.types import FetchResult, FetchStatus
from src.models import Event, RawEvent
from src.sources.base import SourceCtx
from src.sources.build import build_events
from src.venues import VENUE_HOOKS, VenueHook, hook_for, load_venue_hooks, run_hook, venue_hook

TODAY = date(2026, 11, 15)


def _ctx() -> SourceCtx:
    return SourceCtx(http=None, today=TODAY, window_days=365, city=get_city("london"))


def _cfg(name: str = "Dummy Hall", **kw) -> VenueConfig:
    return VenueConfig(name=name, url="https://example.org/whats-on/", **kw)


@pytest.fixture
def registry(monkeypatch):
    """A private registry with one dummy hook (`dummy_hall`)."""
    monkeypatch.setattr(venues, "VENUE_HOOKS", {})
    monkeypatch.setattr(venues, "_loaded", True)

    @venue_hook("dummy_hall")
    class Dummy(VenueHook):
        def pre_fetch(self, cfg):
            return cfg.model_copy(update={"url": cfg.url + "?all=1"})

        def pre_source(self, cfg, fr):
            return FetchResult(fr.status, url=fr.url, body=(fr.body or "").replace("NAV", ""))

        def post_raw(self, cfg, raws, ctx):
            return [r for r in raws if not r.title.startswith("Gift")]

        def post_build(self, cfg, events, ctx):
            return [e.model_copy(update={"time": e.time or "19:00"}) for e in events]
    return venues.VENUE_HOOKS


def test_binding_is_explicit(registry):
    assert hook_for(_cfg(hook="dummy_hall")) is registry["dummy_hall"]
    assert hook_for(_cfg("dummy_hall")) is None                       # name never binds
    assert hook_for(_cfg(hook="nope")) is None


def test_shipped_registry_loads():
    hooks = load_venue_hooks()
    assert "fixes" not in hooks and all(isinstance(h, VenueHook) for h in hooks.values())


def test_identity_defaults():
    h, cfg, ctx = VenueHook(), _cfg(), _ctx()
    raws, evs = [RawEvent(title="a")], [Event(title="a", venue="X", date=TODAY, url="u")]
    assert h.pre_fetch(cfg) is cfg and h.pre_source(cfg, None) is None
    assert h.post_raw(cfg, raws, ctx) is raws and h.post_build(cfg, evs, ctx) is evs
    assert run_hook("post_raw", _cfg(), raws, ctx) is raws                     # no hook: identity
    with pytest.raises(ValueError):
        run_hook("post_parse", cfg, raws, ctx)


def test_dummy_hook_all_stages(registry):
    cfg, ctx = _cfg(hook="dummy_hall"), _ctx()
    assert run_hook("pre_fetch", cfg, cfg, ctx).url.endswith("?all=1")
    fr = run_hook("pre_source", cfg, FetchResult(FetchStatus.OK, url="u", body="NAV<p>x</p>"), ctx)
    assert fr.body == "<p>x</p>"
    raws = [RawEvent(title="Gift card", date_text="20 Nov 2026"), RawEvent(title="Play", date_text="20 Nov 2026")]
    [e] = build_events(raws, cfg, ctx)
    assert (e.title, e.time) == ("Play", "19:00")
    assert ctx.stats["hook.post_raw"] == 1 and ctx.stats["hook.post_build"] == 1
    ctx2 = _ctx()                                    # unchanged output: no stat
    build_events([RawEvent(title="Play", date_text="20 Nov 2026", time_text="8pm")], cfg, ctx2)
    assert "hook.post_raw" not in ctx2.stats and "hook.post_build" not in ctx2.stats


@pytest.mark.parametrize("bad", [None, "x", [1], ({},)])
def test_wrong_return_type_is_isolated(registry, bad):
    class Bad(VenueHook):
        def post_build(self, cfg, events, ctx):
            return bad
    venue_hook("bad_hall")(Bad)
    ctx = _ctx()
    evs = build_events([RawEvent(title="Gig", date_text="20 Nov 2026")], _cfg(hook="bad_hall"), ctx)
    assert [e.title for e in evs] == ["Gig"]
    assert ctx.stats["hook.post_build.raised"] == 1 and len(ctx.errors) == 1
    assert ctx.errors[0].startswith("Dummy Hall: hook bad_hall.post_build raised TypeError")


def test_hook_that_raises_is_isolated(registry):
    class Boom(VenueHook):
        def post_raw(self, cfg, raws, ctx):
            raise RuntimeError("kaboom")
    venue_hook("boom_hall")(Boom)
    ctx = _ctx()
    evs = build_events([RawEvent(title="Gig", date_text="20 Nov 2026")], _cfg("Boom Hall", hook="boom_hall"), ctx)
    assert [e.title for e in evs] == ["Gig"]
    assert ctx.errors == ["Boom Hall: hook boom_hall.post_raw raised RuntimeError: kaboom"]


def test_broken_hook_module_reported(monkeypatch):
    import importlib
    import pkgutil
    real = importlib.import_module
    monkeypatch.setattr(venues, "_loaded", False)
    monkeypatch.setattr(venues, "_LOAD_ERRORS", [])
    monkeypatch.setattr(venues, "VENUE_HOOKS", {})
    monkeypatch.setattr(pkgutil, "iter_modules", lambda path: [pkgutil.ModuleInfo(None, "broken_hall", False)])

    def fake(name, *a, **k):
        if name.endswith(".broken_hall"):
            raise SyntaxError("bad hook")
        return real(name, *a, **k)
    monkeypatch.setattr(importlib, "import_module", fake)
    ctx = _ctx()
    load_venue_hooks(ctx)
    load_venue_hooks(ctx)                                                         # reported once per ctx
    assert ctx.errors == ["venue hook broken_hall: import failed (SyntaxError: bad hook)"]


def test_crawl_sites_runs_pre_fetch_and_pre_source(registry, monkeypatch):
    """crawl_sites maps cfgs through pre_fetch before fetching and fr through pre_source before sources."""
    from src.sources import html_sites
    cfg = _cfg(hook="dummy_hall", extract=["llm"])
    monkeypatch.setattr(html_sites, "_venues", lambda p, t: (get_city("london"), [cfg]))
    seen: dict = {}

    async def fake_fetch_all(vs, **kw):
        seen["urls"] = [v.url for v in vs]
        return {v.name: FetchResult(FetchStatus.OK, url=v.url, body="NAV<p>x</p>") for v in vs}

    async def fake_parse(c, fr, ctx):
        seen["body"], seen["url"] = fr.body, c.url
        return [], {"events": 0, "source": None, "raws": 0, "fetch": fr.status.value, "build.failed": 0,
                    "dropped_past": 0, "stats": {}, "errors": []}
    monkeypatch.setattr(html_sites, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(html_sites, "parse_venue", fake_parse)
    monkeypatch.setattr(html_sites.timing, "log_runtime", lambda **kw: None)
    asyncio.run(html_sites.crawl_sites(None))
    assert seen == {"urls": ["https://example.org/whats-on/?all=1"], "body": "<p>x</p>",
                    "url": "https://example.org/whats-on/?all=1"}


def test_coliseum_stays_an_adapter():
    """The Coliseum module parses <script id=json-data> (a parser, not a fix): it stays an adapter."""
    from src.sources.adapters import get_adapter
    assert get_adapter({"adapter": {"kind": "coliseum"}}).direct
