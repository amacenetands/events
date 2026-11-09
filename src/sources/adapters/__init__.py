"""Per-venue adapter registry. See README.md in this folder."""
from __future__ import annotations
import importlib
import pkgutil
from .base import Adapter, Rec, REGISTRY, register

_loaded = False


def _load_all() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from . import cards, json_adapter, auto  # noqa: F401  (register built-ins)
    from . import venues
    for m in pkgutil.iter_modules(venues.__path__):
        importlib.import_module(f"{venues.__name__}.{m.name}")


def get_adapter(site: dict) -> Adapter:
    """Adapter for a site dict: `adapter.kind` (default auto)."""
    _load_all()
    kind = (site.get("adapter") or {}).get("kind", "auto")
    if kind not in REGISTRY:
        raise KeyError(f"unknown adapter kind {kind!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[kind]()


__all__ = ["Adapter", "Rec", "register", "get_adapter", "REGISTRY"]
