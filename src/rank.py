"""Query-aware ranking v2. Ranking ORDERS and highlights; it never hides events.

score()          heuristic score (components weighted by constants below)
pick_diverse()   top-N with venue/category caps (rest stay in normal sections)
llm_rerank()     optional deepseek rerank of the top candidates, heuristic fallback
rank_events()    entry point -> RankResult(picks, reasons, scores)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from .llm import run_llm
from .models import Event

# ── weights (documented knobs) ───────────────────────────────────────────────
W_AUD_FIT = 3.0            # kids/family event when filter wants family/kids
W_AUD_ALL = 1.0            # audience "all" when filter wants family/kids
W_AUD_ADULT = -10.0        # adult-only when filter wants family/kids
W_AGE_TOO_OLD = -5.0       # min_age above filter's min_age_max
W_VENUE_STEP = 2.0         # per venue-weight step above 1 (weight 1..3 -> 0,+2,+4)
W_NEW_RECENT = 2.0         # first_seen within NEW_DAYS
W_NEW_TITLE = 1.0          # title never seen in older crawls
NEW_DAYS = 7
W_CLOSING = 3.0            # run closing within CLOSING_DAYS ("last chance")
CLOSING_DAYS = 7
W_STALE = -2.0             # open-ended run that started > STALE_DAYS ago
STALE_DAYS = 60
W_SOON = 1.0               # one-off within 48h
W_HAS_TIME = 1.0
W_UNDETERMINED = -5.0
W_ONE_OFF = 2.0
W_RUN = 0.5
W_THEATRE_RECURRING = 0.5
W_AVOID = -3.0             # title/description hits a profile `avoid` phrase
W_PRICEY = -4.0            # ticket/course price >= PRICEY_GBP
PRICEY_GBP = 100
W_INTEREST = 1.0           # title hits a profile `interests` phrase
W_CHECK_DAYS = -2.5        # run > CHECK_DAYS_SPAN days with unknown session days (range-only listing)
CHECK_DAYS_SPAN = 7
W_SOLD_OUT = -20.0         # every session sold out (also excluded from candidates/digest)
TOP_N = 12
MAX_PER_VENUE = 2
MAX_PER_CATEGORY = 4
LLM_POOL = 40

PROFILE_YAML = Path(__file__).parent.parent / "profile.yaml"


@dataclass
class RankCtx:
    today: date = field(default_factory=date.today)
    now: datetime = field(default_factory=datetime.now)
    new_fps: set = field(default_factory=set)          # fingerprints first seen within NEW_DAYS
    seen_titles: set | None = None                     # titles from older crawls (None = unknown)
    weights: dict = field(default_factory=dict)        # venue/source name (lower) -> 1..3
    profile: dict = field(default_factory=dict)


@dataclass
class RankResult:
    picks: list[Event]
    reasons: dict[str, str]        # fingerprint -> short reason
    scores: dict[str, float]       # fingerprint -> score (all candidates)
    llm_used: bool = False


def _tkey(e: Event) -> str:
    return re.sub(r"\W+", " ", e.title.lower()).strip()


def load_weights(path: Path | None = None) -> dict[str, int]:
    """Lowercased venue name → weight (only non-default weights; rank treats missing as 1).
    Every entry of the city sites files (or of `path`), skip/disabled included."""
    from . import config
    try:
        venues = config.venues_from_file(path) if path is not None else config.all_venues()
    except Exception:
        return {}
    return {v.name.lower(): v.weight for v in venues if v.weight != 1}


def load_profile(path: Path = PROFILE_YAML) -> dict:
    try:
        return yaml.safe_load(open(path)) or {}
    except Exception:
        return {}


def build_ctx(first_seen: dict[str, str] | None, events: list[Event], today: date | None = None,
              profile: dict | None = None, weights: dict | None = None,
              now: datetime | None = None) -> RankCtx:
    """first_seen: fingerprint -> iso ts (events table). Novelty only if an older crawl exists.
    today/now should be in the city's timezone (see query.city_today/city_now)."""
    today = today or (now.date() if now else date.today())
    ctx = RankCtx(today=today, now=now or datetime.combine(today, datetime.now().time()),
                  weights=load_weights() if weights is None else weights,
                  profile=load_profile() if profile is None else profile)
    if first_seen:
        cut = (datetime.now(timezone.utc) - timedelta(days=NEW_DAYS)).isoformat()
        if min(first_seen.values()) < cut:
            ctx.new_fps = {fp for fp, ts in first_seen.items() if ts >= cut}
            ctx.seen_titles = {_tkey(e) for e in events if e.fingerprint() not in ctx.new_fps}
    return ctx


