"""
HTML venue crawl: fetch listing pages (src/fetch), then parse each venue through its
ordered `extract` sources (pipeline v3 §1.2).

    crawl_sites(...)  — fetch_all (or the stored scrapes with --parse-only) -> per venue:
                        sources (jsonld, adapter, follow_links, spektrix, llm, ...) ->
                        build_events -> classify
    parse_sites(...)  — the same over stored pages only (offline), events only
"""
import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from .. import timing
from ..classify import classify
from ..fetch import cached_results, fetch_all
from ..fetch.types import FetchResult, FetchStatus
from ..models import Event
from .base import SourceCtx, get_source, safe_records
from .build import build_events
from ..venues import run_hook

WINDOW_DAYS = 90               # API sources (spektrix) fetch this far ahead; page sources never filter
VENUE_CONCURRENCY = 32         # venues parsed at once (LLM / detail-page fetches have their own limits)
# Sources that read the listing page; skipped when there is no body (api mode, nothing stored).
BODY_SOURCES = frozenset({"jsonld", "microdata", "time_tags", "next_data", "adapter", "follow_links", "llm"})
# save_crawl rule: a venue with >= 1 event and no ctx.errors entry replaces its rows; a venue
# with any error (source raised, partial LLM, stale page, dead API...) is merged into its old
# rows; a venue with 0 events never wipes anything. Sources write ctx.errors for real
# failures only (base.py).


def city_today(city) -> date:
    return datetime.now(ZoneInfo(city.tz)).date()


def _load_sites(pipeline: str | None = None, include_disabled: bool = False) -> list[dict]:
    """Legacy-shaped site dicts for one pipeline tag, via config.load_venues.
    pipeline=None → London default (Moscow-tagged venues excluded); a Moscow tag
    (moscow / moscow-theatre) → that city's venues carrying it; any other tag →
    London venues carrying it. {YEAR} in urls is resolved to the current year."""
    from .. import config
    city = config.city_for_tag(pipeline)
    year = city_today(config.get_city(city)).year
    venues = config.load_venues(city, include_disabled=include_disabled,
                                tags=[pipeline] if pipeline else None)
    out = []
    for v in venues:
        d = v.to_legacy_dict()
        d["url"] = v.resolved_url(year)
        out.append(d)
    return out


def configured_site_names(pipeline: str | None = None, include_disabled: bool = False) -> list[str]:
    """Names of all non-skipped sites for a pipeline. include_disabled keeps
    fetch-disabled (bot-blocked) venues so their cached rows still surface."""
    return [s["name"] for s in _load_sites(pipeline=pipeline, include_disabled=include_disabled)]


def _venues(pipeline: str | None, test_site: str | None):
    from .. import config
    city = config.city_for_tag(pipeline)
    venues = config.load_venues(city, tags=[pipeline] if pipeline else None)
    if test_site:
        venues = [v for v in venues if v.name.lower() == test_site.lower()]
    return config.get_city(city), venues


def _spektrix_client(cfg, fr: FetchResult):
    try:
        from .spektrix import client_for_venue
    except ModuleNotFoundError:
        return None
    return client_for_venue(cfg, fr.body or None)


def source_plan(cfg, fr: FetchResult, ctx: SourceCtx | None = None) -> list[str]:
    """Source kinds to run, in order: cfg.extract (+ `spektrix` first when the adapter is
    `kind: spektrix`); `adapter` only with a page adapter configured; page sources only
    with a body. A Spektrix fingerprint on an unlisted venue is only counted
    (`spektrix.detected_unlisted`), never run."""
    kinds = list(dict.fromkeys(cfg.extract))
    akind = (cfg.adapter or {}).get("kind")
    if akind == "spektrix" and "spektrix" not in kinds:
        kinds.insert(0, "spektrix")
    out = []
    for k in kinds:
        if k == "adapter" and (not cfg.adapter or akind == "spektrix"):
            continue
        if k in BODY_SOURCES and not fr.body:
            continue
        out.append(k)
    if ctx is not None and "spektrix" not in out and fr.body and _spektrix_client(cfg, fr) is not None:
        ctx.stats["spektrix.detected_unlisted"] = 1
    return out


async def parse_venue(cfg, fr: FetchResult, ctx: SourceCtx) -> tuple[list[Event], dict]:
    """(events, stats) for one venue: ordered sources, first non-empty wins (all with
    `merge: true`), then build_events. `ctx` is this venue's own context."""
    raws, won = [], []
    if fr.body and not fr.ok:
        ctx.errors.append(f"{cfg.name}: stale page ({fr.note or fr.status.value})")
    for kind in source_plan(cfg, fr, ctx):
        try:
            src = get_source(kind)
        except KeyError:
            ctx.stats[f"{kind}.not_implemented"] = 1
            continue
        got = await safe_records(src, cfg, fr, ctx)
        if got:
            raws.extend(got)
            won.append(kind)
            if not cfg.merge:
                break
    events = build_events(raws, cfg, ctx)
    stats = {"events": len(events), "source": "+".join(won) or None, "raws": len(raws),
             "fetch": fr.status.value, "build.failed": ctx.stats.get("build.failed", 0),
             "dropped_past": ctx.stats.get("build.dropped_past", 0),
             "stats": dict(ctx.stats), "errors": list(ctx.errors)}
    return events, stats


