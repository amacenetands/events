#!/usr/bin/env python3
"""
London / Moscow events: crawl once, query many ways.

  python main.py crawl --city london [--force-download --force-parse --fetch-only --parse-only --test-site NAME]
  python main.py query --city london --when today --audience family --kinds one_off,run [--time evening]
                       [--days weekday] [--category concert] [--new-only] [--rank|--no-rank]
                       [--preset NAME] [--send] [--mask] [--russian] [--max-age-days 7] [--auto-crawl]

Legacy flags still work (crawl cached sites, then query with the matching preset
from pipelines.yaml; they SEND email unless --dry-run):
  python main.py [--family|--evening|--daytime|--museums-only|--theatres|--tag moscow]
                 [--today] [--date-window N] [--dry-run] [--no-rank] [--no-dedup] [--mask]
                 [--fetch-only|--parse-only|--force-download|--force-parse|--test-site NAME]
"""
import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from src.sources import api_sources_for
from src.sources.html_sites import crawl_sites, configured_site_names
from src.dedup import filter_unseen, mark_seen
from src import rank as rankmod
from src.classify import classify
from src.tidy import tidy
from src.digest import generate_digest_v2, coverage_summary, save_digest_v2
from src.translate import translate_descriptions
from src.email_sender import send_digest
from src.models import Event
from src.query import (is_banned_url, Filter, query, describe, filter_from_dict, load_presets, preset_filter, parse_time_arg,
                       resolve_when, city_today, city_now)
from src import events_db, timing as _timing

from src import config as _config


def _city_tag(city: str) -> str | None:
    """Legacy pipeline tag for a city's crawl (cities.yaml: None for London, "moscow")."""
    return _config.legacy_pipeline_tag(city)


def _tags_by_source() -> dict[str, list[str]]:
    """Venue name → tags, over every sites entry (skip/disabled included: cached rows)."""
    return {v.name: list(v.tags) for v in _config.all_venues()}


def _dedup_by_fingerprint(events: list[Event]) -> list[Event]:
    """Within-run dedup by event_key (title|venue|date|time) so matinee + evening both survive.
    (Name kept for callers; seen-history still uses Event.fingerprint().)"""
    seen, out = set(), []
    for e in events:
        k = e.event_key()
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def _absolutize_urls(events: list[Event]) -> None:
    """Ensure every event url is absolute http(s): join against the source site url."""
    from urllib.parse import urljoin
    year = date.today().year
    base = {v.name: v.resolved_url(year) for v in _config.all_venues()}
    for e in events:
        u = (e.url or "").strip()
        if not u.startswith(("http://", "https://")):
            b = base.get(e.source, "")
            e.url = urljoin(b, u) if b else u
            if not e.url.startswith(("http://", "https://")):
                e.url = b or e.url


# ---------------------------------------------------------------------------
# crawl
# ---------------------------------------------------------------------------