def _event_dt(e: Event) -> datetime:
    h, m = 0, 0
    if e.time:
        try:
            h, m = (int(x) for x in e.time.split(":")[:2])
        except ValueError:
            pass
    return datetime.combine(e.date, datetime.min.time()).replace(hour=h, minute=m)


def score_parts(e: Event, f, ctx: RankCtx) -> dict[str, float]:
    p: dict[str, float] = {}
    aud = getattr(f, "audience", "any")
    if aud in ("family", "kids"):
        if e.audience in ("kids", "family"):
            p["audience"] = W_AUD_FIT
        elif e.audience == "all":
            p["audience"] = W_AUD_ALL
        elif e.audience == "adult":
            p["audience"] = W_AUD_ADULT
    mx = getattr(f, "min_age_max", None)
    if mx is not None and e.min_age is not None and e.min_age > mx:
        p["age"] = W_AGE_TOO_OLD
    w = ctx.weights.get(e.source.lower()) or ctx.weights.get(e.venue.lower()) or 1
    if w > 1:
        p["venue"] = W_VENUE_STEP * (w - 1)
    if e.fingerprint() in ctx.new_fps:
        p["new"] = W_NEW_RECENT
    if ctx.seen_titles is not None and _tkey(e) not in ctx.seen_titles:
        p["new title"] = W_NEW_TITLE
    end = e.end_date if (e.kind == "run" or (e.kind == "recurring" and e.end_date)) else None
    if end is not None and e.date <= ctx.today and 0 <= (end - ctx.today).days <= CLOSING_DAYS:
        p["closing"] = W_CLOSING
    elif e.kind in ("run", "recurring") and e.end_date is None and (ctx.today - e.date).days > STALE_DAYS:
        p["stale"] = W_STALE
    if e.kind == "one_off" and timedelta(0) <= _event_dt(e) - ctx.now <= timedelta(hours=48):
        p["soon"] = W_SOON
    elif e.kind == "one_off" and e.date == ctx.today:
        p["soon"] = W_SOON
    pm = re.search(r"£\s*(\d[\d,]*)", e.price or "")
    if pm and int(pm.group(1).replace(",", "")) >= PRICEY_GBP:
        p["pricey"] = W_PRICEY
    if e.time:
        p["timed"] = W_HAS_TIME
    if e.date_undetermined:
        p["undetermined"] = W_UNDETERMINED
    if (e.kind != "one_off" and not e.byday and not e.sessions_known
            and (e.end_date is None or (e.end_date - e.date).days > CHECK_DAYS_SPAN)):
        p["check days"] = W_CHECK_DAYS
    if e.sold_out:
        p["sold out"] = W_SOLD_OUT
    if e.kind == "one_off":
        p["one-off"] = W_ONE_OFF
    elif e.kind == "run":
        p["run"] = W_RUN
    elif e.kind == "recurring" and e.category == "theatre":
        p["theatre run"] = W_THEATRE_RECURRING
    prof = ctx.profile or {}
    cw = (prof.get("category_weights") or {}).get(e.category)
    if cw:
        p["category pref"] = float(cw)
    text = f"{e.title} {e.description or ''}".lower()
    if any(a.lower() in text for a in prof.get("avoid") or []):
        p["avoid"] = W_AVOID
    if any(i.lower() in text for i in prof.get("interests") or []):
        p["interest"] = W_INTEREST
    return p


def score(e: Event, f, ctx: RankCtx) -> float:
    return sum(score_parts(e, f, ctx).values())


