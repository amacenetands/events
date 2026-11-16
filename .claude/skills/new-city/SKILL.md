---
name: new-city
description: |
  Spin up a new city for the events pipeline with config only: cities.yaml entry, seeded and
  verified sites/<city>.yaml, a mini gold set, then a first crawl + coverage report. No pipeline
  code edits. Trigger: "/new-city <name>", "add city", "new city", "add a city", "set up <city>".
---

# New city

Input: a city name (`<city>` below: lowercase ASCII key, e.g. `paris`). Moscow was migrated
onto exactly this path (cities.yaml + sites/moscow.yaml, nothing else) and is the worked
example to copy from.

Rules for the whole run:
- Touch only `cities.yaml`, `sites/<city>.yaml`, `tests/gold/<slug>/` and, when YAML cannot
  express a fix, `src/venues/<city>_*.py` hooks (docs/venue-hooks.md). Never edit `src/` pipeline code.
  If a step seems to need a code change, stop and report it to the user instead.
- Network: single-venue fetches only (`--test-site`), max 8 concurrent checks. Run the full crawl once, at step 6.
- Bot walls (Cloudflare, Qrator, DDoS-Guard, captcha): never try a bypass. Look for a feed,
  an ICS export, a ticketing platform (Spektrix/Ticketsolve), or an aggregator page instead;
  otherwise `disabled: true` + `disabled_reason:`.
- Keep a running list of what needs a human (step 7). Do not guess where a person must check.

## 1. City entry

Add `<city>:` to `cities.yaml` from the commented template at the bottom of that file:
`label, display_name, tz, lang, currency, pricey, sites: sites/<city>.yaml, geo: [lat, lon],
api_sources`.

- `lang` must be one the date grammar knows (`Lang` in `src/normalize/dates.py`: en, ru).
  If it is not, **stop**: tell the user a locale table must be added to `src/normalize/dates.py`
  first (month names, short months, weekday names, "from / until / every" words). Without it
  every local-language date is silently lost. `--check` rejects an unknown `lang`.
