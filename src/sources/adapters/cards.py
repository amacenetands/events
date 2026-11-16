"""CardListAdapter: CSS-selector driven. sites/<city>.yaml:
  adapter: {kind: cards, card: ".ev", title: "h3", date: ".d", time: ".t", link: "a", desc: ".cat"}"""
from __future__ import annotations
from bs4 import BeautifulSoup
from .base import Adapter, Rec, register
from .textutil import squash, strip_host


def _t(el, sel: str | None) -> str:
    if not sel:
        return ""
    e = el.select_one(sel)
    return squash(e.get_text(" ", strip=True)) if e else ""


@register("cards")
class CardListAdapter(Adapter):
    direct = True

    def records(self, html: str, site_cfg: dict) -> list[Rec]:
        c = site_cfg.get("adapter") or {}
        soup = BeautifulSoup(html, "html.parser")
        out: list[Rec] = []
        for el in soup.select(c["card"]):
            a = el.select_one(c.get("link", "a[href]")) or (el if el.name == "a" else None)
            href = strip_host(a["href"]) if a is not None and a.get("href") else ""
            title = _t(el, c.get("title")) or squash(el.get_text(" ", strip=True))[:100]
            out.append(Rec(title=title, date=_t(el, c.get("date")), time=_t(el, c.get("time")),
                           url=href, desc=_t(el, c.get("desc"))[: c.get("desc_max", 160)]))
        return out
