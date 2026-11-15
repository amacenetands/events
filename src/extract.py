"""Claude CLI / Ollama extraction — concurrent async batches, configurable backend."""
import json
import re
import hashlib
import asyncio
import os
from datetime import date
from .models import Event
from .dedup import get_cached_parse, get_html_fetched_at, save_extraction, _TRUSTED_EMPTY_MODELS
from .llm import run_llm, _CLAUDE_MODEL, current_model_label
from . import timing

_MODEL = _CLAUDE_MODEL


def _default_batch_size() -> int:
    """Sites per LLM call. 1 for the Claude CLI: multi-site prompts dilute
    attention and haiku silently returns [] for some sites (e.g. Café Oto
    yielded 12 alone, 0 in a 3-site batch). Ollama keeps 3 to cut request count.
    Override with EXTRACTION_BATCH_SIZE."""
    env = os.environ.get("EXTRACTION_BATCH_SIZE")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    backend = (os.environ.get("LLM_BACKEND") or os.environ.get("EXTRACTION_BACKEND", "ollama")).lower()
    # 2 for Claude CLI: keeps dilution low (Café Oto extracts fine at ≤2) while
    # halving the call count vs 1 — batch=1 tripled requests and the CLI
    # rate-limited 56 sites to death. Ollama: 1 — local qwen has no per-request
    # cost and batch>1 dilutes attention → field misalignment (wrong time/date
    # assigned across a dense list, seen on RU venues). DeepSeek: 5 — API handles
    # multi-venue prompts well. 3 (was 5): deepseek-chat caps output at 8192 tokens
    # and 5-site batches truncated mid-JSON (lost Science Museum / Angel Comedy chunks).
    if backend == "deepseek":
        return 3
    return 1 if backend == "ollama" else 2


BATCH_SIZE = _default_batch_size()
# Max summed prompt-text chars per batch. DeepSeek caps output at 8192 tokens, so
# dense batches (~26-29k chars) truncated mid-JSON and forced a split retry (2x calls).
BATCH_CHAR_BUDGET = int(os.environ.get("BATCH_CHAR_BUDGET", "12000"))

# Parse-cache versioning — folded into the content hash so logic changes that do
# NOT alter the input text still invalidate cached extractions. Bump
# EXTRACT_VERSION on _build_events / post-processing changes; _PROMPT_VERSION is
# derived automatically from the prompt below, so editing the schema/date-rules
# busts the cache on its own. Per-site `cache_version` in sites.yaml busts one
# venue. See _text_hash.
EXTRACT_VERSION = "7"  # 7: kind/audience/min_age/recurrence schema + classify post-pass; 6: reducer + chunked units

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
    # DeepSeek API: 5 concurrent calls — API handles parallelism well, each call
    # carries 5 venues → 25 venues in-flight. Claude CLI: 1 — serial to avoid
    # rate limits. Ollama: 1 — local model serial.
    if backend == "deepseek":
        return 5
    return 1

_CONCURRENCY = _default_concurrency()

_DATE_SIGNAL = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
    r"|january|february|march|april|june|july|august|september|october|november|december"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|\d{4}-\d{2}-\d{2}"
    # Russian months (nominative + genitive roots)
    r"|янв|фев|март|марта|апр|ма[йя]|июн[ья]?|июл[ья]?|авг|сен|окт|ноя|дек"
    r"|января|февраля|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
    # Russian weekdays (short + full)
    r"|пн|вт|ср|чт|пт|сб|вс"
    r"|понедельник|вторник|среда|четверг|пятница|суббота|воскресенье"
    # Numeric day-first (DD.MM or DD.MM.YYYY)
    r"|\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)\b",
    re.IGNORECASE,
)

