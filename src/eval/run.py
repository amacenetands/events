"""Run the crawler's per-venue parse over the gold set and score every field.

Gold venue = tests/gold/<slug>/ (conventions: tests/gold/README.md) with
  meta.yaml      name, url, city, extract, source (expected winning kind), today (fetch date of
                 page.html), venue (explicit VenueConfig overrides on top of the sites entry),
                 details {url path: file} (follow_links), spektrix_fixtures (dir), notes
  page.html      the listing page as fetched (trimmed)
  expected.yaml  events: title, date, end_date, kind, byday, time, price [price_min], url,
                 sessions_known, start_known, evidence
  llm_reply.json {text_hash, records}: recorded LLM records for this page (`eval --record-llm`)

Offline and deterministic: `html_sites.parse_venue` runs exactly as in a crawl, detail pages
and API calls are served from the gold dir through httpx.MockTransport, the LLM parse cache
answers from the recorded reply (the live engine raises), the follow_links page cache is
bypassed, and every source/adapter gets the gold `today`. Field scores are over matched events;
coverage (found / found_wrong_date / missed / extra) is reported separately. A field is
correct when gold and pipeline agree, including both null.
"""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml

from ..config import DEFAULT_CITY, VenueConfig, all_venues, get_city
from ..fetch.types import FetchResult, FetchStatus
from ..models import Event
from ..sources.base import SourceCtx, load_builtin_sources

__all__ = ["GOLD_DIR", "FIELDS", "Counts", "Mismatch", "EvalReport", "GoldVenue", "StaleReply",
           "load_gold", "load_known_failures", "run_gold", "norm_price", "norm_title", "title_ratio",
           "reply_hash", "meta_divergence"]

GOLD_DIR = Path(__file__).resolve().parents[2] / "tests" / "gold"
FIELDS = ("title", "date", "end_date", "kind", "byday", "time", "price", "url", "sessions_known",
          "start_known")
TITLE_MIN = 0.9
DATE_GAP_MAX = 7          # title-only matches further apart than this are missed + extra
_WD = {d: i for i, d in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}


class StaleReply(RuntimeError):
    """A recorded llm_reply.json no longer matches the engine input for its page."""


# ── normalisation ────────────────────────────────────────────────────────────

