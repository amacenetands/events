---
name: london-events
description: |
  Run the weekly London events digest. Scrapes ~134 venues + 2 event APIs
  (Skiddle, Songkick), extracts events via local LLM (Ollama qwen3:8b,
  switchable to Claude Haiku), ranks heuristically, emails a digest to
  dtonkonog@gmail.com.
---

Run the London events digest:

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

Edit `/Users/dtonkonog/code/london_events/sites.yaml`:

```yaml
- name: "Venue Name"
  url: "https://venue.com/events"
  category_hint: "concert"   # concert | exhibition | kids | gallery | theatre | other
  js_required: false         # true if JS needed to render events
  skip: false
  notes: "Optional context for Claude extraction"
```

## Troubleshooting

- **No events from a site**: run `--test-site "Name"` to debug
- **Email not sending**: check GMAIL_USER + GMAIL_APP_PASSWORD in `.env`
- **Check dedup DB**: `sqlite3 /Users/dtonkonog/code/london_events/cache/memory.db "SELECT fingerprint, seen_date FROM event_state WHERE seen_date IS NOT NULL ORDER BY seen_date DESC LIMIT 20"`
- **Reset seen events**: `sqlite3 /Users/dtonkonog/code/london_events/cache/memory.db "UPDATE event_state SET seen_date=NULL"`
- **Storage**: one SQLite DB at `cache/memory.db` — tables `scrapes` (raw HTML), `extractions` (parsed LLM results), `event_state` (seen + exhibition tags). Raw caches regenerate on a 3-day TTL.
