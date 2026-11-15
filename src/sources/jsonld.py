"""`jsonld` source: schema.org Event nodes in <script type="application/ld+json"> -> RawEvent."""
from __future__ import annotations

import json
import re
from typing import Any, Iterator

from ..config import VenueConfig
from ..fetch.types import FetchResult
from ..models import RawEvent, clean_text
from .base import SourceCtx, base_url, register_source

__all__ = ["JsonLdSource", "event_nodes", "node_to_raw", "raws_from_html"]

# Same type filter as html_sites._JSONLD_EVENT_TYPES / _parse_jsonld_nodes.
EVENT_TYPES = frozenset({
    "Event", "MusicEvent", "VisualArtsEvent", "ExhibitionEvent", "TheaterEvent", "EducationEvent",
    "ChildrensEvent", "ScreeningEvent", "ComedyEvent", "DanceEvent", "Festival", "SocialEvent",
})
DESC_MAX = 300
_BLOCK = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
_CURRENCY = {"GBP": "£", "EUR": "€", "USD": "$", "RUB": "₽", "RUR": "₽"}
_DAY_NAMES = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")
_TIME_PREFIX = re.compile(r"^(\d{1,2}:\d{2})")
_TYPE_WORD = {   # schema.org Event subtype -> RawEvent.category_text (models._CAT_ALIASES maps these)
    "MusicEvent": "music", "TheaterEvent": "theatre", "DanceEvent": "dance", "ExhibitionEvent": "exhibition",
    "VisualArtsEvent": "exhibition", "EducationEvent": "talk", "ScreeningEvent": "film",
    "ComedyEvent": "comedy", "Festival": "festival", "ChildrensEvent": "kids", "LiteraryEvent": "talk",
}


def _is_event(node: dict) -> bool:
    t = node.get("@type", "")
    types = [str(x) for x in (t if isinstance(t, list) else [t])]
    return any(x in EVENT_TYPES for x in types) or "Event" in str(t)


def _walk(data: Any) -> Iterator[dict]:
    """Top-level dicts, top-level arrays (ATG) and @graph members."""
    if isinstance(data, list):
        for x in data:
            yield from _walk(x)
    elif isinstance(data, dict):
        if isinstance(data.get("@graph"), list):
            yield from _walk(data["@graph"])
        else:
            yield data


def event_nodes(html: str) -> list[dict]:
    """Event nodes from every ld+json block (malformed blocks skipped)."""
    nodes: list[dict] = []
    for block in _BLOCK.findall(html or ""):
        try:
            data = json.loads(block.strip(), strict=False)
        except ValueError:
            continue
        nodes.extend(n for n in _walk(data) if _is_event(n))
    return nodes


def _str(v: Any) -> str:
    return str(v).strip() if v not in (None, False) else ""


def _offers(node: dict) -> list[dict]:
    o = node.get("offers")
    return [x for x in (o if isinstance(o, list) else [o]) if isinstance(x, dict)]


def _amounts(offers: list[dict], default_cur: str) -> tuple[list[float], list[str], str]:
    """(numeric prices, non-numeric price strings, currency symbol) across offers."""
    vals, raw, cur = [], [], ""
    for o in offers:
        code = _str(o.get("priceCurrency"))
        cur = cur or _CURRENCY.get(code.upper(), code)
        for k in ("lowPrice", "price", "highPrice"):
            v = o.get(k)
            if v is None or v is False or v == "":
                continue
            try:
                vals.append(float(str(v).replace(",", "")))
            except (TypeError, ValueError):
                raw.append(_str(v))
    return vals, raw, cur or default_cur


def _fmt_amount(v: float, cur: str) -> str:
    n = f"{v:.2f}".removesuffix(".00")
    return f"{n} ₽" if cur == "₽" else f"{cur}{n}"


def _with_currency(text: str, cur: str) -> str:
    """'12 - 18' -> '£12 - 18', 'PWYC from 1' -> 'PWYC from £1'; text with a symbol unchanged."""
    if re.search(r"[£$€₽]|руб", text) or not re.search(r"\d", text):
        return text
    if cur == "₽":
        return re.sub(r"(\d+(?:[.,]\d+)?)(?!.*\d)", r"\1 ₽", text, count=1)
    return re.sub(r"(?<![\d.,])(\d)", lambda m: cur + m.group(1), text, count=1)


def price_text(node: dict, default_cur: str = "£") -> str:
    """'£12', '£10 – £30', 'Free' from offers; non-numeric prices kept verbatim (currency added); '' when none."""
    vals, raw, cur = _amounts(_offers(node), default_cur)
    if raw:
        return _with_currency(raw[0], cur)
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi == 0:
        return "Free"
    if lo == hi:
        return _fmt_amount(lo, cur)
    return f"{_fmt_amount(lo, cur)} – {_fmt_amount(hi, cur)}"