# Base schema — venue and url omitted; injected per-site or from known data
_EVENT_SCHEMA = """\
  title        string
  date         string   YYYY-MM-DD  — start/opening date (one-off: the single date it occurs)
  end_date     string|null  YYYY-MM-DD — closing date. REQUIRED when kind=run; null otherwise
  time         string|null  HH:MM 24h — start time. Look near the date: "7.30pm", "doors 19:00", "from 11am", "10:00-12:00" (use start). null only if truly absent; never write TBC
  category     string   concert|theatre|exhibition|talk|workshop|tour|film|comedy|festival|other  (NOT "kids" — that is audience)
  kind         string   one_off = single dated occurrence | run = exhibition/show/season with start AND end date | recurring = repeating series ("every Sat", weekly club, regular tours)
  recurrence   string|null  only for kind=recurring, short: "Saturdays", "daily 11:00"
  audience     string   kids = made FOR children (storytime, puppet show, "ages 3-7", under-5s) | family = children+adults together (family workshop, half-term, "(family)") | adult = 18+/late-night/wine/cocktail | all = general public (default)
  min_age      int|null   youngest suitable age if stated ("ages 5+" -> 5, "under 5s" -> 0); else null
  date_confirmed bool   true if date is a specific confirmed date found in text; false if estimated/inferred
  description  string|null  one sentence max
  price        string|null
  url          string|null  the event's OWN page: copy the "-> /path" link on that event's line in the text (never the listing page itself); null if no such link"""

# Extra fields for aggregator sites where venue/url differ per event
_VENUE_URL_FIELDS = """\
  venue        string   — actual event location (not the aggregator site name)
  url          string   — event-specific link"""

_DATE_RULES = """\
Date rules (today is {today}):
- OUTPUT CONTRACT — every event is EXACTLY one of:
    (a) ONE-OFF (single day): kind=one_off, `date`=that day, end_date=null.
    (b) RUN (exhibition, show, display, season): kind=run, `date`=start/opening, `end_date`=closing date. end_date is REQUIRED here — NEVER null.
    (c) RECURRING series with listed dates: emit one kind=recurring event PER listed date (end_date=null, recurrence="Saturdays" etc.).
  If an event spans multiple days you MUST capture its closing date — look for "until", "to", "till", "ends", "through", "runs", "–"/"—" date ranges (e.g. "17 Jun – 20 Sep", "until 4 Oct"). Do NOT collapse a multi-day run into a single date, and do NOT emit a multi-day event with end_date=null.
- CONFIRMED DATE REQUIRED: only emit an event if you can find a specific date (e.g. "14 Jun", "Sunday 15 June") in the text. Set date_confirmed=true.
- Extract ALL upcoming or currently-running events — do not filter by date range; include everything with a confirmed future or current date.
- Skip events whose date is clearly in the past (before today).
- [STRUCTURED EVENT] blocks at the top of the text contain machine-readable data — startDate/endDate are confirmed dates, treat them as date_confirmed=true. If startDate is in the past and endDate is in the future, treat as a currently-running ongoing event (kind=run, date=today, end_date=endDate).
- Recurring/series events (e.g. "select Sundays", "every weekend", "Jun–Aug"): ONLY include if the text lists specific upcoming dates OR a [STRUCTURED EVENT] block confirms the date range. Emit one event per specific upcoming date, or if a confirmed series runs now through a future end_date, emit once as kind=run. If no specific dates, SKIP entirely.
- Ongoing exhibitions/shows: kind=run, date=opening date (or today if already open and opening date not stated), end_date=closing date (REQUIRED — if no closing date is findable, do NOT emit it as a single-day event on today; search harder or skip). Set date_confirmed=true only if opening date is explicitly stated.
- One-off events (talks, concerts, workshops, performances, previews): kind=one_off, date_confirmed=true.
- Include ALL events regardless of time of day — daytime, evening, weekday, weekend.
- Always capture the time when present anywhere near the event (title line, date line, description). time=null only if none is given. Never write TBC, unknown, or similar.
- Return [] if no qualifying events.
- Text may be in Russian. Month name mapping: января=01, февраля=02, марта=03, апреля=04, мая=05, июня=06, июля=07, августа=08, сентября=09, октября=10, ноября=11, декабря=12. Example: "19 июня 2026" → 2026-06-19. Numeric dates are DAY-first: 19.06.2026 = 19 June 2026 → 2026-06-19. Always output `date` and `end_date` as YYYY-MM-DD."""

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

