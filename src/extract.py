"""LLM extraction engine for the `llm` source: reduced page text -> RawEvent strings.

Concurrent async batches over the configured backend, per-site parse cache, chunking,
split-on-truncation, weak-model upgrade and empty-result retry. No date logic here:
the model copies strings; sources/build.py normalises them.
"""
import json
import re
import hashlib
import asyncio
import os
from datetime import date
from .models import RawEvent
from .dedup import get_cached_parse, save_extraction
from .llm import run_llm, current_model_label
from .normalize.urls import fold
from . import timing


def _default_batch_size() -> int:
    """Sites per LLM call. 1 for the Claude CLI: multi-site prompts dilute
    attention and haiku silently returns [] for some sites (e.g. Café Oto
    yielded 12 alone, 0 in a 3-site batch). Ollama 1, DeepSeek 3 (see below).
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
# EXTRACT_VERSION on to_raws / post-processing changes; _PROMPT_VERSION is
# derived automatically from the prompt below, so editing the schema/date-rules
# busts the cache on its own. Per-site `cache_version` in sites.yaml busts one
# venue. See _text_hash.
EXTRACT_VERSION = "8"  # 8: raw-string RawEvent schema (normalize does dates); 7: kind/audience schema; 6: reducer + chunks

# Concurrent LLM calls: 1 for Ollama (single local model — parallelism just contends) and
# the Claude CLI (rate limits), 5 for DeepSeek. Override with LLM_CONCURRENCY.
def _default_concurrency() -> int:
    env_val = os.environ.get("LLM_CONCURRENCY")
    if env_val is not None:
        try:
            return max(1, int(env_val))
        except ValueError:
            pass
    backend = (os.environ.get("LLM_BACKEND") or os.environ.get("EXTRACTION_BACKEND", "ollama")).lower()
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


# Raw-string schema: the model COPIES page strings; src/normalize turns them into dates/
# times/prices deterministically (sources/build.py). Venue is added for aggregators.
_EVENT_SCHEMA = """\
  title           string   the event's name, copied verbatim
  date_text       string|null  the event's date text EXACTLY as written, incl. ranges, lists and "On now"/"Ongoing": "Sat 22 Nov", "Fri 21 Nov – Sun 7 Dec", "Until 4 Jan", "5, 12 & 19 Dec", "19 ноября"
  end_text        string|null  closing date as written, ONLY when shown apart from date_text ("Closes 4 Jan")
  time_text       string|null  start time(s) as written ("7.30pm", "Doors 19:00", "Tue–Sat 7pm, Sat 2.30pm")
  price_text      string|null  price as written ("£15 / £10 conc", "Free", "From £12")
  recurrence_text string|null  repeat rule as written ("Every Friday", "Saturdays", "Weekdays 10am")
  url             string|null  the event's OWN link: copy the "-> /path" on that event's line exactly; null if none
  desc            string|null  short description or subtitle copied from the text, max 200 characters
  category_text   string|null  the page's own type label for the event ("Concert", "Exhibition", "Talk", "Family")
  audience_text   string|null  stated audience ("Kids", "Family", "Adults only", "Under 5s")
  age_text        string|null  stated age guidance ("18+", "Ages 5-11", "0-4 years")"""

_VENUE_FIELD = """\
  venue           string   the actual event location as written (this site lists events at many venues)"""

_RULES = """\
Rules:
- COPY strings verbatim from the text. Do NOT convert dates to ISO or any other format, do NOT compute, infer or complete dates, weekdays, years or end dates, and do NOT translate.
- Text may be in Russian (or another language): copy those strings as-is, in the original language.
- Times go in time_text, not date_text.
- One object per event. When an event lists several dates or a range, put them all in date_text exactly as written; never split, expand or shorten them.
- Include every event that has date text (including "On now" / "Ongoing") or a repeat rule, whether past, current or future (date filtering happens later).
- Skip items with no date text and no repeat rule, and non-events (navigation, newsletters, venue hire, gift vouchers, membership).
- Use null for anything not in the text; never write "TBC", "unknown" or a guess.
- A site with no events gets []."""

_INTRO_ONE = "Find the events listed in the text of a venue web page. Return ONLY a JSON object with the site name as its single key, no prose."
_INTRO_MANY = "Find the events listed in the text of several venue web pages. Return ONLY a single JSON object keyed by site name, no prose."

_BATCH_PROMPT = """\
Today is {weekday} {today} (reference only: never use it to rewrite or complete dates).
{intro}

