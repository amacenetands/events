---
name: london-events
description: |
  Run the weekly London events digest. Scrapes ~134 venues + 2 event APIs
  (Skiddle, Songkick), extracts events via local LLM (Ollama qwen3:8b,
  switchable to Claude Haiku), ranks heuristically, emails a digest to
  dtonkonog@gmail.com.
---

Crawl and query are now separate (see also the `events` skill for the interactive flow):

```bash
cd /Users/dtonkonog/code/london_events
venv/bin/python main.py crawl --city london [--parse-only|--fetch-only|--force-download|--force-parse|--test-site "Name"]
venv/bin/python main.py query --city london --when next:7 --audience family --kinds one_off,run --time any --dry-run
# add --send to email (default prints only); --preset family|evening|daytime|museums|theatres|standard|moscow
```

Legacy one-shot (crawl cached sites then query with the preset; emails unless --dry-run):

```bash
/Users/dtonkonog/code/london_events/run.sh 2>&1 | rtk log
```

## Options

- **Dry run** (no email, no dedup marking):
  ```bash
  /Users/dtonkonog/code/london_events/run.sh --dry-run 2>&1 | rtk log
  ```

- **Test a single venue**:
  ```bash
  /Users/dtonkonog/code/london_events/run.sh --test-site "Café Oto" 2>&1 | rtk log
  ```

- **Send test email** (no dedup marking):
  ```bash
  /Users/dtonkonog/code/london_events/run.sh --send-test 2>&1 | rtk log
  ```

## Adding new venues

Edit `/Users/dtonkonog/code/london_events/sites/london.yaml` (or `sites/moscow.yaml`):

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

## Troubleshooting

- **No events from a site**: run `--test-site "Name"` to debug
- **Email not sending**: check GMAIL_USER + GMAIL_APP_PASSWORD in `.env`
- **Check dedup DB**: `sqlite3 /Users/dtonkonog/code/london_events/cache/memory.db "SELECT fingerprint, seen_date FROM event_state WHERE seen_date IS NOT NULL ORDER BY seen_date DESC LIMIT 20"`
- **Reset seen events**: `sqlite3 /Users/dtonkonog/code/london_events/cache/memory.db "UPDATE event_state SET seen_date=NULL"`
- **Storage**: one SQLite DB at `cache/memory.db` — tables `scrapes` (raw HTML), `extractions` (parsed LLM results), `event_state` (seen history), `events` (latest crawl per city, one row per fingerprint + first_seen), `crawls` (freshness). Presets live in `pipelines.yaml` as saved filters (src/query.py `Filter`). Raw caches regenerate on a 3-day TTL.