async def crawl(
    city: str,
    *,
    force_download: bool = False,
    force_parse: bool = False,
    fetch_only: bool = False,
    parse_only: bool = False,
    test_site: str | None = None,
) -> int:
    """Fetch (stale pages only, unless forced) + parse + classify venues for city; persist
    to the events table. test_site: only that venue, events printed, nothing persisted."""
    t0 = _timing.now()
    tag = _city_tag(city)
    print(f"Crawl: {city}\n" + "=" * 40)

    errors: list[str] = []
    sites = crawl_sites(pipeline=tag, test_site=test_site, force_download=force_download,
                        force_parse=force_parse, fetch_only=fetch_only, parse_only=parse_only, errors=errors)
    if fetch_only:
        await sites
        return 0
    api = [] if test_site else [src(city=city) for src in api_sources_for(_config.get_city(city).api_sources)]   # cities.yaml
    results = await asyncio.gather(*api, sites, return_exceptions=True)
    scraped: list[Event] = []
    for r in results[:-1]:
        if isinstance(r, Exception):
            print(f"  Source error: {r}")
        elif isinstance(r, list):
            scraped.extend(classify(r))      # API events; html events are classified in crawl_sites
    if isinstance(results[-1], Exception):
        print(f"WARNING: HTML source failed entirely ({results[-1]}) — not saving; keeping previous {city} events.")
        return 0
    frs = results[-1].frs
    scraped.extend(results[-1].events)

    scraped = [e for e in scraped if not is_banned_url(e.url)]
    scraped = _dedup_by_fingerprint(scraped)
    _absolutize_urls(scraped)
    today = city_today(Filter(city=city))
    scraped = [e for e in scraped if (e.end_date or e.date) >= today]
    if test_site:
        print(f"\nExtracted {len(scraped)} events from {test_site!r} (not persisted):\n")
        for e in scraped:
            print(f"  • {e.title} | {e.venue} | {e.date}{'..' + str(e.end_date) if e.end_date else ''} | "
                  f"t={e.time} | {e.price or '-'} | {e.category} | kind={e.kind} | aud={e.audience} | "
                  f"min_age={e.min_age} | {e.url}")
        return len(scraped)
    if not scraped:
        print("Crawl produced 0 current events — keeping previous events table.")
        return 0
    cid = events_db.save_crawl(city, scraped, _tags_by_source(), errors=errors,
                              oldest_fetched_at=_oldest_fetched(frs, scraped))
    print(f"Saved {len(scraped)} events for {city} (crawl {cid}); {len(errors)} sites with errors")
    for n in errors[:15]:
        print(f"  ! {n}")
    secs = round(_timing.now() - t0, 1)
    print(f"⏱ Total: {secs:.0f}s", flush=True)
    _timing.log_runtime(stage="crawl", city=city, events=len(scraped), secs=secs)
    return len(scraped)


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------

def _stale_venues(events, max_age_days: float) -> list[tuple[str, float]]:
    """(venue, age_days) for venues whose rows are in `events` but whose page was last
    fetched > max_age_days ago (oldest first). Per-source, so sites that failed this crawl
    and sites with no current rows never trigger a global stale warning."""
    try:
        from src import dedup
        year = date.today().year
        urls = {v.name: v.resolved_url(year) for v in _config.all_venues()}
        out = []
        for src in sorted({e.source for e in events if e.source in urls}):
            at = dedup.get_html_fetched_at(urls[src])
            if not at:
                continue
            d = datetime.fromisoformat(at)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            a = events_db.age_days(d)
            if a is not None and a > max_age_days:
                out.append((src, a))
        return sorted(out, key=lambda x: -x[1])
    except Exception:
        return []


def _oldest_fetched(frs: dict, events: list[Event]) -> str | None:
    """Oldest page fetch among venues that produced events this crawl (ISO), else None."""
    used = {e.source for e in events}
    ats = [fr.fetched_at for name, fr in frs.items() if name in used and fr.fetched_at]
    return min(ats).isoformat() if ats else None


def _age_str(age: float) -> str:
    return f"{age * 24:.0f}h" if age < 2 else f"{age:.1f}d"


