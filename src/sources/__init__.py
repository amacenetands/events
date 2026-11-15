from .skiddle import fetch_skiddle
from .songkick import fetch_songkick

# All API sources by cities.yaml name — add new sources here; html_sites handled separately
API_SOURCE_REGISTRY = {"skiddle": fetch_skiddle, "songkick": fetch_songkick}
API_SOURCES = list(API_SOURCE_REGISTRY.values())


def api_sources_for(names) -> list:
    """Fetch fns for cities.yaml `api_sources` names; ValueError on an unknown name."""
    unknown = [n for n in names if n not in API_SOURCE_REGISTRY]
    if unknown:
        raise ValueError(f"unknown api_sources {unknown}; known: {sorted(API_SOURCE_REGISTRY)}")
    return [API_SOURCE_REGISTRY[n] for n in names]
