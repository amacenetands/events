"""Deterministic post-pass over ALL events (LLM, JSON-LD, adapter-direct).

Single choke point: main.run_pipeline calls classify(scraped) right after the
sources are gathered. Fixes audience/kind/time/category that the LLM (or the
hard-coded JSON-LD path) got wrong or left blank. Never drops events.
"""
from __future__ import annotations
import re
from collections import defaultdict
from .models import Event, norm_category, parse_time

# --- audience ---------------------------------------------------------------
_ADULT_STRONG = re.compile(r"(?<![\d])(?:18|21)\s?\+|\b(?:over[- ]18s?|adults?[- ]only|strictly adults)\b", re.I)
_ADULT_WEAK = re.compile(r"\b(lates?|wine|cocktails?|after[- ]hours)\b", re.I)
_KIDS_T = re.compile(
    r"\b(?:toddlers?|babies|tots|storytimes?|story[- ]time|nursery|pre-?school|little ones|rhyme[- ]?time|"
    r"under[- ]?\d+'?s|children['’]s|kids['’]|kids club|for kids|for children|"
    r"(?:baby|babies|toddlers?)\s+(?:and\s+toddler\s+)?(?:class(?:es)?|sensory|rhymes?|music|sessions?|club|time|group|yoga|disco|signing)|"
    r"(?:class(?:es)?|sensory|rhymes?|music|sessions?|club)\s+for\s+(?:babies|toddlers)|"
    r"ages?\s*\d+|age\s*\d+\s*\+|\d+\s*\+\s*yrs?|for \d+\s?(?:-|–|to)\s?\d+s?)(?!\w)", re.I)
_PRIVATE = re.compile(
    r"\b(?:gcse|a[- ]levels?|ks[1-5]|key stage [1-5]|cpd|educators?|members['’]? only|private hire|corporate|"
    r"members['’]? (?:(?:closing|opening|early|exclusive|private|morning|evening|late)\s+)*(?:weekend\s+)?(?:views?|viewings?|previews?|events?|evenings?|mornings?|hours?|tours?)|"
    r"disabled students['’]? allowance|student finance|prospective students|(?:for|to) (?:current )?students only|applicants?['’]? (?:day|event|session)|"
    r"school (?:groups?|parties|trips?|visits?|bookings?)|schools?\s*(?:&|and)\s*colleges?|(?:for|to) schools?|schools? only|"
    r"colleges? groups?|(?:for|to) teachers?|teachers['’]? (?:preview|session|workshop|event|evening|training|resources?|pack|only)|"
    r"teachers only|trade (?:only|event|show|visit|visitors?|day|professionals?)|(?:for|to) the trade)\b", re.I)
_ONLINE = re.compile(r"\b(?:webinar|online[- ]only|online event|lecture recordings?|recorded (?:taster )?lectures?|recording available|on[- ]demand|live[- ]?stream(?:ed)? only|virtual(?!\s+reality)|zoom)\b|^\s*online\b|\(online\)", re.I)
_ONLINE_T = re.compile(r"\b(?:live-?stream(?:ed)?|online|virtual(?!\s+reality)|webinar|zoom)\b", re.I)
# venue that is a list of towns (touring show / exhibition elsewhere), e.g. "Cheltenham, Petworth, Rugby"
_TOWN_LIST = re.compile(r"^[A-Z][a-z]+(?: [A-Z][a-z]+)?(?:, [A-Z][a-z]+(?: [A-Z][a-z]+)?){2,}$")


def is_elsewhere(e) -> bool:
    """Event listed at non-London touring venues rather than the source venue."""
    v = (e.venue or "").strip()
    return bool(_TOWN_LIST.match(v)) and "london" not in v.lower() and v.lower() != (e.source or "").lower()


_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b", re.I)
_IN_PERSON = re.compile(r"\b(?:hybrid|in[- ]person|on[- ]?site|in the (?:gallery|museum|theatre|hall))\b", re.I)
_FAMILY_T = re.compile(r"\b(\(family\)|family|families|half[- ]term)\b", re.I)
_KIDS_D = re.compile(
    r"\b(for (?:kids|children|toddlers|babies|ages?\s*\d)|ages?\s*\d+|aged \d+|under[- ]?\d+'?s|toddlers?|storytime)\b", re.I)
_FAMILY_D = re.compile(r"\b(family[- ](?:friendly|workshop|activity|activities|event|show|fun|day|session)|for (?:the )?(?:whole )?famil(?:y|ies))\b", re.I)
_AGE_RANGE = re.compile(r"\bages?\s*(\d{1,2})\s*(?:-|–|to|\+)?", re.I)
_AGE_PLUS = re.compile(r"\b(\d{1,2})\s?\+(?!\d)")
_AGED = re.compile(r"\b(?:aged|from age|from ages?)\s*(\d{1,2})\b", re.I)

