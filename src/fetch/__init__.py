"""Fetch layer (pipeline v3). Types only for now; fetchers arrive in step 3."""
from .types import FetchResult, FetchStatus, is_blocked_page, visible_text

__all__ = ["FetchResult", "FetchStatus", "is_blocked_page", "visible_text"]
