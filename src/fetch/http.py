"""Plain HTTP fetcher: conditional requests, bot-wall detection, one retry.

`fetch_http` never raises; the caller (src.fetch.fetch_venue) owns the cache, so a
304 comes back as NOT_MODIFIED with an empty body for the caller to fill.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import httpx

from .headers import HEADERS
from .types import FetchResult, FetchStatus, is_blocked_page, visible_text

EMPTY_MIN_TEXT = 500          # visible chars below which an HTML page counts as EMPTY
RETRY_BACKOFF = 2.0           # seconds before the single retry (5xx / timeout / network)
_WALL_STATUSES = (403, 429, 503)
_HTMLISH = re.compile(r"html|xml|^$", re.I)
# Bodies that are data, not pages: no wall heuristics (a JSON feed has no visible text).
_DATA_CT = re.compile(r"json|calendar|csv|rss|atom", re.I)
_BINARY_CT = re.compile(r"^(application/(pdf|zip|octet-stream|vnd\.|msword|x-)|image/|audio/|video/|font/)", re.I)

# Indirection so tests can skip the real backoff.
_sleep = asyncio.sleep


def _is_empty(body: str, content_type: str) -> bool:
    if _HTMLISH.search(content_type.split(";")[0].strip()) and "json" not in content_type:
        return len(visible_text(body)) < EMPTY_MIN_TEXT
    return len(body.strip()) == 0


def classify(resp: httpx.Response, url: str) -> FetchResult:
    """Map one HTTP response to a FetchResult (no retries, no cache)."""
    ct = resp.headers.get("content-type", "")
    base = dict(
        url=str(resp.url) or url, content_type=ct,
        etag=resp.headers.get("etag"), last_modified=resp.headers.get("last-modified"),
        fetched_at=datetime.now(timezone.utc),
    )
    code = resp.status_code
    if code == 304:
        return FetchResult(FetchStatus.NOT_MODIFIED, note="HTTP 304", **base)
    mime = ct.split(";")[0].strip().lower()
    if code < 400 and _BINARY_CT.search(mime) and not _DATA_CT.search(mime):
        return FetchResult(FetchStatus.ERROR, note=f"HTTP {code} unsupported content-type {mime}", **base)
    body = resp.text
    challenge = resp.headers.get("cf-mitigated", "").lower() == "challenge" or (
        not _DATA_CT.search(mime) and is_blocked_page(body))
    if code in _WALL_STATUSES and challenge:
        return FetchResult(FetchStatus.BLOCKED, note=f"HTTP {code} bot wall", **base)
    if code >= 400:
        return FetchResult(FetchStatus.ERROR, note=f"HTTP {code}", **base)
    if challenge:
        return FetchResult(FetchStatus.BLOCKED, note=f"HTTP {code} bot wall page", **base)
    if _is_empty(body, ct):
        return FetchResult(FetchStatus.EMPTY, body=body,
                           note=f"HTTP {code}, {len(visible_text(body))} visible chars", **base)
    return FetchResult(FetchStatus.OK, body=body, note=f"HTTP {code}", **base)


async def fetch_http(url: str, *, client: httpx.AsyncClient, etag: str | None = None,
                     last_modified: str | None = None, timeout: float = 30) -> FetchResult:
    """GET url with HEADERS (+ If-None-Match / If-Modified-Since when validators given).
    Statuses: OK, NOT_MODIFIED (body ''), EMPTY, BLOCKED, ERROR. One retry with backoff
    on 5xx (unless it is a bot wall), timeouts and transport errors."""
    headers = dict(HEADERS)
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    last: FetchResult | None = None
    for attempt in range(2):
        if attempt:
            await _sleep(RETRY_BACKOFF)
        try:
            resp = await client.get(url, headers=headers, follow_redirects=True, timeout=timeout)
        except httpx.TimeoutException as e:
            last = FetchResult(FetchStatus.ERROR, url=url, note=f"timeout: {type(e).__name__}")
            continue
        except httpx.HTTPError as e:
            last = FetchResult(FetchStatus.ERROR, url=url, note=f"{type(e).__name__}: {e}"[:300])
            continue
        except Exception as e:  # never raise out of a fetcher
            return FetchResult(FetchStatus.ERROR, url=url, note=f"{type(e).__name__}: {e}"[:300])
        last = classify(resp, url)
        if resp.status_code >= 500 and last.status is FetchStatus.ERROR:
            continue
        break
    assert last is not None
    if attempt:
        last.note = f"{last.note} (after retry)"
    return last
