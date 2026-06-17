"""Claude CLI / Ollama extraction — concurrent async batches, configurable backend."""
import json
import re
import hashlib
import asyncio
import os
from datetime import date
from .models import Event
from .dedup import get_cached_parse, get_html_fetched_at, save_extraction
from .llm import run_llm, _CLAUDE_MODEL

BATCH_SIZE = 3
_MODEL = _CLAUDE_MODEL

# Concurrency: default 1 for Ollama (single local model — parallelism just contends),
# 3 for Claude. Override with LLM_CONCURRENCY env var.
def _default_concurrency() -> int:
    env_val = os.environ.get("LLM_CONCURRENCY")
    if env_val is not None:
        try:
            return max(1, int(env_val))
        except ValueError:
            pass
    backend = (os.environ.get("LLM_BACKEND") or os.environ.get("EXTRACTION_BACKEND", "ollama")).lower()
    return 1 if backend == "ollama" else 3

_CONCURRENCY = _default_concurrency()

_DATE_SIGNAL = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
    r"|january|february|march|april|june|july|august|september|october|november|december"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)

# Base schema — venue and url omitted; injected per-site or from known data
_EVENT_SCHEMA = """\
  title        string
  date         string   YYYY-MM-DD  — the confirmed specific date this event occurs
  end_date     string|null  — for ongoing exhibitions only
  time         string|null  HH:MM 24h — set null if unknown, never write TBC
  category     string   concert|exhibition|kids|gallery|theatre|other
  is_one_off   bool     true=single specific date; false=ongoing exhibition/show
  is_kids      bool     true if primarily FOR children under 12
  is_family    bool     true if suitable for families/adults+children together
  date_confirmed bool   true if date is a specific confirmed date found in text; false if estimated/inferred
  description  string|null  one sentence max
  price        string|null"""

# Extra fields for aggregator sites where venue/url differ per event
_VENUE_URL_FIELDS = """\
  venue        string   — actual event location (not the aggregator site name)
  url          string   — event-specific link"""

_DATE_RULES = """\
Date rules (today is {today}):
- CONFIRMED DATE REQUIRED: only emit an event if you can find a specific date (e.g. "14 Jun", "Sunday 15 June") in the text. Set date_confirmed=true.
- Extract ALL upcoming or currently-running events — do not filter by date range; include everything with a confirmed future or current date.
- Skip events whose date is clearly in the past (before today).
- [STRUCTURED EVENT] blocks at the top of the text contain machine-readable data — startDate/endDate are confirmed dates, treat them as date_confirmed=true. If startDate is in the past and endDate is in the future, treat as a currently-running ongoing event (is_one_off=false, date=today, end_date=endDate).
- Recurring/series events (e.g. "select Sundays", "every weekend", "Jun–Aug"): ONLY include if the text lists specific upcoming dates OR a [STRUCTURED EVENT] block confirms the date range. Emit one event per specific upcoming date, or if a confirmed series runs now through a future end_date, emit once as is_one_off=false. If no specific dates, SKIP entirely.
- Ongoing exhibitions: is_one_off=false, date=opening date (or today if already open and opening not stated), end_date=closing date. Set date_confirmed=true only if opening date is explicitly stated.{exhibition_hint}
- One-off events (talks, concerts, workshops, performances, previews): is_one_off=true, date_confirmed=true.
- Include ALL events regardless of time of day — daytime, evening, weekday, weekend.
- time=null if no time found in text. Never write TBC, unknown, or similar.
- Return [] if no qualifying events."""

_BATCH_PROMPT = """\
Extract upcoming events from MULTIPLE venue websites. Return ONLY a single JSON object keyed by site name, no prose.

Format:
{{
  "Site Name 1": [ {{event}}, ... ],
  "Site Name 2": [ {{event}}, ... ]
}}

Each event:
{schema}

{date_rules}

{sites}"""


def _date_trim(text: str, max_chars: int = 6000) -> str:
    lines = text.splitlines()
    kept = [l for l in lines if l.startswith("[STRUCTURED EVENT]") or _DATE_SIGNAL.search(l)]
    result = "\n".join(kept)
    if len(result) < len(text) * 0.05:
        return text[:max_chars]
    return result[:max_chars]


def _content_fingerprint(text: str) -> str:
    lines = text.splitlines()
    structured = [l for l in lines if l.startswith("[STRUCTURED EVENT]")]
    if structured:
        return "\n".join(structured)
    date_lines = [l for l in lines if _DATE_SIGNAL.search(l)]
    return "\n".join(date_lines) if date_lines else text[:2000]


def _text_hash(text: str) -> str:
    """16-hex-char SHA-256 of the content fingerprint."""
    return hashlib.sha256(_content_fingerprint(text).encode()).hexdigest()[:16]


