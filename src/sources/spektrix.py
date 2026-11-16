"""Spektrix source: venue page -> Spektrix client id -> public API v3 -> RawEvent.

Deterministic, no LLM. Endpoints (https://integrate.spektrix.com/docs/apieventfiltering):
  GET {base}/events?instanceStart_from=D&instanceStart_to=D
  GET {base}/instances?startFrom=D&startTo=D&$expand=priceList   (bulk: one call per client)
  GET {base}/events/{id}/instances?start_from=D&start_to=D&$expand=priceList   (fallback)
  GET {base}/instances/{id}/status                               (check_availability only)
base = https://system.spektrix.com/<client>/api/v3, or https://<custom domain>/<client>/api/v3.
Each endpoint spells its date filters differently; the docs say so.

Observed 2026-11-15 (not in the docs):
- `*_to` / `startTo` is exclusive (a bare date = midnight): we send end + 1 day and
  filter inclusively ourselves. Misspelt filters look ignored: bulk `/instances` with
  `start_from` (the per-event spelling) ran > 2 min instead of ~3 s.
- Instances carry no `isSoldOut`, and `priceList` is only an id unless `$expand=priceList`.
- `webUrl` is often "" and `webEventId` null.
- Add-ons are flagged per client (`attribute_DontIncludeOnWhatsOn`,
  `attribute_SupplementaryEvent`, a "Supplementary" grouping attribute) or only by name.
- Bulk `/instances` for Barbican (~3.3k sessions / 90 d) takes ~40 s: BULK_TIMEOUT.
- Use local wall-clock fields (`start`, `firstInstanceDateTime`) only: the `*Utc`
  siblings are inconsistent (some clients return local time with a "Z").
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from urllib.parse import urljoin, urldefrag, urlsplit

import httpx

from ..config import VenueConfig
from ..fetch.headers import HEADERS
from ..fetch.types import FetchResult
from ..models import RawEvent, clean_text, norm_category
from ..normalize.urls import anchor_lines, match_title_url
from .base import SourceCtx, base_url, register_source

log = logging.getLogger(__name__)

SYSTEM_HOST = "system.spektrix.com"
SOURCE_KIND = "spektrix"
INSTANCE_CONCURRENCY = 6
INSTANCE_DELAY = 0.3          # seconds between instance calls, per client
REQUEST_TIMEOUT = 20.0        # per request
BULK_TIMEOUT = 90.0           # bulk /instances for a big client
CLIENT_TIMEOUT = 180.0        # whole per-event fallback for one client
STATUS_CAP = 40               # max /status calls per client
DESC_MAX = 240

# ── fingerprint ──────────────────────────────────────────────────────────────

_ID = r"([A-Za-z0-9_-]{2,64})"
_Q = r"[\"']"
_NOT_CLIENTS = {"stable", "latest", "beta", "apitesting", "api", "website", "files", "docs",
                "integrate", "webcomponents", "secure", "v3", "help", "demo"}
_DOMAIN_IN_TAG = re.compile(r"\b(?:custom-domain|spektrix-domain|clientDomain)" + _Q + r"?\s*[=:]\s*"
                            + _Q + r"([\w.-]+\.[a-z]{2,})", re.I)

# Widget/config declarations of the venue's own client: trusted on one hit.
_STRONG = [
    re.compile(r"<spektrix-[\w-]+\b[^>]*?\bclient-name\s*=\s*" + _Q + _ID + r"[^>]*>", re.I),
    re.compile(r"<[^>]*\b(?:data-)?spektrix-client(?:-name)?\s*=\s*" + _Q + _ID + r"[^>]*>", re.I),
    re.compile(_Q + r"spektrix" + _Q + r"\s*:\s*\{\s*" + _Q + r"clientName" + _Q + r"\s*:\s*"
               + _Q + _ID + _Q + r"[^{}]*\}", re.I),
    re.compile(r"\bSpektrixClient(?:Name)?\s*[=:]\s*" + _Q + _ID, re.I),
]
# URL mentions: (attr-or-var name before the URL)?, host, client, path rest.
_URL = re.compile(
    r"(?:\b([\w-]+)\s*[=:]\s*" + _Q + r"?)?https?:(?:\\?/){2}([\w.-]+\.[a-z]{2,})(?:\\?/)+" + _ID
    + r"(?:\\?/)+(api(?:\\?/)v3|website(?:\\?/)[\w./\\-]*|files(?:\\?/))", re.I)
# Integration (own box office): script/iframe/form/img src, API bases, spektrix_* config.
_OWN_ATTRS = {"src", "action", "data-src"}


@dataclass(frozen=True)
class SpektrixClient:
    """A Spektrix client id plus the venue's custom ticket domain, if any."""
    client: str
    domain: str | None = None

    @property
    def bases(self) -> list[str]:
        """API base URLs to try, system host first."""
        out = [f"https://{SYSTEM_HOST}/{self.client}/api/v3"]
        if self.domain and self.domain.lower() != SYSTEM_HOST:
            out.append(f"https://{self.domain}/{self.client}/api/v3")
        return out