@dataclass
class SitesResult:
    events: list[Event] = field(default_factory=list)
    frs: dict[str, FetchResult] = field(default_factory=dict)    # venue -> fetch result


async def crawl_sites(
    pipeline: str | None = None,
    test_site: str | None = None,
    *,
    force_download: bool = False,
    force_parse: bool = False,
    fetch_only: bool = False,
    parse_only: bool = False,
    errors: list[str] | None = None,
    summary: dict | None = None,
    frs: dict[str, FetchResult] | None = None,
) -> SitesResult:
    """Fetch (src/fetch; stored pages only with parse_only, then sources are offline) and
    parse every venue; classified Events + the FetchResults. `errors` gets one
    "<venue>: <why>" per venue with any ctx error (save_crawl merges those); `summary`
    gets {venue: stats}. `frs` (tests) replaces fetching."""
    _t0 = timing.now()
    city, venues = _venues(pipeline, test_site)
    if test_site and not venues:
        print(f"  [crawl] Site '{test_site}' not found in {city.sites}")
        return SitesResult()
    today = city_today(city)
    offline = parse_only and not fetch_only
    res = SitesResult()
    async with httpx.AsyncClient() as client:
        ctxs = {v.name: SourceCtx(http=client, today=today, window_days=WINDOW_DAYS, city=city,
                                  force=force_download, force_parse=force_parse, offline=offline)
                for v in venues}
        venues = [run_hook("pre_fetch", v, v, ctxs[v.name]) for v in venues]     # venue hooks
        if frs is not None:
            res.frs = frs
        elif offline:
            res.frs = cached_results(venues, today)
        else:
            t = timing.now()
            res.frs = await fetch_all(venues, client=client, force=force_download, today=today)
            counts: dict[str, int] = {}
            for fr in res.frs.values():
                counts[fr.status.value] = counts.get(fr.status.value, 0) + 1
            print(f"  [fetch] {len(res.frs)} venues in {timing.now() - t:.0f}s: "
                  + " ".join(f"{k}={v}" for k, v in sorted(counts.items())), flush=True)
        if fetch_only:
            return res
        sem = asyncio.Semaphore(VENUE_CONCURRENCY)

        async def one(cfg) -> tuple[list[Event], dict]:
            fr = res.frs.get(cfg.name) or FetchResult(FetchStatus.ERROR, url=cfg.url, note="not fetched")
            async with sem:
                vctx = ctxs[cfg.name]
                fr = run_hook("pre_source", cfg, fr, vctx) or FetchResult(
                    FetchStatus.ERROR, url=cfg.url, note="dropped by venue hook")
                try:
                    return await parse_venue(cfg, fr, vctx)
                except Exception as ex:   # noqa: BLE001 — a build bug must not sink the crawl
                    return [], {"events": 0, "source": None, "raws": 0, "fetch": fr.status.value,
                                "build.failed": 0, "dropped_past": 0, "stats": {"parse.raised": 1},
                                "errors": [f"{cfg.name}: parse raised {type(ex).__name__}: {str(ex)[:80]}"]}

        results = await asyncio.gather(*(one(v) for v in venues))

    for cfg, (evs, st) in zip(venues, results):
        res.events.extend(evs)
        if summary is not None:
            summary[cfg.name] = st
        extra = "".join(f" {k}={st[k]}" for k in ("build.failed", "dropped_past") if st[k])
        flags = "".join(f" {k}" for k in st["stats"] if k.endswith(("thin", "detected_unlisted", "offline")))
        print(f"  [parse] {cfg.name}: {st['events']} events [{st['source'] or '-'}] raws={st['raws']} "
              f"fetch={st['fetch']}{extra}{flags}", flush=True)
        for er in st["errors"][:3]:
            print(f"      ! {er}", flush=True)
        if errors is not None and st["errors"]:
            errors.append(st["errors"][0])

    res.events = classify(res.events)
    dt = timing.now() - _t0
    print(f"  [parse] {len(venues)} venues → {len(res.events)} events in {dt:.0f}s", flush=True)
    timing.log_runtime(stage="parse", pipeline=pipeline, venues=len(venues), events=len(res.events),
                       secs=round(dt, 1))
    return res


async def parse_sites(pipeline: str | None = None, test_site: str | None = None, force_parse: bool = False,
                      errors: list[str] | None = None, summary: dict | None = None,
                      frs: dict[str, FetchResult] | None = None) -> list[Event]:
    """Offline parse of the stored pages (no listing fetches, no API calls); Events only."""
    return (await crawl_sites(pipeline, test_site, force_parse=force_parse, parse_only=True,
                              errors=errors, summary=summary, frs=frs)).events
