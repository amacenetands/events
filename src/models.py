from pydantic import BaseModel, field_validator, model_validator
from datetime import date
from typing import Optional, Literal
import hashlib

Category = Literal["concert", "theatre", "exhibition", "talk", "workshop", "tour", "film", "comedy", "festival", "other"]
Kind = Literal["one_off", "run", "recurring"]
Audience = Literal["kids", "family", "adult", "all"]

CATEGORIES = ("concert", "theatre", "exhibition", "talk", "workshop", "tour", "film", "comedy", "festival", "other")
_CAT_ALIASES = {
    "lecture": "talk", "talks": "talk", "gallery": "exhibition", "exhibitions": "exhibition",
    "music": "concert", "gig": "concert", "concerts": "concert", "performance": "theatre",
    "play": "theatre", "dance": "theatre", "cinema": "film", "screening": "film",
    "workshops": "workshop", "class": "workshop", "tours": "tour", "festivals": "festival",
    "stand-up": "comedy", "standup": "comedy",
}


def norm_category(v) -> str:
    c = str(v or "other").strip().lower()
    c = _CAT_ALIASES.get(c, c)
    return c if c in CATEGORIES else "other"


def clean_text(v):
    """html.unescape (repeatedly: &amp;#038;) + strip tags; <br> -> ' / '."""
    import re, html
    if not isinstance(v, str):
        return v
    if "&" not in v and "<" not in v:
        return v
    for _ in range(3):
        u = html.unescape(v)
        if u == v:
            break
        v = u
    v = re.sub(r"\s*<\s*br\s*/?\s*>\s*", " / ", v, flags=re.I)
    v = re.sub(r"</?[a-zA-Z][^<>]*>", "", v)
    return re.sub(r"[ \t]+", " ", v).strip()


def parse_time(v):
    import re
    if not v:
        return None
    v = str(v).strip().lower()

    # named times
    if v in ("noon", "midday"):
        return "12:00"
    if v == "midnight":
        return "00:00"

    # extract HH[:.]MM and optional am/pm
    # handles: "7pm", "7:30pm", "7.30pm", "7 pm", "7:30 pm", "19:30", "19.30"
    m = re.match(r"(\d{1,2})(?:[:\.](\d{2}))?\s*(am|pm)?", v)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2)) if m.group(2) else 0
    period = m.group(3) or ""

    if period == "pm" and h != 12:
        h += 12
    elif period == "am" and h == 12:
        h = 0
    elif not period and h > 23:
        return None

    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


class Event(BaseModel):
    title: str
    venue: str
    date: date
    end_date: Optional[date] = None
    time: Optional[str] = None  # "19:30"
    category: Category = "other"
    kind: Kind = "one_off"          # one_off | run (start+end) | recurring (repeating series)
    audience: Audience = "all"      # kids | family | adult | all
    min_age: Optional[int] = None
    recurrence: Optional[str] = None  # "Saturdays", "daily 11:00"
    date_undetermined: bool = False  # date/range couldn't be pinned — show in TBC section
    url: str
    description: Optional[str] = None
    price: Optional[str] = None
    source: str = ""
    public: bool = True    # False: schools/members/trade/private-hire only (set by classify)
    online: bool = False   # True: webinar / livestream only (set by classify)
    # v3 fields — defaulted so stored event_json rows without them still load
    byday: Optional[list[int]] = None   # run/recurring: session weekdays, 0=Mon..6=Sun
    sessions_known: bool = True         # False: multi-day range with no weekday/session info
    start_known: bool = True            # False: "on now until X"; `date` = earliest known/assumed day
    sold_out: bool = False              # every listed session sold out
    raw_date: Optional[str] = None      # date text as found on the page (debug/eval)

    # Derived, read-only compat views (old code/cached JSON used these as fields)
    @property
    def is_one_off(self) -> bool:
        # a recurring series expanded to dated instances (no end_date) acts as one-off downstream
        return self.kind == "one_off" or (self.kind == "recurring" and self.end_date is None)

    @property
    def is_kids(self) -> bool:
        return self.audience == "kids"

    @property
    def is_family(self) -> bool:
        return self.audience in ("kids", "family")

    @model_validator(mode="before")
    @classmethod
    def _compat(cls, d):
        if not isinstance(d, dict):
            return d
        d = dict(d)
        legacy_cat = str(d.get("category") or "").lower()
        is_one_off = d.pop("is_one_off", None)
        is_kids = d.pop("is_kids", None)
        is_family = d.pop("is_family", None)
        if not d.get("kind"):
            if is_one_off is False:
                d["kind"] = "run"
            else:
                d["kind"] = "one_off"
        elif d["kind"] not in ("one_off", "run", "recurring"):
            d["kind"] = "run" if is_one_off is False else "one_off"
        if d.get("audience") not in ("kids", "family", "adult", "all"):
            if is_kids or legacy_cat == "kids":
                d["audience"] = "kids"
            elif is_family:
                d["audience"] = "family"
            else:
                d["audience"] = "all"
        if legacy_cat == "kids":
            d["category"] = "other"
        else:
            d["category"] = norm_category(d.get("category"))
        ma = d.get("min_age")
        if ma is not None:
            try:
                d["min_age"] = int(ma)
            except (TypeError, ValueError):
                d["min_age"] = None
        return d

    @field_validator("byday", mode="before")
    @classmethod
    def _byday(cls, v):
        if v is None:
            return None
        days = sorted({int(x) for x in v})
        if any(d < 0 or d > 6 for d in days):
            raise ValueError(f"byday out of range 0..6: {v!r}")
        return days or None

    @field_validator("title", "venue", "description", mode="before")
    @classmethod
    def _clean(cls, v):
        return clean_text(v)

    @field_validator("date", mode="before")
    @classmethod
    def parse_date(cls, v):
        if isinstance(v, str):
            from datetime import datetime
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %B %Y", "%d %b %Y"):
                try:
                    return datetime.strptime(v, fmt).date()
                except ValueError:
                    continue
        return v

    @field_validator("time", mode="before")
    @classmethod
    def normalise_time(cls, v):
        return parse_time(v)

    def fingerprint(self) -> str:
        raw = f"{self.title.lower().strip()}|{self.venue.lower().strip()}|{self.date.isoformat()}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def event_key(self) -> str:
        """Within-run dedup + events-table PK. Unlike fingerprint(), distinguishes
        matinee/evening on the same day. fingerprint() stays time-free for seen-history."""
        raw = (f"{self.title.lower().strip()}|{self.venue.lower().strip()}|"
               f"{self.date.isoformat()}|{self.time or ''}")
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


class RawEvent(BaseModel):
    """One event as found on the page — strings only, no parsing (normalize/ does that)."""
    title: str
    date_text: Optional[str] = None        # date or start of range, verbatim ("Sat 22 Nov", "Until 4 Jan")
    end_text: Optional[str] = None         # end of range, verbatim, when given separately
    time_text: Optional[str] = None        # time(s), verbatim ("7.30pm", "Tue-Sat 7pm, Sat 2.30pm")
    price_text: Optional[str] = None       # price, verbatim ("£15 / £10 conc", "Free")
    recurrence_text: Optional[str] = None  # repeat rule, verbatim ("Every Saturday", "Weekdays")
    url: Optional[str] = None              # event/detail link (may be relative)
    desc: Optional[str] = None             # short description / subtitle
    venue: Optional[str] = None            # venue when the page lists several (aggregators)
    source_kind: Optional[str] = None      # producer: "jsonld", "adapter:cards", "llm", "spektrix", ...
    sold_out: Optional[bool] = None        # True when the source says so (Spektrix status); None = unknown