def _site(host: str) -> str:
    """Registrable-ish part of a host: 'tickets.bushtheatre.co.uk' -> 'bushtheatre.co.uk'."""
    parts = host.lower().strip(".").split(".")
    n = 3 if len(parts) > 2 and parts[-2] in ("co", "org", "ac", "gov", "com", "net") else 2
    return ".".join(parts[-n:])


def _clean_domain(d: str | None) -> str | None:
    return None if not d or d.lower().endswith("spektrix.com") else d.lower()


def _strong(html: str) -> SpektrixClient | None:
    hits: Counter[SpektrixClient] = Counter()
    for rx in _STRONG:
        for m in rx.finditer(html):
            c = m.group(1).lower()
            if c in _NOT_CLIENTS:
                continue
            dm = _DOMAIN_IN_TAG.search(m.group(0))
            hits[SpektrixClient(c, _clean_domain(dm.group(1) if dm else None))] += 1
    if not hits:
        return None
    best = max(hits, key=lambda sc: (sum(v for k, v in hits.items() if k.client == sc.client),
                                     sc.domain is not None))
    return best


def _url_scores(html: str, page_host: str) -> dict[str, tuple[float, str | None]]:
    """client -> (score, custom domain). +3 integration use, +1 plain link, +5 own host."""
    scores: Counter[str] = Counter()
    domains: dict[str, str | None] = {}
    for m in _URL.finditer(html):
        attr, host, client, rest = (m.group(1) or "").lower(), m.group(2).lower(), m.group(3).lower(), m.group(4)
        if client in _NOT_CLIENTS or host.startswith("webcomponents."):
            continue
        is_system = host.endswith("spektrix.com")
        if not is_system and not rest.lower().startswith(("api", "website")):
            continue                     # <custom>/<x>/files/ is not a Spektrix signature
        own = attr in _OWN_ATTRS or "spektrix" in attr or rest.lower().startswith("api")
        scores[client] += 3 if own else 1
        if not is_system:
            domains.setdefault(client, host)
            if page_host and _site(host) == _site(page_host):
                scores[client] += 5
    return {c: (s, domains.get(c)) for c, s in scores.items()}


def detect_scored(html: str | None, url: str = "") -> tuple[SpektrixClient, float] | None:
    """(client, confidence 0..1) for the page's own Spektrix client, or None.
    A widget declaration wins outright. Otherwise a URL candidate needs score ≥ 3
    (an integration use or two links) and ≥ 60% of all Spektrix URL score, or a custom
    domain on the page's own site — so a lone link to another venue's box office loses."""
    if not html or "spektrix" not in html.lower():
        return None
    strong = _strong(html)
    scores = _url_scores(html, urlsplit(url).hostname or "")
    if strong:
        dom = strong.domain or (scores.get(strong.client, (0, None))[1])
        return SpektrixClient(strong.client, dom), 1.0
    if not scores:
        return None
    client, (score, dom) = max(scores.items(), key=lambda kv: kv[1][0])
    share = score / sum(s for s, _ in scores.values())
    if score >= 3 and share >= 0.6:
        return SpektrixClient(client, dom), min(0.9, 0.3 + score / 20)
    return None


def detect(html: str | None, url: str = "") -> SpektrixClient | None:
    """Client id (+ custom domain) of the venue's own Spektrix box office, or None."""
    hit = detect_scored(html, url)
    return hit[0] if hit else None


def fingerprint(html: str | None, url: str = "") -> str | None:
    """Spektrix client id referenced by a venue page, or None."""
    c = detect(html, url)
    return c.client if c else None


# ── event filtering / mapping (pure) ─────────────────────────────────────────

# Always an add-on, whatever the venue programmes. A leading `*` is the box-office
# convention for merch/hidden items ('**Pizzas', '*Drinks').
_ADDON_GENERIC = re.compile(
    r"\b(headsets?|listening device|vouchers?|gift (?:cards?|vouchers?|memberships?)|"
    r"packages?|secure my booking|test event|blue badge|car park(?:ing)?|parking (?:space|permit|pass|ticket|voucher)s?|"
    r"(?:cushion|seat|locker|coat|blanket) hire|name[- ]a[- ]seat|for testing only|do not use|"
    r"pre[- ]?orders?|ice[- ]?creams?|merch(?:andise)?|donations?|booking fees?|gift aid|souvenirs?|(?:concert|souvenir) programmes?)\b|"
    r":\s*programmes?(?:\s*\d{4}.*)?$|^\W*programmes?\W*$|\b(?:bar|food|drinks?) menu\b|^\W*test for\b|^\W*parking\W*$|^\s*\*", re.I)
# Add-on only when the rest of the name is a sibling show's title ("<Show> Programme").
_ADDON_SUFFIX = re.compile(
    r"^\W*(?:(?:and|&|\+|with)\W+)?(?:programmes?|programs?|playtexts?|play ?texts?|scripts?|"
    r"cocktails?(?: and programme)?|drinks?|deals?|merch(?:andise)?|souvenirs?|posters?|"
    r"parking|dinner|dining|meal deal|interval drinks?)(?:\W+(?:deal|bundle|package))?"
    r"(?:\W*£\s*\d+(?:\.\d\d)?)?\W*$", re.I)
