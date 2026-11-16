# Pipeline v3 plan — coverage, accuracy, modularity, new cities

Written 2026-11-15 from: a Sonnet accuracy audit of the 3,412 London rows in
`cache/memory.db`, a Sonnet coverage audit of all 391 venues, a Sonnet web review
of current scraping practice, and a code review of `src/`.

## 0. What the evidence says

### Accuracy (London rows, 2026-11-15)

| metric | n | % |
|---|---|---|
| time null | 1416 | 41.5 |
| price null | 2910 | 85.3 |
| url is the venue listing page (≥4 events share it) | 1165 | 34.1 |
| kind one_off / recurring / run | 1726 / 941 / 745 | 51 / 28 / 22 |
| run > 20 days in workshop/talk/tour/concert (weekly series stored as daily run) | 122 | 16% of runs |
| recurring with recurrence = "multiple dates" (no weekday) | 859 | 91% of recurring |
| expired rows still in table | 159 | 4.7 |
| time visible in listing HTML but not stored | 207 | — |
| price visible in listing HTML but not stored | 213 | — |

Error classes, by cause:

| class | cause | where |
|---|---|---|
| weekly series stored as `run`, `recurrence=null` (WordPlay, Family Freestyle, ~160 rows) | prompt rule "emit once as kind=run"; schema has no weekday field; query treats run as daily | `src/extract.py` `_DATE_RULES`, `src/query.py` `_span` |
| "On now until …" tours (V&A 35, Ronnie Scott's, Globe) get start = crawl date | prompt rule `date=today` | `_DATE_RULES` |
| run 20–23 Jan but shows only Wed/Fri/Sat | listing has only the range; no "sessions unknown" state | schema |
| price present in listing, dropped (V&A 62, Wilton's 44) | LLM omission; price sits after venue line; no hint | prompt, no regex fallback |
| time present in listing, dropped (207) | LLM omission / "Various times" | prompt, no regex fallback |
| time/price only on detail page (Green Note 82/82, Sadler's, Kings Place) | listing lacks it; `follow_event_links` used by 3 venues only | `html_sites._expand_event_links` |
| url = listing page (Kings Place 93/93, Angel Comedy, Pleasance) | link follows description in reduced text; LLM falls back to listing | `reduce.py` line builder, `_build_events` |
| 12h/24h conversion | none found | — |

### Coverage (London = 304 of the 391 entries; 87 are parked Moscow)

| bucket | venues | weight |
|---|---|---|
| OK | 210 | 239 |
| zero events (page has text, nothing stored) | 27 | 29 |
| JS-rendered (no event text in HTML) | 30 | 30 |
| disabled | 26 | 36 |
| not scraped (incl. 4 `skip: true`) | 7 | 9 |
| stale > 21 d | 3 | 3 |
| blocked, not disabled | 1 | 1 |

72 London venues yield 0 events. Top-10 venues give 35% of 3,458 events. 54 venues give 1–2 events.

Machine-readable data already in the cached HTML (all London / the 84 failing venues):
JSON-LD Event 21 / 7 · `<time datetime>` 36 / 3 · `__NEXT_DATA__` 4 / 0 · ICS 9 / 4 · RSS 87 / 23 (mostly WordPress post feeds) · **Spektrix refs 46 / 6**.

Verified live (curl, 2026-11-15):
- Cloudflare 403 / challenge: British Museum, Southbank, Royal Academy, Kings Place, The Other Palace, Young Vic. LW Theatres (Palladium) 404.
- **`https://system.spektrix.com/<client>/api/v3/events` returns JSON through Cloudflare** for kingsplace (370 events), theotherpalace, southwarkplayhouse, gate, youngvic, theatre503, bushtheatre, royalcourt, parktheatre, lyrichammersmith, jacksonslane. Items carry `name, firstInstanceDateTime, lastInstanceDateTime, instanceDates, webUrl, description`; `/events/{id}/instances` gives per-session times and prices. Needs a date window and an add-on filter (`attribute_DontIncludeOnWhatsOn`, "Programme £5").
- ATG pages (Aldwych, Dominion) carry JSON-LD `TheaterEvent` with `startDate: "17-11-2026"` (DD-MM-YYYY) — our JSON-LD path exists but stores 0 events for them, so the date parse is failing on that format.
- NHM is live 200 again (`__NEXT_DATA__` has no event dates). 7 Delfont Mackintosh theatres share one JS template with Organization JSON-LD only.
- Zero-event venues with real text to inspect: Guildhall School (18k chars), St Giles (16k), Spitalfields Music, Half Moon, Theatro Technis, The Bedford.

### Code review: patches vs platform

The adapter registry (`src/sources/adapters/`) is the one real extension point
and it is good. Everything else per-venue was bolted onto the orchestrator:

1. **Routing is a hand-written ladder** in `parse_sites()`: follow_event_links →
   JSON-LD → adapter(direct) → thin → LLM. Each rung was added for one venue
   class. The same ladder is duplicated in `fetch_html_sites()` for `--test-site`,
   so the test path and the bulk path can disagree.
2. **sites.yaml is a grab-bag of 20 keys** with no schema: `js_required`,
   `stealth`, `remove_selectors`, `follow_event_links`, `link_pattern`,
   `max_links`, `must_determine_venue`, `adapter`, `cache_version`,
   `category_hint`, `notes`, `delay`, `wait_for`, `skip`, `disabled`, `pipeline`,
   `weight`, `type`, `map`. `pipeline:` doubles as city marker and preset tag.
3. **`notes:` is the de-facto per-venue patch mechanism**: 390 free-text strings
   injected into the LLM prompt. Opaque, untestable, costs tokens on every call,
   and mostly contains location trivia ("Lambeth — regular workshops…") that the
   model does not need.
4. **No post-extraction override hook.** If a venue is bad at dates/times there
   is nowhere to put "this venue's times are on the detail page" or "ranges here
   mean weekly" except an adapter (pre-LLM) or `notes`.
5. **Normalisation is scattered and duplicated.** Date/range/recurrence logic
   lives in `adapters/textutil.py`, `reduce.rec_to_event`, the extract prompt,
   `extract._build_events`, and `classify._RECUR`. Kids/family regexes exist
   twice (`reduce._KIDS_RE` vs `classify._KIDS_T`). `Rec` has no `price`,
   `end_date` or `recurrence` field, so adapters cannot carry a price even when
   the page has one.
6. **Fetch has no status model.** A Cloudflare "Just a moment…" page is stored
   as a successful scrape (Southbank). No ETag / If-Modified-Since. No API or
   feed fetchers.
7. **No evaluation harness.** `scripts/reduce_regress.py` diffs the reducer
   only. No gold set, no per-venue fill-rate tracking, no fixtures.
8. **City is hard-coded in 8 places**: `CITY_TAG`, `CITY_TZ`, `MOSCOW_TAGS`,
   `API_SOURCES if city == "london"`, `lang = "ru"` hack, `'Москва'` in digest,
   Songkick metro id, `_TOWN_LIST` "london" check.

### Research takeaways (sources in agent report; vendor claims flagged there)

- Deterministic beats LLM where available: JSON-LD/microdata Event, `<time datetime>`, ticketing APIs (Spektrix v3 `GET /api/v3/events` + `/instances` is public for web-visible events; Ticketsolve; Ticketmaster Discovery 5k/day), ICS/RSS.
- LLMs are measurably worse at dates than at other fields (relative dates up to 35% worse). Current advice: give today's date + weekday, have the model **copy raw date text**, parse in code, check weekday ↔ date consistency.
- Recurrence: use schema.org `eventSchedule` / RRULE shape (`byDay`, `until`, `startTime`) rather than a free string.
- Self-healing pattern: LLM proposes selectors/config once; deterministic code runs and validates; LLM re-invoked only on validation failure.
- Eval: 20–50 gold records per source, fixtures of raw HTML, per-field fill-rate drift per run; venues dropping to 0 flagged.
- Fetching: plain HTTP with conditional requests first; Patchright for JS; do not build Cloudflare bypasses — use the venue's feed/API or drop it.
- No independent benchmark shows markdown beats HTML for extraction; keep our reducer.

## 1. Target architecture

```
Fetch ──► Source ──► Normalize ──► Enrich ──► Classify ──► Store ──► Query/Rank/Digest
 (registry)  (registry,          (one module,   (detail pass,   (unchanged)
             per-venue list)     deterministic)  conditional)
                         ▲            ▲             ▲
                 venues/<slug>.py hooks + yaml `fix:` block
```

### 1.1 Fetch — registry + status

`src/fetch/`: `http`, `browser`, `browser_stealth`, `api_spektrix`, `feed_ics`,
`feed_rss`. All return

```python
FetchResult(status: ok|blocked|empty|error|not_modified, body, content_type,
            etag, last_modified, fetched_at, note)
```

- Block detection (Cloudflare challenge, "Access denied", captcha, `<noscript>enable JavaScript`) → `blocked`; never overwrites the last good scrape.
- Conditional requests via stored ETag/Last-Modified.
- Status recorded per venue per crawl → coverage report (§3) and digest footer.

### 1.2 Source — ordered per-venue list, all emitting `RawEvent`

Replace the ladder with `extract: [jsonld, adapter, llm]` (default) resolved
through one registry. Built-ins: `jsonld`, `microdata`, `time_tags`,
`next_data`, `adapter:<kind>`, `follow_links`, `spektrix`, `ics`, `llm`.
First source returning ≥1 record wins unless `merge: true`.

`RawEvent` replaces `Rec` and the LLM's dict: **strings only**, as found on the page.

```python
RawEvent(title, date_text, end_text="", time_text="", price_text="", url="",
         desc="", venue="", recurrence_text="", source_kind="")
```

The LLM prompt shrinks to: find events, copy the title, date text, time text,
price text, link. No date arithmetic in the model. Today + weekday in the header.

### 1.3 Normalize — one deterministic module

`src/normalize/` (dates, times, prices, urls, audience). Takes `RawEvent` →
`Event`. Owns every regex now spread over textutil/reduce/extract/classify.

- Dates: range grammar + weekday grammar + `closes/until` + "On now" → start
  `None` (unknown start, not crawl date). Output `start, end, byday: set[int]|None,
  sessions_known: bool`. A range with no weekday and no listed sessions is
  `kind=run, sessions_known=False`.
- Weekday ↔ date consistency check (reject/flag when "Thursday 19 Nov" is not a Thursday).
- Times: current `_TIME_*` regexes + "Various times" → None; price: `Free`,
  `£x`, `£x–£y`, `From £x`.
- URL: nearest per-event link in the reduced block; never the listing URL when
  a per-event link exists within the block.
- Kids/family/age: one copy (from classify).

### 1.4 Enrich — detail-page pass, conditional and capped

For events still missing `time`, `price`, or with `sessions_known=False`, and
only when the venue allows it (`enrich: {fields: [time, price, sessions], max: 20}`):
fetch the event URL (conditional, cached by URL), run `time_tags` /
`jsonld` / a sessions regex (`Thursday 19 November 10 am`), fill the gaps, set
`byday` from the real sessions, mark sold-out. Supersedes `follow_event_links`.

### 1.5 Venue overrides — the sanctioned patch mechanism

Two layers, both registered like adapters today and both covered by fixtures:

**Declarative** (`sites/<city>.yaml`, validated by a `VenueConfig` pydantic model):

```yaml
- name: Sadler's Wells
  url: https://www.sadlerswells.com/whats-on/
  fetch: {mode: http}
  extract: [jsonld, adapter, llm]
  enrich: {fields: [time, price, sessions], max: 30}
  fix:
    range_means_weekly: true       # a bare date range is a weekly series, not a daily run
    time_default: null
    date_order: dmy
    url_pattern: "/whats-on/[^/]+/$"
    drop_title_re: "^Gift Vouchers"
  tags: [theatres, family]
  weight: 3
  hint: "Mostly dance; family workshops are on the East site."   # LLM-facing, optional
```

**Python** (`src/venues/<slug>.py`), when YAML cannot express it:

```python
@venue("sadlers_wells")
class SadlersWells(VenueHooks):
    def records(self, html, cfg) -> list[RawEvent]: ...   # optional adapter
    def post(self, events, cfg) -> list[Event]: ...        # optional fix-up
```

`notes:` is retired. Location trivia moves to `map`/`type`; anything the LLM
truly needs becomes `hint:` (measured: 390 notes ≈ 15–25k prompt tokens per
full crawl).

### 1.6 Config & city split

- `cities.yaml`: `london: {tz: Europe/London, lang: en, label: "London Events", sites: sites/london.yaml, api_sources: [skiddle, songkick], songkick_metro: 24426}`; `moscow: {...}`.
- `sites/london.yaml`, `sites/moscow.yaml`. `pipeline:` → `tags:`. `CITY_TAG`, `MOSCOW_TAGS`, the `lang=ru` hack and `'Москва'` all come from `cities.yaml`.
- `--test-site` becomes a filter on the same pipeline; `fetch_html_sites()` is deleted.

### 1.7 Evaluation harness

- `fixtures/<venue>/page.html` + `gold.json` for ~30 venues across the error
  classes above (bootstrapped by a Sonnet agent reading detail pages, then
  spot-checked by hand). `pytest` runs Source+Normalize over fixtures and
  reports per-field precision/recall; CI fails on regression.
- `main.py eval`: fill-rate per venue per field + event count vs last crawl;
  flags venues at 0 or −50%. Written to `cache/eval.jsonl`, summarised in the
  digest footer next to the stale list.

## 2. Accuracy work items

| # | item | fixes | files |
|---|---|---|---|
| A1 | `byday` on `Event`; `run` may carry it; `_span`/`_days_ok` honour it | weekly-as-daily (~160 rows) | models, query, digest |
| A2 | prompt: range + weekday/"every" → `recurrence_text`; "On now" → `start=None`; today + weekday in header | WordPlay/Freestyle/V&A classes | extract |
| A3 | `sessions_known=False` for a >7-day run in workshop/talk/tour/concert with no weekday; query shows it with a "check days" badge and rank penalises it | range-only listings | normalize, query, rank |
| A4 | price regex over the reduced block + prompt hint ("£/Free after the date or venue line") | V&A, Wilton's (~300) | normalize, extract |
| A5 | time regex fallback over the block | 207 missed times | normalize |
| A6 | nearest-link URL assignment; post-fill when LLM returns the listing URL | 1165 rows | reduce/normalize |
| A7 | enrich pass (§1.4) for time/price/sessions | Green Note, Sadler's, Kings Place | new |
| A8 | prune expired rows at `save_crawl`; replace "multiple dates" with weekday/time | 159 rows, 859 labels | events_db, normalize |

## 3. Coverage work items

| # | item | est. gain | files |
|---|---|---|---|
| C1 | `FetchResult.status`; blocked pages never overwrite good cache; status in digest footer | stops cache poisoning | fetch |
| C2 | **Spektrix source**: fingerprint `spektrix.com`/`client-name` in HTML → `/api/v3/events` + `/instances` (dates, times, prices, sold-out) → `RawEvent`, no LLM. 11 clients verified; 6–7 venues go from 0 to live, Kings Place leaves `disabled` | +150–300 events | sources/spektrix.py |
| C3 | JSON-LD: accept DD-MM-YYYY (ATG), add `microdata`, `time_tags`, `next_data` sources | +30–80 | sources |
| C4 | other platforms by fingerprint: Ticketsolve (OSO, Watermans), Eventbrite org pages (LTM), ATG/LOVEtheatre feed for the 7 Delfont Mackintosh houses | +20–40 | sources |
| C5 | ICS discovery (`<link rel=alternate type=text/calendar>`, `/events.ics`): Ealing Culture, Chiswick Calendar, Old Red Lion, The Bedford; RSS only when the feed is an events feed | +20–40 | fetch |
| C6 | Patchright fetcher for JS pages; retest the 26 disabled (NHM, NPG, Young Vic, Ronnie Scott's are live again); Cloudflare-blocked weight-3 venues (Southbank, British Museum, Royal Academy) only via their own feeds/APIs or a sanctioned render — no bypasses | +80–150 | fetch, sites |
| C7 | inspect the 27 zero-event venues that have text (Guildhall, St Giles, Spitalfields, Half Moon …) — likely reducer or filter loss, cheap wins | +20–60 | eval |
| C8 | conditional requests (ETag / If-Modified-Since); fix dead URLs (Palladium) | politeness, speed | fetch, store |

## 4. New city = a skill, not code

`.claude/skills/new-city/SKILL.md`, invoked as `/new-city <name>`. The pipeline
code is not touched; the skill writes `cities.yaml` + `sites/<city>.yaml` +
optional `src/venues/<city>/*.py` and drives the existing CLI.

Steps the skill encodes:

1. `cities.yaml` entry: tz, lang, label, API sources available there.
2. Seed venues: OSM Overpass (`amenity=theatre|arts_centre|music_venue`, `tourism=museum|gallery`) + Wikidata P856 (official site) + web search for "<city> what's on" aggregators. Rank by size/weight; cap ~80 for a visit.
3. For each venue: fetch home page, locate the what's-on URL, fingerprint platform (Spektrix/Ticketsolve/JSON-LD/feeds), pick `fetch` + `extract`, write the YAML entry. Reuses the existing `add-venue` and `validate-venues` skills.
4. `main.py crawl --city <name> --test-site …` per venue; when `auto` yields a thin page, the agent proposes an `adapter: {kind: cards, …}` config, validates it against the saved page (≥N records with parseable dates), and only then commits it to YAML.
5. Mini gold set (5–10 venues) → `fixtures/<city>/`; `main.py eval --city <name>`.
6. Full crawl, `query --city <name>`, report coverage buckets and stale/blocked list.

Moscow is migrated onto this path first as the proof (it already has a staging YAML and `_check_moscow.py`).

## 5. Phasing

| phase | scope | outcome |
|---|---|---|
| **P0** (days) | A1, A2, A3, A6, A8, C1, C3 date-format fix, **C2 Spektrix** (self-contained adapter, highest verified ROI) — minimal schema change, no rearch | the visible bugs stop; blocked pages stop poisoning cache; ~10 venues come back |
| **P1** (1–2 wks) | `RawEvent` + `normalize/` + `VenueConfig` + Source registry; delete `fetch_html_sites`; retire `notes`; fixtures + `eval` | one code path; per-venue fixes have a home; regressions caught |
| **P2** | A4, A5, A7 enrich pass; C4–C8 | time/price fill rate up; coverage up |
| **P3** | `cities.yaml` + sites split; `/new-city` skill; Moscow migrated | a new city in an afternoon |

Costs and risks:
- Any prompt edit invalidates `_PROMPT_VERSION` → full re-extraction on next crawl (≈1 DeepSeek call per LLM venue).
- Enrich pass adds requests; capped per venue, cached by URL, conditional.
- Adapter → auto fallback currently masks failures silently; `eval` must log it.
- `dateparser` is a new dependency; alternative is to extend `textutil` grammar.

Decisions (2026-11-15): own date grammar on `python-dateutil` (already used; RU months in `textutil`; `dateparser` is slower and ambiguous on ranges) · Patchright replaces crawl4ai (same Playwright API, one layer fewer) · `notes` → `about` (never sent to the LLM); a migration moves extraction-relevant text into `hint`.

## 6. Execution log

Branch `rewrite/pipeline-v3`. Each step: Opus builder → Opus reviewer → tests → commit.

| step | scope | status |
|---|---|---|
| 1a | `src/normalize/` dates/times/prices/urls + tests | committed 26e6389 (561 tests) |
| 1b | `Event` fields, `RawEvent`, `FetchResult`, `VenueConfig`, `cities.yaml`, `load_venues` | committed 1450e11 |
| 2a-i | `src/sources/base.py` (Source protocol, SourceCtx, registry), `build.py` (RawEvent→Event via normalize), sources `jsonld`, `adapter`, `follow_links` + tests | done (review: 4 HIGH fixed — open-ended series kept live, no date pre-filter in sources, build fully exception-safe, `_PARTIAL_AFTER` regex; RawEvent +category/audience/age_text; SourceCtx.force; safe_records; CityConfig.currency; jsonld 37→111 events on cache vs legacy, 0 lost) |
| 2a-ii | `llm` source with raw-string prompt; `crawl_sites` = fetch_all once → `parse_venue` over `cfg.extract`; legacy fetch/ladder deleted; `--test-site` as filter; EXTRACT_VERSION 8 | done 6589ea1 (review: spektrix auto-promotion gated behind `extract`; slot collapse in spektrix source — Barbican 3,196→251; shared title→anchor matcher; offline `--parse-only`; BLOCKED fallback body parsed + stale; 824 tests) |
| 2b | Spektrix source (43 London clients verified) + platform fingerprint | done f6a08ca (bulk `/instances`, startTo exclusive→end+1, `sold_out`, scored fingerprint, add-on rules; Barbican 201 ev/3,244 sessions in 32 s — timed-entry slots must collapse downstream in step 4) |
| 3 | `src/fetch/` http (conditional, block detection) + Patchright browser; scrape status | done b24478d, wired in 6589ea1 (per-loop browser state, per-call throttle, cached-body fallback ≤4×TTL, `FetchCfg.gap`; stealth does NOT pass Southbank 403 — feeds/APIs only) |
| 4 | query/rank/digest honour `byday` / `sessions_known`; prune expired | done ddb3d5f (series contract: recurring+byday = series, instance = recurring+byday None; event_days via occurrences; schedule_label EN/RU; prune_past keeps open series/TBC; A3 baseline: 156 range-only runs in cache) |
| 5 | venue hooks + `fix:` block; `notes` migration; `sites/<city>.yaml`; city config everywhere | done c66678a (865 tests): hook registry bound via explicit `hook:` only, no built-in hooks shipped (sadlers_wells → config, green_note → hint + `build.kept_variant`); `fix:` byday/weekly/time_default/range_means_weekly (bool or {min_days,title_re}); `sites.yaml` deleted (slipped into 406faaa), London 300 / Moscow 90, St Paul's dupe merged; `notes` → `about` + `hint` (28 hints); city = file membership; weekday-range-beside-date-range → byday; Southbank WordPlay pinned Fri via fix.weekly; Sadler's Wells ranges stay sessions_known=False until step 6 |
| 6 | enrich pass (detail pages) | |
| 7 | fixtures + gold + `main.py eval` | built: 14 venues / 92 gold events, offline harness, drift, KNOWN_FAILURES (45); first-run F1 date .98 time .98 price .67 url .83 byday .40; review running |
| 8 | `/new-city` skill; Moscow migrated | |
| 9 | full crawl; before/after metrics | |

Shared contract both 2a/2b use: `Source.records(cfg: VenueConfig, fr: FetchResult | None, ctx: SourceCtx) -> list[RawEvent]`; `SourceCtx(http, today, window_days, city, stats, errors, force)`; `register_source`/`SOURCES`/`safe_records` in `src/sources/base.py`; `HEADERS` in `src/fetch/headers.py`. RawEvent string fields: title, date_text, end_text, time_text, price_text, recurrence_text, url, desc, venue, source_kind, sold_out, category_text, audience_text, age_text.
