#!/usr/bin/env python3
"""
London events digest.

Usage:
  python main.py                        # full weekly run: fetch, save, email
  python main.py --family               # family pipeline: 30-day window, 3-section digest
  python main.py --daytime              # weekday daytime concerts pipeline
  python main.py --dry-run              # no email, no dedup marking
  python main.py --send-test            # email but skip dedup marking
  python main.py --mask                 # mark events as seen after sending
  python main.py --no-rank              # skip Claude ranking, include all one-off events
  python main.py --no-dedup             # skip seen-events history filter
  python main.py --date-window N        # override date window (default: 14 standard, 30 family/daytime)
  python main.py --today                # filter to today only
  python main.py --test-site "Name"     # test single site from sites.yaml
  python main.py --museums-only         # museums pipeline only
  python main.py --force-download       # bypass download cache, re-fetch all sites
  python main.py --force-parse          # bypass parse cache, re-run LLM extraction
"""
import argparse
import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

_crawl4ai_dir = os.environ.get("CRAWL4AI_BASE_DIRECTORY", "/tmp/.crawl4ai")
os.environ["CRAWL4AI_BASE_DIRECTORY"] = _crawl4ai_dir
Path(_crawl4ai_dir).mkdir(parents=True, exist_ok=True)

from src.sources import API_SOURCES
from src.sources.html_sites import fetch_html_sites
from src.dedup import (
    filter_unseen, mark_seen,
    load_cached_exhibitions, upsert_exhibitions, purge_expired_exhibitions,
)
from src.rank import pre_filter, rank
from src.digest import (
    generate_digest, save_digest,
    generate_family_digest, save_family_digest,
    generate_daytime_digest, save_daytime_digest,
)
from src.translate import translate_descriptions
from src.email_sender import send_digest
from src.models import Event


# ---------------------------------------------------------------------------
# Shared helpers (unchanged)
# ---------------------------------------------------------------------------

def _filter_date_window(events: list[Event], window: int) -> list[Event]:
    from datetime import date as _date, timedelta
    today = _date.today()
    cutoff = today + timedelta(days=window)
    result = []
    for e in events:
        if e.is_one_off:
            if today <= e.date <= cutoff:
                result.append(e)
        else:
            end = e.end_date or cutoff
            if end >= today:
                result.append(e)
    return result


def _dedup_within_run(events: list[Event]) -> list[Event]:
    # Dedup by fingerprint (title|venue|date) only. NOT by URL: single-venue
    # sites emit many distinct events that all share the venue's listing-page
    # URL (no per-event links), so URL-deduping collapsed them to one event.
    # True cross-source duplicates share a fingerprint, so nothing is lost.
    seen_fp: set[str] = set()
    unique = []
    for e in events:
        fp = e.fingerprint()
        if fp not in seen_fp:
            seen_fp.add(fp)
            unique.append(e)
    return unique


def _crawl_summary(counts: dict[str, int]) -> str:
    lines = ["", "---", "## Venues Parsed", ""]
    total = sum(counts.values())
    lines.append(f"_{len(counts)} venues · {total} events extracted_")
    lines.append("")
    lines.append("| Venue | Events |")
    lines.append("|---|---|")
    for venue, count in sorted(counts.items(), key=lambda x: -x[1]):
        lines.append(f"| {venue} | {count} |")
    return "\n".join(lines)


def _emit_digest(
    events: list[Event],
    generate_fn: Callable,
    save_fn: Callable[[str], Path],
    dry_run: bool,
    crawl_counts: dict[str, int] | None = None,
) -> None:
    digest = generate_fn(events)
    if crawl_counts is not None:
        digest += _crawl_summary(crawl_counts)
    path = save_fn(digest)
    print(f"Saved: {path}")
    print("\n" + "=" * 40)
    print(digest)
    print("=" * 40)
    send_digest(digest, dry_run=dry_run)


# ---------------------------------------------------------------------------
# PipelineSpec + assemble-result container
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PipelineSpec:
    name: str                    # header printed, e.g. "London Events Digest"
    pipeline_tag: str | None     # html_sites pipeline filter: None | "museums" | "family" | "daytime"
    include_apis: bool           # whether to also gather API_SOURCES
    default_window: int          # 14 / 30 / 3
    save_fn: Callable            # save_digest / save_family_digest / save_daytime_digest
    assemble: Callable           # (scraped, ctx) -> (events, generate_fn) | None


@dataclass
class _Ctx:
    """Flags + runtime state passed to each assemble function."""
    flags_dry_run: bool
    flags_send_test: bool
    flags_mask: bool
    flags_no_rank: bool
    flags_no_dedup: bool
    flags_today_only: bool
    flags_russian: bool
    window: int
    extraction_errors: list[str]


