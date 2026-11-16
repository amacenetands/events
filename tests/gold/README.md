# Gold set (pipeline v3 §1.7)

One directory per venue. `src/eval` runs the crawler's own `html_sites.parse_venue` on
`page.html` offline and scores every field against `expected.yaml`.

    venv/bin/python scripts/eval.py                 # table + every mismatch
    venv/bin/python scripts/eval.py --baseline      # exit 1 on drift vs baseline.json
    venv/bin/python main.py eval --only cafe-oto --show

## Files

| file | content |
|---|---|
| `meta.yaml` | `name` (looks up the venue's `sites/<city>.yaml` entry), `url`, `city`, `extract`, `source` (expected winning kind), `today`, `venue:` (explicit overrides on top of the sites entry), `details:` {URL path: file} for follow_links, `spektrix_fixtures:` dir, `notes` (source of truth) |
| `page.html` | listing page from `cache/memory.db` scrapes, trimmed (≤ 40 KB; scripts/styles stripped except JSON-LD and adapter payloads) |
| `expected.yaml` | gold events, each with an `evidence:` quote from the page |
| `llm_reply.json` | `{text_hash, records}` — recorded LLM engine records for this page (llm venues) |
| `details/*.html` | detail pages served to follow_links (≤ 40 KB) |

Top level: `KNOWN_FAILURES.yaml` (current pipeline errors), `baseline.json` (drift reference).

## Gold conventions

- **today** = the fetch date of `page.html` (scrapes.fetched_at). Every source, adapter and
  the LLM reducer get this date; nothing reads the wall clock.
- **Price** is compared as one number: the lowest *general-admission* tier on the page
  (standard / advance / door / DICE). Member, concession, student, under-N / over-N, access
  and other restricted tiers are excluded, as are parentheticals and booking/transaction fee
  notes (`£15.50 (£14 + £1.50 transaction fee)` = 15.5). Free / "вход свободный" /
  "бесплатно" = 0. Not listed on the page = `null` (correct only when the pipeline also
  yields null). "PWYC from N" = N. Keep the verbatim page text in `price:`; when the text
  cannot be normalised mechanically, add a numeric `price_min:` override.
- **"On now until X" / "Closes X"**: `date` = capture day (`today`), `end_date` = X,
  `start_known: false`.
- **Ranges**: a bare range with no weekday / session info is `kind: run`,
  `sessions_known: false` — except a range of ≤ 2 days inclusive, whose sessions are both
  days (`sessions_known: true`). Do not infer weekdays from the weekday of the range ends.
- **Series**: a weekly programme is `kind: recurring` + `byday` (step-4 contract), with
  `date` = its first session (or capture day when start is unknown) and `end_date` = last.
- **Explicit performance lists** (adapter JSON with every booking) are one `one_off` per
  performance with its own time.
- **title** is scored after normalisation (case, punctuation, `&` = and); matching needs a
  title ratio ≥ 0.9 and then prefers same date + time, then same date, then the nearest date
  within 7 days (counted as `found_wrong_date`).
- **url** = the event's own page when the listing links one; the listing URL only when the
  page has no per-event link.

## Adding / refreshing a venue

    scripts/eval.py --capture SLUG --url URL [--keep CSS --items CSS --limit N --json-scripts RX] [--force]
    # write meta.yaml + expected.yaml by reading the page (quote evidence per event)
    scripts/eval.py --record-llm SLUG [--force]     # ONE live LLM call (llm venues only)
    scripts/eval.py --restamp-llm SLUG              # page/config changed but the reply still applies

A recorded reply carries the engine cache key (`extract._text_hash` of the reduced page +
prompt/logic versions + site header). When the reducer, prompt or venue config changes the
input, the run fails with "reply stale, re-record".

## KNOWN_FAILURES.yaml

Every mismatch the current pipeline makes on gold, keyed (venue, title, date, field) with
the wrong value it produced (`got:`) and a one-line cause. `tests/test_eval.py` asserts the
mismatch set equals this file: a fixed bug is an **unexpected pass** (delete the entry), a
new or changed wrong value is a **new failure**. After either, run
`scripts/eval.py --update-baseline`.
