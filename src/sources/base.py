"""Source contract: every source turns (venue config, fetched page) into RawEvents."""
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

@runtime_checkable
class Source(Protocol):
    kind: str
    async def records(self, cfg: VenueConfig, fr: FetchResult | None, ctx: SourceCtx) -> list[RawEvent]: ...

SOURCES: dict[str, Source] = {}
def register_source(src: Source) -> Source:
    SOURCES[src.kind] = src
    return src


_BUILTIN_MODULES = ("jsonld", "adapter_source", "follow_links")
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
        try:
            importlib.import_module(f"{__package__}.{m}")
        except ImportError:
            pass
    _loaded = True


def get_source(kind: str) -> Source:
    """Registered source for `kind`; KeyError listing the known kinds."""
    load_builtin_sources()
    if kind not in SOURCES:
        raise KeyError(f"unknown source kind {kind!r}; known: {sorted(SOURCES)}")
    return SOURCES[kind]