_REASON_LABEL = {"venue": "key venue", "new": "new this week", "new title": "first time listed",
                 "closing": "last chance", "soon": "coming up soon", "audience": "family fit",
                 "interest": "matches your interests", "category pref": "preferred category"}


def reason(e: Event, f, ctx: RankCtx) -> str:
    parts = score_parts(e, f, ctx)
    keys = [k for k, v in sorted(parts.items(), key=lambda kv: -kv[1]) if k in _REASON_LABEL and v > 0]
    return ", ".join(_REASON_LABEL[k] for k in keys[:2])


def candidates(events: list[Event]) -> list[Event]:
    """Events eligible for picks: dated one-offs and runs (recurring instances excluded; sold out excluded)."""
    return [e for e in events if not e.date_undetermined and not e.sold_out
            and (e.kind in ("one_off", "run") or (e.kind == "recurring" and e.end_date))]


def pick_diverse(ordered: list[Event], n: int = TOP_N, max_venue: int = MAX_PER_VENUE,
                 max_cat: int = MAX_PER_CATEGORY) -> list[Event]:
    out: list[Event] = []
    vc: dict[str, int] = {}
    cc: dict[str, int] = {}
    seen_titles: set[str] = set()
    for e in ordered:
        if len(out) >= n:
            break
        if vc.get(e.venue, 0) >= max_venue or cc.get(e.category, 0) >= max_cat:
            continue
        if _tkey(e) in seen_titles:
            continue
        out.append(e)
        vc[e.venue] = vc.get(e.venue, 0) + 1
        cc[e.category] = cc.get(e.category, 0) + 1
        seen_titles.add(_tkey(e))
    return out


# ── LLM rerank ───────────────────────────────────────────────────────────────

def _profile_text(profile: dict, family: bool = True) -> str:
    bits = []
    if profile.get("interests"):
        bits.append("Likes: " + ", ".join(profile["interests"]))
    if profile.get("avoid"):
        bits.append("Avoid: " + ", ".join(profile["avoid"]))
    if family and profile.get("kids_ages"):
        bits.append("Kids ages: " + ", ".join(str(a) for a in profile["kids_ages"]))
    if profile.get("notes"):
        bits.append(str(profile["notes"]))
    return "\n".join(bits)


_TITLE_DATES = re.compile(r"\s*\((?:[^)]*\b20\d\d\b[^)]*|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\w*\s*[-–]\s*\w+)\)")
_KIND = {"one_off": "one", "run": "run", "recurring": "rec"}
_AUD = {"kids": "kids", "family": "fam", "adult": "adult", "all": "all"}


def _when(e: Event, single_day: bool, today: date | None) -> str:
    """Compact date: one-off -> 'HH:MM' (prefixed '%a%d%b' if multi-day query); run -> 'to %d%b' (+' last' if closing)."""
    if e.kind == "one_off":
        d = "" if single_day else e.date.strftime("%a%d%b")
        return f"{d} {e.time or ''}".strip() or "-"
    w = f"to {e.end_date.strftime('%d%b')}" if e.end_date else "open"
    from .digest import days_label
    long_range = e.end_date is None or (e.end_date - e.date).days > CHECK_DAYS_SPAN
    lab = (days_label(e.byday) or ("check days" if not e.sessions_known and long_range else "")
           or (e.recurrence or "")[:20])
    if lab:
        w = f"{lab} {w}"
    if today and e.end_date and 0 <= (e.end_date - today).days <= CLOSING_DAYS:
        w += " last"
    return w