# Venues whose whole programme is for children (matched on lower-case source/venue)
FAMILY_VENUES = ("polka", "unicorn", "little angel", "discover story", "primary times", "puppet", "horniman kids")

# --- kind -------------------------------------------------------------------
_DAYS = r"(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?s?"
_RECUR_D = re.compile(rf"\bevery\s+{_DAYS}\b", re.I)   # description: strong pattern only
_OPEN_BEFORE = re.compile(r"\b(?:open|opens|opening|closed)\b[^.;]{0,25}$", re.I)
_RECUR = re.compile(rf"\b(every\s+(?:{_DAYS}|day|week|weekend|morning|afternoon|evening|month)|daily|weekly|each\s+{_DAYS}|{_DAYS}\s+(?:and|&)\s+{_DAYS}\s+(?:weekly|each week)|select\s+{_DAYS})\b", re.I)

# --- time -------------------------------------------------------------------
_TIME_COLON = re.compile(r"(?<![\d£$€.,:])(\d{1,2}:[0-5]\d)(?:\s?(am|pm))?(?![\d:])", re.I)
_TIME_AMPM = re.compile(r"(?<![\d£$€.,:])(\d{1,2}(?:[.:][0-5]\d)?)\s?(am|pm)\b", re.I)
_TIME_CUE = re.compile(r"\b(?:at|from|doors|starts?|begins?|@)\s+(\d{1,2}\.[0-5]\d)(?![\d.])", re.I)

# --- category (only refines "other") ---------------------------------------
_CAT_KW = [
    ("workshop", re.compile(r"\bworkshops?|masterclass|\bclass(es)?\b", re.I)),
    ("tour", re.compile(r"\btours?\b", re.I)),
    ("talk", re.compile(r"\b(talks?|lectures?|in conversation|panel|q&a)\b", re.I)),
    ("film", re.compile(r"\b(screening|film|cinema)\b", re.I)),
    ("comedy", re.compile(r"\b(comedy|stand-?up)\b", re.I)),
    ("concert", re.compile(r"\b(concert|recital|orchestra|quartet|live music|gig)\b", re.I)),
    ("exhibition", re.compile(r"\bexhibition\b", re.I)),
    ("festival", re.compile(r"\bfestival\b", re.I)),
    ("theatre", re.compile(r"\b(theatre|play|musical|puppet)\b", re.I)),
]

_hints: dict[str, str] | None = None


def _site_hints() -> dict[str, str]:
    global _hints
    if _hints is None:
        _hints = {}
        try:
            from . import config
            for v in config.all_venues():
                if v.category_hint:
                    _hints[v.name.lower()] = v.category_hint
        except Exception:
            pass
    return _hints


_RANGE_AGE = re.compile(r"\b(?:ages?|aged|years?|yrs?|under|over)?\s*(\d{1,2})\s?(?:-|–|to)\s?(\d{1,2})\s?(?:s\b|\+|years?|yrs?|year[- ]olds?)", re.I)
_AGE_CTX = re.compile(r"\b(?:ages?|aged|from age)\s*(\d{1,2})\s?(?:(?:-|–|to)\s?(\d{1,2}))?\s?(\+)?", re.I)
_YOUNG_ADULT = re.compile(r"\b(?:1[6-9]|2\d)\s?(?:-|–|to)\s?(?:2\d|3\d)\s?s?\b(?!\s*(?:nov|dec|jan|feb|mar|apr|may|jun|jul|aug|sep|oct)\w*)", re.I)