def norm_title(t: str) -> str:
    t = (t or "").casefold().replace("&", " and ").replace("ё", "е")
    t = re.sub(r"[^\w]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_ratio(a: str, b: str) -> float:
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()


_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_FEE = re.compile(r"(?:\+|excl\.?|excludes?|plus)?\s*[£$€]?\s*\d+(?:\.\d+)?\s*(?:booking|transaction|service|handling)\s+fees?"
                  r"|(?:booking|transaction|service|handling)\s+fees?[^|;/\n]*", re.I)
_TIER = re.compile(r"\b(?:members?|membership|friends?|patrons?|concessions?|conc\b|students?|under\s*-?\s*\d+s?"
                   r"|u\d+s?\b|over\s*\d+s?|\d+\s*\+|unwaged|seniors?|child(?:ren)?|kids?|young|access|wheelchair"
                   r"|carers?|companions?|nhs|jobseekers?|disabled)|член|студент|льгот|пенсион|детск", re.I)
_FREE = re.compile(r"\bfree\b|бесплатн|вход\s+свободный|свободный\s+вход")


def norm_price(v: Any) -> str | None:
    """Comparable price = lowest general-admission amount (tests/gold/README.md):
    parentheticals and fee notes stripped, member / concession / student / age tiers dropped
    (unless every tier is one), Free = 0. '£15 | £12 Advance | £8 MEMBERS' -> '12'."""
    if v is None:
        return None
    s = str(v).casefold()
    s = re.sub(r"(?<=\d)[\s  ,](?=\d{3}(?!\d))", "", s)      # 1 500 / 1,500
    s = _FEE.sub(" ", _PAREN.sub(" ", s))
    segs = [x for x in re.split(r"[|;/\n,]", s) if x.strip()]
    general = [x for x in segs if not _TIER.search(x)] or segs
    nums: list[float] = []
    for x in general:
        nums += [float(n) for n in re.findall(r"\d+(?:\.\d+)?", x)]
        if _FREE.search(x):
            nums.append(0.0)
    return f"{min(nums):g}" if nums else None


def _norm_url(u: Any) -> str | None:
    if not u:
        return None
    p = urlsplit(str(u).strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", p.query, ""))


def _norm_date(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return v.isoformat() if isinstance(v, date) else str(v)


def _norm_byday(v: Any) -> str | None:
    if not v:
        return None
    out = sorted({x if isinstance(x, int) else _WD[str(x).strip().casefold()[:3]] for x in v})
    return ",".join(map(str, out))


def _norm_time(v: Any) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, int):          # YAML sexagesimal: 19:30 unquoted -> 1170
        return f"{v // 60:02d}:{v % 60:02d}"
    return str(v)


def _norm_bool(v: Any, default: bool = True) -> bool:
    return default if v is None else bool(v)


def expected_fields(e: dict) -> dict[str, Any]:
    price = norm_price(e.get("price"))
    if e.get("price_min") is not None:              # explicit numeric override (README)
        price = f"{float(e['price_min']):g}"
    return {"title": norm_title(e["title"]), "date": _norm_date(e.get("date")),
            "end_date": _norm_date(e.get("end_date")), "kind": e.get("kind") or "one_off",
            "byday": _norm_byday(e.get("byday")), "time": _norm_time(e.get("time")), "price": price,
            "url": _norm_url(e.get("url")), "sessions_known": _norm_bool(e.get("sessions_known")),
            "start_known": _norm_bool(e.get("start_known"))}


def event_fields(ev: Event) -> dict[str, Any]:
    return {"title": norm_title(ev.title), "date": _norm_date(ev.date), "end_date": _norm_date(ev.end_date),
            "kind": ev.kind, "byday": _norm_byday(ev.byday), "time": ev.time or None,
            "price": norm_price(ev.price), "url": _norm_url(ev.url), "sessions_known": ev.sessions_known,
            "start_known": ev.start_known}


# ── report types ─────────────────────────────────────────────────────────────

@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def add(self, exp: Any, got: Any) -> bool:
        """Score one (expected, got) pair; True when correct."""
        if exp is None and got is None:
            self.tn += 1
            return True
        if exp is not None and got == exp:
            self.tp += 1
            return True
        if got is not None:
            self.fp += 1
        if exp is not None:
            self.fn += 1
        return False

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
                "p": round(self.precision, 4), "r": round(self.recall, 4), "f1": round(self.f1, 4)}


@dataclass(frozen=True)
class Mismatch:
    venue: str
    title: str          # gold title (pipeline title for "extra")
    date: str | None    # gold date[Ttime] (pipeline date[Ttime] for "extra")
    field: str          # one of FIELDS, or "missing" (gold event not found) / "extra" (not in gold)
    expected: Any
    got: Any
    evidence: str = ""

    def line(self) -> str:
        ev = (self.evidence or "").replace("\n", " ")[:90]
        return f"{self.venue} | {self.title} | {self.date} | {self.field} | {self.expected} | {self.got} | {ev}"

    @property
    def key(self) -> tuple[str, str, str | None, str]:
        return (self.venue, self.title, self.date, self.field)

    @property
    def keyed_got(self) -> tuple[tuple[str, str, str | None, str], Any]:
        """(key, got) — a known failure whose wrong value changes counts as a new failure."""
        return (self.key, self.got)


@dataclass
class EvalReport:
    fields: dict[str, Counts] = field(default_factory=lambda: {f: Counts() for f in FIELDS})
    by_source: dict[str, dict[str, Counts]] = field(default_factory=dict)
    by_venue: dict[str, dict[str, Counts]] = field(default_factory=dict)
    venues: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: dict[str, dict[str, Any]] = field(default_factory=dict)   # gold id -> found/wrong
    extras: list[str] = field(default_factory=list)                  # "slug|title|date[Ttime]"
    mismatches: list[Mismatch] = field(default_factory=list)
    got: dict[str, list[Event]] = field(default_factory=dict, repr=False)   # slug -> built events (debug)

    @property
    def coverage(self) -> dict[str, int]:
        v = self.venues.values()
        return {k: sum(x[k] for x in v) for k in ("expected", "found", "found_wrong_date", "missed", "extra")}

    def mismatch_keys(self) -> set[tuple[str, str, str | None, str]]:
        return {m.key for m in self.mismatches}

    def to_dict(self) -> dict[str, Any]:
        def block(d: dict[str, Counts]) -> dict[str, Any]:
            return {f: c.to_dict() for f, c in d.items()}
        return {"fields": block(self.fields),
                "by_source": {s: block(d) for s, d in sorted(self.by_source.items())},
                "by_venue": {s: block(d) for s, d in sorted(self.by_venue.items())},
                "venues": self.venues, "coverage": self.coverage, "events": self.events,
                "extras": sorted(self.extras), "mismatches": [m.__dict__ for m in self.mismatches]}

    def table(self) -> str:
        rows = [f"{'field':<15}{'P':>7}{'R':>7}{'F1':>7}{'tp':>5}{'fp':>5}{'fn':>5}{'tn':>5}"]
        for f, c in self.fields.items():
            rows.append(f"{f:<15}{c.precision:>7.3f}{c.recall:>7.3f}{c.f1:>7.3f}{c.tp:>5}{c.fp:>5}{c.fn:>5}{c.tn:>5}")
        cov = self.coverage
        rows.append(f"coverage: expected {cov['expected']} found {cov['found']} (wrong date "
                    f"{cov['found_wrong_date']}) missed {cov['missed']} extra {cov['extra']} "
                    f"({len(self.venues)} venues)")
        return "\n".join(rows)

    def by_source_table(self) -> str:
        rows = [f"{'source':<14}" + "".join(f"{f[:8]:>9}" for f in FIELDS)]
        for s, d in sorted(self.by_source.items()):
            rows.append(f"{s:<14}" + "".join(f"{d[f].f1:>9.3f}" for f in FIELDS))
        return "\n".join(rows)


# ── gold loading ─────────────────────────────────────────────────────────────

def _sites_entry(name: str) -> VenueConfig | None:
    return next((v for v in all_venues() if v.name == name), None)


@dataclass
class GoldVenue:
    slug: str
    dir: Path
    meta: dict[str, Any]
    expected: list[dict[str, Any]]

    @property
    def name(self) -> str:
        return self.meta["name"]

    @property
    def source(self) -> str:
        return self.meta.get("source") or self.meta["extract"][0]

    @property
    def today(self) -> date:
        t = self.meta["today"]
        return t if isinstance(t, date) else date.fromisoformat(str(t))

    def overrides(self) -> dict[str, Any]:
        """Explicit config from meta: `venue:` plus `extract` when given."""
        o = dict(self.meta.get("venue") or {})
        if self.meta.get("extract"):
            o["extract"] = self.meta["extract"]
        return o

    def cfg(self) -> VenueConfig:
        """The venue's sites/<city>.yaml entry (by name) with meta overrides on top; venues not
        in a sites file (synthetic, dropped) are built from meta alone."""
        base = _sites_entry(self.name)
        if base is None:
            return VenueConfig(name=self.name, url=self.meta["url"], **self.overrides())
        return VenueConfig.model_validate({**base.model_dump(exclude_unset=True), **self.overrides()})

    def page(self) -> str:
        p = self.dir / "page.html"
        return p.read_text() if p.exists() else ""

    def llm_reply(self) -> dict | None:
        p = self.dir / "llm_reply.json"
        return json.loads(p.read_text()) if p.exists() else None


def meta_divergence(g: GoldVenue) -> list[str]:
    """Fields where meta overrides differ from the venue's sites entry ([] when not in sites)."""
    base = _sites_entry(g.name)
    if base is None:
        return []
    have = base.model_dump()
    want = VenueConfig.model_validate({**base.model_dump(exclude_unset=True), **g.overrides()}).model_dump()
    return sorted(k for k in g.overrides() if have.get(k) != want.get(k))


def load_gold(gold_dir: Path | None = None, only: str | None = None,
              city: str | None = None) -> list[GoldVenue]:
    """Gold venues, optionally one (`only`: slug or name) and/or one city (meta `city`, default london)."""
    gold_dir = gold_dir or GOLD_DIR
    out = []
    for d in sorted(p for p in gold_dir.iterdir() if (p / "meta.yaml").exists()):
        meta = yaml.safe_load((d / "meta.yaml").read_text())
        if only and only.casefold() not in (d.name.casefold(), meta["name"].casefold()):
            continue
        if city and (meta.get("city") or DEFAULT_CITY).casefold() != city.casefold():
            continue
        exp = yaml.safe_load((d / "expected.yaml").read_text()) or []
        out.append(GoldVenue(d.name, d, meta, exp))
    return out


def load_known_failures(gold_dir: Path | None = None) -> dict[tuple[str, str, str | None, str], dict]:
    """KNOWN_FAILURES.yaml -> {(venue, title, date, field): {"got": value, "reason": text}}.
    Each entry is one gold event (or pipeline-only "extra" event) with `got: {field: value}`."""
    p = (gold_dir or GOLD_DIR) / "KNOWN_FAILURES.yaml"
    if not p.exists():
        return {}
    out = {}
    for e in yaml.safe_load(p.read_text()) or []:
        for f, got in (e.get("got") or {}).items():
            out[(e["venue"], e["title"], str(e["date"]) if e.get("date") else None, f)] = {
                "got": got, "reason": e.get("reason", "")}
    return out


# ── offline plumbing ─────────────────────────────────────────────────────────

_SPX_PATH = re.compile(r"^/(?P<client>[^/]+)/api/v3/(?:events(?:/(?P<eid>[^/]+)/instances)?"
                       r"|instances/(?P<iid>[^/]+)/status|(?P<bulk>instances))$")


def _transport(g: GoldVenue) -> httpx.MockTransport:
    """Detail pages from gold `details` (keyed by full URL path), Spektrix API from
    `spektrix_fixtures`; anything else 404."""
    details = {k.rstrip("/"): g.dir / v for k, v in (g.meta.get("details") or {}).items()}
    spx = g.meta.get("spektrix_fixtures")
    spx_dir = (GOLD_DIR.parents[1] / spx) if spx else None

    def handle(req: httpx.Request) -> httpx.Response:
        f = details.get(req.url.path.rstrip("/"))
        if f is not None and f.exists():
            return httpx.Response(200, text=f.read_text(), headers={"content-type": "text/html"})
        m = _SPX_PATH.match(req.url.path)
        if spx_dir is not None and m:
            c = m["client"]
            f = (spx_dir / f"{c}_bulk.json" if m["bulk"] else
                 spx_dir / f"{c}_instances_{m['eid']}.json" if m["eid"] else
                 spx_dir / f"{c}_status_{m['iid']}.json" if m["iid"] else spx_dir / f"{c}_events.json")
            if f.exists():
                return httpx.Response(200, content=f.read_bytes(), headers={"content-type": "application/json"})
        return httpx.Response(404, text="<html>not in gold</html>", headers={"content-type": "text/html"})
    return httpx.MockTransport(handle)


@contextlib.contextmanager
def _patched(obj: Any, **attrs: Any) -> Iterator[None]:
    """Temporarily set module attributes (missing ones are created, then removed)."""
    missing = object()
    old = {k: getattr(obj, k, missing) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is missing:
                delattr(obj, k)
            else:
                setattr(obj, k, v)


def reply_hash(site: dict) -> str:
    """The engine's cache key for one site dict (page text + prompt/logic versions + header)."""
    from .. import extract as ex
    return ex._text_hash(site["text"], site)


@contextlib.contextmanager
def offline(replies: dict[str, dict] | None, stale: list[str] | None = None) -> Iterator[None]:
    """No page cache for follow_links; with `replies` (venue name -> {text_hash, records}) the
    LLM parse cache answers from the gold reply and the live engine is unreachable. A reply
    whose text_hash differs from the current engine input is appended to `stale`."""
    from .. import extract as ex
    from ..sources import follow_links as FL

    def recorded(site, force_parse=False):
        rep = (replies or {}).get(site["name"])
        if rep is None:
            return [], "gold-missing"
        if rep.get("text_hash") != reply_hash(site) and stale is not None:
            stale.append(site["name"])
        return list(rep.get("records") or []), "gold"

    async def no_network(*a, **k):
        raise RuntimeError("eval: live LLM call attempted in recorded mode")

    from .. import fetch as fetch_pkg
    from ..fetch import browser as fetch_browser_mod

    async def no_render(url, **k):        # enrich on a browser-mode venue: never a live render
        return FetchResult(FetchStatus.ERROR, url=url, note="eval: no browser")

    class NoThrottle:                     # enrich's per-host gap: no sleeping in eval
        async def wait(self, url, gap):
            return None

    with contextlib.ExitStack() as st:
        st.enter_context(_patched(FL, _cache_get=lambda url, force: None, _cache_put=lambda *a, **k: None))
        st.enter_context(_patched(fetch_browser_mod, fetch_browser=no_render))
        st.enter_context(_patched(fetch_pkg, HostThrottle=NoThrottle))
        if replies is not None:
            st.enter_context(_patched(ex, cached_records=recorded, extract_raw_batch_async=no_network))
        yield


def fetch_result(g: GoldVenue, cfg: VenueConfig) -> FetchResult:
    return FetchResult(FetchStatus.OK, body=g.page(), url=cfg.url, content_type="text/html")


async def _venue_events(g: GoldVenue, today: date, llm: str) -> tuple[list[Event], list[str], list[str]]:
    """parse_venue exactly as the crawl runs it, on the gold page with the gold `today`."""
    from ..sources.html_sites import WINDOW_DAYS, parse_venue
    cfg = g.cfg()
    if llm == "skip":
        cfg = cfg.model_copy(update={"extract": [k for k in cfg.extract if k != "llm"]})
    async with httpx.AsyncClient(transport=_transport(g)) as http:
        ctx = SourceCtx(http=http, today=today, window_days=WINDOW_DAYS, city=get_city(g.meta.get("city")))
        events, stats = await parse_venue(cfg, fetch_result(g, cfg), ctx)
    won = (stats.get("source") or "").split("+") if stats.get("source") else []
    return events, won, list(stats.get("errors") or [])


# ── scoring ──────────────────────────────────────────────────────────────────

def _ev_time(ev: Event) -> str | None:
    return ev.time or None


def _match(expected: list[dict], got: list[Event]) -> tuple[list[tuple[int, int]], list[int], list[int], set[int]]:
    """One-to-one (expected idx, got idx), title ≥ TITLE_MIN in three passes: same date and
    time, same date, then nearest date within DATE_GAP_MAX days (scored as a date error and
    counted in found_wrong_date). Returns (pairs, missed, extra, wrong-date expected idx)."""
    pairs: list[tuple[int, int]] = []
    used_e: set[int] = set()
    used_g: set[int] = set()
    wrong_date: set[int] = set()
    for pass_ in ("date_time", "date", "near"):
        for i, e in enumerate(expected):
            if i in used_e:
                continue
            ed, et = _norm_date(e.get("date")), _norm_time(e.get("time"))
            best, best_key = None, None
            for j, ev in enumerate(got):
                if j in used_g:
                    continue
                r = title_ratio(e["title"], ev.title)
                if r < TITLE_MIN:
                    continue
                gap = abs((ev.date - date.fromisoformat(ed)).days) if ed else 0
                if pass_ in ("date_time", "date") and gap:
                    continue
                if pass_ == "date_time" and _ev_time(ev) != et:
                    continue
                if pass_ == "near" and gap > DATE_GAP_MAX:
                    continue
                key = (r, -gap)
                if best_key is None or key > best_key:
                    best, best_key = j, key
            if best is not None:
                pairs.append((i, best))
                used_e.add(i)
                used_g.add(best)
                if pass_ == "near":
                    wrong_date.add(i)
    return (sorted(pairs), [i for i in range(len(expected)) if i not in used_e],
            [j for j in range(len(got)) if j not in used_g], wrong_date)


def _when(e: dict) -> str | None:
    """Gold date, with the gold time when set ("2026-12-19T10:00"): two sessions of one title
    on one day stay distinct in mismatch keys and baseline ids."""
    d, t = _norm_date(e.get("date")), _norm_time(e.get("time"))
    return f"{d}T{t}" if d and t else d


def _event_id(slug: str, e: dict) -> str:
    return f"{slug}|{e['title']}|{_when(e)}"


def score_venue(rep: EvalReport, g: GoldVenue, got: list[Event], won: list[str], errors: list[str]) -> None:
    pairs, missed, extra, wrong_date = _match(g.expected, got)
    src = g.source
    per_src = rep.by_source.setdefault(src, {f: Counts() for f in FIELDS})
    per_ven = rep.by_venue.setdefault(g.slug, {f: Counts() for f in FIELDS})
    for i, j in pairs:
        e, ev = g.expected[i], got[j]
        exp, have = expected_fields(e), event_fields(ev)
        wrong = []
        for f in FIELDS:
            ok = rep.fields[f].add(exp[f], have[f])
            per_src[f].add(exp[f], have[f])
            per_ven[f].add(exp[f], have[f])
            if not ok:
                wrong.append(f)
                rep.mismatches.append(Mismatch(g.slug, e["title"], _when(e), f, exp[f], have[f],
                                               e.get("evidence", "")))
        rep.events[_event_id(g.slug, e)] = {"found": True, "wrong": wrong}
    for i in missed:
        e = g.expected[i]
        rep.events[_event_id(g.slug, e)] = {"found": False, "wrong": list(FIELDS)}
        rep.mismatches.append(Mismatch(g.slug, e["title"], _when(e), "missing",
                                       _norm_date(e.get("date")), None, e.get("evidence", "")))
    for j in extra:
        ev = got[j]
        when = f"{ev.date.isoformat()}T{ev.time}" if ev.time else ev.date.isoformat()
        rep.extras.append(f"{g.slug}|{ev.title}|{when}")
        rep.mismatches.append(Mismatch(g.slug, ev.title, when, "extra", None, ev.date.isoformat(), ""))
    rep.got[g.slug] = got
    rep.venues[g.slug] = {"name": g.name, "source": src, "won": "+".join(won) or None,
                          "expected": len(g.expected), "found": len(pairs),
                          "found_wrong_date": len(wrong_date), "missed": len(missed),
                          "extra": len(extra), "errors": errors}


def run_gold(today: date | None = None, only: str | None = None, llm: str = "recorded",
             gold_dir: Path | None = None, city: str | None = None) -> EvalReport:
    """Score the pipeline on the gold set. `today` overrides every venue's capture date (default:
    each meta's `today`); `llm` = "recorded" (offline replay) | "live" (real backend) | "skip".
    Raises StaleReply when a recorded reply's text_hash no longer matches its page."""
    load_builtin_sources()
    gold = load_gold(gold_dir, only, city)
    replies = None
    if llm == "recorded":
        replies = {g.name: r for g in gold if (r := g.llm_reply()) is not None}
    stale: list[str] = []

    async def go():
        return await asyncio.gather(*(_venue_events(g, today or g.today, llm) for g in gold))

    with offline(replies, stale):
        results = asyncio.run(go())
    if stale:
        raise StaleReply(f"llm reply stale, re-record (scripts/eval.py --record-llm SLUG --force): "
                         f"{', '.join(sorted(set(stale)))}")
    rep = EvalReport()
    for g, (got, won, errors) in zip(gold, results):
        score_venue(rep, g, got, won, errors)
    return rep
