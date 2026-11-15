"""Deterministic date parsing: venue date strings -> DateSpec (one_off / run / recurring)."""
from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo

from .times import _scan

__all__ = ["DateSpec", "parse_date_text", "parse_date_specs", "weekday_of", "occurrences"]

ROLL_DAYS = 60          # a year-less date more than this far in the past means next year

Order = Literal["dmy", "mdy"]
Lang = Literal["en", "ru"]


@dataclass(frozen=True)
class DateSpec:
    """Normalised date description of one event."""
    kind: Literal["one_off", "run", "recurring"]
    start: date | None
    end: date | None
    byday: frozenset[int] | None
    instances: tuple[date, ...]
    times: tuple[str, ...]
    sessions_known: bool
    start_known: bool
    inconsistent: bool
    raw: str
    span: tuple[int, int] | None = None     # char offsets of the matched date text in `raw`


# --- vocab --------------------------------------------------------------------
_EN_MON = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
           r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_MON = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
_RU_MON_RE = r"янв|фев|мар|апр|ма[яйе]|июн|июл|авг|сен|окт|ноя|дек"
_RU_MON = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "ма": 5, "июн": 6, "июл": 7, "авг": 8,
           "сен": 9, "окт": 10, "ноя": 11, "дек": 12}

# singular forms (attachable to a date) per weekday, EN then RU
_WD_EN = ("mon(?:day)?", "tue(?:s(?:day)?)?", "wed(?:s|nesday)?", "thu(?:r(?:s(?:day)?)?)?",
          "fri(?:day)?", "sat(?:urday)?", "sun(?:day)?")
_WD_RU = ("пн|понедельник", "вт|вторник", "ср|среда|среду", "чт|четверг", "пт|пятница|пятницу",
          "сб|суббота|субботу", "вс|воскресенье")
_WD_RU_PL = ("понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям")
_WD_RU_GEN = ("понедельника", "вторника", "среды", "четверга", "пятницы", "субботы", "воскресенья")
_WD_ONE = re.compile(r"(?<![\w])(?:" + "|".join(
    f"(?P<d{i}>{en}|{ru})" for i, (en, ru) in enumerate(zip(_WD_EN, _WD_RU))) + r")\.?(?![\w])", re.I)
_WD_ANY = re.compile(r"(?<![\w])(?:" + "|".join(
    f"(?P<d{i}>(?:{en})s?|{pl}|{gen}|{ru})" for i, (en, ru, pl, gen)
    in enumerate(zip(_WD_EN, _WD_RU, _WD_RU_PL, _WD_RU_GEN))) + r")\.?(?![\w])", re.I)
_WD_ALT = "|".join(f"(?:{en})s?|{pl}|{gen}|{ru}" for en, ru, pl, gen in zip(_WD_EN, _WD_RU, _WD_RU_PL, _WD_RU_GEN))
_WD_W = r"(?<![\w])(?:" + _WD_ALT + r")\.?(?![\w])"
_WD_PLURAL = re.compile(r"(?:mon|tues|wednes|thurs|fri|satur|sun)days\b|ам\b|ям\b", re.I)
_WD_SPAN_SEP = re.compile(r"^\s*(?:[–—−-]|→|to|till|until|through|thru|по|до)\s*$", re.I)
_ALL = frozenset(range(7))
_DAILY = re.compile(r"\b(?:daily|every\s*day|each\s+day|7\s+days\s+a\s+week)\b|ежедневно|каждый\s+день", re.I)
_WEEKEND = re.compile(r"\bweekends?\b|выходн", re.I)
_WEEKDAYS = re.compile(r"\bweekdays\b|будн", re.I)
_RECUR_CUE = re.compile(
    r"\b(?:every|each)\s+(?:other\s+|second\s+|single\s+)?(?:" + _WD_ALT
    + r"|days?|weeks?|weekends?|months?|mornings?|afternoons?|evenings?|nights?)\b"
    r"|\b(?:weekly|fortnightly|monthly)\b"
    r"|кажд\w*\s+(?:" + "|".join(_WD_RU) + r"|день|дн|недел|месяц|выходн|утр|вечер)"
    r"|по\s+(?:" + "|".join(_WD_RU_PL) + r")", re.I)
_IRREG = re.compile(
    r"\b(?:first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th)\s+(?:" + _WD_ALT + r")\s+(?:of|in)\b"
    r"|\bevery\s+(?:other|second|2nd|two|2|third|3rd|three|3)\s+(?:weeks?|" + _WD_ALT + r")\b"
    r"|\balternate\s+(?:" + _WD_ALT + r")|\b(?:fortnightly|bi-?weekly|monthly)\b|\bonce\s+a\s+month\b"
    r"|\b(?:every|each)\s+month\b|раз\s+в\s+месяц|кажд\w*\s+втор", re.I)