def _age_span(text: str) -> tuple[int, int | None] | None:
    """(min_age, max_age|None) from age wording; real values, no 18 cutoff."""
    m = _AGE_CTX.search(text)
    if m:
        return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)
    m = _RANGE_AGE.search(text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo < hi:
            return lo, hi
    m = _AGE_PLUS.search(text)
    if m and int(m.group(1)) < 16:
        return int(m.group(1)), None
    return None


def _min_age(text: str) -> int | None:
    sp = _age_span(text)
    return sp[0] if sp else None


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


_T_EXPR = r"(\d{1,2}(?:[.:][0-5]\d)?\s?(?:am|pm)|\d{1,2}[:.][0-5]\d)(?![\d.]*\d)"
_TIME_CUED = re.compile(r"\b(?:at|from|doors(?:\s+open)?|starts?|begins?|@)\s*" + _T_EXPR, re.I)


def _time_from(title: str, desc: str) -> str | None:
    """Explicit time in the title, else a cued time (at/from/doors/starts) in the description."""
    for rx in (_TIME_AMPM, _TIME_COLON):
        m = rx.search(title)
        if m:
            return parse_time(m.group(1) + (m.group(2) or ""))
    m = _TIME_CUE.search(title)
    if m:
        return parse_time(m.group(1))
    m = _TIME_CUED.search(desc)
    if m:
        return parse_time(m.group(1))
    return None


def _classify_one(e: Event, hints: dict[str, str]) -> dict:
    upd: dict = {}
    title, desc = e.title or "", e.description or ""
    text = f"{title} {desc}"

    # audience
    aud = e.audience
    span = _age_span(text)
    lo, hi = span if span else (None, None)
    adult_age = lo is not None and 16 <= lo <= 99
    teen_age = lo is not None and 12 <= lo < 16 and (hi is None or hi >= 16)
    explicit_family = bool(_FAMILY_T.search(title) or _FAMILY_D.search(desc))
    kt = _KIDS_T.search(title) or _KIDS_D.search(desc)
    if kt and re.match(r"(?:ages?|aged|\d)", kt.group(0), re.I) and (adult_age or teen_age):
        kt = None      # age wording that isn't for children
    if _ADULT_STRONG.search(text):
        aud = "adult"
    elif adult_age and not explicit_family:
        aud = "adult"
    elif kt:
        aud = "kids"
    elif _FAMILY_T.search(title) or _FAMILY_D.search(desc):
        if aud not in ("kids",):
            aud = "family"
    elif _ADULT_WEAK.search(title) and aud == "all":
        aud = "adult"
    if teen_age and aud == "kids" and not explicit_family:
        aud = "all"
    if aud == "all":   # venue default: upgrade only, never override explicit adult
        hint = hints.get((e.source or "").lower()) or hints.get((e.venue or "").lower())
        low = f"{e.source} {e.venue}".lower()
        if hint == "kids" or any(v in low for v in FAMILY_VENUES):
            aud = "family"
    if aud != e.audience:
        upd["audience"] = aud
    if aud in ("kids", "family", "adult") and e.min_age is None and lo is not None and (aud != "adult" or adult_age):
        upd["min_age"] = lo
    elif aud == "adult" and e.min_age is None and re.search(r"\b18\s?\+|over[- ]18", text, re.I):
        upd["min_age"] = 18

    # kind
    kind = e.kind
    if kind == "run" and e.end_date and e.end_date <= e.date:
        kind = "one_off"
        upd["end_date"] = None
    if kind == "one_off":
        m = _RECUR.search(title)
        if not m:
            m = _RECUR_D.search(desc)
            if m and _OPEN_BEFORE.search(desc[:m.start()]):
                m = None
        if m:
            kind = "recurring"
            upd["recurrence"] = e.recurrence or m.group(0).lower()
    if kind != e.kind:
        upd["kind"] = kind

    # time
    if e.time is None and kind != "run":
        t = _time_from(title, desc)
        if t:
            upd["time"] = t

    # visibility
    if e.public and _PRIVATE.search(text):
        upd["public"] = False
    if not e.online and (_ONLINE.search(text) or _ONLINE_T.search(title)) and not _IN_PERSON.search(text):
        upd["online"] = True
    if _POSTCODE.search(e.venue or "") or len(e.venue or "") > 60:
        upd["venue"] = e.source or e.venue[:60]

    # category
    cat = norm_category(e.category)
    if cat == "other":
        for c, rx in _CAT_KW:
            if rx.search(title):
                cat = c
                break
    if cat != e.category:
        upd["category"] = cat
    return upd


def classify(events: list[Event]) -> list[Event]:
    hints = _site_hints()
    out: list[Event] = []
    for e in events:
        try:
            upd = _classify_one(e, hints)
            out.append(e.model_copy(update=upd) if upd else e)
        except Exception:
            out.append(e)
    # same title+venue on >=3 distinct dates in this run -> recurring series
    groups: dict[tuple, set] = defaultdict(set)
    for e in out:
        if e.kind == "one_off":
            groups[(_norm_title(e.title), (e.venue or "").lower())].add(e.date)
    multi = {k for k, ds in groups.items() if len(ds) >= 3}
    if multi:
        out = [
            e.model_copy(update={"kind": "recurring", "recurrence": e.recurrence or "multiple dates"})
            if e.kind == "one_off" and (_norm_title(e.title), (e.venue or "").lower()) in multi else e
            for e in out
        ]
    return out