async def run_query(
    f: Filter,
    *,
    preset: str | None = None,
    send: bool = False,
    mask: bool = False,
    do_rank: bool | None = None,
    llm_rank: bool = False,
    rank_backend: str | None = None,
    top_n: int = rankmod.TOP_N,
    russian: bool = False,
    max_age_days: float = 7,
    auto_crawl: bool = False,
    crawl_kwargs: dict | None = None,
    quiet: bool = False,
) -> list[Event]:
    loaded = events_db.load_events(f.city)
    today, now = city_today(f), city_now(f)
    loaded.events = tidy(classify(loaded.events), today, {n.lower() for n in configured_site_names(_city_tag(f.city), include_disabled=True)})   # re-apply deterministic rules so new classifier flags need no recrawl
    age = events_db.age_days(loaded.crawled_at)
    if age is None:
        print(f"WARNING: no crawl for {f.city}. Run: venv/bin/python main.py crawl --city {f.city}")
    elif age > max_age_days:
        print(f"WARNING: {f.city} crawl is {_age_str(age)} old (> {max_age_days}d). "
              f"Run: venv/bin/python main.py crawl --city {f.city}")
    else:
        print(f"Crawl: {f.city}, {_age_str(age)} old, {loaded.n_events} events")
    stale = _stale_venues(loaded.events, max_age_days)
    if stale:
        names = ", ".join(f"{n} ({_age_str(a)})" for n, a in stale[:5])
        more = f", +{len(stale) - 5} more" if len(stale) > 5 else ""
        print(f"WARNING: {len(stale)} venues' data is older than {max_age_days:g}d: {names}{more}. "
              f"Re-crawl: venv/bin/python main.py crawl --city {f.city} --force-download")
    if (age is None or age > max_age_days) and auto_crawl:
        await crawl(f.city, **(crawl_kwargs or {}))
        loaded = events_db.load_events(f.city)

    qstats: dict = {}
    events = query(loaded.events, f, today=today, tags_by_source=loaded.tags_by_source, unseen=filter_unseen,
                   stats=qstats)
    hid = qstats.get("untimed_hidden", 0)
    print(f"Filter: {describe(f, today)}\n  {len(loaded.events)} crawled → {len(events)} match"
          + (f" ({hid} untimed one-offs hidden: no start time, not daytime-inferable)" if hid else ""))

    ctx = rankmod.build_ctx(loaded.first_seen, loaded.events, today=today, now=now, city=f.city)
    new_fps = ctx.new_fps

    rank_on = (True if (f.rank if do_rank is None else do_rank) is None else (f.rank if do_rank is None else do_rank)) or llm_rank
    picks = reasons = scores = None
    if rank_on:
        res = await rankmod.rank_events(events, f, ctx, n=top_n, use_llm=llm_rank, backend=rank_backend)
        picks, reasons, scores = res.picks, res.reasons, res.scores
        print(f"Ranked: {len(picks)} top picks of {len(scores)} candidates"
              f"{' (LLM rerank)' if res.llm_used else ''}; all {len(events)} events kept")
        for i, e in enumerate(picks, 1):
            print(f"  {i:2}. {e.title[:60]} | {e.venue} | {e.date:%a %d %b}{' ' + e.time if e.time else ''}"
                  f" | {e.category} | {reasons.get(e.fingerprint(), '')}")

    if russian:
        print("Translating descriptions to Russian...")
        await translate_descriptions(events)

    footer = ""
    if f.coverage:
        base = query(loaded.events, Filter(city=f.city, when=f.when, drop_religious=False, public_only=False,
                                           include_online=True), today=today)
        counts: dict[str, int] = {}
        for e in base:
            counts[e.source] = counts.get(e.source, 0) + 1
        footer = coverage_summary(counts, configured_site_names(_city_tag(f.city)))

    digest = generate_digest_v2(events, f, picks=picks, new_fps=new_fps, title=f.label, footer=footer,
                               reasons=reasons, scores=scores, today=today, lang=f.lang)
    slug = f"{preset or 'events'}_{f.city}"
    path = save_digest_v2(digest, slug)
    print(f"Saved: {path}")
    if not quiet:
        print("\n" + "=" * 40 + "\n" + digest + "\n" + "=" * 40)

    if send:
        send_digest(digest, dry_run=False, label=f.label or f"{f.city.capitalize()} Events")
        if mask:
            mark_seen([e for e in events if e.kind != "run"])
            print("Marked as seen in dedup DB")
    else:
        print("[dry] not emailed (use --send)." + (" --mask ignored without --send." if mask else ""))
    return events


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_crawl_flags(p):
    p.add_argument("--force-download", action="store_true")
    p.add_argument("--force-parse", action="store_true")
    p.add_argument("--fetch-only", action="store_true", help="fetch HTML to cache only")
    p.add_argument("--parse-only", action="store_true", help="parse from cache only — no HTTP fetches")
    p.add_argument("--test-site", metavar="NAME", help="only this venue: fetch if stale, parse, print, do not persist")


