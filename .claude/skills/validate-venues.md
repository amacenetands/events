---
name: validate-venues
description: |
  Validate venues in sites/<city>.yaml — HTTP-check every URL, report dead links and bot walls,
  fingerprint pages, show last-crawl fetch status per venue. Trigger: "validate venues",
  "check urls", "which venues are broken", "/validate-venues".
---

# Venue Validator

`scripts/validate_venues.py` (replaces the old `_check_moscow.py`).

## Run it

```bash
cd /Users/dtonkonog/code/london_events
venv/bin/python scripts/validate_venues.py --city london          # one city (default: every city)
venv/bin/python scripts/validate_venues.py --city moscow --dead-only
venv/bin/python scripts/validate_venues.py --venue "Barbican" --probe     # platform / JSON-LD / feeds + suggestion
venv/bin/python scripts/validate_venues.py --url "https://…/whats-on" --city london   # page not in YAML yet
venv/bin/python scripts/validate_venues.py --city moscow --status        # offline: last crawl, per venue
venv/bin/python scripts/validate_venues.py --city london --adapter-check "V&A"   # offline: adapter on stored page
venv/bin/python scripts/validate_venues.py --fix-skip                    # also skip:true / disabled venues
```

## What it checks

- GET with redirects, 20s timeout, 8 concurrent, Accept-Language from the city's `lang`
- OK / WARN (403, 429, 503, bot wall, bad TLS cert) / DEAD (404, DNS, timeout, other errors)
- Redirects are printed (`→ final url`): update the YAML when the move is permanent
- Browser-mode venues are fetched over plain HTTP too: 200 only means the host is up
- `--status` (no network): buckets `ok`, `zero_events`, `stale` (>7d), `error`, `blocked`,
  `no_fetch_row` from the scrapes + events tables

## After running

For each dead venue:
1. Open the venue's home page and find the new listing URL; update `sites/<city>.yaml`
2. Defunct / squatted domain: `disabled: true` + `disabled_reason:`
3. Bot wall: look for a feed, ICS, ticketing platform or aggregator; never a bypass
4. Re-run to confirm, then `venv/bin/python main.py crawl --city <city> --test-site "<name>"`
