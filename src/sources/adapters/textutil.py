"""Shared date/time regexes and helpers for the deterministic reducer."""
from __future__ import annotations
import contextvars
import re
import datetime as dt

MON = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
_M = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
R1 = re.compile(r"\b(\d{1,2})\s?(?:st|nd|rd|th)?\s+" + _M + r"(?:,?\s+(20\d\d))?", re.I)
R2 = re.compile(r"\b" + _M + r"\s+(\d{1,2})(?:st|nd|rd|th)?(?!\d|[:.]\d)\b(?:,?\s+(20\d\d))?", re.I)
R3 = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)")
# Russian: "19 июня 2026" and numeric day-first "19.06.2026" / "19.06"
RU_MON = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "ма": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
R4 = re.compile(r"\b(\d{1,2})\s+(янв|фев|мар|апр|ма[яй]|июн|июл|авг|сен|окт|ноя|дек)[а-я]*\.?(?:,?\s+(20\d\d))?", re.I)
R5 = re.compile(r"(?<![\d:.])(\d{1,2})\.(0\d|1[0-2])(?:\.((?:20)?\d\d))?(?![\d:])")
DATE_RE = re.compile("|".join(r.pattern for r in (R1, R2, R3, R4, R5)), re.I)
TIME = re.compile(r"\b(?:[01]?\d|2[0-3])[:.][0-5]\d\b(?:\s?[ap]m)?|\b\d{1,2}\s?[ap]m\b", re.I)


def _ru_mon(w: str) -> int:
    w = w.lower()
    return RU_MON["мар" if w.startswith("мар") else "ма" if w.startswith("ма") else w[:3]]


def all_dates(t: str, today: dt.date | None = None) -> list[dt.date]:
    """All dates in text. Year-less ones: upcoming occurrence; but if the date passed
    within the last 60 days it is a past date (dropped), not next year's."""
    today = today or dt.date.today()
    out: list[dt.date] = []

    def add(y, m, d):
        try:
            if y is None:
                x = dt.date(today.year, m, d)
                if x < today:
                    if (today - x).days <= 60:
                        return          # recently past, not next year's occurrence
                    x = dt.date(today.year + 1, m, d)
            else:
                x = dt.date(int(y), m, d)
        except ValueError:
            return
        out.append(x)

    for m in R1.finditer(t):
        add(m.group(3), MON[m.group(2)[:3].lower()], int(m.group(1)))
    for m in R2.finditer(t):
        add(m.group(3), MON[m.group(1)[:3].lower()], int(m.group(2)))
    for m in R3.finditer(t):
        add(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for m in R4.finditer(t):
        add(m.group(3), _ru_mon(m.group(2)), int(m.group(1)))
    for m in R5.finditer(t):
        y = m.group(3)
        add(None if not y else (2000 + int(y) if len(y) == 2 else int(y)), int(m.group(2)), int(m.group(1)))
    return out


def future_dates(t: str, today: dt.date | None = None, horizon_days: int = 800) -> set[dt.date]:
    today = today or dt.date.today()
    end = today + dt.timedelta(days=horizon_days)
    return {d for d in all_dates(t, today) if today <= d <= end}


def explicit_dates(t: str) -> list[dt.date]:
    """Only dates carrying an explicit year."""
    out = []
    for m in R1.finditer(t):
        if m.group(3):
            try: out.append(dt.date(int(m.group(3)), MON[m.group(2)[:3].lower()], int(m.group(1))))
            except ValueError: pass
    for m in R2.finditer(t):
        if m.group(3):
            try: out.append(dt.date(int(m.group(3)), MON[m.group(1)[:3].lower()], int(m.group(2))))
            except ValueError: pass
    for m in R3.finditer(t):
        try: out.append(dt.date(int(m[1]), int(m[2]), int(m[3])))
        except ValueError: pass
    for m in R4.finditer(t):
        if m.group(3):
            try: out.append(dt.date(int(m.group(3)), _ru_mon(m.group(2)), int(m.group(1))))
            except (ValueError, KeyError): pass
    for m in R5.finditer(t):
        y = m.group(3)
        if y:
            try: out.append(dt.date(2000 + int(y) if len(y) == 2 else int(y), int(m.group(2)), int(m.group(1))))
            except ValueError: pass
    return out


def past_only(t: str, today: dt.date | None = None) -> bool:
    """True when every date has an explicit year and all are before today."""
    today = today or dt.date.today()
    ex = explicit_dates(t)
    return bool(ex) and max(ex) < today and not future_dates(t, today)


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


_SITE_HOST: contextvars.ContextVar[str | None] = contextvars.ContextVar("site_host", default=None)


def _host(u: str) -> str:
    h = re.sub(r"^https?://", "", u or "").split("/")[0].split("@")[-1].lower()
    return h[4:] if h.startswith("www.") else h


def set_site_host(url: str | None):
    """Record the site being reduced so strip_host only relativises same-host links."""
    return _SITE_HOST.set(_host(url) if url else None)


def strip_host(href: str) -> str:
    """Make same-site absolute URLs relative; keep external absolute URLs intact."""
    href = href or ""
    m = re.match(r"^https?://[^/]+", href)
    if not m:
        return href
    site = _SITE_HOST.get()
    if site is None or _host(m.group(0)) == site:
        return href[m.end():]
    return href
