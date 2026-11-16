"""Pick the per-event URL out of a reduced-text block."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

__all__ = ["pick_event_url", "is_map_url", "is_listing_url", "url_host", "anchor_lines", "match_title_url"]

_ARROW = re.compile(r"->\s*(\S+)")
_MD_LINK = re.compile(r"\]\((\S+?)\)")
_BARE = re.compile(r"(?<![\w(])(https?://[^\s|<>\"')]+)")
_BAD_SCHEME = re.compile(r"^(?:mailto|tel|javascript|data|sms|whatsapp):", re.I)
_ASSET = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|ico|css|js|woff2?|ttf|mp4|mp3)(?:$|\?)", re.I)
_GENERIC_PATH = re.compile(
    r"^/(?:|index\.html?|home|contact(?:-us)?|about(?:-us)?|basket|cart|checkout|login|log-?in|sign-?in|account"
    r"|my-account|search|privacy(?:-policy)?|cookies?(?:-policy)?|terms.*|faqs?|donate|support-us|membership"
    r"|newsletter|visit|access(?:ibility)?)/?$", re.I)
_MAP_HOSTS = re.compile(r"(?:^|\.)(?:maps\.app\.goo\.gl|maps\.apple\.com|what3words\.com|w3w\.co|citymapper\.com"
                        r"|waze\.com|openstreetmap\.org|mapquest\.com)$")
_MAP_PATH_HOSTS = re.compile(r"(?:^|\.)(?:google\.[a-z.]+|goo\.gl|g\.co|bing\.com|yandex\.[a-z]+|2gis\.[a-z]+)$")
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
    if s.scheme not in ("http", "https") or not s.netloc or is_map_url(u):
        return None
    return urlunsplit((s.scheme, s.netloc, s.path, s.query, ""))


def is_map_url(u: str) -> bool:
    """Directions/map link: Google/Apple/Bing/Yandex/2GIS maps, goo.gl/maps, what3words, citymapper, Waze, OSM."""
    try:
        s = urlsplit(u or "")
    except ValueError:
        return False
    host = s.netloc.lower().removeprefix("www.")
    path = s.path.lower()
    if _MAP_HOSTS.search(host) or host.startswith("maps."):
        return True
    return bool(_MAP_PATH_HOSTS.search(host)) and (path.startswith("/maps") or "/maps/" in path)


def _is_listing(u: str, listing_url: str) -> bool:
    """True when u is the listing page itself (or a filter/pagination view of it) or a generic site page."""
    h, p, q, keys = _norm(u)
    lh, lp, lq, _ = _norm(listing_url)
    if _GENERIC_PATH.match(p if p != "/" else "/"):
        return True
    if h == lh and p == lp:
        return not q or q == lq or keys <= _LISTING_QUERY_KEYS
    return False


def is_listing_url(u: str, listing_url: str) -> bool:
    """u is the listing page (or a filter/pagination view of it) or a generic site page."""
    try:
        return _is_listing(u, listing_url)
    except Exception:
        return False


def url_host(u: str) -> str:
    """Lower-case host without 'www.' ('' when none)."""
    try:
        return _norm(u or "")[0]
    except Exception:
        return ""


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


# ── title -> per-event link (shared by the jsonld / llm / spektrix sources) ──────

_TAG_PREFIX = re.compile(r"^\s*\[[A-Z]{1,3}\]\s*")
_ANCHOR = re.compile(r"<a\b[^>]*?\bhref\s*=\s*([\"'])(.*?)\1[^>]*>(.*?)</a\s*>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_QUOTES = str.maketrans({c: "'" for c in "‘’`´"} | {c: '"' for c in "“”"})


def fold(s: str) -> str:
    """Case/quote/whitespace-folded text for title comparison."""
    import html as _html
    return _WS.sub(" ", _html.unescape(s or "").translate(_QUOTES)).strip().casefold()


def anchor_lines(html: str) -> list[str]:
    """'<anchor text> -> <href>' for every <a href> in a page (the reduced-text link form)."""
    import html as _html
    out = []
    for _, href, inner in _ANCHOR.findall(html or ""):
        text = _WS.sub(" ", _html.unescape(_TAGS.sub(" ", inner))).strip()
        if text and href:
            out.append(f"{text} -> {_html.unescape(href).strip()}")
    return out


def _label(line: str) -> str:
    """First ' | ' field of a reduced line (or the text before ' -> '), tag prefix dropped."""
    body = line.rsplit(" -> ", 1)[0] if " -> " in line else line
    return fold(_TAG_PREFIX.sub("", body).split(" | ", 1)[0])


def match_title_url(title: str, lines: list[str], base_url: str, *, pattern: str | None = None) -> str | None:
    """Per-event URL for `title` from reduced/anchor lines, by tier: a line whose label equals
    the title, else one whose label starts with it, else (same-site links only) one whose label
    contains it as a phrase ("More info about <title>"). The first tier with any match must
    give one distinct URL, else None."""
    t = fold(title)
    if len(t) < 4:
        return None
    host = _norm(base_url or "")[0]
    phrase = re.compile(r"(?<!\w)" + re.escape(t) + r"(?!\w)")
    labelled = [(_label(ln), ln) for ln in lines or [] if isinstance(ln, str)]
    tiers = ((lambda lab: lab == t, False), (lambda lab: lab.startswith(t), False),
             (lambda lab: bool(phrase.search(lab)), True))
    for ok, same_site in tiers:
        urls = {u for lab, ln in labelled if ok(lab) and (u := pick_event_url([ln], base_url, pattern=pattern))}
        if same_site:
            urls = {u for u in urls if _norm(u)[0] == host}
        if len(urls) == 1:
            return urls.pop()
        if len(urls) > 1:
            return None
    return None
