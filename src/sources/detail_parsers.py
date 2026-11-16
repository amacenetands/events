"""Named detail-page parsers for the `follow_links` source (`follow: {detail: <name>}`).
A parser turns one event page into a RawEvent when the generic `<time datetime>` reader cannot
(no machine-readable dates on the page)."""
from __future__ import annotations

import re
from datetime import date
from typing import Callable

from ..models import RawEvent
from ..normalize import parse_date_specs
from .base import SourceCtx

__all__ = ["DETAIL_PARSERS", "delfont"]

_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
_DAY = rf"\d{{1,2}}(?:\s+{_MONTH})?(?:\s+\d{{4}})?"
_UNTIL = re.compile(rf"^Booking until\s+(?P<end>\d{{1,2}}\s+{_MONTH}\s+\d{{4}})$", re.I)
_RANGE = re.compile(rf"^(?P<start>{_DAY})\s+[-–—]\s+(?P<end>\d{{1,2}}\s+{_MONTH}\s+\d{{4}})$", re.I)
_SCHED_END = re.compile(r"^(Show Running Time|Christmas|Extra|First |Latecomers)", re.I)


def _end_of(text: str, today: date) -> tuple[date | None, date | None]:
    specs = parse_date_specs(text, today, tz="Europe/London")
    return (specs[0].start, specs[0].end or specs[0].start) if specs else (None, None)


_TIME = r"\d{1,2}(?:[.:]\d\d)?\s?[ap]m"
_ROW = re.compile(rf"((?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day(?:\s*(?:[-–—&,]|and)\s*(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day)*)"
                  rf"\s*:?\s*({_TIME}(?:\s*(?:&|and)\s*{_TIME})*)", re.I)


def _schedule(lines: list[str]) -> str | None:
    """'Monday - Saturday 7.30pm; Thursday & Saturday 2.30pm' from the 'Performance Schedule' block
    (the label/time halves of a row may sit on separate lines); stops before dated exceptions."""
    try:
        i = lines.index("Performance Schedule")
    except ValueError:
        return None
    block = []
    for ln in lines[i + 1:i + 16]:
        if _SCHED_END.match(ln):
            break
        block.append(ln)
    rows = [f"{d} {t}" for d, t in _ROW.findall(" ".join(block))]
    return "; ".join(rows) or None


def delfont(html: str, url: str, ctx: SourceCtx, link_pattern: str = "") -> RawEvent | None:
    """Delfont Mackintosh show page: header 'Booking until D Month YYYY' | 'D Month - D Month YYYY',
    'Performance Schedule' weekday/time rows. One run per show (open runs start today)."""
    from bs4 import BeautifulSoup
    from .follow_links import _title
    soup = BeautifulSoup(html, "html.parser")
    title = _title(soup)
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n", strip=True).splitlines() if ln.strip()]
    start = end = None
    for ln in lines[:120]:
        m = _UNTIL.match(ln) or _RANGE.match(ln)
        if m:
            end = _end_of(m["end"], ctx.today)[1]
            if m.groupdict().get("start"):
                s_txt = m["start"]
                if not re.search(r"\d{4}$", s_txt):
                    s_txt += " " + str(end.year if end else ctx.today.year)
                if not re.search(rf"{_MONTH}", s_txt):
                    s_txt = re.sub(r"^(\d{1,2})", rf"\1 {m['end'].split()[1]}", s_txt)
                start = _end_of(s_txt, ctx.today)[0]
                if start and end and start > end:       # '19 December - 23 January 2027'
                    start = start.replace(year=start.year - 1)
            break
    if not title or end is None or end < ctx.today:
        return None
    start = max(start or ctx.today, ctx.today)
    return RawEvent(title=title, date_text=f"{start.isoformat()} – {end.isoformat()}",
                    time_text=_schedule(lines), url=url, category_text="theatre", source_kind="follow_links")


DETAIL_PARSERS: dict[str, Callable[..., RawEvent | None]] = {"delfont": delfont}