def _sold_out(node: dict) -> bool | None:
    """True when every offer says SoldOut; None when unknown."""
    av = [_str(o.get("availability")).rsplit("/", 1)[-1].lower() for o in _offers(node)]
    av = [a for a in av if a]
    return True if av and all(a == "soldout" for a in av) else None


def _schedule(node: dict) -> dict:
    sch = node.get("eventSchedule")
    sch = sch[0] if isinstance(sch, list) and sch else sch
    return sch if isinstance(sch, dict) else {}


def recurrence_text(sch: dict) -> str:
    """eventSchedule -> 'Thursdays 10:00' / 'daily 11:00' / ''."""
    if not sch:
        return ""
    dow = sch.get("byDay")
    dow = dow if isinstance(dow, list) else [dow] if dow else []
    days = []
    for d in dow:
        name = _str(d).rsplit("/", 1)[-1].lower()
        days += [p for p in _DAY_NAMES if p.lower().startswith(name[:3])][:1] if name else []
    freq = _str(sch.get("repeatFrequency")).upper()
    head = ", ".join(days) or ("daily" if freq in ("P1D", "DAILY") else "weekly" if freq in ("P1W", "WEEKLY") else "")
    tm = _TIME_PREFIX.match(_str(sch.get("startTime")))
    return " ".join(x for x in (head, tm.group(1) if tm else "") if x)


def _venue(node: dict, cfg: VenueConfig) -> str:
    loc = node.get("location")
    loc = loc[0] if isinstance(loc, list) and loc else loc
    name = clean_text(_str(loc.get("name"))) if isinstance(loc, dict) else ""
    return name if name and name.lower() != cfg.name.lower() else ""


def _url(node: dict, base: str) -> str:
    """Event link from url / @id, absolute against the page URL."""
    from urllib.parse import urljoin
    for k in ("url", "@id"):
        v = _str(node.get(k))
        if v.startswith(("http", "/")):
            return urljoin(base, v) if base else v
    return ""


def _types(node: dict) -> list[str]:
    t = node.get("@type", "")
    return [str(x).rsplit("/", 1)[-1] for x in (t if isinstance(t, list) else [t])]


def category_text(node: dict) -> str:
    """Our type word for the most specific schema.org Event subtype ('' for plain Event)."""
    words = [_TYPE_WORD[t] for t in _types(node) if t in _TYPE_WORD]
    specific = [w for w in words if w != "kids"]
    return (specific or words or [""])[0]


def node_to_raw(node: dict, cfg: VenueConfig, base: str = "", currency: str = "£") -> RawEvent | None:
    """One Event node -> RawEvent; None without a name or a start date. No date filtering."""
    sch = _schedule(node)
    title = clean_text(_str(node.get("name")))
    start = _str(node.get("startDate")) or _str(sch.get("startDate"))
    if not title or not start:
        return None
    desc = clean_text(_str(node.get("description")))[:DESC_MAX] or None
    return RawEvent(
        title=title, date_text=start, end_text=_str(sch.get("endDate")) or _str(node.get("endDate")) or None,
        price_text=price_text(node, currency) or None, recurrence_text=recurrence_text(sch) or None,
        url=_url(node, base) or None, desc=desc, venue=_venue(node, cfg) or None,
        sold_out=_sold_out(node), category_text=category_text(node),
        audience_text="kids" if "ChildrensEvent" in _types(node) else "",
        age_text=clean_text(_str(node.get("typicalAgeRange"))), source_kind="jsonld",
    )


def raws_from_html(html: str, cfg: VenueConfig, base: str = "", currency: str = "£") -> list[RawEvent]:
    """RawEvents for every usable Event node in a page."""
    return [r for r in (node_to_raw(n, cfg, base, currency) for n in event_nodes(html)) if r is not None]


class JsonLdSource:
    """Pure: parses the fetched listing page only."""
    kind = "jsonld"

    async def records(self, cfg: VenueConfig, fr: FetchResult | None, ctx: SourceCtx) -> list[RawEvent]:
        if fr is None or not fr.ok or not fr.body:
            return []
        out = raws_from_html(fr.body, cfg, base_url(cfg, fr, ctx), ctx.city.currency)
        ctx.stats["jsonld.records"] = ctx.stats.get("jsonld.records", 0) + len(out)
        return out


SOURCE = register_source(JsonLdSource())