# Auto-derived from the prompt: any edit to schema / date-rules / batch template
# changes this digest → cached extractions for LLM-path sites re-run once.
_PROMPT_VERSION = hashlib.sha256(
    (_EVENT_SCHEMA + _VENUE_URL_FIELDS + _DATE_RULES + _BATCH_PROMPT).encode()
).hexdigest()[:8]


def _prompt_text(s: dict) -> str:
    """Text for one LLM unit. Reducer output (has "chunks") is already compact and
    record-aligned, so it is sent whole; legacy raw text still gets date-trimmed."""
    if s.get("chunks") is not None:
        return s["text"]
    return _date_trim(_clean_text(s["text"]), max_chars=6000)


def _date_trim(text: str, max_chars: int = 6000) -> str:
    # Keep date-bearing lines AND the few lines just before each: an event's
    # title usually sits on the line(s) above its date. Keeping only date lines
    # left the model with bare dates and no names → it emitted generic
    # "Exhibition"/"Concert" placeholder titles. ctx_before recovers the titles.
    lines = text.splitlines()
    ctx_before = int(os.environ.get("DATE_TRIM_CTX", "2"))
    keep: set[int] = set()
    for i, l in enumerate(lines):
        if l.startswith("[STRUCTURED EVENT]") or _DATE_SIGNAL.search(l):
            for j in range(max(0, i - ctx_before), i + 1):
                keep.add(j)
    kept = [lines[i] for i in sorted(keep)]
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


def _text_hash(text: str, site: dict | None = None) -> str:
    """16-hex-char SHA-256 of (logic version + prompt version + content + per-site).

    Folding the version tokens in means a change to extraction logic or the
    prompt invalidates cached parses even when the input text is byte-identical.
    """
    parts = [EXTRACT_VERSION, _PROMPT_VERSION, _content_fingerprint(text)]
    cv = (site or {}).get("cache_version")
    if cv is not None:
        parts.append(f"cv={cv}")
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


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


def _fix_urls(raw_list: list[dict], text: str, listing_url: str) -> list[dict]:
    """Events whose url is missing or equals the listing page: use the per-card
    '-> href' link from the reduced text on a line containing the event title."""
    norm = lambda u: str(u or "").strip().rstrip("/")
    links = []
    for ln in (text or "").splitlines():
        if " -> " in ln:
            body, href = ln.rsplit(" -> ", 1)
            href = href.strip()
            if href and not href.startswith(("#", "javascript", "mailto")):
                links.append((body.lower(), href))
    if not links:
        return raw_list
    for e in raw_list:
        u = e.get("url")
        if u and u != "null" and norm(u) != norm(listing_url):
            continue
        t = str(e.get("title") or "").strip().lower()
        if len(t) < 4:
            continue
        for body, href in links:
            if t in body:
                e["url"] = href
                break
    return raw_list


def _build_events(raw_list: list[dict], source_url: str, source_name: str) -> list[Event]:
    events = []
    for item in raw_list:
        title = item.get("title", "")
        if _GENERIC_TITLE_RE.match(str(title)):
            continue
        if not item.get("url") or item["url"] in ("", "null"):
            item["url"] = source_url
        else:
            from urllib.parse import urljoin
            item["url"] = urljoin(source_url, str(item["url"]).strip())
        if not item.get("venue") or item["venue"] in ("", "null"):
            item["venue"] = source_name
        item["source"] = source_name
        is_ongoing = item.get("kind") == "run" or (not item.get("kind") and item.get("is_one_off") is False)
        date_confirmed = item.pop("date_confirmed", True)
        # Instead of dropping events we couldn't date, flag them so the digest
        # can list them in a separate "exact dates undetermined" section.
        #   - one-off with an unconfirmed/inferred date → date itself is a guess
        #   - ongoing/multi-day with no valid closing date → range incomplete
        undetermined = False
        if is_ongoing:
            ed, sd = item.get("end_date"), item.get("date")
            if not ed or (isinstance(ed, str) and isinstance(sd, str) and ed <= sd):
                undetermined = True
                item["end_date"] = None
        elif not date_confirmed:
            undetermined = True
        if not item.get("date"):
            # truly no date — keep it (flagged) so it still surfaces
            item["date"] = date.today().isoformat()
            undetermined = True
        item["date_undetermined"] = undetermined
        try:
            events.append(Event(**item))
        except Exception as ex:
            print(f"  [build] dropped invalid event {str(item.get('title'))[:40]!r}: {str(ex)[:80]}", flush=True)
            continue
    return events


