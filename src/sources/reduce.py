"""Deterministic pre-LLM reducer: html -> records -> chunks (or direct Events)."""
from __future__ import annotations
import dataclasses as dc
import re
import datetime as dt
from urllib.parse import urljoin
from .adapters import get_adapter, Rec
from .adapters.auto import AutoAdapter, text_records
from .adapters.textutil import all_dates, DATE_RE, set_site_host

CHUNK_CHARS = 6000
MAX_CHUNKS = 8
_KIDS_RE = re.compile(r"kids|children|ages \d|under \d", re.I)
_FAMILY_RE = re.compile(r"family", re.I)
_CLOSES_RE = re.compile(r"\b(?:closes?|until|till|through|ends?)\s*:?\s*(?:on\s+)?(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?(?=\d|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))", re.I)


@dc.dataclass
class Reduced:
    lines: list[str]
    chunks: list[str]
    thin: bool = False
    events: list | None = None     # set => structured, skip the LLM
    adapter: str = "auto"
    n_records: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def chunk_lines(lines: list[str], size: int = CHUNK_CHARS) -> list[str]:
    out, cur, n = [], [], 0
    for l in lines:
        if n + len(l) + 1 > size and cur:
            out.append("\n".join(cur)); cur, n = [], 0
        cur.append(l); n += len(l) + 1
    if cur:
        out.append("\n".join(cur))
    return out


_TIME_RE = re.compile(r"(?<![\d:.])([01]?\d|2[0-3])(?:[:.]([0-5]\d))?\s?([ap]m)?(?![\d:.]?\d)", re.I)


def _time_of(rec: Rec) -> str | None:
    """First HH:MM (24h) or h[:mm]am/pm in rec.time/date, after blanking out dates."""
    txt = f"{rec.time} {rec.date}".strip()
    txt = DATE_RE.sub(" ", txt)
    txt = re.sub(r"\b\d{4}-\d\d-\d\d\b", " ", txt)
    for m in _TIME_RE.finditer(txt):
        h, mi, ap = int(m.group(1)), m.group(2), (m.group(3) or "").lower()
        if not mi and not ap:
            continue                      # bare number is not a time
        if ap:
            if h < 1 or h > 12:
                continue
            h = h % 12 + (12 if ap == "pm" else 0)
        return f"{h:02d}:{mi or '00'}"
    return None


_RANGE_GAP_RE = re.compile(r"^[\s,]*(?:[–—-]|to|until|till|through)[\s,]*$", re.I)


def _is_true_range(text: str) -> bool:
    """'d1 – d2' (exactly two dates joined by a dash/to/until) vs a list of dates."""
    ms = list(DATE_RE.finditer(text))
    if len(ms) != 2:
        return False
    gap = re.sub(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?|\d{1,2}[:.]\d\d\s?(?:[ap]m)?", "", text[ms[0].end():ms[1].start()], flags=re.I)
    return bool(_RANGE_GAP_RE.match(gap))


def rec_to_event(rec: Rec, site: dict, today: dt.date) -> list:
    """Events from a structured Rec ([] if title/date unparseable). One Event per
    distinct date for a date list (kind=recurring when >=3); only a true
    'd1 - d2' range (or 'closes d') becomes a single kind=run with end_date."""
    from ..models import Event
    if not rec.title or not rec.date:
        return []
    ds = sorted(set(all_dates(rec.date, today)))
    if not ds:
        return []
    title = re.sub(r"\s+\d{1,2}[:.]\d\d$", "", rec.title).strip() or rec.title
    url = re.sub(r"#.*$", "", rec.url)
    hint = site.get("category_hint", "other")
    kids = hint == "kids" or bool(_KIDS_RE.search(rec.title))
    common = dict(
        title=title, venue=site["name"], time=_time_of(rec),
        category="other" if hint == "kids" else hint,
        audience="kids" if kids else ("family" if _FAMILY_RE.search(rec.title) else "all"),
        url=urljoin(site["url"], url) if url else site["url"],
        description=rec.desc or None, source=site["name"],
    )
    specs: list[tuple] = []   # (date, end_date, kind, recurrence)
    if len(ds) == 1 and _CLOSES_RE.search(rec.date):   # "Closes 17 Jan 2027" => ongoing until then
        start, end = min(today, ds[0]), ds[0]
        specs.append((start, None if start == end else end, "one_off" if start == end else "run", None))
    elif len(ds) == 1:
        specs.append((ds[0], None, "one_off", None))
    elif len(ds) == 2 and _is_true_range(rec.date):
        specs.append((ds[0], ds[1], "run", None))
    else:
        kind = "recurring" if len(ds) >= 3 else "one_off"
        specs.extend((d, None, kind, "multiple dates" if kind == "recurring" else None) for d in ds)
    out = []
    for d, e, kind, rcr in specs:
        try:
            out.append(Event(date=d, end_date=e, kind=kind, recurrence=rcr, **common))
        except Exception:
            return []
    return out


def _live(ev, today: dt.date) -> bool:
    return (ev.end_date or ev.date) >= today


def reduce_site(html: str, site: dict, pre_text: str | None = None) -> Reduced:
    today = dt.date.today()
    name = site.get("name", "?")
    set_site_host(site.get("url"))
    if not html:
        recs = text_records(pre_text or "")
        adapter_name = "text"
    else:
        adapter = get_adapter(site)
        adapter_name = adapter.kind
        recs = adapter.dedupe([r for r in adapter.records(html, site)])
        if not recs and adapter.kind != "auto":
            print(f"  [reduce] {name}: adapter {adapter.kind!r} found 0 records — falling back to auto")
            adapter, adapter_name = AutoAdapter(), "auto"
            recs = adapter.records(html, site)
        elif adapter.direct and recs and not (site.get("adapter") or {}).get("llm"):
            recs = [r for r in recs if r.title]
            per = [rec_to_event(r, site, today) for r in recs]
            if recs and all(per):
                evs = [e for grp in per for e in grp]
                live = [e for e in evs if _live(e, today)]
                seen, uniq = set(), []
                for e in live:
                    k = (e.title, e.date, e.time)
                    if k not in seen:
                        seen.add(k); uniq.append(e)
                print(f"  [reduce] {name}: {len(recs)} structured records -> {len(uniq)} events [adapter:{adapter.kind}, no LLM]")
                return Reduced([], [], False, uniq, adapter.kind, len(recs))
    lines = [r.line() for r in recs]
    chunks = chunk_lines(lines)
    if len(chunks) > MAX_CHUNKS:
        print(f"  [reduce] {name}: {len(chunks)} chunks > {MAX_CHUNKS}; truncating")
        chunks = chunks[:MAX_CHUNKS]
        lines = "\n".join(chunks).split("\n")
    thin = not recs
    if thin:
        print(f"  [reduce] {name}: thin page — needs js_required or different URL")
    return Reduced(lines, chunks, thin, None, adapter_name, len(recs))