_ADDON_FLAGS = ("attribute_DontIncludeOnWhatsOn", "attribute_SupplementaryEvent")
_TYPE_ATTR = re.compile(r"type|categor|grouping|kind|class", re.I)
_ADDON_VALUES = {"supplementary", "add-on", "addon", "add on", "extras", "extra"}
_SKIP_PRICE_TYPE = re.compile(r"companion|carer|wheelchair|personal assistant|\bpa\b|complimentary|\bcomp\b", re.I)


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y")
    return bool(v)


def _base_title(name: str) -> str:
    """'GIRLS' / 'A Homo Alone 2: Yikes…' -> lowercase main title before ':' / ' - '."""
    return re.split(r"\s*(?::|\s[-–—]\s)\s*", name.strip().lower(), maxsplit=1)[0].strip()


def is_addon(ev: dict[str, Any], siblings: Iterable[str] = ()) -> bool:
    """Programmes, headsets, hidden items. `siblings`: other event names of the client,
    for the '<Show> Programme' rule."""
    if any(_truthy(ev.get(k)) for k in _ADDON_FLAGS):
        return True
    for k, v in ev.items():
        if (k.startswith("attribute_") and _TYPE_ATTR.search(k[10:]) and isinstance(v, str)
                and v.strip().lower() in _ADDON_VALUES):
            return True
    name = ev.get("name") if isinstance(ev.get("name"), str) else ""
    if _ADDON_GENERIC.search(name):
        return True
    low = name.lower()
    for sib in siblings:
        base = _base_title(sib)
        if len(base) >= 4 and sib.strip().lower() != low and low.startswith(base):
            if _ADDON_SUFFIX.match(low[len(base):]):
                return True
    return False


def _parse_dt(s: Any) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.rstrip("Z")).replace(tzinfo=None)
    except ValueError:
        return None


def _as_date(d: date | datetime) -> date:
    return d.date() if isinstance(d, datetime) else d


def in_window(ev: dict[str, Any], start: date, end: date) -> bool:
    """Event has a first instance and its run overlaps [start, end]."""
    first = _parse_dt(ev.get("firstInstanceDateTime"))
    if first is None:
        return False
    last = _parse_dt(ev.get("lastInstanceDateTime")) or first
    return first.date() <= end and last.date() >= start


def _name(ev: Any) -> str | None:
    n = ev.get("name") if isinstance(ev, dict) else None
    n = clean_text(n).strip() if isinstance(n, str) else ""
    return n or None


def select_events(events: Iterable[Any], start: date, end: date, max_events: int,
                  stats: "SpektrixStats | None" = None) -> tuple[list[dict[str, Any]], int]:
    """Drop add-ons, zero-instance, out-of-window and malformed events; order by the first
    date inside the window; cap. Returns (kept, dropped_count); malformed ones also count
    in stats.events_failed."""
    events = list(events)
    names = [n for n in (_name(e) for e in events) if n]
    kept, dropped = [], 0
    for ev in events:
        try:
            if not _name(ev) or not isinstance(ev.get("id"), str):
                raise ValueError("no id/name")
            ok = not is_addon(ev, names) and in_window(ev, start, end)
        except Exception:                                    # noqa: BLE001 — one bad event
            dropped += 1
            if stats is not None:
                stats.events_failed += 1
            continue
        if ok:
            kept.append(ev)
        else:
            dropped += 1
    floor = datetime.combine(start, datetime.min.time())
    kept.sort(key=lambda e: max(_parse_dt(e.get("firstInstanceDateTime")) or floor, floor))
    return kept[:max_events], dropped


def first_sentence(text: str | None, limit: int = DESC_MAX) -> str | None:
    """HTML-stripped first sentence, capped at `limit` chars."""
    t = clean_text(text) if isinstance(text, str) else ""
    t = re.sub(r"(?:\s*/\s*){2,}", " / ", t or "")      # runs of <br> -> one separator
    t = re.sub(r"\s+", " ", t).strip(" /")
    if not t:
        return None
    m = re.search(r"^(.+?[.!?])(?:\s|$)", t)
    s = m.group(1) if m else t
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _money(x: float) -> str:
    return f"£{x:.0f}" if abs(x - round(x)) < 1e-9 else f"£{x:.2f}"


def _price_ok(p: Any) -> bool:
    if not isinstance(p, dict) or not isinstance(p.get("amount"), (int, float)):
        return False
    tt = (p.get("ticketType") or {}).get("name") if isinstance(p.get("ticketType"), dict) else ""
    return not (isinstance(tt, str) and _SKIP_PRICE_TYPE.search(tt))