def _parse_object(raw: str) -> dict[str, list[dict]]:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return {}


def _parse_object_strict(raw: str) -> dict[str, list[dict]]:
    """Like _parse_object but distinguishes 'no events' from 'broken response'.

    Returns {} only for a real (empty) JSON object. RAISES when the reply has no
    JSON object at all (prose / refusal / empty) or the object is malformed
    (truncated) — so the caller can retry/fall back instead of trusting a bogus 0.
    """
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        raise ValueError(f"no JSON object in reply: {(raw or '')[:60]!r}")
    try:
        return json.loads(m.group())
    except json.JSONDecodeError as e:
        raise ValueError(f"malformed/truncated JSON object: {e}") from e


def _future_date_signals(text: str) -> int:
    """Distinct concrete future dates in text (day+month or ISO; year-less ones roll
    forward). The old impl counted only digit-bearing _DATE_SIGNAL tokens, which for
    English text ("5 March") are bare month names -> always 0 -> retry never fired."""
    from .sources.adapters.textutil import future_dates
    return len(future_dates(text))


def _retry_target(model_label: str) -> tuple[str, str] | None:
    """(backend, label) to retry a suspicious empty with, or None."""
    if model_label in ("haiku", "claude"):
        if os.environ.get("DEEPSEEK_API_KEY"):
            return ("deepseek", "deepseek")
        return None
    return ("claude", "haiku")


def _make_batch_prompt(sites_data: list[dict], today: str) -> str:
    parts = []
    for s in sites_data:
        parts.append(f"=== SITE: {s['name']} | URL: {s['url']} | Category hint: {s['category_hint']} ===")
        if s.get("must_determine_venue"):
            parts.append("[Extract also: venue (actual event location) and url (event-specific link) for each event]")
        if s.get("notes"):
            parts.append(f"[Note: {s['notes']}]")
        parts.append(_prompt_text(s))
        parts.append("")
    return _BATCH_PROMPT.format(
        schema=_EVENT_SCHEMA,
        date_rules=_DATE_RULES.format(today=today),
        sites="\n".join(parts),
    )