def build_filter(args) -> Filter:
    base = load_presets()[args.preset] if args.preset else {}
    ov: dict = {}
    if args.city:
        ov["city"] = args.city
    if args.when:
        ov["when"] = args.when
    if args.audience:
        ov["audience"] = args.audience
    if args.kinds:
        ov["kinds"] = args.kinds
    if args.time:
        ov["time"] = args.time
    if args.strict_time:
        ov["strict_time"] = True
    if args.days:
        ov["days"] = args.days
    if args.category:
        ov["categories"] = args.category
    if args.venue:
        ov["venues"] = args.venue
    if args.venue_tag:
        ov["venue_tags"] = args.venue_tag
    if args.min_age_max is not None:
        ov["min_age_max"] = args.min_age_max
    if args.new_only:
        ov["new_only"] = True
    if args.keep_religious:
        ov["drop_religious"] = False
    if args.include_private:
        ov["public_only"] = False
    if args.include_online:
        ov["include_online"] = True
    _cname = ov.get("city") or base.get("city")
    try:
        _lang = _config.get_city(_cname).lang
    except KeyError:
        raise SystemExit(f"unknown city {_cname!r} (preset {args.preset!r}); "
                         f"known: {', '.join(_config.city_names())}") from None
    if _lang != "en" and "lang" not in base:
        ov["lang"] = _lang         # e.g. Moscow digests default to RU headings (cities.yaml lang)
    f = filter_from_dict(base, **ov)
    resolve_when(f.when)   # validate early
    return f


