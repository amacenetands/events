"""London Coliseum: <script id="json-data"> = [{id: slug, bookings: [{event_name, start, title: '7.30pm', ...}]}].
One Rec per production (date range as a run), title from event_name, page /events/<slug>/."""
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
        today, out = today or dt.date.today(), []
        for prod in data if isinstance(data, list) else []:
            by_title: dict[str, list] = {}
            for b in prod.get("bookings") or []:
                try:
                    d = dt.date.fromisoformat(b["start"][:10])
                except (KeyError, ValueError):
                    continue
                if d >= today:
                    by_title.setdefault(b.get("event_name") or prod.get("id", ""), []).append((d, b.get("title", "")))
            for title, bk in by_title.items():
                bk.sort()
                ds = sorted({d for d, _ in bk})
                times = [t for _, t in bk if t]
                date = _fmt(ds[0]) if len(ds) == 1 else f"{_fmt(ds[0])} - {_fmt(ds[-1])}"
                out.append(Rec(title=title, date=date, time=times[0] if times else "",
                               url=f"/events/{prod['id']}/" if prod.get("id") else ""))
        return out
