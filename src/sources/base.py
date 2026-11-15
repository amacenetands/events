"""Source contract: every source turns (venue config, fetched page) into RawEvents.

- `records()` MAY raise; callers (parse_sites) always go through `safe_records`, which
  turns an exception into `ctx.stats["<kind>.raised"]` + one `ctx.errors` line and [].
- Page sources read `fr.body` whenever it is non-empty, whatever `fr.status`: fetch fills
  it from the cache on NOT_MODIFIED and serves the last good body on BLOCKED/ERROR
  (parse_sites then flags the venue "stale page"). `api` mode is OK with an empty body.
  Resolve relative links with `base_url(cfg, fr, ctx)` (post-redirect `fr.url`, else listing).
- `ctx.errors` is for real failures only (a source raised, partial data, dead API): any
  entry makes save_crawl merge the venue's rows instead of replacing them. Benign
  outcomes ("0 records, next source") go to `ctx.stats`.
- `ctx.offline` (--parse-only): no network — API sources return [], detail pages from cache only.
- Sources never filter by date; `build.build_events` drops past events.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable
import httpx
from ..config import CityConfig, VenueConfig
from ..fetch.types import FetchResult
from ..models import RawEvent

@dataclass
class SourceCtx:
    http: httpx.AsyncClient
    today: date
    window_days: int
    city: CityConfig
    stats: dict[str, int] = field(default_factory=dict)   # free-form counters, e.g. "spektrix.events_failed"
    errors: list[str] = field(default_factory=list)       # "<venue>: <what>"
    force: bool = False                                    # bypass cached detail pages (re-download)
    force_parse: bool = False                              # bypass the LLM extraction cache
    offline: bool = False                                  # --parse-only: no network except the LLM

@runtime_checkable
class Source(Protocol):
    kind: str
    async def records(self, cfg: VenueConfig, fr: FetchResult | None, ctx: SourceCtx) -> list[RawEvent]: ...

SOURCES: dict[str, Source] = {}
def register_source(src: Source) -> Source:
    SOURCES[src.kind] = src
    return src


_BUILTIN_MODULES = ("jsonld", "adapter_source", "follow_links", "llm_source")
_OPTIONAL_MODULES = ("spektrix",)     # owned elsewhere; may be mid-edit
_loaded = False


def load_builtin_sources() -> None:
    """Import the built-in source modules so they register (lazy, idempotent)."""
    global _loaded
    if _loaded:
        return
    import importlib
    for m in _BUILTIN_MODULES:
        importlib.import_module(f"{__package__}.{m}")
    for m in _OPTIONAL_MODULES:
        name = f"{__package__}.{m}"
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as e:   # only a missing module is tolerated, not a broken one
            if e.name != name:
                raise
    _loaded = True


def get_source(kind: str) -> Source:
    """Registered source for `kind`; KeyError listing the known kinds."""
    load_builtin_sources()
    if kind not in SOURCES:
        raise KeyError(f"unknown source kind {kind!r}; known: {sorted(SOURCES)}")
    return SOURCES[kind]


def base_url(cfg: VenueConfig, fr: FetchResult | None, ctx: SourceCtx) -> str:
    """URL relative links resolve against: the post-redirect fetch URL, else the listing URL."""
    return (fr.url if fr is not None and fr.url else "") or cfg.resolved_url(ctx.today.year)


async def safe_records(src: Source, cfg: VenueConfig, fr: FetchResult | None, ctx: SourceCtx) -> list[RawEvent]:
    """src.records(); an exception becomes a stat + one error line and []."""
    try:
        return await src.records(cfg, fr, ctx)
    except Exception as ex:   # noqa: BLE001 — one broken source must not sink the crawl
        key = f"{src.kind}.raised"
        ctx.stats[key] = ctx.stats.get(key, 0) + 1
        ctx.errors.append(f"{cfg.name}: source {src.kind} raised {type(ex).__name__}: {str(ex)[:80]}")
        return []
