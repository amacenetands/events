"""Deterministic time parsing: venue strings -> 24h "HH:MM"."""
from __future__ import annotations

import re

__all__ = ["parse_time_text", "all_times", "to_24h"]

_LB = r"(?:(?<=T)|(?<![\w£$€₽.,:/+]))"          # not inside a price/number/date; ISO "T19:30" ok
_HM_COLON = r"(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?::[0-5]\d)?"
_HM_DOT = r"(?P<h>[01]?\d|2[0-3])\.(?P<m>[0-5]\d)"
_CUE = "(?:" + "|".join(rf"(?<=\b{w}\s)" for w in ("at", "from", "doors", "open", "start", "starts", "begins")) + r"|(?<=@\s)|(?<=@))"
_MER = r"\s?(?P<ap>[ap])\.?m\b\.?"

def _g(pat: str, n: str) -> str:
    """Rename h/m/ap groups with suffix n."""
    return pat.replace("?P<h>", f"?P<h{n}>").replace("?P<m>", f"?P<m{n}>").replace("?P<ap>", f"?P<ap{n}>")


_TOKEN = re.compile(_LB + "(?:" + "|".join([
    r"(?P<noon>12\s*noon|noon|midday|12\s*midnight|midnight)",
    _g(_HM_COLON, "1") + "(?:" + _g(_MER, "1") + r")?(?!\d)",
    _g(_HM_DOT, "2") + _g(_MER, "2"),
    r"(?P<h3>1[0-2]|0?\d)" + _g(_MER, "3"),
    _CUE + r"(?P<h4>[01]?\d|2[0-3])\.(?P<m4>[0-5]\d)(?![\d.]*\d)",
]) + ")", re.I)   # order matters: earlier alternatives win at the same position
# bare start hour of a range whose end carries the meridiem: "6–8pm", "10–12pm"
_RANGE_START = re.compile(_LB + r"(?P<h>1[0-2]|0?\d)(?:[.:](?P<m>[0-5]\d))?\s*(?:[–—-]|to)\s*$", re.I)
_NUMERIC_DATE = re.compile(r"(?<!\d)\d{4}-\d\d-\d\d(?!\d)|(?<![\d.:])\d{1,2}(?P<s>[/.-])\d{1,2}(?P=s)\d{2,4}(?![.:]?\d)")
# a meridiem-less start that is really the head of a shared-meridiem range: "at 3.30–4.15pm"
_RANGE_TO_MER = re.compile(r"\s*(?:[–—-]|to)\s*(?:1[0-2]|0?\d)(?:[.:][0-5]\d)?\s?[ap]\.?m\b", re.I)
# dedicated time field: bare "19.30", "19:30", "19", "19h", "19 ч"
_FIELD = re.compile(r"^\s*(?P<h>[01]?\d|2[0-3])(?:[.:h](?P<m>[0-5]\d))?\s*(?:h|hrs?|ч\.?)?\s*$", re.I)


def to_24h(h: int, m: int, ap: str | None) -> str | None:
    """12h/24h parts -> 'HH:MM' (models._parse_time rules); None if invalid."""
    ap = (ap or "").lower()[:1]
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    if ap and not 0 <= h <= 23:
        return None
    if h > 23 or m > 59:
        return None
    return f"{h:02d}:{m:02d}"


def _scan(text: str) -> list[tuple[int, str]]:
    """(position, HH:MM) for every time token in text."""
    if not text:
        return []
    text = _NUMERIC_DATE.sub(lambda mm: " " * len(mm.group(0)), str(text))
    out: list[tuple[int, str]] = []
    for mt in _TOKEN.finditer(text):
        g = mt.groupdict()
        if g["noon"]:
            out.append((mt.start(), "00:00" if "night" in g["noon"].lower() else "12:00"))
            continue
        for i in "1234":
            if g.get("h" + i) is not None:
                h, mi, ap = int(g["h" + i]), int(g.get("m" + i) or 0), g.get("ap" + i)
                break
        else:
            continue
        if ap and h > 12:
            continue                                   # "19pm"
        if not ap and h <= 12 and _RANGE_TO_MER.match(text, mt.end()):
            continue                                   # emitted with the range's meridiem below
        t = to_24h(h, mi, ap)
        if not t:
            continue
        if ap:   # shared meridiem: "6–8pm" -> 18:00, "10–12pm" -> 10:00
            rs = _RANGE_START.search(text[max(0, mt.start() - 12):mt.start()])
            if rs:
                sh, sm = int(rs.group("h")), int(rs.group("m") or 0)
                st = to_24h(sh, sm, ap)
                if st and st > t:
                    st = to_24h(sh, sm, "a" if ap.lower().startswith("p") else "p")
                if st and sh == 12:
                    st = f"12:{sm:02d}"
                if st:
                    out.append((mt.start() - 1, st))
        out.append((mt.start(), t))
    return out


def all_times(text: str) -> list[str]:
    """Every start/end time in text as HH:MM, in order, de-duplicated."""
    return list(dict.fromkeys(t for _, t in sorted(_scan(text))))


def parse_time_text(text: str, field: bool = False) -> str | None:
    """First time in text as HH:MM, or None ('Various times', 'TBC').
    field=True: text is a dedicated time field, so bare '19.30' / '19:30' / '19' are accepted."""
    if field and isinstance(text, str):
        fm = _FIELD.match(text)
        if fm:
            return to_24h(int(fm["h"]), int(fm["m"] or 0), None)
    ts = all_times(text)
    return ts[0] if ts else None