Format:
{{
  "Site Name": [ {{event}}, ... ]
}}

Each event (JSON keys):
{schema}

{rules}

{sites}"""

_SITE_HEADER = "=== SITE: {name} | URL: {url} | Language: {lang}{cat} ==="
_VENUE_NOTE = "[Extract also: venue (actual event location) for each event]"
_HINT_NOTE = "[Venue note — context only; never copy dates/times/prices from it: {hint}]"
_PART_NOTE = "[This is part {i} of {n} of the page; other parts are processed separately.]"

# Auto-derived from the prompt: any edit to schema / rules / templates changes this
# digest -> cached extractions for LLM-path sites re-run once.
_PROMPT_VERSION = hashlib.sha256("\x00".join((
    _EVENT_SCHEMA, _VENUE_FIELD, _RULES, _INTRO_ONE, _INTRO_MANY, _BATCH_PROMPT,
    _SITE_HEADER, _VENUE_NOTE, _HINT_NOTE, _PART_NOTE)).encode()).hexdigest()[:8]

DESC_MAX = 200
_RAW_FIELDS = ("title", "date_text", "end_text", "time_text", "price_text", "recurrence_text", "url",
               "desc", "venue", "category_text", "audience_text", "age_text")
# Keys a model sometimes uses from older/other schemas -> RawEvent field.
_ALIASES = {"name": "title", "date": "date_text", "dates": "date_text", "start_date": "date_text",
            "end_date": "end_text", "end": "end_text", "time": "time_text", "times": "time_text",
            "start_time": "time_text", "price": "price_text", "recurrence": "recurrence_text",
            "link": "url", "href": "url", "ticket_url": "url", "booking_url": "url",
            "description": "desc", "category": "category_text", "audience": "audience_text",
            "age": "age_text", "min_age": "age_text", "location": "venue", "venue_name": "venue"}
_NULLISH = {"", "null", "none", "n/a", "tbc", "tba", "unknown"}
_DROP = object()


def _s(v):
    """Model value -> stripped string; None for null-ish; _DROP for unusable (dict) values."""
    if v is None:
        return None
    if isinstance(v, dict):
        return _DROP
    if isinstance(v, bool):
        return None
    if isinstance(v, (list, tuple)):
        if any(isinstance(x, (dict, list)) for x in v):
            return _DROP
        v = ", ".join(str(x).strip() for x in v if x not in (None, ""))
    v = str(v).strip()
    return None if v.lower() in _NULLISH else v


def _bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "yes", "sold out", "soldout"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("false", "no"):
        return False
    return None


def to_raws(raw_list: list, stats: dict | None = None) -> list[RawEvent]:
    """Model/cached dicts -> RawEvents (strings only; untitled or non-dict items dropped).
    Dict-valued fields are dropped and counted in stats["llm.dropped_keys"]."""
    out: list[RawEvent] = []
    for d in raw_list or []:
        if not isinstance(d, dict):
            continue
        f: dict[str, str | None] = {}
        for k, v in d.items():
            key = k if k in _RAW_FIELDS else _ALIASES.get(k)
            if not key or f.get(key) is not None:
                continue
            val = _s(v)
            if val is _DROP:
                if stats is not None:
                    stats["llm.dropped_keys"] = stats.get("llm.dropped_keys", 0) + 1
                continue
            f[key] = val
        if not f.get("title"):
            continue
        if f.get("desc"):
            f["desc"] = f["desc"][:DESC_MAX]
        for k in ("category_text", "audience_text", "age_text"):
            f[k] = f.get(k) or ""
        out.append(RawEvent(**{k: f.get(k) for k in _RAW_FIELDS}, sold_out=_bool(d.get("sold_out")),
                            source_kind="llm"))
    return out


def _content_fingerprint(text: str) -> str:
    lines = text.splitlines()
    date_lines = [l for l in lines if _DATE_SIGNAL.search(l)]
    return "\n".join(date_lines) if date_lines else text[:2000]


def _site_block(s: dict) -> list[str]:
    cat = s.get("category_hint") or "other"
    out = [_SITE_HEADER.format(name=s["name"], url=s["url"], lang=s.get("lang") or "en",
                               cat=f" | Category hint: {cat}" if cat != "other" else "")]
    if s.get("must_determine_venue"):
        out.append(_VENUE_NOTE)
    if s.get("part"):
        out.append(_PART_NOTE.format(i=s["part"][0], n=s["part"][1]))
    if s.get("hint"):
        out.append(_HINT_NOTE.format(hint=s["hint"]))
    out += [s["text"], ""]
    return out


def _text_hash(text: str, site: dict | None = None) -> str:
    """16-hex-char SHA-256 of (logic version + prompt version + content + per-site header).

    The version tokens and the site header (hint, category, venue flag, lang) mean a change
    to extraction logic, prompt or venue config invalidates cached parses even when the
    page text is byte-identical. `today` is deliberately not part of it.
    """
    parts = [EXTRACT_VERSION, _PROMPT_VERSION, _content_fingerprint(text)]
    if site:
        parts.append("\n".join(_site_block({**site, "text": "", "part": None})))
        if site.get("cache_version") is not None:
            parts.append(f"cv={site['cache_version']}")
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def _parse_object_strict(raw: str):
    """JSON from a reply (code fences stripped): dict, or a bare list for one site.

    RAISES when the reply has no JSON at all (prose / refusal / empty) or it is malformed
    (truncated) — so the caller can retry/fall back instead of trusting a bogus 0.
    """
    txt = _FENCE.sub("", raw or "").strip()
    try:
        data = json.loads(txt)
        if isinstance(data, (dict, list)):
            return data
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON object in reply: {(raw or '')[:60]!r}")
    try:
        return json.loads(m.group())
    except json.JSONDecodeError as e:
        raise ValueError(f"malformed/truncated JSON object: {e}") from e


def _fold_name(s: str) -> str:
    return re.sub(r"[\"'«»]", "", fold(s))


def _site_records(parsed, name: str, n_sites: int):
    """This site's list from a reply; [] for an empty reply; None when the reply has
    other keys but not this site (never cached as an empty)."""
    if isinstance(parsed, list):
        return parsed if n_sites == 1 else None
    if not parsed:
        return []
    if name in parsed:
        v = parsed[name]
    elif n_sites == 1 and len(parsed) == 1:
        v = next(iter(parsed.values()))
    else:
        key = next((k for k in parsed if _fold_name(k) == _fold_name(name)), None)
        if key is None:
            return None
        v = parsed[key]
    if v is None:
        return []
    if isinstance(v, dict):
        return [v]
    return v if isinstance(v, list) else None


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


def _make_batch_prompt(sites_data: list[dict], today: date | str) -> str:
    d = date.fromisoformat(today) if isinstance(today, str) else today
    schema = _EVENT_SCHEMA + ("\n" + _VENUE_FIELD if any(s.get("must_determine_venue") for s in sites_data) else "")
    return _BATCH_PROMPT.format(
        weekday=d.strftime("%A"), today=d.isoformat(), intro=_INTRO_ONE if len(sites_data) == 1 else _INTRO_MANY,
        schema=schema, rules=_RULES, sites="\n".join(line for s in sites_data for line in _site_block(s)),
    )


def cached_records(site: dict, force_parse: bool = False) -> tuple[list[dict], str] | None:
    """(cached raw dicts, hit label) for a site dict, or None on a miss."""
    return get_cached_parse(site["name"], force=force_parse, fetched_at=site.get("fetched_at"),
                            content_hash=_text_hash(site["text"], site), model=current_model_label())


Tagged = list[tuple[str, dict]]     # (site name, raw record dict)
SiteError = tuple[str, str]         # (site name, what failed)


def _tag(raw_list: list, name: str) -> Tagged:
    return [(name, d) for d in raw_list if isinstance(d, dict)]


async def extract_raw_batch_async(
    sites_data: list[dict],
    errors: list[SiteError] | None = None,
    force_parse: bool = False,
    today: date | None = None,
    sem: asyncio.Semaphore | None = None,
) -> dict[str, list[dict]]:
    """Raw record dicts per site name (to_raws() makes RawEvents) for site dicts {name, url,
    text, chunks, category_hint, hint, lang, must_determine_venue, cache_version, fetched_at}.

    Cache: per site, keyed on _text_hash (content + versions + site header); a TTL hit is
    accepted only for legacy unhashed rows. `errors` gets (name, "failed") when the whole
    site failed, (name, "chunk i/n failed") for a lost chunk; failures are never cached.
    Concurrency: bounded by `sem` (shared across calls) or a fresh _CONCURRENCY semaphore.
    """
    today = today or date.today()
    model = current_model_label()
    out: dict[str, list[dict]] = {s["name"]: [] for s in sites_data}
    errs: list[SiteError] = errors if errors is not None else []
    uncached_sites: list[dict] = []

    def _err(name: str, msg: str) -> None:
        if (name, msg) not in errs:
            errs.append((name, msg))

    for s in sites_data:
        h = _text_hash(s["text"], s)
        cached = get_cached_parse(s["name"], force=force_parse, fetched_at=s.get("fetched_at"),
                                  content_hash=h, model=model)
        if cached is not None:
            raw_list, hit_type = cached
            out[s["name"]] = [d for d in raw_list if isinstance(d, dict)]
            print(f"  [llm] {s['name']}: {len(out[s['name']])} records [{hit_type}]", flush=True)
            timing.log_runtime(venue=s["name"], model=model, events=len(out[s["name"]]), cache_hit=True, secs=0.0)
        else:
            s["_text_hash"] = h  # carry hash through to save_extraction
            uncached_sites.append(s)

    if not uncached_sites:
        return out

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
            u = {**s, "text": c, "chunks": [c], "part": (i, len(chunks)), "_parent": s, "_idx": i}
            s["_units"].append(u)
            units.append(u)

    def _nm(x: dict) -> str:
        return x.get("_parent", x)["name"]

    def _merge_raw(raw: list[dict]) -> list[dict]:
        seen, res = set(), []
        for e in raw:
            k = (str(e.get("title", "")).strip().lower(), str(e.get("date_text") or e.get("date") or ""),
                 str(e.get("time_text") or e.get("time") or ""))
            if k not in seen:
                seen.add(k)
                res.append(e)
        return res

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
    sem = sem or asyncio.Semaphore(_CONCURRENCY)

    async def _chunk_done(parent: dict, _model: str, dt: float, nsites: int, retry: bool, failed_idx: int | None = None, unit: dict | None = None) -> Tagged:
        """Account one finished chunk (ok or failed). On the last chunk, build the
        parent's records from whatever chunks succeeded. A failed chunk never loses
        the other chunks' records; a partial result is not cached (so it re-runs)."""
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

    async def _chunk_finalize(parent: dict, _model: str, dt: float, nsites: int, retry: bool) -> Tagged:
        name, n = parent["name"], parent["_n"]
        merged = _merge_raw(parent["_raw"])
        failed = sorted(parent["_failed"])
        for i in failed:
            print(f"  [llm] {name} (chunk {i}/{n} failed)", flush=True)
            _err(name, f"chunk {i}/{n} failed")
        if not merged and not failed and retry:
            n_sig = _future_date_signals(parent["text"])
            if _model == "qwen":      # weak-model empty is provisional: always upgrade (as for non-chunked sites)
                tgt = ("claude", "haiku")
            else:
                tgt = _retry_target(_model) if n_sig >= 3 else None
            if tgt:
                print(f"  [llm] {name}: 0 records over {n} chunks ({n_sig} date signals) — retrying with {tgt[1]}", flush=True)
                parent["_raw"], parent["_pending"], parent["_failed"], parent["_done"] = [], n, [], set()
                got: Tagged = []
                for u in parent["_units"]:
                    try:
                        got.extend(await _extract_sites([u], backend=tgt[0], model_label=tgt[1], retry=False))
                    except Exception as e:
                        print(f"  [llm] {name}: {tgt[1]} retry failed ({str(e)[:60]}); not caching empty", flush=True)
                        return []
                return got
        recs = _tag(merged, name)
        print(f"  [llm] {name}: {len(recs)} records [{_model}] ({dt:.0f}s, {n} chunks{', PARTIAL' if failed else ''})", flush=True)
        timing.log_runtime(venue=name, model=_model, events=len(recs), secs=round(dt, 1), batch_size=nsites)
        if not failed:
            save_extraction(name, merged, content_hash=parent.get("_text_hash"), model=_model)
        return recs

    async def _extract_sites(sites: list[dict], backend: str | None = None, model_label: str | None = None, retry: bool = True) -> Tagged:
        """One LLM call for `sites`. Raises on call/parse failure (handled by
        _run_batch's split-retry + haiku fallback). A valid empty result yields 0
        records, no raise. backend/model_label override the default (used by the
        claude fallback to re-run a venue the local model couldn't parse)."""
        _model = model_label or model
        prompt = _make_batch_prompt(sites, today)
        t0 = timing.now()
        # Hard wall-clock cap per LLM call: run_llm's internal httpx read-timeout
        # resets per streamed chunk, so a slow/infinite ollama generation never
        # fires it and stalls the whole run. Wrap in asyncio.wait_for so a hung
        # venue is cancelled and skipped.
        raw = await asyncio.wait_for(run_llm(prompt, backend=backend, timeout=420, retries=2), timeout=450)
        dt = timing.now() - t0
        parsed = _parse_object_strict(raw)              # raises on truncated/garbled JSON
        recs: Tagged = []
        empty_under_weak: list[dict] = []
        suspicious: list[tuple[dict, int]] = []
        missing: list[dict] = []
        for site in sites:
            raw_list = _site_records(parsed, site["name"], len(sites))
            if raw_list is None:            # reply keyed by other names: a failure, not an empty
                print(f"  [llm] {site['name']}: not in reply (keys: {list(parsed)[:3] if isinstance(parsed, dict) else 'list'})", flush=True)
                missing.append(site)
                continue
            parent = site.get("_parent")
            if parent is not None:
                if id(site) not in parent.get("_done", ()):
                    parent["_raw"].extend(r for r in raw_list if isinstance(r, dict))
                recs.extend(await _chunk_done(parent, _model, dt, len(sites), retry, unit=site))
                continue
            # Defer a weak-model (qwen) empty to an in-run haiku upgrade rather than
            # caching a 0 that get_cached_parse would distrust and re-parse anyway.
            if not raw_list and _model == "qwen":
                empty_under_weak.append(site)
                continue
            if not raw_list and retry and _model != "qwen":
                n_sig = _future_date_signals(site["text"])
                if n_sig >= 3 and _retry_target(_model):
                    suspicious.append((site, n_sig))
                    continue
            site_recs = _tag(raw_list, site["name"])
            recs.extend(site_recs)
            print(f"  [llm] {site['name']}: {len(site_recs)} records [{_model}] ({dt:.0f}s)", flush=True)
            timing.log_runtime(venue=site["name"], model=_model, events=len(site_recs), secs=round(dt, 1), batch_size=len(sites))
            save_extraction(site["name"], raw_list, content_hash=site.get("_text_hash"), model=_model)
        if missing:
            recs.extend(await _fail(missing))
        if empty_under_weak:
            names = ", ".join(s["name"] for s in empty_under_weak)
            print(f"  [llm] {len(empty_under_weak)} empty under {_model} → haiku upgrade: {names}", flush=True)
            try:
                recs.extend(await _extract_sites(empty_under_weak, backend="claude", model_label="haiku"))
            except Exception as e:
                print(f"  [llm] haiku upgrade failed ({str(e)[:60]}); leaving unparsed for retry next run", flush=True)
        for site, n_sig in suspicious:
            fb_backend, fb_label = _retry_target(_model)
            print(f"  [llm] {site['name']}: 0 records but {n_sig} date signals — retrying with {fb_label}", flush=True)
            try:
                got = await _extract_sites([site], backend=fb_backend, model_label=fb_label, retry=False)
            except Exception as e:
                print(f"  [llm] {site['name']}: {fb_label} retry failed ({str(e)[:60]}); not caching empty", flush=True)
                continue
            recs.extend(got)
            if not got:  # both agree on empty → cache the original empty too
                print(f"  [llm] {site['name']}: 0 records [{_model}] ({dt:.0f}s)", flush=True)
                save_extraction(site["name"], [], content_hash=site.get("_text_hash"), model=_model)
        return recs

    async def _fail(batch: list[dict]) -> Tagged:
        """Terminal failure of units: whole-site error, or (for a chunk of a multi-chunk
        site) isolate it and keep the other chunks' records."""
        res: Tagged = []
        for s in batch:
            if s.get("_parent") is not None:
                res.extend(await _chunk_done(s["_parent"], model, 0.0, 1, True, failed_idx=s["_idx"], unit=s))
            else:
                _err(s["name"], "failed")
        return res

    def _truncated(e: Exception) -> bool:
        return isinstance(e, ValueError) and "malformed" in str(e)

    async def _split_run(u: dict, depth: int) -> Tagged:
        """Output was cut off (deepseek 8192-token cap): halve this chunk on record
        (line) boundaries and retry each half; max depth 2, then fail the chunk."""
        lines = u["text"].split("\n")
        if depth >= 2 or len(lines) < 2:
            return await _fail([u])
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
        print(f"  [llm] {_nm(u)}: output truncated -> splitting chunk in 2 (depth {depth + 1})", flush=True)
        res: Tagged = []
        for h in halves:
            try:
                res.extend(await _extract_sites([h]))
            except Exception as e2:
                if _truncated(e2):
                    res.extend(await _split_run(h, depth + 1))
                else:
                    print(f"  [llm] split half failed: {str(e2)[:80]}", flush=True)
                    res.extend(await _fail([h]))
        return res

    async def _run_batch(batch: list[dict]) -> Tagged:
        async with sem:
            try:
                return await _extract_sites(batch)
            except Exception as e:
                if len(batch) <= 1 and _truncated(e):
                    return await _split_run(batch[0], batch[0].get("_depth", 0))
                if len(batch) <= 1:
                    names = list(dict.fromkeys(_nm(s) for s in batch))
                    # Auto-fallback: a primary-model failure retries the venue with
                    # claude/haiku before giving up, stored as model="haiku" so it
                    # self-heals. Skip when the primary IS claude.
                    if model != "haiku":
                        try:
                            print(f"  [llm] {names[0]}: {model} failed ({str(e)[:50]}) → fallback to haiku", flush=True)
                            return await _extract_sites(batch, backend="claude", model_label="haiku")
                        except Exception as e_fb:
                            print(f"  [llm] FAILED ({', '.join(names)}): {model}+haiku both failed: {str(e_fb)[:80]}")
                            return await _fail(batch)
                    print(f"  [llm] FAILED ({', '.join(names)}): {e}")
                    return await _fail(batch)
                # Split-retry: a failed/garbled multi-site response is usually
                # truncation or a transient rate limit — retry each site alone.
                print(f"  [llm] batch of {len(batch)} failed ({e}); retrying sites individually")
                res: Tagged = []
                for s in batch:
                    try:
                        res.extend(await _extract_sites([s]))
                    except Exception as e2:
                        if _truncated(e2):
                            res.extend(await _split_run(s, s.get("_depth", 0)))
                            continue
                        print(f"  [llm] FAILED ({s['name']}): {e2}")
                        res.extend(await _fail([s]))
                return res

    # Run batches concurrently (bounded by sem); a batch that still raises (e.g. the
    # cache write) fails only its own sites.
    results = await asyncio.gather(*[_run_batch(b) for b in batches], return_exceptions=True)
    for b, tagged in zip(batches, results):
        if isinstance(tagged, BaseException):
            print(f"  [llm] batch crashed: {type(tagged).__name__}: {str(tagged)[:80]}", flush=True)
            for u in b:
                _err(_nm(u), f"failed ({type(tagged).__name__})")
            continue
        for name, d in tagged:
            out.setdefault(name, []).append(d)
    return out
