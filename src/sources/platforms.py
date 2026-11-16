"""Ticketing-platform fingerprints: venue page -> (platform, client id).

Lets the source router send a venue to a deterministic platform source instead of the
LLM. Detectors are pure functions over cached HTML returning (client, confidence 0..1);
the most confident one wins, so a page that merely links another venue's box office
does not outrank the page's own platform.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Callable
from urllib.parse import urlsplit

from . import spektrix

Scored = tuple[str, float]
Detector = Callable[[str, str], "Scored | None"]


def _site_label(url: str) -> str:
    """'https://www.osoarts.org.uk/x' -> 'osoarts' (first label of the registrable part)."""
    host = urlsplit(url).hostname or ""
    return spektrix._site(host).split(".")[0] if host else ""


def _dominant(hits: Counter[str], min_hits: int = 2, min_share: float = 0.6) -> Scored | None:
    if not hits:
        return None
    top, n = hits.most_common(1)[0]
    share = n / sum(hits.values())
    return (top, min(0.9, 0.4 + 0.05 * n)) if n >= min_hits and share >= min_share else None


# ── spektrix ─────────────────────────────────────────────────────────────────

def _spektrix(html: str, url: str = "") -> Scored | None:
    hit = spektrix.detect_scored(html, url)
    return (hit[0].client, hit[1]) if hit else None


# ── ticketsolve ──────────────────────────────────────────────────────────────
# Box offices live at https://<client>.ticketsolve.com/(ticketbooth/)shows/...
_TS_HOST = re.compile(r"https?:(?:\\?/){2}([a-z0-9][a-z0-9-]{1,62})\.ticketsolve\.com\b", re.I)
_TS_NOT_CLIENTS = {"www", "app", "api", "help", "support", "cdn", "assets", "static", "demo"}


def ticketsolve(html: str, url: str = "") -> Scored | None:
    """`<client>.ticketsolve.com` that matches the page's own site name (1.0), or that has
    ≥ 2 links and ≥ 60% of all Ticketsolve links. One link to another venue's box office
    (Golden Goose -> upstairsatthegatehouse) is not enough."""
    if "ticketsolve.com" not in (html + url).lower():
        return None
    hits = Counter(h for h in (m.group(1).lower() for m in _TS_HOST.finditer(url + " " + html))
                   if h not in _TS_NOT_CLIENTS)
    own = _site_label(url)
    if own and own in hits:
        return own, 1.0
    return _dominant(hits)


# ── eventbrite organiser ─────────────────────────────────────────────────────
# Organiser pages: eventbrite.<tld>/o/<slug>-<numeric id>.
_EB_ORG = re.compile(r"eventbrite\.[a-z.]{2,6}/o/(?:[\w-]*?-)?(\d{6,})", re.I)


def eventbrite_org(html: str, url: str = "") -> Scored | None:
    """Organiser id when `url` is an organiser page (1.0), or when one organiser owns
    ≥ 60% of ≥ 2 organiser links. A search page (/d/...) mixing many organisers —
    London Transport Museum's cached page — returns None.
    TODO(C4): organiser-events API source; LTM needs its organiser id in sites/<city>.yaml."""
    m = _EB_ORG.search(url)
    if m:
        return m.group(1), 1.0
    return _dominant(Counter(_EB_ORG.findall(html)))


# ── dice ─────────────────────────────────────────────────────────────────────

def dice(html: str, url: str = "") -> Scored | None:
    """TODO(C4): DICE venue/partner widget (`dice.fm/venue/<slug>`, `DICE_VENUE`,
    widgets.dice.fm). Venues mostly link single events; no public venue feed confirmed."""
    return None


DETECTORS: list[tuple[str, Detector]] = [
    ("spektrix", _spektrix),
    ("ticketsolve", ticketsolve),
    ("eventbrite_org", eventbrite_org),
    ("dice", dice),
]


def detect_platform(html: str | None, url: str = "") -> tuple[str, str] | None:
    """(platform, client id) of the most confident detector, e.g. ("spektrix", "kingsplace")."""
    if not html and not url:
        return None
    best: tuple[float, str, str] | None = None
    for name, fn in DETECTORS:
        hit = fn(html or "", url)
        if hit and (best is None or hit[1] > best[0]):
            best = (hit[1], name, hit[0])
    return (best[1], best[2]) if best else None
