"""Per-venue Python hooks: for what neither `fix:` nor `hint:` can express (docs/venue-hooks.md).

One module per hook, `src/venues/<slug>.py`, registering a `VenueHook` subclass with
`@venue_hook("<slug>")`. A venue opts in explicitly with `hook: <slug>` in sites/<city>.yaml
(no name-derived binding; `python -m src.config --check` fails on unknown or unused hooks).
Every stage defaults to identity:

    pre_fetch(cfg) -> cfg                         before the listing is fetched (crawl_sites)
    pre_source(cfg, fr) -> fr                     before the sources see the page (crawl_sites)
    post_raw(cfg, raws, ctx) -> raws              before RawEvent -> Event (build_events)
    post_build(cfg, events, ctx) -> events        after build + fixes.apply_fixes (build_events)
    post_enrich(cfg, events, ctx) -> events       after the enrich pass (parse_venue; runs without enrich too)

`run_hook()` is how callers invoke a stage. A hook that raises, or returns the wrong type,
is reported (ctx.stats["hook.<stage>.raised"] + one ctx.errors line) and its input passes
through unchanged. Parsers stay adapters (src/sources/adapters/venues/).
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar

if TYPE_CHECKING:
    from ..config import VenueConfig
    from ..fetch.types import FetchResult
    from ..models import Event, RawEvent
    from ..sources.base import SourceCtx

__all__ = ["VenueHook", "VENUE_HOOKS", "venue_hook", "load_venue_hooks", "hook_for", "run_hook", "STAGES"]

STAGES = ("pre_fetch", "pre_source", "post_raw", "post_build", "post_enrich")
_NOT_HOOKS = frozenset({"fixes"})          # modules in this package that are not venue hooks


class VenueHook:
    """Base class: override only the stages you need."""
    slug: str = ""

    def pre_fetch(self, cfg: "VenueConfig") -> "VenueConfig":
        return cfg

    def pre_source(self, cfg: "VenueConfig", fr: "Optional[FetchResult]") -> "Optional[FetchResult]":
        return fr

    def post_raw(self, cfg: "VenueConfig", raws: "list[RawEvent]", ctx: "SourceCtx") -> "list[RawEvent]":
        return raws

    def post_build(self, cfg: "VenueConfig", events: "list[Event]", ctx: "SourceCtx") -> "list[Event]":
        return events

    def post_enrich(self, cfg: "VenueConfig", events: "list[Event]", ctx: "SourceCtx") -> "list[Event]":
        return events


VENUE_HOOKS: dict[str, VenueHook] = {}
_LOAD_ERRORS: list[str] = []
_loaded = False

H = TypeVar("H", bound=type)


def venue_hook(slug: str) -> Callable[[H], H]:
    """Class decorator: register one instance of the hook under `slug`."""
    def deco(cls: H) -> H:
        inst = cls()
        inst.slug = slug
        VENUE_HOOKS[slug] = inst
        return cls
    return deco


def load_venue_hooks(ctx: "SourceCtx | None" = None) -> dict[str, VenueHook]:
    """Import every src/venues/<slug>.py once; a module that fails to import is skipped and
    reported (into ctx.errors when given, once per ctx)."""
    global _loaded
    if not _loaded:
        _loaded = True
        for m in pkgutil.iter_modules(__path__):
            if m.name.startswith("_") or m.name in _NOT_HOOKS:
                continue
            try:
                importlib.import_module(f"{__name__}.{m.name}")
            except Exception as ex:   # noqa: BLE001 — one broken hook must not sink the crawl
                _LOAD_ERRORS.append(f"venue hook {m.name}: import failed ({type(ex).__name__}: {str(ex)[:80]})")
    if ctx is not None:
        ctx.errors.extend(e for e in _LOAD_ERRORS if e not in ctx.errors)
    return VENUE_HOOKS


def hook_for(cfg: "VenueConfig", ctx: "SourceCtx | None" = None) -> Optional[VenueHook]:
    """The venue's hook (cfg.hook), None when it sets none or the slug is unregistered."""
    if not cfg.hook:
        return None
    return load_venue_hooks(ctx).get(cfg.hook)


def _check_type(stage: str, out: Any) -> None:
    from ..config import VenueConfig
    from ..fetch.types import FetchResult
    from ..models import Event, RawEvent
    ok = {"pre_fetch": lambda o: isinstance(o, VenueConfig),
          "pre_source": lambda o: o is None or isinstance(o, FetchResult),
          "post_raw": lambda o: isinstance(o, list) and all(isinstance(x, RawEvent) for x in o),
          "post_build": lambda o: isinstance(o, list) and all(isinstance(x, Event) for x in o),
          "post_enrich": lambda o: isinstance(o, list) and all(isinstance(x, Event) for x in o)}[stage]
    if not ok(out):
        raise TypeError(f"returned {type(out).__name__}, not the {stage} type")


def run_hook(stage: str, cfg: "VenueConfig", value: Any, ctx: "SourceCtx | None" = None) -> Any:
    """`value` passed through the venue's hook stage (identity when no hook). Never raises.
    pre_fetch: value=cfg; pre_source: value=fr; post_*: the list."""
    if stage not in STAGES:
        raise ValueError(f"unknown hook stage {stage!r}")
    h = hook_for(cfg, ctx)
    if h is None:
        return value
    try:
        if stage == "pre_fetch":
            out = h.pre_fetch(value)
        elif stage == "pre_source":
            out = h.pre_source(cfg, value)
        else:
            out = getattr(h, stage)(cfg, value, ctx)
        _check_type(stage, out)
    except Exception as ex:   # noqa: BLE001
        if ctx is not None:
            key = f"hook.{stage}.raised"
            ctx.stats[key] = ctx.stats.get(key, 0) + 1
            ctx.errors.append(f"{cfg.name}: hook {h.slug}.{stage} raised {type(ex).__name__}: {str(ex)[:80]}")
        return value
    if ctx is not None and out is not value and out != value:
        ctx.stats[f"hook.{stage}"] = ctx.stats.get(f"hook.{stage}", 0) + 1
    return out