_EXCEPT = re.compile(
    r"(?:\bclosed(?:\s+on)?|\bexcept(?:\s+(?:on|for))?|\bexcluding|\bnot\s+on|\bno\s+(?:shows?|performances?|sessions?)\s+on|кроме)"
    r"\s*:?\s*(?P<w>" + _WD_W + r"(?:\s*(?:,|&|and|or|и|/)\s*" + _WD_W + r")*)", re.I)
_UNKNOWN = re.compile(r"\b(?:tbc|tba|tbd|various\s+dates|multiple\s+dates|dates?\s+(?:to\s+be\s+)?(?:announced|confirmed)|to\s+be\s+(?:announced|confirmed)|coming\s+soon)\b|дата\s+уточняется", re.I)
_DASHISH = re.compile(r"[–—−-]|→|\bto\b|\bпо\b", re.I)
_EVERY_TAIL = re.compile(r"\b(?:every|each)\s*$", re.I)
_WS = re.compile(r"\s")

# --- date mentions ------------------------------------------------------------
_ORD = r"(?:\s?(?:st|nd|rd|th))?"
_YEAR = (r"(?:[,\s]+(?P<y>20\d\d)(?:\s*г\.?)?(?!\d)|\s+(?P<yy>2\d)(?![\d:.]|\s*(?:\d|[ap]\.?m\b|st|nd|rd|th|noon|midday|"
         r"(?:" + _EN_MON + r")\b)))?")
_PATTERNS = [
    ("iso", re.compile(r"(?<!\d)(?P<y>20\d\d)-(?P<m>\d\d)-(?P<d>\d\d)(?:[T\s](?P<H>\d\d):(?P<M>\d\d)(?::\d\d(?:\.\d+)?)?(?P<off>Z|[+-]\d\d:?\d\d)?)?(?!\d)")),
    ("num", re.compile(r"(?<![\d.:/£$€-])(?P<a>\d{1,2})(?P<s>[/.-])(?P<b>\d{1,2})(?P=s)(?P<y>20\d\d|\d\d)(?![\d.:]?\d)")),
    ("dm", re.compile(r"(?<![\d.:£$€₽,])(?P<d>\d{1,2})" + _ORD + r"(?:\s+of)?[\s.]*(?P<mon>" + _EN_MON + r")\b\.?" + _YEAR, re.I)),
    ("md", re.compile(r"(?<![\w])(?P<mon>" + _EN_MON + r")\b\.?\s+(?P<d>\d{1,2})" + _ORD + r"\b(?![:.]\d|\s?[ap]\.?m\b|%)(?:,?\s+(?P<y>20\d\d)(?!\d))?", re.I)),
    ("ru", re.compile(r"(?<![\d.:])(?P<d>\d{1,2})\s+(?P<mon>" + _RU_MON_RE + r")[а-я]*\.?" + _YEAR, re.I)),
    ("rudot", re.compile(r"(?<![\d.:])(?P<d>\d{1,2})\.(?P<m>0\d|1[0-2])(?![\d:]|\.\d)")),
    ("mon", re.compile(r"(?<![\w])(?P<mon>january|february|march|april|may|june|july|august|september|october"
                       r"|november|december|jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec)\.?(?![\w])"
                       r"(?:,?\s+(?P<y>20\d\d)(?!\d))?", re.I)),
]
# a bare month name only counts with a year, a cue word before it, or as one end of a range
_MON_CUE = re.compile(r"(?:\b(?:in|during|throughout|all|until|till|through|thru|to)\s+(?:of\s+)?)$", re.I)
_R_SEP = r"[–—−-]|→|›|»|to|until|till|through|по|до"
_PARTIAL_BEFORE = re.compile(r"(?<![\d.:£$€₽])(?P<d>\d{1,2})" + _ORD + r"\s*(?P<sep>" + _R_SEP + r"|and|&|,|и)\s*$", re.I)
_RANGE_SEP_WORD = re.compile(r"^(?:" + _R_SEP + r")$", re.I)
_PARTIAL_AFTER = re.compile(r"^\s*(?P<sep>[–—−-]|→|to|&|and|,)\s*(?P<d>\d{1,2})" + _ORD
                            + r"\b(?![:./-]\d|\s?[ap]\.?m\b|\s*(?:" + _EN_MON + r")\b|\s*(?:" + _RU_MON_RE
                            + r")|\s*[%+]|\s*(?:yrs?|years?)\b)(?:,?\s+(?P<y>20\d\d)(?!\d))?", re.I)