def price_text(inst: dict[str, Any]) -> str | None:
    """'From £15' (cheapest paid band), '£12' (single price), 'Free', or None.
    Companion/carer/wheelchair/comp ticket types are ignored."""
    pl = inst.get("priceList")
    prices = pl.get("prices") if isinstance(pl, dict) else None
    amounts = sorted({float(p["amount"]) for p in prices or [] if _price_ok(p)})
    if not amounts:
        return None
    paid = [a for a in amounts if a > 0]
    if not paid:
        return "Free"
    return _money(paid[0]) if len(paid) == 1 else f"From {_money(paid[0])}"


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def sold_out(inst: dict[str, Any], status: dict[str, Any] | None = None) -> bool | None:
    """True/False when known: `isSoldOut` if the client exposes it, else /status
    `available == 0` of `capacity > 0`. None = unknown. Unverified on real sold-out data."""
    if "isSoldOut" in inst and inst["isSoldOut"] is not None:
        return _truthy(inst["isSoldOut"])
    if status:
        cap, avail = _int(status.get("capacity")), _int(status.get("available"))
        if cap and cap > 0 and avail is not None:
            return avail <= 0
    return None


def _desc(ev: dict[str, Any]) -> str | None:
    return first_sentence(ev.get("description")) or first_sentence(ev.get("htmlDescription"))


def slugify(text: str) -> str:
    """'Stévenin: Husbands' -> 'stevenin-husbands'; apostrophes vanish ("There's" -> "theres")."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"['’`]", "", t.lower())).strip("-")


def _attr_values(ev: dict[str, Any]) -> list[tuple[str, str]]:
    """(attribute name, value) of the event's non-empty, non-flag string attributes."""
    out = []
    for k, v in ev.items():
        if k.startswith("attribute_") and isinstance(v, str) and v.strip() and v.strip().lower() not in ("true", "false"):
            out.append((k[10:], clean_text(v).strip()))
    return out


def _url(ev: dict[str, Any], tpl: str | None = None) -> str:
    """webUrl, else the venue's `event_url` template ({id} {slug} {name} {category}
    = slug of attribute_Category/Genre/EventType), else ''."""
    u = ev.get("webUrl")
    u = u.strip() if isinstance(u, str) else ""
    if u or not tpl:
        return u
    attrs = {k.lower(): v for k, v in _attr_values(ev)}
    cat = next((attrs[k] for k in ("category", "genre", "eventtype") if k in attrs), "")
    try:
        return tpl.format(id=ev.get("id") or "", slug=slugify(_name(ev) or ""),
                          name=_name(ev) or "", category=slugify(cat))
    except (KeyError, IndexError, ValueError):
        return ""


# Audience words stated (mapped to 'kids' / 'family', the words build.py reads) in the event's own attributes or name. No bare "junior(s)" in a
# name ("Junior Doctors"), no "relaxed" (an access label, not an audience).
# Training for practitioners, not an audience of kids even at a children's venue.
_ADULT_COURSE = re.compile(r"\b(bootcamp|masterclass(?:es)?|cpd|professional development|training|practitioners?|"
                           r"foundation course|creating theatre for|surgeries|career)\b", re.I)
_ADULT_DESC = re.compile(r"\b(bootcamp|cpd|professional development)\b", re.I)
_PANTO = re.compile(r"\bpanto(?:mime)?\b", re.I)
_AUD_ATTR = re.compile(r"\b(kids?|child(?:ren)?(?:'s)?|babies|baby|toddlers?|juniors?|famil(?:y|ies)|under[- ]?5s?|youth)\b", re.I)
_AUD_NAME = re.compile(r"\b(kids?|child(?:ren)?(?:'s)?|babies|baby|toddlers?|famil(?:y|ies)|under[- ]?5s?)\b", re.I)
# Audience claims in prose: only unambiguous phrasing ("a family drama" does not count).
_AUD_DESC = re.compile(r"\b(?:for|suitable for|perfect for|ideal for|aimed at)\s+(?:all\s+)?(famil(?:y|ies))\b"
                       r"|\b(?:suitable for|aimed at|designed for)\s+(?:young\s+)?(kids|children|babies|toddlers)\b"
                       r"|\b(family[- ](?:friendly|show|performance|workshop))\b", re.I)
_AGE_ATTR = re.compile(r"age|audience", re.I)
_AGE_TXT = (
    re.compile(r"\bages?[sd]?\s*(?:range|guidance|recommendation)?\s*:?\s*(\d{1,2}\s*(?:[-–—]|to)\s*\d{1,2}|\d{1,2}\s*\+?)(?!\d|:|\.\d)", re.I),
    re.compile(r"(?<![£\d.])\b(\d{1,2}\s*(?:[-–—]|to)\s*\d{1,2})\s*(?:years?|yrs?)(?:[- ]olds?)?\b", re.I),
    re.compile(r"(?<![£\d.])\b(\d{1,2}\s*\+)\s*(?:years?|yrs?)\b", re.I),
    re.compile(r"(?<![£\d.])\b(\d{1,2}\s*\+)(?=\s*(?:only|$|[.,;)]))", re.I),
)
_CAT_ATTR = ("category", "genre", "artform", "eventtype", "type")


def _age_in(text: str) -> str | None:
    for rx in _AGE_TXT:
        m = rx.search(text)
        if m:
            return re.sub(r"\s+", "", m.group(1))
    return None


