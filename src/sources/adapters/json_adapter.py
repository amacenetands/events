"""JsonAdapter: events in an inline <script> JSON blob or JS literal. sites/<city>.yaml:
  adapter: {kind: json, script: "script#data", js_after: "new Cal(", path: "a.b.*",
            title: title, date: start, time: t, url: url}"""
from __future__ import annotations
import json
from bs4 import BeautifulSoup
from .base import Adapter, Rec, register


@register("json")
class JsonAdapter(Adapter):
    direct = True

    def records(self, html: str, site_cfg: dict) -> list[Rec]:
        c = site_cfg.get("adapter") or {}
        soup = BeautifulSoup(html, "html.parser")
        sc = soup.select_one(c["script"])
        if sc is None:
            return []
        txt = sc.string or sc.get_text()
        try:
            if c.get("js_after"):
                i = txt.index(c["js_after"]) + len(c["js_after"])
                data, _ = json.JSONDecoder().raw_decode(txt[i:])
            else:
                data = json.loads(txt)
        except (ValueError, json.JSONDecodeError):
            return []
        nodes = [data]
        for part in filter(None, c.get("path", "").split(".")):
            nxt = []
            for n in nodes:
                if part == "*":
                    nxt.extend(n.values() if isinstance(n, dict) else n if isinstance(n, list) else [])
                elif isinstance(n, dict):
                    nxt.append(n.get(part))
            nodes = nxt
        flat = [x for n in nodes for x in (n if isinstance(n, list) else [n])]
        g = lambda n, k: str(n.get(k, "") or "") if k else ""
        return [Rec(title=g(n, c["title"]), date=g(n, c["date"]), time=g(n, c.get("time")),
                    url=g(n, c.get("url")), desc=g(n, c.get("desc"))[:160])
                for n in flat if isinstance(n, dict)]