_AGE_PRE = re.compile(r"(?:\bages?|\baged|\byears?|\byrs|\d\+)\s*:?\s*(?:\d{1,2}\s*(?:[–—−-]|to)\s*)?$", re.I)
_RANGE_GAP = re.compile(r"^[\s,.]*(?:[–—−-]+|→|›|»|to|until|till|through|thru|по|до)[\s,.]*$", re.I)
_UNTIL_CUE = re.compile(r"(?:\b(?:closes?|closing|until|till|til|through|thru|ends?|to)|(?<![\w])(?:до|по))\s*:?\s*(?:on\s+)?$", re.I)
_FROM_CUE = re.compile(r"(?:\b(?:from|starts?|starting|begins?|beginning|commencing|opens?|opening)|(?<![\w])[сc])\s*:?\s*(?:on\s+)?$", re.I)
_ONWARDS = re.compile(r"^\s*(?:onwards?|and\s+beyond)\b", re.I)
_PRE_WD = re.compile(r"(?:" + _WD_ONE.pattern + r")[,.]?\s*$", re.I)
_POST_WD = re.compile(r"^[\s,•·|(]*(?:" + _WD_ONE.pattern + r")\)?", re.I)
_WD_LIST_HEAD = re.compile(r"(?:" + _WD_ANY.pattern.replace("?P<d", "?P<l") + r")\s*(?:&|and|,|/|\+|и)\s*$", re.I)
_T_STRICT = r"\d{1,2}(?:[:.]\d\d(?:\s?[ap]\.?m\.?)?|\s?[ap]\.?m\.?)"
_TIME_RANGE = re.compile(r"(?:" + _T_STRICT + r"|\d{1,2})\s*(?:[–—−-]|to|until|till|до)\s*" + _T_STRICT
                         + r"|" + _T_STRICT + r"\s*(?:[–—−-]|to|until|till|до)\s*\d{1,2}(?:[:.]\d\d)?", re.I)
_TIMEISH = re.compile(r"\d{1,2}(?:[:.]\d\d)?\s?(?:[ap]\.?m\.?)?|\b(?:at|from|@|doors)\b", re.I)
_GROUP_CUT = re.compile(r"[,;&|/•·]|\band\b|(?<![\w])и(?![\w])", re.I)


@dataclass
class _M:
    """One date mention in the text."""
    start: int
    end: int
    d: int
    m: int
    y: int | None
    kind: str = "dm"
    wd: int | None = None          # attached weekday
    wd_post: bool = False          # weekday written after the date
    time: str | None = None
    val: date | None = None
    partial: bool = False
    inferred: bool = False         # year guessed from today, not borrowed/explicit
    from_next: bool = False        # year borrowed from a later explicit mention
    weak: bool = False             # bare month name without year/cue
    wd_byday: bool = False         # attached weekday reinterpreted as a schedule day

    @property
    def month_only(self) -> bool:
        return self.kind == "mon"

    @property
    def last(self) -> date | None:
        """Last day covered (whole month for month-only mentions)."""
        if self.val is None or not self.month_only:
            return self.val
        return self.val.replace(day=calendar.monthrange(self.val.year, self.val.month)[1])


def _wd_index(mt: re.Match) -> int:
    """Index of the matched dN group."""
    return next(i for i in range(7) if mt.group(f"d{i}"))


def _mon_ru(w: str) -> int:
    """Russian month word -> month number (textutil._ru_mon)."""
    w = w.lower()
    return _RU_MON["мар" if w.startswith("мар") else "ма" if w.startswith("ма") else w[:3]]


def _year(mt: re.Match) -> int | None:
    """Year from y / yy groups."""
    gd = mt.groupdict()
    if gd.get("y"):
        y = int(gd["y"])
        return 2000 + y if y < 100 else y
    if gd.get("yy"):
        return 2000 + int(gd["yy"])
    return None


