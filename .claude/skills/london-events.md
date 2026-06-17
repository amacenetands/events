---
name: london-events
description: |
  Run the weekly London events digest. Scrapes 25+ venues + 3 APIs, 
  uses Claude to extract and rank events, emails top 20 to dtonkonog@gmail.com.
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
- **Check dedup DB**: `sqlite3 /Users/dtonkonog/code/london_events/memory.db "SELECT * FROM seen_events ORDER BY seen_date DESC LIMIT 20"`
- **Reset seen events**: `sqlite3 /Users/dtonkonog/code/london_events/memory.db "DELETE FROM seen_events"`