def classify_event(ev: dict[str, Any]) -> tuple[str, str, str]:
    """(category_text, audience_text, age_text) from the event's attributes, name and
    description. Generic: no per-venue knowledge; blank when nothing is stated."""
    attrs = _attr_values(ev)
    by_key = {k.lower(): v for k, v in attrs}
    cat = ""
    for key in _CAT_ATTR:
        v = by_key.get(key, "")
        if v and norm_category(v) != "other":
            cat = v
            break
    aud: list[str] = []

    def add(it: Iterable[re.Match]) -> None:
        for m in it:
            w = "family" if next(g for g in m.groups() if g).lower().startswith("famil") else "kids"
            if w not in aud:
                aud.append(w)
    for k, v in attrs:
        if not (_AGE_ATTR.search(k) and re.search(r"\d", v)):      # age fields feed age_text
            add(_AUD_ATTR.finditer(v))
    add(_AUD_NAME.finditer(_name(ev) or ""))
    desc = " ".join(t for t in (clean_text(ev.get("description")) if isinstance(ev.get("description"), str) else "",
                                clean_text(ev.get("htmlDescription")) if isinstance(ev.get("htmlDescription"), str) else "") if t)
    add(_AUD_DESC.finditer(desc))
    desc_head = first_sentence(desc, 400) or ""
    if any(_PANTO.search(t) for t in (_name(ev) or "", desc_head, *(v for _, v in attrs))):
        add(re.finditer(r"(famil)y", "family"))
    if _ADULT_COURSE.search(_name(ev) or "") or _ADULT_DESC.search(desc_head):
        return cat, "adult", ""
    age = next((v for k, v in attrs if _AGE_ATTR.search(k) and re.search(r"\d", v)), "")
    if not age:
        # Prose ages only count when they speak to the audience: "Age guidance: 7+" on an
        # opera means "no under-7s", not "kids", and "16+" would read as family downstream.
        t = _age_in(_name(ev) or "") or _age_in(desc) or ""
        lo = re.match(r"\d+", t)
        if t and lo and (aud or int(lo.group()) >= 18):
            age = t
    return cat, ", ".join(sorted(aud)), age


def event_to_raw(ev: dict[str, Any], url_tpl: str | None = None) -> RawEvent:
    """One RawEvent for a whole run (no instance call)."""
    first = _parse_dt(ev.get("firstInstanceDateTime"))
    last = _parse_dt(ev.get("lastInstanceDateTime"))
    cat, aud, age = classify_event(ev)
    return RawEvent(
        title=_name(ev) or "",
        date_text=first.date().isoformat() if first else None,
        end_text=last.date().isoformat() if last and first and last.date() != first.date() else None,
        url=_url(ev, url_tpl), desc=_desc(ev), source_kind=SOURCE_KIND,
        category_text=cat, audience_text=aud, age_text=age,
    )


def instance_to_raw(ev: dict[str, Any], inst: dict[str, Any],
                    status: dict[str, Any] | None = None, url_tpl: str | None = None) -> RawEvent | None:
    """One RawEvent per session; None for cancelled/undated instances."""
    start = _parse_dt(inst.get("start"))
    title = _name(ev)
    if start is None or not title or _truthy(inst.get("cancelled")):
        return None
    cat, aud, age = classify_event(ev)
    return RawEvent(
        title=title, date_text=start.date().isoformat(), time_text=start.strftime("%H:%M"),
        price_text=price_text(inst), url=_url(ev, url_tpl), desc=_desc(ev),
        sold_out=sold_out(inst, status), source_kind=SOURCE_KIND,
        category_text=cat, audience_text=aud, age_text=age,
    )


# Timed-entry / daily-slot events (exhibitions, tours): more sessions than this on one
# day, or in the whole window, collapse into one run RawEvent.
SLOTS_PER_DAY = 2
SLOTS_PER_WINDOW = 14
_WD_PLURAL = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")
_AMOUNT = re.compile(r"£\s*(\d+(?:\.\d+)?)")


def _price_key(p: str | None) -> float:
    if not p:
        return float("inf")
    if p.strip().lower() == "free":
        return 0.0
    m = _AMOUNT.search(p)
    return float(m.group(1)) if m else float("inf")


def collapse_slots(raws: list[RawEvent]) -> list[RawEvent] | None:
    """One run RawEvent for an event's sessions when they look like timed-entry slots
    (> SLOTS_PER_DAY on a day or > SLOTS_PER_WINDOW in all); None = keep per-session."""
    days = sorted({r.date_text for r in raws if r.date_text})
    per_day: dict[str, int] = {}
    for r in raws:
        per_day[r.date_text or ""] = per_day.get(r.date_text or "", 0) + 1
    if not days or (max(per_day.values()) <= SLOTS_PER_DAY and len(raws) <= SLOTS_PER_WINDOW):
        return None
    wds = sorted({date.fromisoformat(d).weekday() for d in days})
    price = min((r.price_text for r in raws if r.price_text), key=_price_key, default=None)
    so = [r.sold_out for r in raws]
    first = raws[0]
    return [first.model_copy(update=dict(
        date_text=days[0], end_text=days[-1] if days[-1] != days[0] else None,
        recurrence_text=", ".join(_WD_PLURAL[w] for w in wds) if len(wds) < 7 else None,
        time_text="", price_text=price,
        sold_out=True if so and all(x is True for x in so) else (False if any(x is False for x in so) else None),
    ))]


