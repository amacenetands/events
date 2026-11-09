---
name: events
description: |
  Interactive London/Moscow events digest. Use when the user says "run events",
  "events for today", "family events", "what's on", "events this weekend".
  Asks what they want, crawls only if the cache is stale, queries with
  orthogonal filters, shows results, asks before emailing.
---

Repo: `/Users/dtonkonog/code/london_events` (use `venv/bin/python`). Crawl (slow, LLM) and query (instant, filters) are separate.

## 1. Ask (one AskUserQuestion call, up to 4 questions)
1. City: London / Moscow
2. When: Today / Tomorrow / This weekend / Next 7 days / Other (ask for N days or YYYY-MM-DD..YYYY-MM-DD)
3. Who + what: Family-friendly / Adults / Anything (multi: include exhibitions & runs? one-offs only?)
4. Time of day: Any / Daytime (morning+afternoon) / Evening

## 2. Map answers to flags
- when: `today` | `tomorrow` | `weekend` | `next:7` | `next:N` (N days incl. today) | `range:A..B`; "today" uses the city's timezone
- audience: `--audience family|adult|any` (family includes kids events)
- kinds: `--kinds one_off,run,recurring` (default all); "one-offs only" = `--kinds one_off`
- time: `--time any|daytime|evening` (add `--strict-time` to drop events with no listed time)
- optional: `--days weekday|weekend`, `--category concert,talk`, `--venue-tag museums`, `--new-only`, `--rank`
- Hidden by default: school/teacher/GCSE/members-only/private-hire events (`--include-private` to keep) and online-only events (`--include-online`). Moscow digests render RU headings.

## 3. Freshness check (never sends)
```bash
venv/bin/python main.py query --city london --when today --audience family --dry-run --quiet
```
First lines print `Crawl: london, 5.0d old, N events` or `WARNING: ... crawl is Xd old` / `no crawl`.

## 4. Crawl only if stale (>7d or none). Default LLM backend deepseek
```bash
LLM_BACKEND=deepseek venv/bin/python main.py crawl --city london          # fetch + parse all venues
LLM_BACKEND=deepseek venv/bin/python main.py crawl --city london --parse-only   # reuse cached HTML, no HTTP
```
Moscow: `--city moscow` (use `LLM_BACKEND=ollama` for qwen-primary if the user prefers). Tell the user the crawl takes minutes; run in background.

## 5. Query (dry by default; prints digest, saves `output/<slug>_<date>.md`)
```bash
venv/bin/python main.py query --city london --when weekend --audience family --kinds one_off,run --time any --days any --dry-run
```
Presets: `--preset family|evening|daytime|museums|theatres|standard|moscow` (flags override preset). Show counts (`N crawled -> M match`) and the top ~10 items.

## 6. Ask before emailing
Only after the user confirms: re-run the same command with `--send` (add `--mask` to mark one-offs as seen so they don't repeat). Never use `--send` without confirmation.

## Notes
- Legacy flags (`main.py --family --today --dry-run`) = crawl (cached) + query with the preset; they email unless `--dry-run`.
- `--auto-crawl` makes `query` crawl itself when stale (off by default).
- Test one venue: `venv/bin/python main.py crawl --city london --test-site "Name" --parse-only`