# ---------------------------------------------------------------------------
# Per-pipeline assemble functions
# ---------------------------------------------------------------------------

async def _assemble_standard(
    scraped: list[Event],
    ctx: _Ctx,
) -> tuple[list[Event], Callable, dict] | None:
    """Standard (London Events) pipeline core — verbatim logic from run()."""
    window = ctx.window

    crawl_counts: dict[str, int] = {}
    for e in scraped:
        crawl_counts[e.source] = crawl_counts.get(e.source, 0) + 1

    print(f"\nRaw total: {len(scraped)} events")
    all_events = _filter_date_window(scraped, window)
    all_events = _dedup_within_run(all_events)
    print(f"After date filter + dedup: {len(all_events)}")

    all_one_offs = [e for e in all_events if e.is_one_off]
    exhibitions = [e for e in all_events if not e.is_one_off]

    one_offs = all_one_offs if ctx.flags_no_dedup else filter_unseen(all_one_offs)
    print(f"After history dedup: {len(one_offs)} new one-offs, {len(exhibitions)} exhibitions")

    if ctx.flags_today_only:
        from datetime import date as _date
        today = _date.today()
        one_offs = [e for e in one_offs if e.date == today]
        exhibitions = [e for e in exhibitions if e.date <= today <= (e.end_date or today)]
        print(f"After today filter ({today}): {len(one_offs)} one-offs, {len(exhibitions)} exhibitions")

    if not one_offs and not exhibitions:
        return None

    candidates = pre_filter(one_offs)
    print(f"After scoring: {len(candidates)} candidates → Claude")

    if ctx.flags_no_rank:
        ranked = candidates
        rest_events: list[Event] = []
        print(f"Skipping ranking — {len(ranked)} candidates ordered by score")
    else:
        ranked = await rank(candidates)
        print(f"After Claude ranking: {len(ranked)} top picks")
        ranked_ids = {id(e) for e in ranked}
        rest_events = [e for e in candidates if id(e) not in ranked_ids]

    should_mask = ctx.flags_mask and not ctx.flags_dry_run and not ctx.flags_send_test
    if should_mask:
        mark_seen(one_offs)
        print("Marked as seen in dedup DB")

    lang = "ru" if ctx.flags_russian else "en"
    if ctx.flags_russian:
        print("Translating descriptions to Russian...")
        await translate_descriptions(ranked + rest_events + exhibitions)

    generate_fn = lambda events: generate_digest(events, rest=rest_events, exhibitions=exhibitions, lang=lang)
    # crawl_counts built over ALL raw events before date filter — attach via closure
    return (ranked, generate_fn, crawl_counts)


async def _assemble_family(
    scraped: list[Event],
    ctx: _Ctx,
) -> tuple[list[Event], Callable, dict] | None:
    """Family pipeline core — verbatim logic from run_family()."""
    window = ctx.window

    # cached_exhibitions already loaded before sources were gathered;
    # stashed in ctx via a field set by run_pipeline.
    cached_exhibitions: list[Event] = ctx._family_cached_exhibitions  # type: ignore[attr-defined]
    cached_fps = {e.fingerprint() for e in cached_exhibitions}

    scraped = _filter_date_window(scraped, window)
    scraped = [e for e in scraped if e.is_kids or e.is_family]
    print(f"After date filter + family filter: {len(scraped)} events")

    crawl_counts: dict[str, int] = {}
    for e in scraped:
        crawl_counts[e.source] = crawl_counts.get(e.source, 0) + 1

    should_mask = ctx.flags_mask and not ctx.flags_dry_run and not ctx.flags_send_test
    if should_mask:
        upsert_exhibitions(scraped)
        print("Updated exhibition cache")

    scraped_one_offs = [e for e in scraped if e.is_one_off]
    new_exhibitions = [e for e in scraped if not e.is_one_off and e.fingerprint() not in cached_fps]
    new_exhibition_fps = {e.fingerprint() for e in new_exhibitions}
    all_exhibitions = _dedup_within_run(new_exhibitions + cached_exhibitions)

    unseen_one_offs = _dedup_within_run(scraped_one_offs if ctx.flags_no_dedup else filter_unseen(scraped_one_offs))
    print(f"One-offs: {len(unseen_one_offs)} new · Exhibitions: {len(all_exhibitions)} total ({len(new_exhibitions)} new)")

    if not unseen_one_offs and not all_exhibitions:
        return None

    if should_mask:
        mark_seen(unseen_one_offs)

    lang = "ru" if ctx.flags_russian else "en"
    all_events_for_digest = unseen_one_offs + all_exhibitions
    if ctx.flags_russian:
        print("Translating descriptions to Russian...")
        await translate_descriptions(all_events_for_digest)

    extraction_errors = ctx.extraction_errors
    generate_fn = lambda events: generate_family_digest(
        events,
        new_exhibition_fps=new_exhibition_fps,
        failed_sites=extraction_errors or None,
        lang=lang,
    )
    return (all_events_for_digest, generate_fn, crawl_counts)