- Venues belong to the city whose `sites/<city>.yaml` lists them: no city tag needed. Optional:
  `parked_tags` for venue groups crawled only on request (`--tag <tag>`), listed in
  `require_tags` too so `--tag <tag>` alone finds the city (Moscow's `moscow-theatre`).
- `api_sources`: `skiddle` (UK listings, needs `geo`), `songkick` (needs `songkick_metro`,
  the metro id from songkick.com/metro-areas/<id>). Use `[]` when neither covers the city.
- Create `sites/<city>.yaml` with a header comment and `venues: []`.

```bash
venv/bin/python -m src.config --check
```
Accept: exit 0 and a `<city>: 0 entries` line.

## 2. Seed venues

```bash
venv/bin/python scripts/seed_venues.py --city <city> --lat <lat> --lon <lon> --radius-km 8
```
Queries OSM Overpass (theatres, arts centres, music venues, concert halls, cinemas, museums,
galleries) and Wikidata (P856 official website, sitelinks as notability), drops hosts already
in `sites/<city>.yaml`, ranks, caps at 80 → `cache/seed/<city>.yaml` (gitignored; review only).
Offline/replay: `--overpass-json FILE --wikidata-json FILE`.

Then run every query in the file's "Web-search checklist" header (WebSearch) for listings
aggregators and festivals OSM cannot see (city what's-on guides, the municipal culture
portal, ticketing hubs). Add the useful ones as candidates by hand.

Each candidate carries review-only keys (`note, shared_host, score, kind, wikidata, sitelinks,
osm, lat, lon, alt_names`): strip them when copying an entry into `sites/<city>.yaml`.
`note` flags a venue whose only web presence is a social/portal page (url left null);
`shared_host` flags two venues on one site (two halls, or a shared portal): check both.
Set `SEED_CONTACT` (a URL or email) to put contact info in the User-Agent (Wikimedia policy).

Accept: a reviewed shortlist (aim 40–80) covering concerts, theatre, museums/galleries,
talks, family. Show it to the user before step 3 if it is under 30 or has no family venues.

## 3. Per venue: entry

For each shortlisted venue:

1. Find the what's-on/afisha URL from its home page (WebFetch / curl). Prefer the listing
   that shows dates on the page itself, in the local language.
2. Fingerprint the page and get a first fetch/extract guess:
   ```bash
   venv/bin/python scripts/validate_venues.py --url "<whats-on url>" --city <city>
   ```
   Prints `platform` (Spektrix/Ticketsolve/Eventbrite/DICE via `src/sources/platforms.py`),
   JSON-LD event count, `<time>` tags, SSR data, RSS/Atom/ICS feeds, bot wall, and a
   `suggest: fetch=… extract=…`.
3. Write the entry in `sites/<city>.yaml` (schema: `src/config.py` VenueConfig; see the
   `add-venue` skill):
   ```yaml
   - name: <local name>
     url: <whats-on url>
     tags: [family]            # optional, omit if none: family / museums / theatres … (query --venue-tag)
     category_hint: concert    # theatre|concert|exhibition|lecture|gallery|kids|festival|comedy|other
     fetch: {mode: browser}    # omit for http; browser when the page is a thin JS shell
     extract: [jsonld, llm]    # from `suggest`; default [jsonld, adapter, llm]
     hint: "…"                 # LLM-facing only: where dates/times/prices are, what to skip
     about: "…"                # human notes, never sent to the LLM
   ```
   Feeds: an ICS or RSS feed beats HTML; Spektrix → `extract: [spektrix, jsonld, llm]`.
4. After each batch:
   ```bash
   venv/bin/python -m src.config --check --city <city>
   venv/bin/python scripts/validate_venues.py --city <city>
   ```
   Accept: config check exit 0; no `DEAD` venue left (fix the URL, or `disabled: true` +
   `disabled_reason:`). `WARN` 403/bot wall → find a feed/platform/aggregator or disable it.
   `bad TLS cert` → note it for the human list; the crawler does not skip cert checks.

## 4. Per venue: extraction check

```bash
venv/bin/python main.py crawl --city <city> --test-site "<venue name>"
```
Prints every extracted event (nothing is saved). Accept: events have title, a real date,
time where the page shows one, a per-event URL where the page links one.

When `auto` gives 0 events or a thin page, propose a cards adapter from the stored page and
validate it offline before writing it:
```bash
venv/bin/python scripts/validate_venues.py --city <city> --adapter-check "<venue name>" \
    --adapter '{kind: cards, card: "li.event", title: "h3", date: ".date", time: ".time", link: "a"}'
```
Accept: `OK` (≥ 3 records, ≥ 80% with a parseable date). Only then add the `adapter:` block
to the venue, and re-run `main.py crawl --city <city> --test-site "<venue name>" --parse-only`.
Inline JSON blobs: `{kind: json, script: …, path: …, title: …, date: …}` (src/sources/adapters/README.md).
Systematic date/time quirks go in `fix:` (`date_order`, `time_default`, `range_means_weekly`,
`weekly`, `drop_title_re`, `url_pattern`), not in code.

## 5. Mini gold set (at least 5 venues, up to 10)

Pick venues of different shapes (JSON-LD, adapter, LLM, browser). For each, after a
`--test-site` run has stored the page:
```bash
venv/bin/python main.py eval --capture <slug> --url "<whats-on url>" --city <city> [--keep CSS --limit N]
```
That writes `tests/gold/<slug>/page.html` and a `meta.yaml` stub (`city: <city>`). Fill in
`source`/`notes`, write `expected.yaml` by reading the page (an `evidence:` quote per event;
conventions in tests/gold/README.md). LLM venues: `scripts/eval.py --record-llm <slug>` (one live call).
```bash
venv/bin/python main.py eval --city <city>
```
Accept: at least 5 gold venues for the city (`eval --city <city>` exits 1 with a warning
when there are none); runs offline; every mismatch understood. Then add each remaining mismatch to
`tests/gold/KNOWN_FAILURES.yaml`, run `scripts/eval.py --update-baseline`, and
`venv/bin/python -m pytest -q tests/test_eval.py` must pass.

## 6. Full crawl, query, coverage

```bash
venv/bin/python main.py crawl --city <city>
venv/bin/python main.py query --city <city> --when next:14 --quiet
venv/bin/python scripts/validate_venues.py --city <city> --status
```
`--status` buckets every active venue from the last crawl: `ok`, `zero_events`, `stale`,
`error`, `blocked`, `no_fetch_row`. Accept: report the buckets to the user; each
`zero_events` / `blocked` venue gets a next step (adapter, feed, disable) or goes on the human list.

## 7. Hand-off: what needs a human

Always list these for the user, with venue names:
- A `lang` with no table in `src/normalize/dates.py` (step 1): blocks the city until added.
- Weekday/recurrence claims (`fix.weekly`, `fix.byday`, `range_means_weekly`): confirm on the
  venue's own page or by phone; the pipeline cannot tell "Fridays" from a run's end days.
- Bot-walled or geo-blocked venues: need a feed/API/partner listing, not a scraper workaround.
- Sites with bad TLS certificates, squatted or moved domains, venues that look closed.
- Gold expected.yaml spot-check (a person reads 2–3 venues against the live page).
- Prices in a currency or format `normalize/prices.py` does not parse (shows as null).

## Done when

`python -m src.config --check` and `venv/bin/python -m pytest -q` are green, the city crawls and
queries, gold runs via `main.py eval --city <city>`, and the user has the coverage report + human list.
