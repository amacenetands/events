"""JsArrayAdapter: events in an inline JS array literal of object literals (unquoted keys), e.g.
`const EVENTS=[{d:"2026-11-07",time:"12:00",name:"X",sub:"Y",price:"£5"}, ...];`
  adapter: {kind: js_array, var: EVENTS, title: name, date: d, time: time, desc: sub, price: price, url: link}
Only the literal array is read; rows pushed later by script (loops) are not seen."""
from __future__ import annotations
import json
import re
from bs4 import BeautifulSoup
from .base import Adapter, Rec, register

_KEY = re.compile(r'([{,]\s*)([A-Za-z_$][\w$]*)(\s*:)')
_STR = re.compile(r'"(?:\\.|[^"\\])*"')


def _array_text(src: str, var: str) -> str | None:
    m = re.search(rf'\b(?:const|let|var)\s+{re.escape(var)}\s*=\s*\[', src)
    if not m:
        return None
    i, depth, j = m.end() - 1, 0, m.end() - 1
    while j < len(src):
        if src[j] == '"':
            s = _STR.match(src, j)
            j = s.end() if s else j + 1
            continue
        depth += (src[j] == '[') - (src[j] == ']')
        j += 1
        if depth == 0:
            return src[i:j]
    return None


def _to_json(lit: str) -> list:
    parts, pos = [], 0
    for s in _STR.finditer(lit):          # quote bare keys only outside string literals
        parts.append(_KEY.sub(r'\1"\2"\3', lit[pos:s.start()]))
        parts.append(s.group())
        pos = s.end()
    parts.append(_KEY.sub(r'\1"\2"\3', lit[pos:]))
    return json.loads(re.sub(r',(\s*[\]}])', r'\1', "".join(parts)))


@register("js_array")
class JsArrayAdapter(Adapter):
    direct = True

    def records(self, html: str, site_cfg: dict, today=None) -> list[Rec]:
        c = site_cfg.get("adapter") or {}
        g = lambda n, k: str(n.get(k, "") or "") if k else ""
        for sc in BeautifulSoup(html, "html.parser").find_all("script"):
            lit = _array_text(sc.string or sc.get_text(), c.get("var", "EVENTS"))
            if lit is None:
                continue
            try:
                data = _to_json(lit)
            except ValueError:
                return []
            return [Rec(title=g(n, c["title"]), date=g(n, c["date"]), time=g(n, c.get("time")),
                        url=g(n, c.get("url")), desc=g(n, c.get("desc"))[:160], price=g(n, c.get("price")))
                    for n in data if isinstance(n, dict)]
        return []
