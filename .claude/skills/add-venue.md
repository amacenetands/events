---
name: add-venue
description: |
  Add a new venue to sites.yaml with auto-generated remove_selectors.
  Trigger: "add venue", "add [venue name] to pipeline", "/add-venue".
---

# Add Venue

Steps when user asks to add a new venue to the events pipeline:

## 1. Add entry to sites.yaml

```yaml
- name: "Venue Name"
  url: "https://venue.co.uk/whats-on/"
  category_hint: "concert"   # concert | exhibition | theatre | family | other
  js_required: false         # set true if site uses JS rendering
  skip: false
  pipeline: family           # optional: family, museums — omit for general
  notes: "Brief description of what this venue does"
```

## 2. Validate URL reachable

```bash
python scripts/validate_venues.py --venue "Venue Name"
```

If 403: likely JS-rendered or Cloudflare-blocked — set `js_required: true`.

## 3. Generate remove_selectors

```bash
# For static (non-JS) sites:
python scripts/gen_selectors.py --venue "Venue Name" --dry-run
# Review, then apply:
python scripts/gen_selectors.py --venue "Venue Name"
```

For JS sites (crawl4ai), generate selectors manually:
- Inspect the page in browser DevTools
- Identify non-event divs: nav menus, cookie bars, hero banners, newsletter widgets
- Add to sites.yaml under `remove_selectors:`

```yaml
  remove_selectors:
    - "div.cookie-notice"
    - ".hero-banner"
    - "#newsletter-popup"
```

## 4. Test extraction

```bash
python main.py --test-site "Venue Name"
```

Each extracted event MUST have all five fields populated:
- **title** — event name
- **date** — specific date (not just "upcoming" or a month)
- **time** — start time (e.g. 7:30pm) — "TBC" acceptable only if genuinely absent on the page
- **url** — direct link to the individual event page, not the whats-on listing root
- **description** — brief summary of what the event is

If any of these are missing for most events, the page structure needs investigation — the LLM extraction prompt may need a `notes:` hint in sites.yaml explaining where dates/times appear on this site.

If 0 results:
- Check if site needs `js_required: true`
- Inspect fetched markdown: add `print(text[:2000])` in extract_events_from_text
- Check date range: events > 14 days out won't appear (use `--family` for 30-day window)

## 5. Regenerate all selectors (after bulk venue additions)

```bash
python scripts/gen_selectors.py --dry-run   # preview
python scripts/gen_selectors.py             # apply
```

## Required event fields

Every extracted event must have all five fields. If a venue's page omits any, add a `notes:` hint:

```yaml
  notes: "Dates shown as 'Sat 12 Jul' in .event-date span. Times in .event-time. Each card links to /events/{slug}"
```

The extraction LLM reads `notes` before parsing — a specific hint about where dates/times/links appear
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
2. Note where on the page dates, times, and per-event links appear — this goes in `notes:`.

Return JSON: {"remove_selectors": [...], "notes": "..."}
```
