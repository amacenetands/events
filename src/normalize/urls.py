"""Pick the per-event URL out of a reduced-text block."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

__all__ = ["pick_event_url"]

_ARROW = re.compile(r"->\s*(\S+)")
_MD_LINK = re.compile(r"\]\((\S+?)\)")
_BARE = re.compile(r"(?<![\w(])(https?://[^\s|<>\"')]+)")
_BAD_SCHEME = re.compile(r"^(?:mailto|tel|javascript|data|sms|whatsapp):", re.I)
_ASSET = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|ico|css|js|woff2?|ttf|mp4|mp3)(?:$|\?)", re.I)
_GENERIC_PATH = re.compile(
    r"^/(?:|index\.html?|home|contact(?:-us)?|about(?:-us)?|basket|cart|checkout|login|log-?in|sign-?in|account"
    r"|my-account|search|privacy(?:-policy)?|cookies?(?:-policy)?|terms.*|faqs?|donate|support-us|membership"
    r"|newsletter|visit|access(?:ibility)?)/?$", re.I)
_LISTING_QUERY_KEYS = {"page", "paged", "p", "category", "cat", "type", "filter", "date", "month", "view",
                       "sort", "s", "q", "genre", "tag", "offset", "start"}


def _norm(u: str) -> tuple[str, str, str, frozenset]:
    """(host, path, query, query keys) for URL comparison; no fragment/www/trailing slash."""
    s = urlsplit(u)
    host = s.netloc.lower().removeprefix("www.")
    return host, s.path.rstrip("/") or "/", s.query, frozenset(k for k, _ in parse_qsl(s.query, keep_blank_values=True))


def _candidates(lines: list[str]) -> list[str]:
    """Raw hrefs in block order: '->' markers, markdown links, bare http(s) URLs."""
    out: list[str] = []
    for line in lines:
        if not isinstance(line, str):
            continue
        hits = [(m.start(), m.group(1)) for rx in (_ARROW, _MD_LINK, _BARE) for m in rx.finditer(line)]
        seen_pos: set[str] = set()
        for _, h in sorted(hits):
            h = h.rstrip(".,;)")
            if h not in seen_pos:
                seen_pos.add(h)
                out.append(h)
    return out


def _absolute(href: str, listing_url: str) -> str | None:
    """Absolute, fragment-free URL, or None if not a usable event link."""
    href = href.strip().strip("<>\"'")
    if not href or href.startswith("#") or _BAD_SCHEME.match(href) or _ASSET.search(href):
        return None
    u = urljoin(listing_url, href)
    s = urlsplit(u)
    if s.scheme not in ("http", "https") or not s.netloc:
        return None
    return urlunsplit((s.scheme, s.netloc, s.path, s.query, ""))


def _is_listing(u: str, listing_url: str) -> bool:
    """True when u is the listing page itself (or a filter/pagination view of it) or a generic site page."""
    h, p, q, keys = _norm(u)
    lh, lp, lq, _ = _norm(listing_url)
    if _GENERIC_PATH.match(p if p != "/" else "/"):
        return True
    if h == lh and p == lp:
        return not q or q == lq or keys <= _LISTING_QUERY_KEYS
    return False


def pick_event_url(lines: list[str], listing_url: str, *, pattern: str | None = None) -> str | None:
    """Best absolute per-event URL in a block: pattern match > same-host > external; never the listing."""
    try:
        if isinstance(lines, str):
            lines = lines.splitlines()
        rx = re.compile(pattern) if pattern else None
        cands: list[str] = []
        for href in _candidates(lines or []):
            u = _absolute(href, listing_url or "")
            if u and not _is_listing(u, listing_url or "") and u not in cands:
                cands.append(u)
        if not cands:
            return None
        lh = _norm(listing_url or "")[0] if listing_url else ""

        def rank(item: tuple[int, str]) -> tuple[int, int, int]:
            i, u = item
            pm = 0 if rx and (rx.search(u) or rx.search(urlsplit(u).path)) else 1
            return pm, 0 if _norm(u)[0] == lh else 1, i
        return min(enumerate(cands), key=rank)[1]
    except Exception:
        return None
