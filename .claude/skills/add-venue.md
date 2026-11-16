---
name: add-venue
description: |
  Add a new venue to sites/<city>.yaml with auto-generated remove_selectors.
  For a whole new city use the new-city skill.
  Trigger: "add venue", "add [venue name] to pipeline", "/add-venue".
---

# Add Venue

Steps when user asks to add a new venue to the events pipeline:

## 1. Add entry to sites/<city>.yaml (sites/london.yaml, sites/moscow.yaml)

```yaml
- name: Venue Name
  url: https://venue.co.uk/whats-on/
  tags: [family]             # optional: family, museums, theatres, evening, daytime
  category_hint: concert     # theatre | concert | exhibition | lecture | gallery | kids | festival | comedy | other
  fetch: {mode: browser}     # omit for plain http; browser / browser_stealth for JS sites
  hint: "Times are on the detail page"   # optional, sent to the LLM: format/what to skip only
  about: "Brief human description"       # never sent to the LLM
```
Then `venv/bin/python -m src.config --check`.

## 2. Validate URL reachable

```bash
venv/bin/python scripts/validate_venues.py --city <city> --venue "Venue Name" --probe
```

`--probe` also prints the ticketing platform, JSON-LD event count, feeds and a fetch/extract
suggestion (`--url URL` probes a page before it has an entry). If 403 / bot wall: look for a
feed or platform API first; `fetch: {mode: browser}` only helps JS-rendered pages, not walls.

## 3. Generate remove_selectors

```bash
# For static (non-JS) sites:
python scripts/gen_selectors.py --venue "Venue Name" --dry-run
# Review, then apply:
python scripts/gen_selectors.py --venue "Venue Name"
```

For JS sites (browser fetch), generate selectors manually:
- Inspect the page in browser DevTools
- Identify non-event divs: nav menus, cookie bars, hero banners, newsletter widgets
- Add to the venue in sites/<city>.yaml under `fetch: {remove_selectors: [...]}`

```yaml
  remove_selectors:
    - "div.cookie-notice"
    - ".hero-banner"
    - "#newsletter-popup"
```

## 4. Test extraction

```bash
venv/bin/python main.py crawl --city <city> --test-site "Venue Name"
```
Thin page / 0 events → test a cards adapter on the stored page before writing it:
`venv/bin/python scripts/validate_venues.py --city <city> --adapter-check "Venue Name" --adapter '{kind: cards, card: …, title: …, date: …}'`.

Each extracted event MUST have all five fields populated:
- **title** — event name
- **date** — specific date (not just "upcoming" or a month)
- **time** — start time (e.g. 7:30pm) — "TBC" acceptable only if genuinely absent on the page
- **url** — direct link to the individual event page, not the whats-on listing root
- **description** — brief summary of what the event is

If any of these are missing for most events, the page structure needs investigation — the LLM extraction prompt may need a `hint:` in sites/<city>.yaml explaining where dates/times appear on this site.

If 0 results:
- Check if the site needs `fetch: {mode: browser}`
- Re-parse the stored page without fetching: add `--parse-only`

## 5. Regenerate all selectors (after bulk venue additions)

```bash
python scripts/gen_selectors.py --dry-run   # preview
python scripts/gen_selectors.py             # apply
```

## Required event fields

Every extracted event must have all five fields. If a venue's page omits any, add a `hint:`:

```yaml
  hint: "Dates shown as 'Sat 12 Jul' in .event-date span. Times in .event-time. Each card links to /events/{slug}"
```

The extraction LLM reads `hint` before parsing (`about` is never sent) — a specific hint about where dates/times/links appear
on that venue's page dramatically improves extraction accuracy.

## LLM prompt for manual selector identification

When inspecting a venue page manually, use this prompt to Claude:

```
I'm adding [VENUE NAME] ([URL]) to a London events scraper.
The scraper fetches the whats-on page, strips HTML boilerplate, converts to markdown, 
then sends to an LLM to extract event listings.

Each event must have: title, date, time, direct URL to the event page, and description.

Please:
1. Suggest CSS selectors for non-event elements (nav, cookie banners, hero images, 
   newsletter forms) to add to `remove_selectors`.
2. Note where on the page dates, times, and per-event links appear — this goes in `hint:`.

Return JSON: {"remove_selectors": [...], "hint": "..."}
```