def _clean_text(text: str) -> str:
    text = re.sub(r'data:[^)"\s]+', '', text)
    text = re.sub(r'!\[\]\([^)]*\)', '', text)
    text = re.sub(r'(?mi)^.*(cookie|newsletter|sign.?in|log.?in|subscribe|©|\bfooter\b).*$', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


_GENERIC_TITLE_RE = re.compile(
    r"^(concert|event|show|performance|talk|exhibition|screening)\s*[-–]\s*\w{3}\s+\d",
    re.IGNORECASE,
)


def _build_events(raw_list: list[dict], source_url: str, source_name: str) -> list[Event]:
    events = []
    for item in raw_list:
        title = item.get("title", "")
        if _GENERIC_TITLE_RE.match(str(title)):
            continue
        if not item.get("url") or item["url"] in ("", "null"):
            item["url"] = source_url
        if not item.get("venue") or item["venue"] in ("", "null"):
            item["venue"] = source_name
        item["source"] = source_name
        if not item.pop("date_confirmed", True):
            continue
        try:
            events.append(Event(**item))
        except Exception:
            continue
    return events


def _parse_array(raw: str) -> list[dict]:
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return []


def _parse_object(raw: str) -> dict[str, list[dict]]:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return {}


def _make_batch_prompt(sites_data: list[dict], today: str) -> str:
    parts = []
    for s in sites_data:
        parts.append(f"=== SITE: {s['name']} | URL: {s['url']} | Category hint: {s['category_hint']} ===")
        if s.get("must_determine_venue"):
            parts.append("[Extract also: venue (actual event location) and url (event-specific link) for each event]")
        if s.get("notes"):
            parts.append(f"[Note: {s['notes']}]")
        if s.get("exhibition_cache_age") is not None:
            age = s["exhibition_cache_age"]
            parts.append(f"[Exhibitions cached {age} days ago — focus on one-off events only; skip long-running exhibitions unless new or ending within 14 days]")
        parts.append(_date_trim(_clean_text(s["text"]), max_chars=6000))
        parts.append("")
    exhibition_hint = "\n- Skip exhibitions if the site has a recent cache (noted above)."
    return _BATCH_PROMPT.format(
        schema=_EVENT_SCHEMA,
        date_rules=_DATE_RULES.format(today=today, exhibition_hint=exhibition_hint),
        sites="\n".join(parts),
    )


async def extract_events_batch_async(
    sites_data: list[dict],
    errors: list[str] | None = None,
    force_parse: bool = False,
) -> list[Event]:
    """Extract events from site data dicts using the configured LLM backend.

    Cache behaviour:
    - Content-hash hit: cached result reused regardless of TTL when page content
      is byte-identical to the stored fingerprint.
    - TTL hit: cached result reused when within PARSE_TTL_DAYS and not stale vs
      the last HTML download.
    - Miss: LLM called, result stored with content hash.

    Concurrency: bounded by _CONCURRENCY (1 for Ollama, 3 for Claude). Batches
    run concurrently up to that limit; result order matches input order.
    """
    today = date.today().isoformat()

    all_events: list[Event] = []
    uncached_sites: list[dict] = []

    for s in sites_data:
        h = _text_hash(s["text"])
        fetched_at = get_html_fetched_at(s["url"])
        cached = get_cached_parse(s["name"], force=force_parse, fetched_at=fetched_at, content_hash=h)
        if cached is not None:
            raw_list, hit_type = cached
            evts = _build_events(raw_list, s["url"], s["name"])
            all_events.extend(evts)
            print(f"  [HTML] {s['name']}: {len(evts)} events [{hit_type}]")
        else:
            s["_text_hash"] = h  # carry hash through to save_extraction
            uncached_sites.append(s)

    if not uncached_sites:
        return all_events

    batches = [uncached_sites[i:i + BATCH_SIZE] for i in range(0, len(uncached_sites), BATCH_SIZE)]
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _run_batch(batch: list[dict]) -> list[Event]:
        async with sem:
            try:
                prompt = _make_batch_prompt(batch, today)
                raw = await run_llm(prompt, timeout=900)
                parsed = _parse_object(raw)
                events = []
                for site in batch:
                    raw_list = parsed.get(site["name"], [])
                    if not isinstance(raw_list, list):
                        raw_list = []
                    site_events = _build_events(raw_list, site["url"], site["name"])
                    events.extend(site_events)
                    print(f"  [HTML] {site['name']}: {len(site_events)} events")
                    save_extraction(site["name"], raw_list, content_hash=site.get("_text_hash"))
                return events
            except Exception as e:
                names = [s["name"] for s in batch]
                print(f"  [HTML] batch FAILED ({', '.join(names)}): {e}")
                if errors is not None:
                    errors.extend(names)
                return []

    # Run batches concurrently (bounded by sem); gather preserves arg order.
    results = await asyncio.gather(*[_run_batch(b) for b in batches])
    for r in results:
        all_events.extend(r)
    return all_events