# ── HTTP ─────────────────────────────────────────────────────────────────────

@dataclass
class SpektrixStats:
    """Counters for one fetch_events call (filled in place)."""
    events_seen: int = 0
    events_dropped: int = 0
    events_failed: int = 0
    instances: int = 0
    status_calls: int = 0
    collapsed: int = 0            # events whose timed-entry slots became one run
    bulk: bool = False
    base: str | None = None
    errors: list[str] = field(default_factory=list)


class _Pacer:
    """Spaces request starts at least `delay` seconds apart."""

    def __init__(self, delay: float) -> None:
        self.delay, self._last, self._lock = delay, 0.0, asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            gap = self._last + self.delay - time.monotonic()
            if gap > 0:
                await asyncio.sleep(gap)
            self._last = time.monotonic()


def _headers() -> dict[str, str]:
    return {"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"}


async def _get_json(http: httpx.AsyncClient, url: str, params: dict[str, str] | None = None,
                    timeout: float = REQUEST_TIMEOUT) -> Any:
    """GET → parsed JSON; raises httpx.HTTPError / ValueError on anything else."""
    r = await http.get(url, params=params, headers=_headers(), timeout=timeout,
                       follow_redirects=True)
    r.raise_for_status()
    if "json" not in r.headers.get("content-type", ""):
        raise ValueError(f"non-JSON response ({r.headers.get('content-type', '?')})")
    return r.json()


def _window(start: date, end: date, frm: str, to: str) -> dict[str, str]:
    """API date window; `to` is exclusive server-side, so send end + 1 day."""
    return {frm: start.isoformat(), to: (end + timedelta(days=1)).isoformat()}


def _err(stats: SpektrixStats, msg: str) -> None:
    stats.errors.append(msg[:200])


async def _list_events(http: httpx.AsyncClient, sc: SpektrixClient, start: date, end: date,
                       stats: SpektrixStats) -> tuple[str, list[Any]] | None:
    """(base, events) from the first base URL that answers with a JSON list."""
    for base in sc.bases:
        try:
            data = await _get_json(http, f"{base}/events",
                                   _window(start, end, "instanceStart_from", "instanceStart_to"))
        except (httpx.HTTPError, ValueError) as e:
            _err(stats, f"{base}: {type(e).__name__}: {e}")
            continue
        if isinstance(data, list):
            return base, data
        _err(stats, f"{base}: events payload is {type(data).__name__}, not list")
    return None


def _in_range(inst: Any, start: date, end: date, now: datetime | None) -> bool:
    st = _parse_dt(inst.get("start")) if isinstance(inst, dict) else None
    return st is not None and start <= st.date() <= end and (now is None or st >= now)


def _to_raws(ev: dict[str, Any], insts: Iterable[Any], start: date, end: date,
             now: datetime | None, stats: SpektrixStats, url_tpl: str | None = None) -> list[tuple[RawEvent, str | None]]:
    """(RawEvent, instance id) per in-window session; malformed instances are counted."""
    out = []
    for inst in insts:
        try:
            if not _in_range(inst, start, end, now):
                continue
            raw = instance_to_raw(ev, inst, url_tpl=url_tpl)
        except Exception as e:                               # noqa: BLE001 — one bad instance
            _err(stats, f"instance {str(inst.get('id') if isinstance(inst, dict) else '?')}: {e}")
            continue
        if raw:
            out.append((raw, inst.get("id") if isinstance(inst.get("id"), str) else None))
    return out


async def _bulk_instances(http: httpx.AsyncClient, base: str, start: date, end: date,
                          stats: SpektrixStats) -> dict[str, list[Any]] | None:
    """event id -> instances from one bulk call; None when the call fails."""
    params = _window(start, end, "startFrom", "startTo") | {"$expand": "priceList"}
    try:
        data = await _get_json(http, f"{base}/instances", params, timeout=BULK_TIMEOUT)
    except (httpx.HTTPError, ValueError) as e:
        _err(stats, f"bulk instances: {type(e).__name__}: {e}")
        return None
    if not isinstance(data, list):
        _err(stats, f"bulk instances payload is {type(data).__name__}, not list")
        return None
    by_event: dict[str, list[Any]] = {}
    for inst in data:
        ev = inst.get("event") if isinstance(inst, dict) else None
        eid = ev.get("id") if isinstance(ev, dict) else None
        if isinstance(eid, str):
            by_event.setdefault(eid, []).append(inst)
    return by_event


async def _event_instances(http: httpx.AsyncClient, base: str, ev: dict[str, Any], start: date,
                           end: date, pacer: _Pacer, sem: asyncio.Semaphore) -> list[Any]:
    params = _window(start, end, "start_from", "start_to") | {"$expand": "priceList"}
    async with sem:
        await pacer.wait()
        insts = await _get_json(http, f"{base}/events/{ev['id']}/instances", params)
    if not isinstance(insts, list):
        raise ValueError(f"instances payload is {type(insts).__name__}, not list")
    return insts


