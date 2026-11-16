---
name: validate-venues
description: |
  Validate all venues in sites/<city>.yaml — HTTP-check every URL, report dead links,
  suggest marking broken ones skip:true. Trigger: "validate venues", "check urls",
  "which venues are broken", "/validate-venues".
---

# Venue Validator

Checks every URL in `sites/<city>.yaml` with real HTTP requests. Reports dead/unreachable
venues so you can mark them `skip: true` or update the URL.

## Run it

```bash
cd /Users/dtonkonog/code/london_events
/Users/dtonkonog/code/trading_env/bin/python3 scripts/validate_venues.py
```

## Options

```bash
# Only print failures (quiet mode):
python3 scripts/validate_venues.py --dead-only

# Also check venues already marked skip:true:
python3 scripts/validate_venues.py --fix-skip

# Both:
python3 scripts/validate_venues.py --dead-only --fix-skip
```

## What it checks

- HTTP HEAD then GET fallback for each URL
- Follows redirects (up to httpx default)
- 15s timeout per venue
- 8 concurrent requests
- Reports status code, TIMEOUT, REDIRECT_LOOP, or exception type

## After running

For each dead venue:
1. Try opening the URL manually to find new events page
2. If new URL found: update the venue in `sites/<city>.yaml`
3. If venue defunct: set `skip: true` with a note explaining why
4. Run again to confirm fixed

## Notes

- JS-required sites (`js_required: true`) may return 200 even if event content
  needs JS — a 200 only means the page loads, not that events are visible
- Some sites block HEAD requests and return 405/403 — the script falls back to GET
- Cloudflare-protected sites may return 403 even when valid; check manually if unsure