def _iso(mt: re.Match, tz: ZoneInfo | None) -> _M:
    """ISO mention; an explicit offset is converted into tz when given."""
    y, m, d = int(mt["y"]), int(mt["m"]), int(mt["d"])
    mm = _M(mt.start(), mt.end(), d, m, y, kind="iso")
    if mt["H"]:
        H, M = int(mt["H"]), int(mt["M"])
        if tz is not None and mt["off"]:
            off = mt["off"]
            if off == "Z":
                tzi = dt.timezone.utc
            else:
                sign = -1 if off[0] == "-" else 1
                digits = off[1:].replace(":", "")
                tzi = dt.timezone(sign * dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
            x = dt.datetime(y, m, d, H, M, tzinfo=tzi).astimezone(tz)
            mm.d, mm.m, mm.y, H, M = x.day, x.month, x.year, x.hour, x.minute
        mm.time = f"{H:02d}:{M:02d}"
    return mm


def _mentions(text: str, order: Order, lang: Lang, tz: ZoneInfo | None) -> list[_M]:
    """All date mentions, left to right, overlaps removed (earliest/longest wins)."""
    cands: list[_M] = []
    for kind, rx in _PATTERNS:
        if kind == "rudot" and lang != "ru":
            continue
        for mt in rx.finditer(text):
            try:
                if kind == "iso":
                    mm = _iso(mt, tz)
                elif kind == "num":
                    a, b = int(mt["a"]), int(mt["b"])
                    d, m = (b, a) if order == "mdy" else (a, b)
                    if m > 12 and d <= 12:
                        d, m = m, d
                    mm = _M(mt.start(), mt.end(), d, m, _year(mt), kind)
                elif kind == "rudot":
                    mm = _M(mt.start(), mt.end(), int(mt["d"]), int(mt["m"]), None, kind)
                elif kind == "ru":
                    mm = _M(mt.start(), mt.end(), int(mt["d"]), _mon_ru(mt["mon"]), _year(mt), kind)
                elif kind == "mon":
                    if not mt["mon"][0].isupper():
                        continue                      # "may", "march on"
                    mm = _M(mt.start(), mt.end(), 1, _MON[mt["mon"][:3].lower()], _year(mt), kind)
                    mm.weak = mm.y is None and not _MON_CUE.search(text[:mt.start()])
                else:
                    mm = _M(mt.start(), mt.end(), int(mt["d"]), _MON[mt["mon"][:3].lower()], _year(mt), kind)
            except (KeyError, ValueError, OverflowError):
                continue
            if not (1 <= mm.m <= 12 and 1 <= mm.d <= 31):
                continue
            cands.append(mm)
    cands.sort(key=lambda c: (c.start, -(c.end - c.start)))
    out: list[_M] = []
    for c in cands:
        if out and c.start < out[-1].end:
            continue
        out.append(c)
    return _expand_partials(text, out)


def _expand_partials(text: str, ms: list[_M]) -> list[_M]:
    """Add day-only mentions sharing a neighbour's month: '15–17 Nov', '26, 27 June', 'Nov 15–20', 'June 27 & 28'."""
    res: list[_M] = []
    prev_end = 0
    for mm in ms:
        if mm.month_only:
            res.append(mm)
            prev_end = mm.end
            continue
        added: list[_M] = []
        cur = mm.start
        while True:
            seg = text[prev_end:cur]
            pre = _PRE_WD.search(seg)
            if pre:
                seg = seg[:pre.start()]
            pm = _PARTIAL_BEFORE.search(seg)
            if not pm:
                break
            is_range = bool(_RANGE_SEP_WORD.match(pm["sep"]))
            if is_range and (added or int(pm["d"]) >= mm.d):
                break                       # a range partial sits right next to the full mention, before it
            pos = prev_end + pm.start()
            added.insert(0, _M(pos, prev_end + pm.start("sep"), int(pm["d"]), mm.m, mm.y, mm.kind, partial=True))
            cur = pos
            if is_range:
                break
        if added and _AGE_PRE.search(text[max(prev_end, added[0].start - 24):added[0].start]):
            added = []                      # "Ages 5-7, 21 Nov"
        res.extend(added)
        res.append(mm)
        prev_end = mm.end
        last = mm
        while True:
            am = _PARTIAL_AFTER.match(text[last.end:])
            if not am or (not _RANGE_SEP_WORD.match(am["sep"]) and mm.kind != "md") or int(am["d"]) <= last.d:
                break                       # '30 July – 16' (truncated) / '27 & 3' are not day ranges
            y = int(am["y"]) if am["y"] else mm.y
            tail = _M(last.end + am.start("d"), last.end + am.end(), int(am["d"]), mm.m, y, mm.kind, partial=True)
            if y is not None and mm.y is None:
                mm.y = y
                for p in added:
                    p.y = y
            res.append(tail)
            prev_end = tail.end
            if _RANGE_SEP_WORD.match(am["sep"]):
                break
            last = tail
    return res


def _attach_weekdays(text: str, ms: list[_M]) -> None:
    """Record a weekday written right before/after each mention ('Sun 15 Nov', '14 сентября Пн')."""
    for i, mm in enumerate(ms):
        if mm.month_only:
            continue
        lo = ms[i - 1].end if i else 0
        before = text[lo:mm.start]
        pre = _PRE_WD.search(before)
        head = before[:pre.start()] if pre else ""
        if (pre and not _WD_PLURAL.search(pre.group(0)) and not _EVERY_TAIL.search(head)
                and not _WD_LIST_HEAD.search(head)):
            mm.wd = _wd_index(pre)
            mm.start = lo + pre.start()
            continue
        hi = ms[i + 1].start if i + 1 < len(ms) else len(text)
        post = _POST_WD.match(text[mm.end:hi])
        if post and not _WD_PLURAL.search(post.group(0)):
            mm.wd = _wd_index(post)
            mm.wd_post = True
            mm.end = mm.end + post.end()


def _mk(y: int, m: int, d: int) -> date | None:
    """date() or None."""
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _past(today: date, x: date) -> bool:
    """x is more than ROLL_DAYS before today."""
    return (today - x).days > ROLL_DAYS


def _infer(mm: _M, today: date) -> date | None:
    """Year-less mention: this year, or next if > ROLL_DAYS past; an attached weekday can pick the
    other year, but never one that is itself long past (then the weekday is just inconsistent)."""
    def last(x: date) -> date:
        return x.replace(day=calendar.monthrange(x.year, x.month)[1]) if mm.month_only else x

    x = _mk(today.year, mm.m, mm.d)
    if x is None or _past(today, last(x)):
        x = _mk(today.year + 1, mm.m, mm.d) or x
    if x is not None and mm.wd is not None and x.weekday() != mm.wd:
        alt = _mk(today.year + (x.year == today.year), mm.m, mm.d)    # the other of this/next year
        if alt and alt.weekday() == mm.wd and not _past(today, alt):
            return alt
    return x


def _resolve(ms: list[_M], today: date, text: str) -> None:
    """Set .val on every mention; year-less ones borrow only from a mention with an explicit year
    (nearest later, else nearest earlier), otherwise each is inferred independently. The borrowed
    year shifts by one only across a range ('11 Dec – 24 Jan 2027') or when it would be long past."""
    for mm in ms:
        if mm.y is not None:
            mm.val = _mk(mm.y, mm.m, mm.d)

    def ranged(i: int, j: int) -> bool:
        """ms[i] and ms[j] are neighbours joined by a range separator."""
        a, b = min(i, j), max(i, j)
        return b == a + 1 and _is_range_gap(text[ms[a].end:ms[b].start])

    for i, mm in enumerate(ms):
        if mm.y is not None:
            continue
        j = next((k for k in range(i + 1, len(ms)) if ms[k].y is not None and ms[k].val), None)
        if j is not None:
            nxt = ms[j].val
            x = _mk(nxt.year, mm.m, mm.d)
            if x and x > nxt and ranged(i, j):
                x = _mk(nxt.year - 1, mm.m, mm.d) or x
            mm.val = x
            mm.from_next = True
            continue
        j = next((k for k in range(i - 1, -1, -1) if ms[k].y is not None and ms[k].val), None)
        if j is not None:
            prv = ms[j].val
            x = _mk(prv.year, mm.m, mm.d)
            if x and x < prv and (ranged(j, i) or _past(today, x)):
                x = _mk(prv.year + 1, mm.m, mm.d) or x
            mm.val = x
            continue
        mm.val = _infer(mm, today)
        mm.inferred = True


def _days_in_text(text: str) -> frozenset[int] | None:
    """Weekday set named in text (no date stripping)."""
    days: set[int] = set()
    if _DAILY.search(text):
        return _ALL
    if _WEEKEND.search(text):
        days |= {5, 6}
    if _WEEKDAYS.search(text):
        days |= {0, 1, 2, 3, 4}
    hits = list(_WD_ANY.finditer(text))
    for i, h in enumerate(hits):
        a = _wd_index(h)
        days.add(a)
        if i + 1 < len(hits) and _WD_SPAN_SEP.match(text[h.end():hits[i + 1].start()]):
            b = _wd_index(hits[i + 1])
            k = a
            while k != b:
                k = (k + 1) % 7
                days.add(k)
    return frozenset(days) if days else None


def weekday_of(text: str) -> frozenset[int] | None:
    """Weekdays named in text (0=Mon): 'Thursdays', 'Sat & Sun', 'weekends', 'Mon–Fri', 'Сб/Вс', 'daily'."""
    try:
        return _days_in_text(str(text or ""))
    except Exception:
        return None


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    """Replace spans with spaces (keeps positions)."""
    chars = list(text)
    for a, b in spans:
        for k in range(a, min(b, len(chars))):
            chars[k] = " "
    return "".join(chars)


def _norm(raw: str) -> tuple[str, list[int]]:
    """Collapse whitespace and strip; returns text plus a map from text index to raw index."""
    out: list[str] = []
    pos: list[int] = []
    sp = True
    for i, ch in enumerate(raw):
        if ch.isspace():
            if sp:
                continue
            out.append(" ")
            pos.append(i)
            sp = True
        else:
            out.append(ch)
            pos.append(i)
            sp = False
    if out and out[-1] == " ":
        out.pop()
        pos.pop()
    return "".join(out), pos


def _is_range_gap(gap: str) -> bool:
    """Only a dash/to/until (after dropping times and whole time ranges) between two mentions."""
    gap = _TIME_RANGE.sub(" ", gap)
    gap = _TIMEISH.sub(" ", gap)
    return bool(_RANGE_GAP.match(gap))


class _Ctx:
    """Per-parse state shared by the group builders."""

    def __init__(self, text: str, ms: list[_M], today: date):
        self.text, self.ms, self.today = text, ms, today
        self.excl_spans: list[tuple[int, int]] = []
        excl: set[int] = set()
        for mt in _EXCEPT.finditer(text):
            excl |= _days_in_text(mt["w"]) or set()
            self.excl_spans.append(mt.span())
        self.excl = frozenset(excl)
        self.blank = _blank(text, [(m.start, m.end) for m in ms])
        self.rest = _blank(self.blank, self.excl_spans)
        rest = self.rest
        hits = list(_WD_ANY.finditer(rest))
        self.irregular = bool(_IRREG.search(rest))
        self.recur_cue = bool(_RECUR_CUE.search(rest) or _DAILY.search(rest) or _WEEKEND.search(rest)
                              or _WEEKDAYS.search(rest) or self.irregular or len(hits) >= 2
                              or _WD_PLURAL.search(" ".join(h.group(0) for h in hits)))

    def byday(self, a: int, b: int) -> frozenset[int] | None:
        """Weekdays named in text[a:b] outside mentions, minus 'closed Mondays'."""
        days = _days_in_text(self.rest[a:b])
        if self.excl:
            if days is None:
                days = _ALL                     # "closed Mondays" implies open the other days
            if days is not None:
                days = days - self.excl or None
        return days

    def times(self, a: int, b: int, group: list[_M]) -> tuple[str, ...]:
        """Times in text[a:b] outside mentions, ISO times of the group included, in order."""
        found: list[tuple[int, str]] = [(m.start, m.time) for m in group if m.time]
        found += [(a + p, t) for p, t in _scan(self.blank[a:b])]
        return tuple(dict.fromkeys(t for _, t in sorted(found)))


def _spec(kind: str, start: date | None, end: date | None, byday: frozenset[int] | None, instances,
          times, sessions_known: bool, start_known: bool, inconsistent: bool, raw: str,
          span: tuple[int, int] | None = None) -> DateSpec:
    """DateSpec constructor shorthand."""
    return DateSpec(kind, start, end, byday, tuple(instances), tuple(times), sessions_known,
                    start_known, inconsistent, raw, span)


def _group_spec(cx: _Ctx, items: list, a: int, b: int, shared: tuple[frozenset[int] | None, tuple[str, ...]],
                raw: str) -> DateSpec:
    """DateSpec for one group: a single range, or a run of consecutive single mentions."""
    text, today = cx.text, cx.today
    group = [m for it in items for m in it[1:]]
    byday = cx.byday(a, b)
    if byday is None and shared[0] is not None:
        byday = shared[0]
    times = cx.times(a, b, group) or shared[1]
    sk_days = byday is not None and not cx.irregular

    def incons() -> bool:
        return any(m.wd is not None and not m.wd_byday and m.val.weekday() != m.wd for m in group)

    if items[0][0] == "range":
        _, ma, mb = items[0]
        if ma.inferred and mb.inferred:        # anchor on the end: '1 Sep – 20 Nov' is the current run
            e0 = mb.last
            s = _mk(e0.year, ma.m, ma.d)
            if s and s > e0:
                s = _mk(e0.year - 1, ma.m, ma.d) or s
            ma.val = s or ma.val
        s, e = ma.val, mb.last
        if e < s and ma.y is None and ma.from_next:     # anchor is to the right: move the start back
            s0 = _mk(s.year - 1, s.month, s.day)
            if s0 and not _past(today, s0):
                s = s0
        if e < s and mb.y is None:
            e = _mk(e.year + 1, e.month, e.day) or e
        if e < s and ma.y is None:
            s = _mk(s.year - 1, s.month, s.day) or s
        ma.val = s
        if not mb.month_only:
            mb.val = e
        if e < s:                               # contradictory explicit years: two separate dates
            return _spec("recurring", e, s, None, sorted({e, s}), times, True, True, incons(), raw)
        if s == e:
            return _spec("one_off", s, None, None, (), times, True, True, incons(), raw)
        if mb.wd is not None and mb.wd_post and (e - s).days > 7:
            mb.wd_byday = True                  # '19 Nov – 17 Dec, Thursday'
            byday = (byday or frozenset()) | {mb.wd}
            sk_days = not cx.irregular
        return _spec("run", s, e, byday, (), times, sk_days, True, incons(), raw)

    vals = sorted({m.val for m in group})
    first = group[0]
    pre = text[a:first.start]
    if len(vals) == 1:
        v = vals[0]
        if first.month_only and len(group) == 1:
            if _UNTIL_CUE.search(pre) and not _FROM_CUE.search(pre):
                return _spec("run", None, first.last, byday, (), times, sk_days, False, False, raw)
            return _spec("run", v, first.last, byday, (), times, sk_days, True, False, raw)
        if _UNTIL_CUE.search(pre) and not _FROM_CUE.search(pre):
            return _spec("run", None, v, byday, (), times, sk_days, False, incons(), raw)
        if cx.recur_cue and byday is not None:
            return _spec("recurring", v, None, byday, (), times, not cx.irregular, True, incons(), raw)
        if _FROM_CUE.search(pre) or _ONWARDS.match(text[group[-1].end:]):
            return _spec("run", v, None, byday, (), times, sk_days, True, incons(), raw)
        return _spec("one_off", v, None, None, (), times, True, True, incons(), raw)
    if byday is None:
        wds = {x.weekday() for x in vals}
        byday = frozenset(wds) if len(wds) == 1 else None
    return _spec("recurring", vals[0], vals[-1], byday, vals, times, True, True, incons(), raw)


def _parse(raw: str, joined: str, today: date, order: Order, lang: Lang, tz: ZoneInfo | None) -> list[DateSpec]:
    """Core: one DateSpec per disjoint span / instance group."""
    text, pos = _norm(joined)
    if not text:
        return []
    ms = _mentions(text, order, lang, tz)
    _attach_weekdays(text, ms)
    _resolve(ms, today, text)
    ms = [m for m in ms if m.val is not None]

    items: list[tuple] = []
    i = 0
    while i < len(ms):
        if i + 1 < len(ms) and _is_range_gap(text[ms[i].end:ms[i + 1].start]):
            items.append(("range", ms[i], ms[i + 1]))
            i += 2
        else:
            if not ms[i].weak:
                items.append(("single", ms[i]))
            i += 1
    ms = [m for it in items for m in it[1:]]
    cx = _Ctx(text, ms, today)

    def span_of(group: list[_M]) -> tuple[int, int] | None:
        inside = [m for m in group if pos[m.start] < len(raw)]
        if not inside:
            return None
        return pos[min(m.start for m in inside)], min(len(raw), pos[max(m.end for m in inside) - 1] + 1)

    if not items:
        rest = cx.rest
        byday = cx.byday(0, len(text))
        if _UNKNOWN.search(text) and not cx.recur_cue:
            return []
        span = byday is not None and len(byday) > 1 and bool(_DASHISH.search(rest))
        if byday is None and not cx.recur_cue:
            return []
        if byday is not None and not (cx.recur_cue or span):
            return []                     # a bare singular weekday ("Thu") is not a schedule
        times = cx.times(0, len(text), [])
        return [_spec("recurring", None, None, byday, (), times, byday is not None and not cx.irregular,
                      False, False, raw)]

    groups: list[list[tuple]] = []
    for it in items:
        if it[0] == "range" or not groups or groups[-1][-1][0] == "range":
            groups.append([it])
        else:
            groups[-1].append(it)
    firsts = [g[0][1].start for g in groups]
    lasts = [max(m.end for it in g for m in it[1:]) for g in groups]
    cuts = [0]
    for k in range(len(groups) - 1):
        gap = text[lasts[k]:firsts[k + 1]]
        c = _GROUP_CUT.search(gap)
        cuts.append(lasts[k] + c.start() if c else firsts[k + 1])
    cuts.append(len(text))
    if len(groups) > 1:
        sh_a, sh_b = (0, firsts[0]), (lasts[-1], len(text))
        sh_days = cx.byday(*sh_a) or cx.byday(*sh_b)
        sh_times = cx.times(*sh_a, []) + cx.times(*sh_b, [])
        shared = (sh_days, sh_times)
    else:
        shared = (None, ())
    out: list[DateSpec] = []
    for k, g in enumerate(groups):
        sp = _group_spec(cx, g, cuts[k], cuts[k + 1], shared, raw)
        sp = replace(sp, span=span_of([m for it in g for m in it[1:]]))
        if not any((o.kind, o.start, o.end, o.instances) == (sp.kind, sp.start, sp.end, sp.instances) for o in out):
            out.append(sp)
    return out


def _merge(specs: list[DateSpec], raw: str) -> DateSpec | None:
    """One spec covering all disjoint spans; sessions_known=False when spans were merged."""
    if not specs:
        return None
    if len(specs) == 1:
        return specs[0]
    spans = [s.span for s in specs if s.span]
    span = (min(a for a, _ in spans), max(b for _, b in spans)) if spans else None
    times = tuple(dict.fromkeys(t for s in specs for t in s.times))
    inc = any(s.inconsistent for s in specs)
    if all(s.kind != "run" for s in specs):
        vals = sorted({d for s in specs for d in (s.instances or (s.start,)) if d})
        return _spec("recurring", vals[0], vals[-1], None, vals, times, False, True, inc, raw, span)
    start = None if any(s.start is None for s in specs) else min(s.start for s in specs)
    end = None if any(s.kind == "run" and s.end is None for s in specs) else max(s.end or s.start for s in specs)
    bydays = [s.byday for s in specs]
    byday = frozenset().union(*bydays) if all(b is not None for b in bydays) else None
    return _spec("run", start, end, byday, (), times, False, start is not None, inc, raw, span)


def _prepare(text, end_text, recurrence_text, tz) -> tuple[str, ZoneInfo | None] | None:
    """Join the source fields; resolve tz (ignored when unknown)."""
    if not isinstance(text, str):
        return None
    parts = [p for p in (text, end_text, recurrence_text) if isinstance(p, str) and p.strip()]
    zi = None
    if tz:
        try:
            zi = ZoneInfo(tz)
        except Exception:
            zi = None
    return " – ".join(parts), zi


def parse_date_specs(text: str, today: date, *, order: Order = "dmy", lang: Lang = "en",
                     end_text: str = "", recurrence_text: str = "", tz: str | None = None) -> list[DateSpec]:
    """One DateSpec per disjoint date span / instance group, in text order; [] when none (never raises)."""
    try:
        prep = _prepare(text, end_text, recurrence_text, tz)
        if prep is None:
            return []
        return _parse(text, prep[0], today, order, lang, prep[1])
    except Exception:
        return []


def parse_date_text(text: str, today: date, *, order: Order = "dmy", lang: Lang = "en",
                    end_text: str = "", recurrence_text: str = "", tz: str | None = None) -> DateSpec | None:
    """Parse a venue date string into a single DateSpec (disjoint spans merged, sessions_known=False);
    None when no usable date (never raises). end_text/recurrence_text are joined with ' – '."""
    try:
        specs = parse_date_specs(text, today, order=order, lang=lang, end_text=end_text,
                                 recurrence_text=recurrence_text, tz=tz)
        return _merge(specs, text) if specs else None
    except Exception:
        return None


def occurrences(spec: DateSpec | None, start: date, end: date, *, max_n: int = 400) -> list[date]:
    """Concrete dates of spec inside [start, end] (at most max_n); a bare run counts every day.
    A run with unknown start is clamped to one year before its end."""
    if spec is None or end < start:
        return []
    if spec.instances:
        return [d for d in spec.instances if start <= d <= end][:max_n]
    if spec.kind == "one_off":
        return [spec.start] if spec.start and start <= spec.start <= end else []
    if spec.byday is None and spec.kind == "recurring":
        return []
    if spec.start:
        lo = max(start, spec.start)
    else:
        lo = max(start, (spec.end or end) - dt.timedelta(days=365))
    hi = min(end, spec.end) if spec.end else end
    days = spec.byday if spec.byday is not None else _ALL
    out, d = [], lo
    while d <= hi and len(out) < max_n:
        if d.weekday() in days:
            out.append(d)
        d += dt.timedelta(days=1)
    return out