def build_prompt(pool: list[Event], profile: dict, n: int, f=None, today: date | None = None) -> str:
    aud = getattr(f, "audience", "any") or "any"
    family = aud in ("family", "kids")
    single, req = False, ""
    if f is not None and today is not None:
        from .query import describe, resolve_when   # lazy: avoid import cycle
        try:
            start, end = resolve_when(f.when, today)
            single = start == end
        except Exception:
            pass
        req = describe(f, today)
    lines = [f"E{i}|{_TITLE_DATES.sub('', e.title)[:60]}|{e.venue[:30]}|{_KIND.get(e.kind, e.kind)}|{_when(e, single, today)}|"
             f"{e.category}|{_AUD.get(e.audience, e.audience)}" for i, e in enumerate(pool, 1)]
    rules = ["Prefer one-off or rare events (kind=one) over ongoing runs/recurring series; "
             "include a run only if exceptional or closing soon (last)."]
    if family:
        rules.append("Audience is family: prioritise events made for kids (aud kids/fam) suited to the kids' ages; "
                     "drop adult-oriented events.")
    rules.append("Variety: max 2 per venue.")
    prof = _profile_text(profile, family)
    return (f"Pick the {n} best events{' for: ' + req if req else ''}.\n"
            + "".join(f"{i}. {r}\n" for i, r in enumerate(rules, 1))
            + (prof + "\n" if prof else "")
            + "\nid|title|venue|kind|when|cat|aud\n" + "\n".join(lines)
            + f"\n\nReply {n} lines, best first: E<id>|reason (max 8 words). Use list E-ids. Nothing else.")


def parse_llm(text: str, pool: list[Event]) -> list[tuple[Event, str]]:
    out, seen = [], set()
    for ln in text.splitlines():
        m = re.search(r"\bE(\d+)\s*[|:.\-)]\s*(.*)", ln) or re.match(r"\s*\W*(\d+)\s*[|:.\-)]\s*(.*)", ln)
        if not m:
            continue
        i = int(m.group(1))
        if 1 <= i <= len(pool) and i not in seen:
            seen.add(i)
            out.append((pool[i - 1], m.group(2).strip()))
    return out


async def llm_rerank(ordered: list[Event], profile: dict, n: int = TOP_N,
                     pool_size: int = LLM_POOL, backend: str | None = None, f=None,
                     today: date | None = None) -> list[tuple[Event, str]]:
    """Raises on backend error / unparseable reply (caller falls back).
    backend: deepseek | anthropic (Claude API) | claude (CLI) | ollama; default $RANK_BACKEND or deepseek."""
    ones = [e for e in ordered if e.kind != "run"][: pool_size // 2]   # one-offs/recurring guaranteed seats
    have = {id(e) for e in ones}
    pool = sorted(ones + [e for e in ordered if id(e) not in have][: pool_size - len(ones)],
                  key=lambda e: ordered.index(e))
    backend = backend or os.environ.get("RANK_BACKEND", "deepseek")
    raw = await run_llm(build_prompt(pool, profile, n, f, today), backend=backend,
                        max_output_tokens=400, timeout=240 if backend == "claude" else 90, retries=2)  # CLI startup is slow
    res = parse_llm(raw, pool)
    if not res:
        raise ValueError("LLM rerank: no parseable ids")
    return res


async def rank_events(events: list[Event], f, ctx: RankCtx, *, n: int = TOP_N, use_llm: bool = False,
                      backend: str | None = None) -> RankResult:
    cands = candidates(events)
    scores = {e.fingerprint(): score(e, f, ctx) for e in cands}
    ordered = sorted(cands, key=lambda e: (-scores[e.fingerprint()], e.date, e.title))
    picks = pick_diverse(ordered, n)
    reasons = {e.fingerprint(): reason(e, f, ctx) for e in picks}
    used = False
    if use_llm and ordered:
        try:
            res = await llm_rerank(ordered, ctx.profile, n, backend=backend, f=f, today=ctx.today)
            chosen = pick_diverse([e for e, _ in res], n)
            why = {e.fingerprint(): r for e, r in res}
            if len(chosen) < n:   # top up from heuristic order
                have = {id(e) for e in chosen}
                for e in picks:
                    if len(chosen) >= n:
                        break
                    if id(e) not in have and sum(1 for c in chosen if c.venue == e.venue) < MAX_PER_VENUE:
                        chosen.append(e)
            picks, used = chosen, True
            reasons = {e.fingerprint(): why.get(e.fingerprint()) or reason(e, f, ctx) for e in picks}
        except Exception as exc:  # noqa: BLE001 - fallback by design
            print(f"[rank] LLM rerank failed ({type(exc).__name__}: {exc}); using heuristic")
    return RankResult(picks, reasons, scores, used)