async def _assemble_daytime(
    scraped: list[Event],
    ctx: _Ctx,
) -> tuple[list[Event], Callable, dict] | None:
    """Daytime pipeline core — verbatim logic from run_daytime()."""
    window = ctx.window

    events = _filter_date_window(scraped, window)
    events = [e for e in events if e.date.weekday() < 5 and (e.time is None or int(e.time.split(":")[0]) < 16)]
    print(f"After date + weekday + daytime filter: {len(events)} events")

    crawl_counts: dict[str, int] = {}
    for e in events:
        crawl_counts[e.source] = crawl_counts.get(e.source, 0) + 1

    if not events:
        return None

    lang = "ru" if ctx.flags_russian else "en"
    if ctx.flags_russian:
        print("Translating descriptions to Russian...")
        await translate_descriptions(events)

    generate_fn = lambda evts: generate_daytime_digest(evts, date_window=window, lang=lang)
    return (events, generate_fn, crawl_counts)


# ---------------------------------------------------------------------------
# Pipeline specs
# ---------------------------------------------------------------------------

SPEC_STANDARD = PipelineSpec(
    name="London Events Digest",
    pipeline_tag=None,           # overridden to "museums" when museums_only=True
    include_apis=True,           # overridden to False when museums_only=True
    default_window=14,
    save_fn=save_digest,
    assemble=_assemble_standard,
)

SPEC_FAMILY = PipelineSpec(
    name="London Family Events Digest",
    pipeline_tag="family",
    include_apis=False,
    default_window=30,
    save_fn=save_family_digest,
    assemble=_assemble_family,
)

SPEC_DAYTIME = PipelineSpec(
    name="London Daytime Concerts Digest",
    pipeline_tag="daytime",
    include_apis=False,
    default_window=3,
    save_fn=save_daytime_digest,
    assemble=_assemble_daytime,
)


# ---------------------------------------------------------------------------
# Shared skeleton
# ---------------------------------------------------------------------------

async def run_pipeline(
    spec: PipelineSpec,
    *,
    dry_run: bool = False,
    send_test: bool = False,
    mask: bool = False,
    no_rank: bool = False,
    no_dedup: bool = False,
    today_only: bool = False,
    test_site: str | None = None,
    date_window: int | None = None,
    force_download: bool = False,
    force_parse: bool = False,
    russian: bool = False,
    # standard-only override flags (passed in by run() when museums_only=True)
    _pipeline_tag_override: str | None = None,
    _include_apis_override: bool | None = None,
) -> None:
    # 1. Header
    print(spec.name)
    print("=" * 40)

    pipeline_tag = _pipeline_tag_override if _pipeline_tag_override is not None else spec.pipeline_tag
    include_apis = _include_apis_override if _include_apis_override is not None else spec.include_apis

    # 2. Window
    window = date_window or spec.default_window

    # 2a. Family pre-fetch setup
    cached_exhibitions: list[Event] = []
    if spec is SPEC_FAMILY:
        purge_expired_exhibitions()

    # 3. test_site branch — distinct print format per pipeline
    if test_site:
        print(f"Testing site: {test_site}")
        events = await fetch_html_sites(test_site=test_site, force_download=force_download, force_parse=force_parse)
        events = _filter_date_window(events, window)
        if spec is SPEC_FAMILY:
            events = [e for e in events if e.is_kids or e.is_family]
            print(f"\nExtracted {len(events)} family events:\n")
            for e in events:
                print(f"  • {e.title} | {e.venue} | {e.date} | one_off={e.is_one_off} | kids={e.is_kids} | family={e.is_family}")
        elif spec is SPEC_DAYTIME:
            events = [e for e in events if e.date.weekday() < 5 and (e.time is None or int(e.time.split(":")[0]) < 16)]
            print(f"\nExtracted {len(events)} daytime events:\n")
            for e in events:
                print(f"  • {e.title} | {e.venue} | {e.date} | {e.time} | {e.category}")
        else:
            print(f"\nExtracted {len(events)} events:\n")
            for e in events:
                print(f"  • {e.title} | {e.venue} | {e.date} | {e.category} | one_off={e.is_one_off}")
        return

    # 4. Family: load cache after purge, before gather
    if spec is SPEC_FAMILY:
        cached_exhibitions = load_cached_exhibitions()
        print(f"Loaded {len(cached_exhibitions)} cached exhibitions")
        print(f"\nFetching from family sources ({window}-day window)...")
    else:
        print("\nFetching from all sources...")

    # 5. Gather sources
    extraction_errors: list[str] = []
    fetch_kwargs: dict = dict(force_download=force_download, force_parse=force_parse)
    if spec is SPEC_FAMILY:
        fetch_kwargs["errors"] = extraction_errors

    tasks = (
        [src() for src in API_SOURCES] if include_apis else []
    ) + [fetch_html_sites(pipeline=pipeline_tag, **fetch_kwargs)]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    scraped: list[Event] = []
    for r in results:
        if isinstance(r, Exception):
            print(f"  Source error: {r}")
        elif isinstance(r, list):
            scraped.extend(r)

    # 6. Build ctx and call assemble
    ctx = _Ctx(
        flags_dry_run=dry_run,
        flags_send_test=send_test,
        flags_mask=mask,
        flags_no_rank=no_rank,
        flags_no_dedup=no_dedup,
        flags_today_only=today_only,
        flags_russian=russian,
        window=window,
        extraction_errors=extraction_errors,
    )
    if spec is SPEC_FAMILY:
        ctx._family_cached_exhibitions = cached_exhibitions  # type: ignore[attr-defined]

    result = await spec.assemble(scraped, ctx)

    # 7. No-events early return — pipeline-specific message
    if result is None:
        if spec is SPEC_DAYTIME:
            print("No daytime events found.")
        else:
            print("No events found.")
        return

    events, generate_fn, crawl_counts = result

    # 8. Emit digest
    _emit_digest(events, generate_fn, spec.save_fn, dry_run, crawl_counts=crawl_counts)