async def _per_event_instances(http: httpx.AsyncClient, base: str, events: list[dict[str, Any]],
                               start: date, end: date, stats: SpektrixStats, *, delay: float,
                               timeout: float) -> dict[str, list[Any]]:
    """Fallback: one call per event, concurrently; partial results on timeout."""
    pacer, sem = _Pacer(delay), asyncio.Semaphore(INSTANCE_CONCURRENCY)
    tasks = {ev["id"]: asyncio.create_task(_event_instances(http, base, ev, start, end, pacer, sem))
             for ev in events}
    if not tasks:
        return {}
    _, pending = await asyncio.wait(tasks.values(), timeout=timeout)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        _err(stats, f"client timeout: {len(pending)} events not fetched")
    out: dict[str, list[Any]] = {}
    for eid, t in tasks.items():
        if t in pending or t.exception() is not None:
            stats.events_failed += 1
            if t not in pending:
                _err(stats, f"{eid}: {type(t.exception()).__name__}: {t.exception()}")
        else:
            out[eid] = t.result()
    return out


async def _fill_status(http: httpx.AsyncClient, base: str, pairs: list[tuple[RawEvent, str | None]],
                       stats: SpektrixStats, delay: float, cap: int) -> None:
    """Best-effort sold-out from /status for the first `cap` sessions. Unverified on
    real sold-out data: only `capacity > 0 and available == 0` is taken as sold out."""
    pacer, sem = _Pacer(delay), asyncio.Semaphore(INSTANCE_CONCURRENCY)

    async def one(raw: RawEvent, iid: str) -> None:
        async with sem:
            await pacer.wait()
            try:
                st = await _get_json(http, f"{base}/instances/{iid}/status")
            except (httpx.HTTPError, ValueError):
                return
        stats.status_calls += 1
        if isinstance(st, dict):
            so = sold_out({}, st)
            if so is not None:
                raw.sold_out = so

    todo = [(r, i) for r, i in pairs if i and r.sold_out is None][:cap]
    await asyncio.gather(*(one(r, i) for r, i in todo), return_exceptions=True)


async def fetch_events(client: str | SpektrixClient, *, http: httpx.AsyncClient,
                       start: date | datetime, end: date | datetime,
                       with_instances: bool = True, max_events: int = 400,
                       check_availability: bool = False, status_cap: int = STATUS_CAP,
                       now: datetime | None = None, delay: float = INSTANCE_DELAY,
                       timeout: float = CLIENT_TIMEOUT,
                       stats: SpektrixStats | None = None,
                       event_url: str | None = None) -> list[RawEvent]:
    """RawEvents for one Spektrix client in [start, end] (inclusive dates). Sessions that
    started before `now` (default: the current local time) are dropped. Never raises on API
    errors: a dead client returns [], a bad event is skipped and counted in `stats`.
    `event_url`: template for events with an empty webUrl (see `_url`)."""
    sc = client if isinstance(client, SpektrixClient) else SpektrixClient(client)
    stats = stats if stats is not None else SpektrixStats()
    s, e = _as_date(start), _as_date(end)
    listed = await _list_events(http, sc, s, e, stats)
    if listed is None:
        log.warning("spektrix %s: no events endpoint answered: %s", sc.client, stats.errors[-1:])
        return []
    stats.base, raw_events = listed
    stats.events_seen = len(raw_events)
    events, stats.events_dropped = select_events(raw_events, s, e, max_events, stats)
    if not with_instances:
        return _safe_map(lambda e: event_to_raw(e, event_url), events, stats)
    by_event = await _bulk_instances(http, stats.base, s, e, stats)
    stats.bulk = by_event is not None
    if by_event is None:
        by_event = await _per_event_instances(http, stats.base, events, s, e, stats,
                                              delay=delay, timeout=timeout)
    now = now if now is not None else datetime.now()
    per_event = [_to_raws(ev, by_event.get(ev["id"], ()), s, e, now, stats, event_url) for ev in events]
    pairs = [p for ps in per_event for p in ps]
    if check_availability:
        await _fill_status(http, stats.base, pairs, stats, delay, status_cap)
    stats.instances = len(pairs)
    out: list[RawEvent] = []
    for ps in per_event:
        raws = [r for r, _ in ps]
        one = collapse_slots(raws) if raws else None
        if one is not None:
            stats.collapsed += 1
        out.extend(one if one is not None else raws)
    return out


def _safe_map(fn, events: list[dict[str, Any]], stats: SpektrixStats) -> list[RawEvent]:
    out = []
    for ev in events:
        try:
            out.append(fn(ev))
        except Exception as e:                               # noqa: BLE001 — one bad event
            stats.events_failed += 1
            _err(stats, f"{ev.get('id')}: {e}")
    return out


# ── Source registry entry ────────────────────────────────────────────────────