def cmd_query(argv):
    p = argparse.ArgumentParser(prog="main.py query")
    p.add_argument("--preset")
    p.add_argument("--city", choices=_config.city_names())
    p.add_argument("--when", help="today|tomorrow|weekend|next:N (N days incl. today)|range:YYYY-MM-DD..YYYY-MM-DD")
    p.add_argument("--audience", choices=["any", "family", "kids", "adult"])
    p.add_argument("--kinds", help="comma list of one_off,run,recurring")
    p.add_argument("--time", help="any | morning,afternoon,evening | daytime")
    p.add_argument("--strict-time", action="store_true", help="drop events without a time when --time set")
    p.add_argument("--days", choices=["any", "weekday", "weekend"])
    p.add_argument("--category", help="comma list: concert,theatre,exhibition,talk,workshop,tour,film,comedy,festival,other")
    p.add_argument("--venue", help="comma list of venue names")
    p.add_argument("--venue-tag", help="comma list of venue tags (sites/<city>.yaml) (museums, theatres, family...)")
    p.add_argument("--min-age-max", type=int)
    p.add_argument("--new-only", action="store_true", help="skip one-offs already sent (history)")
    p.add_argument("--keep-religious", action="store_true")
    p.add_argument("--include-private", action="store_true", help="keep school/members/trade/private-hire events")
    p.add_argument("--include-online", action="store_true", help="keep webinar/livestream-only events")
    p.add_argument("--rank", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--llm-rank", action="store_true", help="LLM rerank of top candidates (implies --rank)")
    p.add_argument("--rank-backend", choices=["deepseek", "anthropic", "claude", "ollama"],
                   help="LLM for --llm-rank (implies it): anthropic = Claude API (ANTHROPIC_API_KEY), "
                        "claude = Claude CLI; default $RANK_BACKEND or deepseek")
    p.add_argument("--top", type=int, default=rankmod.TOP_N, help="number of top picks")
    p.add_argument("--send", action="store_true", help="email the digest (default: print only)")
    p.add_argument("--dry-run", action="store_true", help="explicit no-send (default)")
    p.add_argument("--mask", action="store_true", help="mark shown one-offs as seen (needs --send)")
    p.add_argument("--russian", action="store_true")
    p.add_argument("--max-age-days", type=float, default=7)
    p.add_argument("--auto-crawl", action="store_true")
    p.add_argument("--parse-only", action="store_true", help="with --auto-crawl: parse cached html only")
    p.add_argument("--quiet", action="store_true", help="don't print the digest body")
    a = p.parse_args(argv)
    f = build_filter(a)
    asyncio.run(run_query(
        f, preset=a.preset, send=a.send and not a.dry_run, mask=a.mask, do_rank=a.rank, llm_rank=a.llm_rank or bool(a.rank_backend), rank_backend=a.rank_backend, top_n=a.top, russian=a.russian,
        max_age_days=a.max_age_days, auto_crawl=a.auto_crawl,
        crawl_kwargs=dict(parse_only=a.parse_only), quiet=a.quiet,
    ))


def cmd_crawl(argv):
    p = argparse.ArgumentParser(prog="main.py crawl")
    p.add_argument("--city", choices=_config.city_names(), default="london")
    _add_crawl_flags(p)
    a = p.parse_args(argv)
    asyncio.run(crawl(a.city, force_download=a.force_download, force_parse=a.force_parse,
                      fetch_only=a.fetch_only, parse_only=a.parse_only, test_site=a.test_site))


def legacy(argv):
    p = argparse.ArgumentParser(description="Events digest (legacy flags → crawl + query)")
    p.add_argument("--dry-run", action="store_true", help="no email, no dedup marking")
    p.add_argument("--send-test", action="store_true", help="send email WITHOUT marking events seen (overrides --mask)")
    p.add_argument("--include-private", action="store_true", help="keep school/members/trade/private-hire events")
    p.add_argument("--include-online", action="store_true", help="keep webinar/livestream-only events")
    p.add_argument("--mask", action="store_true", help="mark events as seen after sending")
    p.add_argument("--no-rank", action="store_true")
    p.add_argument("--museums-only", action="store_true")
    p.add_argument("--today", action="store_true", help="today only (overrides the preset window)")
    p.add_argument("--family", action="store_true")
    p.add_argument("--daytime", action="store_true")
    p.add_argument("--evening", action="store_true")
    p.add_argument("--theatres", action="store_true")
    p.add_argument("--no-dedup", action="store_true")
    p.add_argument("--date-window", type=int, metavar="N")
    p.add_argument("--russian", action="store_true")
    p.add_argument("--tag", metavar="NAME", help="named preset (e.g. moscow)")
    _add_crawl_flags(p)
    a = p.parse_args(argv)

    name = a.tag or next((n for n, on in [("family", a.family), ("daytime", a.daytime), ("evening", a.evening),
                                          ("theatres", a.theatres), ("museums", a.museums_only)] if on), "standard")
    ov: dict = {}
    if a.today:
        ov["when"] = "today"
    elif a.date_window:
        ov["when"] = f"next:{a.date_window}"
    if a.no_dedup:
        ov["new_only"] = False
    if a.include_private:
        ov["public_only"] = False
    if a.include_online:
        ov["include_online"] = True
    f = preset_filter(name, **ov)
    ck = dict(force_download=a.force_download, force_parse=a.force_parse, fetch_only=a.fetch_only,
              parse_only=a.parse_only, test_site=a.test_site)

    async def go():
        n = await crawl(f.city, **ck)
        if a.fetch_only or a.test_site:
            return
        if n == 0 and events_db.load_events(f.city).n_events == 0:
            return
        await run_query(f, preset=name, send=not a.dry_run, mask=a.mask and not a.dry_run and not a.send_test,
                        do_rank=False if a.no_rank else None, russian=a.russian, max_age_days=1e9)
    asyncio.run(go())


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "query":
        cmd_query(argv[1:])
    elif argv and argv[0] == "crawl":
        cmd_crawl(argv[1:])
    else:
        legacy(argv)


if __name__ == "__main__":
    main()