# ---------------------------------------------------------------------------
# Public entry point (API-compatible with original run())
# ---------------------------------------------------------------------------

async def run(
    dry_run: bool = False,
    send_test: bool = False,
    mask: bool = False,
    no_rank: bool = False,
    test_site: str | None = None,
    museums_only: bool = False,
    today_only: bool = False,
    family: bool = False,
    daytime: bool = False,
    no_dedup: bool = False,
    date_window: int | None = None,
    force_download: bool = False,
    force_parse: bool = False,
    russian: bool = False,
) -> None:
    common = dict(
        dry_run=dry_run,
        send_test=send_test,
        mask=mask,
        no_rank=no_rank,
        no_dedup=no_dedup,
        today_only=today_only,
        test_site=test_site,
        date_window=date_window,
        force_download=force_download,
        force_parse=force_parse,
        russian=russian,
    )

    if family:
        await run_pipeline(SPEC_FAMILY, **common)
        return

    if daytime:
        await run_pipeline(SPEC_DAYTIME, **common)
        return

    # Standard pipeline — museums_only alters pipeline_tag and include_apis
    await run_pipeline(
        SPEC_STANDARD,
        **common,
        _pipeline_tag_override="museums" if museums_only else None,
        _include_apis_override=False if museums_only else None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="London events digest")
    parser.add_argument("--dry-run", action="store_true", help="no email, no dedup marking")
    parser.add_argument("--send-test", action="store_true", help="email but skip dedup marking")
    parser.add_argument("--mask", action="store_true", help="mark events as seen after sending (default: off)")
    parser.add_argument("--no-rank", action="store_true", help="skip Claude ranking, use all one-off events")
    parser.add_argument("--museums-only", action="store_true", help="museums pipeline only")
    parser.add_argument("--today", action="store_true", help="filter to today only")
    parser.add_argument("--family", action="store_true", help="family pipeline")
    parser.add_argument("--daytime", action="store_true", help="weekday daytime concerts pipeline")
    parser.add_argument("--test-site", metavar="NAME", help="test single site from sites.yaml")
    parser.add_argument("--no-dedup", action="store_true", help="skip seen-events filter, show all events")
    parser.add_argument("--date-window", type=int, metavar="N", help="date window in days (default: 14 standard, 30 family/daytime)")
    parser.add_argument("--force-download", action="store_true", help="bypass download cache, re-fetch all sites")
    parser.add_argument("--force-parse", action="store_true", help="bypass parse cache, re-run LLM extraction")
    parser.add_argument("--russian", action="store_true", help="generate digest in Russian (venues stay in English)")
    args = parser.parse_args()

    asyncio.run(run(
        dry_run=args.dry_run,
        send_test=args.send_test,
        mask=args.mask,
        no_rank=args.no_rank,
        test_site=args.test_site,
        museums_only=args.museums_only,
        today_only=args.today,
        family=args.family,
        daytime=args.daytime,
        no_dedup=args.no_dedup,
        date_window=args.date_window,
        force_download=args.force_download,
        force_parse=args.force_parse,
        russian=args.russian,
    ))


if __name__ == "__main__":
    main()