def _title_variants(title: str) -> list[str]:
    """Names a listing page may use for a box-office title, most specific first: the title,
    without '(2026)' / '[Autumn 26]' tags, then each side of 'A: B' / 'A - B'."""
    out = [title]
    bare = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", title).strip()
    parts = re.split(r"\s*(?::|\s[-–—]\s|\|)\s*", bare)
    for v in [bare, *parts]:
        if v and v not in out:
            out.append(v)
    return out


def _slug_index(lines: list[str], base: str) -> dict[str, str | None]:
    """last path segment (trailing '-2' dropped) -> same-site URL; None when two URLs share it.
    For cards whose link text is 'Details' / 'More info' but whose URL is the event's slug."""
    host = urlsplit(base).hostname or ""
    idx: dict[str, str | None] = {}
    for ln in lines:
        href = ln.rsplit(" -> ", 1)[-1].strip()
        u = urldefrag(urljoin(base, href))[0].split("?")[0].rstrip("/")
        sp = urlsplit(u)
        seg = sp.path.rsplit("/", 1)[-1]
        if (sp.hostname or "").removeprefix("www.") != host.removeprefix("www.") or sp.path.count("/") < 2 or not seg:
            continue
        key = re.sub(r"-\d+$", "", seg.lower())
        idx[key] = u if idx.get(key, u) == u else None
    return idx


def _fill_urls(raws: list[RawEvent], html: str, base: str) -> list[RawEvent]:
    """Empty webUrl: the listing page's unique anchor for the title (or a variant of it, see
    `_title_variants`), else the one same-site link whose last path segment is the title's
    slug, else '' (build -> listing)."""
    if all(r.url for r in raws):
        return raws
    lines, memo = anchor_lines(html), {}
    slugs = _slug_index(lines, base)
    out = []
    for r in raws:
        if not r.url:
            if r.title not in memo:
                vs = _title_variants(r.title)
                memo[r.title] = (next((u for v in vs if (u := match_title_url(v, lines, base))), None)
                                 or next((u for v in vs if (u := slugs.get(slugify(v)))), None))
            if memo[r.title]:
                r = r.model_copy(update={"url": memo[r.title]})
        out.append(r)
    return out


def client_for_venue(cfg: VenueConfig, html: str | None) -> SpektrixClient | None:
    """`adapter: {kind: spektrix, client: X[, domain: D]}` wins; else fingerprint the page."""
    a = cfg.adapter or {}
    if a.get("kind") == "spektrix" and a.get("client"):
        return SpektrixClient(str(a["client"]).lower(), _clean_domain(a.get("domain")))
    return detect(html, cfg.url)


def _window_days(cfg: VenueConfig, ctx: SourceCtx) -> int:
    """`adapter.window_days` (venues that announce a season far ahead) else the crawl window."""
    w = (cfg.adapter or {}).get("window_days")
    return max(ctx.window_days, w) if isinstance(w, int) and not isinstance(w, bool) else ctx.window_days


class SpektrixSource:
    """Source `spektrix`: venue -> client (config or fingerprint) -> per-session RawEvents."""
    kind = SOURCE_KIND

    def __init__(self, **fetch_kw: Any) -> None:
        self.fetch_kw = fetch_kw          # e.g. max_events, check_availability (tests: delay=0)

    async def records(self, cfg: VenueConfig, fr: FetchResult | None, ctx: SourceCtx) -> list[RawEvent]:
        try:
            body = fr.body if fr is not None and fr.body else None
            sc = client_for_venue(cfg, body)
            if sc is None:
                return []
            if ctx.offline:
                ctx.stats["spektrix.offline"] = ctx.stats.get("spektrix.offline", 0) + 1
                return []
            st = SpektrixStats()
            tpl = (cfg.adapter or {}).get("event_url")
            out = await fetch_events(sc, http=ctx.http, event_url=tpl if isinstance(tpl, str) else None, start=ctx.today,
                                     end=ctx.today + timedelta(days=_window_days(cfg, ctx)),
                                     stats=st, **{"now": datetime.combine(ctx.today, datetime.min.time()),
                                                  **self.fetch_kw})
            self._report(cfg, st, ctx)
            return _fill_urls(out, body, base_url(cfg, fr, ctx)) if body else out
        except Exception as e:                               # noqa: BLE001 — never break a crawl
            ctx.stats["spektrix.crashed"] = ctx.stats.get("spektrix.crashed", 0) + 1
            ctx.errors.append(f"{cfg.name}: spektrix {type(e).__name__}: {e}"[:300])
            return []

    @staticmethod
    def _report(cfg: VenueConfig, st: SpektrixStats, ctx: SourceCtx) -> None:
        for k in ("events_seen", "events_dropped", "events_failed", "instances", "status_calls", "collapsed"):
            ctx.stats[f"spektrix.{k}"] = ctx.stats.get(f"spektrix.{k}", 0) + getattr(st, k)
        if st.base is None:
            ctx.stats["spektrix.dead_clients"] = ctx.stats.get("spektrix.dead_clients", 0) + 1
        ctx.errors.extend(f"{cfg.name}: spektrix {e}" for e in st.errors)


register_source(SpektrixSource())
