"""London Coliseum: <script id="json-data"> = [{id: slug, bookings: [{event_name, start, title: '7.30pm', ...}]}].
One Rec per upcoming booking (explicit performance list: date + its own time), title from
event_name, page /events/<slug>/."""
from __future__ import annotations
import datetime as dt
import json
from bs4 import BeautifulSoup
from ..base import Adapter, Rec, register


def _fmt(d: dt.date) -> str:
    return d.strftime("%-d %B %Y")


@register("coliseum")
class ColiseumAdapter(Adapter):
    direct = True

    def records(self, html: str, site_cfg: dict, today: dt.date | None = None) -> list[Rec]:
        sc = BeautifulSoup(html, "html.parser").select_one("script#json-data")
        if sc is None:
            return []
        try:
            data = json.loads(sc.string or sc.get_text())
        except ValueError:
            return []
        today = today or dt.date.today()
        out: list[tuple[dt.date, Rec]] = []
        for prod in data if isinstance(data, list) else []:
            url = f"/events/{prod['id']}/" if prod.get("id") else ""
            for b in prod.get("bookings") or []:
                try:
                    d = dt.date.fromisoformat(b["start"][:10])
                except (KeyError, ValueError, TypeError):
                    continue
                if d >= today:
                    out.append((d, Rec(title=b.get("event_name") or prod.get("id", ""), date=_fmt(d),
                                       time=b.get("title") or "", url=url)))
        out.sort(key=lambda x: (x[1].title, x[0]))
        return [r for _, r in out]
