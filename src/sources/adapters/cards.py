"""CardListAdapter: CSS-selector driven. sites/<city>.yaml:
  adapter: {kind: cards, card: ".ev", title: "h3", date: ".d", time: ".t", link: "a", desc: ".cat",
            price: ".price"}
Without a `price` selector (or when it finds nothing) the price is the first price-like
segment of the card's own text; without a time field, a time in the title ("Talks Monday 11.30")."""
from __future__ import annotations
import re
from bs4 import BeautifulSoup
from ...normalize import find_price, parse_time_text
from .base import Adapter, Rec, register
from .textutil import squash, strip_host

# a time token closing the title: "… Monday 11.30", "… 19:30", "… 7pm", "… 7.30am"
_TITLE_TIME = re.compile(r"(?<![\w.:])(\d{1,2}(?:[:.]\d\d)?\s?[ap]\.?m\.?|\d{1,2}[:.]\d\d)\s*$", re.I)
_TITLE_TIME_MIN, _TITLE_TIME_MAX = "06:00", "23:59"


def _t(el, sel: str | None) -> str:
    if not sel:
        return ""
    e = el.select_one(sel)
    return squash(e.get_text(" ", strip=True)) if e else ""


def _title_time(title: str) -> str:
    """HH:MM of a time token at the very end of the title, 06:00–23:59; '' otherwise
    ('2:22 A Ghost Story', 'After Midnight', 'Concert 2.50' have none)."""
    m = _TITLE_TIME.search(title)
    if not m:
        return ""
    tok = m.group(1)
    t = parse_time_text(tok) if re.search(r"[ap]\.?m", tok, re.I) else parse_time_text(tok, field=True)
    return t if t and _TITLE_TIME_MIN <= t <= _TITLE_TIME_MAX else ""


def _card_price(el, sel: str | None) -> str:
    """Price text from the `price` selector, else the first price-like segment of the card."""
    p = _t(el, sel)
    if p:
        return p
    return find_price(el.get_text("\n", strip=True)) or ""


@register("cards")
class CardListAdapter(Adapter):
    direct = True

    def records(self, html: str, site_cfg: dict, today=None) -> list[Rec]:
        c = site_cfg.get("adapter") or {}
        soup = BeautifulSoup(html, "html.parser")
        out: list[Rec] = []
        for el in soup.select(c["card"]):
            a = el.select_one(c.get("link", "a[href]")) or (el if el.name == "a" else None)
            href = strip_host(a["href"]) if a is not None and a.get("href") else ""
            title = _t(el, c.get("title")) or squash(el.get_text(" ", strip=True))[:100]
            time = _t(el, c.get("time")) or _title_time(title)
            out.append(Rec(title=title, date=_t(el, c.get("date")), time=time,
                           url=href, desc=_t(el, c.get("desc"))[: c.get("desc_max", 160)],
                           price=_card_price(el, c.get("price"))))
        return out
