"""AutoAdapter: zero-config deterministic reducer.

1. harvest embedded JSON (all <script>, incl. JS literals) -> [J] lines, past dropped
2. strip script/style/svg/..., per-site remove_selectors, nav/header/footer/aside
3. repeated-card detection -> [C] lines, all-past cards dropped
4. <3 records -> flat-line fallback (date/time lines + 2 ctx lines), uncapped
"""
from __future__ import annotations
import json
import re
import datetime as dt
from collections import defaultdict
from bs4 import BeautifulSoup, Comment
from .base import Adapter, Rec, register
from .textutil import DATE_RE, TIME, R3, future_dates, past_only, squash, strip_host

DATEISH_KEY = re.compile(r"(^|_)(start|date|datetime|startdate|start_date|begin|from|when|performance|time|day)s?($|_)|date|start", re.I)
TITLE_KEY = re.compile(r"^(title|name|headline|label|event_?name|show|production|heading)$", re.I)
URL_KEY = re.compile(r"(url|link|href|slug|permalink)$", re.I)
_RE_JSON_START = re.compile(r'\[\s*\{\s*"|\{\s*"')
GENERIC_UI = re.compile(r"(book now|buy tickets?|more info|read more|find out more|learn more|sold out|tickets?|view details|book|see more)", re.I)
STRIP_TAGS = ["script", "style", "svg", "noscript", "iframe"]
FLAT_STRIP = STRIP_TAGS + ["picture", "source", "img", "video", "audio", "canvas", "form", "button", "select", "input"]
MIN_RECORDS = 3
MONTH_HDR = re.compile(r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+20\d\d$", re.I)


# ---------- 1. embedded JSON ----------
def _walk(o, out: list, depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(o, dict):
        title = next((v for k, v in o.items() if TITLE_KEY.match(k) and isinstance(v, str) and 3 <= len(v) <= 200), None)
        dates = [v for k, v in o.items() if isinstance(v, str) and not k.startswith("_")
                 and DATEISH_KEY.search(k) and len(v) < 60 and DATE_RE.search(v)]
        if title and dates:
            url = next((v for k, v in o.items() if URL_KEY.search(k) and isinstance(v, str) and len(v) < 300), "")
            out.append(dict(title=squash(title), date="; ".join(dict.fromkeys(dates)), url=url))
        for v in o.values():
            _walk(v, out, depth + 1)
    elif isinstance(o, list):
        for v in o:
            _walk(v, out, depth + 1)


def json_records(scripts) -> list[dict]:
    out: list[dict] = []
    dec = json.JSONDecoder()
    for s in scripts:
        typ = s.get("type") or ""
        if "ld+json" in typ:        # handled by the existing JSON-LD path
            continue
        t = s.string or s.get_text() or ""
        if len(t) < 80:
            continue
        if "json" in typ or s.get("id") == "__NEXT_DATA__":
            try:
                _walk(json.loads(t), out)
                continue
            except ValueError:
                pass
        if len(t) > 3_000_000:
            continue
        i = 0
        while True:
            m = _RE_JSON_START.search(t, i)
            if not m:
                break
            try:
                obj, end = dec.raw_decode(t[m.start():])
                _walk(obj, out)
                i = m.start() + end
            except ValueError:
                i = m.start() + 1
    seen, res = set(), []
    for r in out:
        k = (r["title"], r["date"])
        if k not in seen:
            seen.add(k)
            res.append(r)
    return res


def _compact_date(v: str) -> str:
    for f, ln in (("%Y-%m-%dT%H:%M", 16), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            d = dt.datetime.strptime(v[:ln], f)
            return d.strftime("%Y-%m-%d %H:%M" if "%H" in f else "%Y-%m-%d")
        except ValueError:
            pass
    return v[:40]


# ---------- 3. repeated cards ----------
def _txt(el) -> str:
    return squash(el.get_text(" ", strip=True))


def _is_card_text(t: str) -> bool:
    rest = TIME.sub("", DATE_RE.sub("", t))
    rest = re.sub(r"[\W\d_]+|\b(mon|tue|wed|thu|fri|sat|sun)\w*\b", " ", rest, flags=re.I)
    return len(rest.strip()) >= 6


def find_cards(root, min_n=3, min_cov=0.5, min_len=12, max_len=1500) -> list[list]:
    cands = []

    def consider(g):
        if len(g) < min_n:
            return
        texts = [_txt(k) for k in g]
        ok = [DATE_RE.search(t) is not None and _is_card_text(t) for t in texts]
        dated = sum(ok)
        if dated / len(g) < min_cov or dated < min_n:
            return
        med = sorted(len(t) for t in texts)[len(g) // 2]
        if not (min_len <= med <= max_len):
            return
        cands.append((dated, med, [k for k, o in zip(g, ok) if o]))

    for parent in root.find_all(True):
        by = defaultdict(list)
        for k in parent.find_all(True, recursive=False):
            by[(k.name, (k.get("class") or [""])[0])].append(k)
        for g in by.values():
            consider(g)
    bytok = defaultdict(list)
    for el in root.find_all(True):
        for c in (el.get("class") or []):
            bytok[(el.name, c)].append(el)
    for g in bytok.values():
        consider(g)
    cands.sort(key=lambda x: (-x[0], -x[1]))
    chosen, taken = [], set()
    for _, _, g in cands:
        g2 = [k for k in g if id(k) not in taken and not any(id(p) in taken for p in k.parents)
              and not any(id(d) in taken for d in k.find_all(True))]
        if len(g2) < min_n:
            continue
        chosen.append(g2)
        for k in g2:
            taken.add(id(k))
            for d in k.find_all(True):
                taken.add(id(d))
    return chosen


_ATOM = re.compile(r"^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?$|^\d{1,2}(?:st|nd|rd|th)?$|^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?(?:\s+20\d\d)?$|^\d{1,2}\s?(?:янв|фев|мар|апр|ма[яй]|июн|июл|авг|сен|окт|ноя|дек)[а-я]*$|^(?:янв|фев|мар|апр|ма[яй]|июн|июл|авг|сен|окт|ноя|дек)[а-я]*$|^(?:пн|вт|ср|чт|пт|сб|вс)\.?,?$|^20\d\d$", re.I)


def card_line(c) -> str:
    """Card text as ' | '-joined parts; split date atoms ('Sat','7','Nov') re-joined with spaces."""
    parts: list[str] = []
    last_date = False
    for s in c.stripped_strings:
        s = squash(s)
        if not s or (parts and parts[-1] == s) or GENERIC_UI.fullmatch(s):
            continue
        if _ATOM.match(s):
            if last_date:
                parts[-1] += " " + s
            else:
                parts.append(s)
            last_date = True
        else:
            parts.append(s)
            last_date = False
    a = c.find("a", href=True)
    href = strip_host(a["href"]) if a else ""
    return " | ".join(parts)[:400] + (f" -> {href}" if href and not href.startswith("#") else "")


# ---------- 4. flat fallback ----------
def flat_lines(soup) -> list[str]:
    s = BeautifulSoup(str(soup), "html.parser")
    for t in s.find_all(FLAT_STRIP + ["nav", "footer", "header", "aside"]):
        t.decompose()
    lines = []
    for blk in s.find_all(["h1", "h2", "h3", "h4", "h5", "li", "p", "div", "span", "td", "a", "time"]):
        if blk.find(["h1", "h2", "h3", "h4", "h5", "li", "p", "div", "td", "tr"]):
            continue
        t = _txt(blk)
        if t:
            lines.append(t)
    return lines


def date_context_lines(lines: list[str], ctx: int = 2, today=None) -> list[str]:
    """Dedupe repeated short non-date lines, keep date/time lines + `ctx` lines before."""
    seen: set[str] = set()
    out: list[str] = []
    for l in lines:
        if l in seen and not DATE_RE.search(l) and len(l) < 80:
            continue
        seen.add(l)
        if not out or out[-1] != l:
            out.append(l)
    keep: set[int] = set()
    for i, l in enumerate(out):
        if MONTH_HDR.match(l):
            keep.add(i)     # "November 2026" section header: bare day numbers follow
        elif (DATE_RE.search(l) or TIME.search(l)) and not past_only(l, today):
            keep.update(range(max(0, i - ctx), i + 1))
    return [out[i] for i in sorted(keep)]


def text_records(text: str, today=None) -> list[Rec]:
    """Reducer for already-flattened text (browser-markdown fallback)."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    lines = [squash(l) for l in text.splitlines() if l.strip()]
    return [Rec(raw=l) for l in dict.fromkeys(date_context_lines(lines, today=today))]


@register("auto")
class AutoAdapter(Adapter):
    def records(self, html: str, site_cfg: dict, today=None) -> list[Rec]:
        soup = BeautifulSoup(html, "html.parser")
        recs: list[Rec] = []
        grp: dict = {}
        for r in json_records(soup.find_all("script")):
            if past_only(r["date"], today):
                continue
            url = re.sub(r"#.*$", "", strip_host(r["url"].replace("\\/", "/")))
            title = re.sub(r"\s+\d{1,2}[:.]\d\d$", "", r["title"])
            grp.setdefault((title, url), []).extend(_compact_date(x) for x in r["date"].split("; "))
        for (title, url), ds in grp.items():
            recs.append(Rec(raw=f"[J] {title} | {', '.join(dict.fromkeys(ds))}" + (f" -> {url}" if url else "")))
        for t in soup.find_all(STRIP_TAGS):
            t.decompose()
        for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
            c.extract()
        for sel in site_cfg.get("remove_selectors") or []:
            try:
                for t in soup.select(sel):
                    t.decompose()
            except Exception:
                continue
        for t in soup.find_all(["nav", "header", "footer", "aside"]):
            t.decompose()
        root = soup.find("main") or soup.body or soup
        for g in find_cards(root):
            for c in g:
                if len(_txt(c)) > 1500:
                    continue
                line = card_line(c)
                if not past_only(line, today):
                    recs.append(Rec(raw="[C] " + line))
        # Flat date lines: sole source when <MIN_RECORDS structured records were found;
        # otherwise only the date lines NOT already covered by a record (cards often
        # miss secondary dates such as "30th Nov - 2nd Dec" listed outside the card grid).
        # Cards join text nodes with " | ", so compare with separators normalised away,
        # else every flat line "Sunday, 7:30 pm GMT ..." looks uncovered and is re-emitted
        # as an orphan title-less line (duplicates + confuses the LLM).
        covered = squash(re.sub(r"\s*\|\s*", " ", "\n".join(r.raw for r in recs)))
        extra = [l for l in date_context_lines(flat_lines(soup), today=today)
                 if len(recs) < MIN_RECORDS or squash(l) not in covered]
        recs += [Rec(raw=l) for l in extra]
        return self.dedupe(recs)