async def extract_events_batch_async(
    sites_data: list[dict],
    errors: list[str] | None = None,
    force_parse: bool = False,
    compare: bool = False,
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
    model = current_model_label()

    all_events: list[Event] = []
    uncached_sites: list[dict] = []

    for s in sites_data:
        h = _text_hash(s["text"], s)
        fetched_at = get_html_fetched_at(s["url"])
        cached = get_cached_parse(s["name"], force=force_parse, fetched_at=fetched_at, content_hash=h, model=model, any_model=(not compare))
        if cached is not None:
            raw_list, hit_type = cached
            evts = _build_events(raw_list, s["url"], s["name"])
            all_events.extend(evts)
            print(f"  [HTML] {s['name']}: {len(evts)} events [{hit_type}]", flush=True)
            timing.log_runtime(venue=s["name"], model=model, events=len(evts), cache_hit=True, secs=0.0)
        else:
            s["_text_hash"] = h  # carry hash through to save_extraction
            uncached_sites.append(s)

    if not uncached_sites:
        return all_events

    # Expand multi-chunk sites into one LLM unit per chunk (each <=6000 chars);
    # results merge back per parent site and are cached under the parent name.
    units: list[dict] = []
    for s in uncached_sites:
        chunks = s.get("chunks") or []
        if len(chunks) <= 1:
            units.append(s)
            continue
        s["_raw"], s["_pending"], s["_failed"], s["_n"], s["_units"] = [], len(chunks), [], len(chunks), []
        for i, c in enumerate(chunks, 1):
            # Same name as the parent: the LLM keys its answer by the SITE header, so a
            # renamed "[part i/n]" header comes back under the bare name and is lost.
            note = f"This is part {i} of {len(chunks)} of the page; other parts are processed separately. " + (s.get("notes") or "")
            u = {**s, "text": c, "chunks": [c], "notes": note.strip(), "_parent": s, "_idx": i}
            s["_units"].append(u)
            units.append(u)

    def _nm(x: dict) -> str:
        return x.get("_parent", x)["name"]

    def _merge_raw(raw: list[dict]) -> list[dict]:
        seen, out = set(), []
        for e in raw:
            k = (str(e.get("title", "")).strip().lower(), e.get("date"), e.get("time"))
            if k not in seen:
                seen.add(k)
                out.append(e)
        return out

    # Never put two units of the same site in one batch (they share the answer key).
    batches: list[list[dict]] = []
    for u in units:
        key = _nm(u)
        for b in batches:
            if (len(b) < BATCH_SIZE and all(_nm(x) != key for x in b)
                    and sum(len(x.get("text", "")) for x in b) + len(u.get("text", "")) <= BATCH_CHAR_BUDGET):
                b.append(u)
                break
        else:
            batches.append([u])
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _chunk_done(parent: dict, _model: str, dt: float, nsites: int, retry: bool, failed_idx: int | None = None, unit: dict | None = None) -> list[Event]:
        """Account one finished chunk (ok or failed). On the last chunk, build the
        parent's events from whatever chunks succeeded. A failed chunk never loses
        the other chunks' events; a partial result is not cached (so it re-runs)."""
        key = id(unit) if unit is not None else ("f", failed_idx)
        done = parent.setdefault("_done", set())
        if key in done:        # batch retry re-reported this unit: don't double-count
            return []
        done.add(key)
        if failed_idx is not None:
            parent["_failed"].append(failed_idx)
        parent["_pending"] -= 1
        if parent["_pending"] > 0:
            return []
        try:
            return await _chunk_finalize(parent, _model, dt, nsites, retry)
        except BaseException:
            # finalisation crashed: un-count this unit so a retry can re-report it
            done.discard(key)
            parent["_pending"] += 1
            if failed_idx is not None and failed_idx in parent["_failed"]:
                parent["_failed"].remove(failed_idx)
            raise

    async def _chunk_finalize(parent: dict, _model: str, dt: float, nsites: int, retry: bool) -> list[Event]:
        name, n = parent["name"], parent["_n"]
        merged = _merge_raw(parent["_raw"])
        failed = sorted(parent["_failed"])
        for i in failed:
            msg = f"{name} (chunk {i}/{n} failed)"
            print(f"  [HTML] {msg}", flush=True)
            if errors is not None and msg not in errors:
                errors.append(msg)
        if not merged and not failed and retry:
            n_sig = _future_date_signals(parent["text"])
            if _model == "qwen":      # weak-model empty is provisional: always upgrade (as for non-chunked sites)
                tgt = ("claude", "haiku")
            else:
                tgt = _retry_target(_model) if n_sig >= 3 else None
            if tgt:
                print(f"  [extract] {name}: 0 events over {n} chunks ({n_sig} date signals) — retrying with {tgt[1]}", flush=True)
                parent["_raw"], parent["_pending"], parent["_failed"], parent["_done"] = [], n, [], set()
                got: list[Event] = []
                for u in parent["_units"]:
                    try:
                        got.extend(await _extract_sites([u], backend=tgt[0], model_label=tgt[1], retry=False))
                    except Exception as e:
                        print(f"  [extract] {name}: {tgt[1]} retry failed ({str(e)[:60]}); not caching empty", flush=True)
                        return []
                return got
        _fix_urls(merged, parent.get("text", ""), parent["url"])
        site_events = _build_events(merged, parent["url"], name)
        print(f"  [HTML] {name}: {len(site_events)} events [{_model}] ({dt:.0f}s, {n} chunks{', PARTIAL' if failed else ''})", flush=True)
        timing.log_runtime(venue=name, model=_model, events=len(site_events), secs=round(dt, 1), batch_size=nsites)
        if not failed:
            save_extraction(name, merged, content_hash=parent.get("_text_hash"), model=_model)
        return site_events

    async def _extract_sites(sites: list[dict], backend: str | None = None, model_label: str | None = None, retry: bool = True) -> list[Event]:
        """One LLM call for `sites`. Raises on call/parse failure (handled by
        _run_batch's split-retry + haiku fallback). A valid empty result yields 0
        events, no raise. backend/model_label override the default (used by the
        claude fallback to re-run a venue the local model couldn't parse)."""
        _model = model_label or model
        prompt = _make_batch_prompt(sites, today)
        t0 = timing.now()
        # Hard wall-clock cap per LLM call: run_llm's internal httpx read-timeout
        # resets per streamed chunk, so a slow/infinite ollama generation never
        # fires it and stalls the whole run (a 14b venue hung indefinitely). Wrap
        # the call in asyncio.wait_for so a hung venue is cancelled and skipped.
        raw = await asyncio.wait_for(run_llm(prompt, backend=backend, timeout=420, retries=2), timeout=450)
        dt = timing.now() - t0
        parsed = _parse_object_strict(raw)              # raises on truncated/garbled JSON
        events: list[Event] = []
        empty_under_weak: list[dict] = []
        suspicious: list[tuple[dict, int]] = []
        for site in sites:
            raw_list = parsed.get(site["name"], [])
            if not isinstance(raw_list, list):
                raw_list = []
            parent = site.get("_parent")
            if parent is not None:
                if id(site) not in parent.get("_done", ()):
                    parent["_raw"].extend(r for r in raw_list if isinstance(r, dict))
                events.extend(await _chunk_done(parent, _model, dt, len(sites), retry, unit=site))
                continue
            # Defer a weak-model (qwen) empty to an in-run haiku upgrade rather than
            # caching a 0 that get_cached_parse would distrust and re-parse anyway —
            # self-heals coverage within one run. Skip when this IS the haiku pass
            # (_model trusted) to avoid recursion.
            if not raw_list and _model == "qwen":
                empty_under_weak.append(site)
                continue
            if not raw_list and retry and _model != "qwen":
                n_sig = _future_date_signals(_prompt_text(site))
                if n_sig >= 3 and _retry_target(_model):
                    suspicious.append((site, n_sig))
                    continue
            _fix_urls(raw_list, site.get("text", ""), site["url"])
            site_events = _build_events(raw_list, site["url"], site["name"])
            events.extend(site_events)
            print(f"  [HTML] {site['name']}: {len(site_events)} events [{_model}] ({dt:.0f}s)", flush=True)
            timing.log_runtime(venue=site["name"], model=_model, events=len(site_events), secs=round(dt, 1), batch_size=len(sites))
            save_extraction(site["name"], raw_list, content_hash=site.get("_text_hash"), model=_model)
        if empty_under_weak:
            names = ", ".join(s["name"] for s in empty_under_weak)
            print(f"  [HTML] {len(empty_under_weak)} empty under {_model} → haiku upgrade: {names}", flush=True)
            try:
                events.extend(await _extract_sites(empty_under_weak, backend="claude", model_label="haiku"))
            except Exception as e:
                print(f"  [HTML] haiku upgrade failed ({str(e)[:60]}); leaving unparsed for retry next run", flush=True)
        for site, n_sig in suspicious:
            fb_backend, fb_label = _retry_target(_model)
            print(f"  [extract] {site['name']}: 0 events but {n_sig} date signals — retrying with {fb_label}", flush=True)
            try:
                got = await _extract_sites([site], backend=fb_backend, model_label=fb_label, retry=False)
            except Exception as e:
                print(f"  [extract] {site['name']}: {fb_label} retry failed ({str(e)[:60]}); not caching empty", flush=True)
                continue
            events.extend(got)
            if not got:  # both agree on empty → cache the original empty too
                print(f"  [HTML] {site['name']}: 0 events [{_model}] ({dt:.0f}s)", flush=True)
                save_extraction(site["name"], [], content_hash=site.get("_text_hash"), model=_model)
        return events

    async def _fail(batch: list[dict], names: list[str]) -> list[Event]:
        """Terminal failure of a single unit: whole-site error, or (for a chunk of
        a multi-chunk site) isolate it and keep the other chunks' events."""
        out: list[Event] = []
        for s in batch:
            if s.get("_parent") is not None:
                out.extend(await _chunk_done(s["_parent"], model, 0.0, 1, True, failed_idx=s["_idx"], unit=s))
            elif errors is not None and s["name"] not in errors:
                errors.append(s["name"])
        return out

    def _truncated(e: Exception) -> bool:
        return isinstance(e, ValueError) and "malformed" in str(e)

    async def _split_run(u: dict, depth: int) -> list[Event]:
        """Output was cut off (deepseek 8192-token cap): halve this chunk on record
        (line) boundaries and retry each half; max depth 2, then fail the chunk."""
        lines = u["text"].split("\n")
        if depth >= 2 or len(lines) < 2:
            return await _fail([u], [_nm(u)])
        mid = len(lines) // 2
        parent = u.get("_parent")
        halves = [{**u, "text": t, "chunks": [t], "_depth": depth + 1} for t in ("\n".join(lines[:mid]), "\n".join(lines[mid:]))]
        if parent is None:   # single-unit site: promote it to its own parent
            u["_raw"], u["_failed"], u["_n"], u["_pending"] = [], [], 2, 2
            for i, h in enumerate(halves, 1):
                h["_parent"], h["_idx"] = u, i
            u["_units"] = halves
        else:
            parent["_pending"] += 1
            parent["_units"] = [x for x in parent["_units"] if x is not u] + halves
            for h in halves:
                h["_parent"], h["_idx"] = parent, u.get("_idx", 1)
        print(f"  [extract] {_nm(u)}: output truncated -> splitting chunk in 2 (depth {depth + 1})", flush=True)
        out: list[Event] = []
        for h in halves:
            try:
                out.extend(await _extract_sites([h]))
            except Exception as e2:
                if _truncated(e2):
                    out.extend(await _split_run(h, depth + 1))
                else:
                    print(f"  [HTML] split half failed: {str(e2)[:80]}", flush=True)
                    out.extend(await _fail([h], [_nm(h)]))
        return out

    async def _run_batch(batch: list[dict]) -> list[Event]:
        async with sem:
            try:
                return await _extract_sites(batch)
            except Exception as e:
                if len(batch) <= 1 and _truncated(e):
                    return await _split_run(batch[0], batch[0].get("_depth", 0))
                if len(batch) <= 1:
                    names = list(dict.fromkeys(_nm(s) for s in batch))
                    # Auto-fallback: a local-model (qwen/8b) parse failure — e.g.
                    # malformed JSON on a dense venue — retries the venue with
                    # claude/haiku before giving up, and stores it tagged
                    # model="haiku" so it self-heals (next run reuses the cached
                    # haiku result, no re-fail). Skip when the primary IS claude.
                    if model != "haiku":
                        try:
                            print(f"  [HTML] {names[0]}: {model} failed ({str(e)[:50]}) → fallback to haiku", flush=True)
                            return await _extract_sites(batch, backend="claude", model_label="haiku")
                        except Exception as e_fb:
                            print(f"  [HTML] FAILED ({', '.join(names)}): {model}+haiku both failed: {str(e_fb)[:80]}")
                            return await _fail(batch, names)
                    print(f"  [HTML] FAILED ({', '.join(names)}): {e}")
                    return await _fail(batch, names)
                # Split-retry: a failed/garbled multi-site response is usually
                # truncation or a transient rate limit — retry each site alone so
                # one bad site (or a cut-off batch) can't silently zero the rest.
                print(f"  [HTML] batch of {len(batch)} failed ({e}); retrying sites individually")
                out: list[Event] = []
                for s in batch:
                    try:
                        out.extend(await _extract_sites([s]))
                    except Exception as e2:
                        if _truncated(e2):
                            out.extend(await _split_run(s, s.get("_depth", 0)))
                            continue
                        print(f"  [HTML] FAILED ({s['name']}): {e2}")
                        out.extend(await _fail([s], [_nm(s)]))
                return out

    # Run batches concurrently (bounded by sem); gather preserves arg order.
    results = await asyncio.gather(*[_run_batch(b) for b in batches])
    for r in results:
        all_events.extend(r)
    return all_events
