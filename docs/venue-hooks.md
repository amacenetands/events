# Venue overrides: `hint:` vs `fix:` vs a hook

Pick the first that works:

1. **`hint:`** (sites/<city>.yaml) — the page is fine, the LLM just needs to be told how to read
   it: "Matinée and Evening cards on one date are separate events", "times are on the detail page",
   "flag family events". Imperative / format text only; human context goes in `about:` (never sent).
2. **`fix:`** — a deterministic rule on built events (src/venues/fixes.py, one place):
   `drop_title_re`, `url_pattern`, `time_default`, `date_order`, `byday`, `weekly`
   (`{"^WordPlay": [4]}`), `range_means_weekly` (`true`, or `{min_days, title_re}` to accept bare
   same-weekday range ends). Only encode facts you have verified (page or user), with a YAML comment
   saying where they came from. A guess (e.g. inferring Thursdays from a Thu–Thu range) is worse
   than `sessions_known=False`, which the digest shows as "check days".
3. **A parser** — the page has structure the generic sources miss: write an adapter
   (src/sources/adapters/venues/, `adapter: {kind: ...}`), not a hook.
4. **A hook** — only when 1–3 cannot express it and you have a fixture proving the need:
   strip a block before sources run, rewrite fetched page state, or a venue-specific transform
   on RawEvents/Events that is not a reusable rule.

## Adding a hook

- `src/venues/<slug>.py` (≤40 lines, docstring says WHY with the evidence), subclass `VenueHook`,
  decorate with `@venue_hook("<slug>")`, override only the stages you need:
  `pre_fetch(cfg)`, `pre_source(cfg, fr)` (crawl_sites), `post_raw(cfg, raws, ctx)`,
  `post_build(cfg, events, ctx)` (build_events, after `fix:`), `post_enrich(cfg, events, ctx)`
  (parse_venue, after the `enrich:` detail-page pass; runs without `enrich:` too).
- Bind it explicitly: `hook: <slug>` on the venue (ASCII; no name-derived binding).
- `python -m src.config --check` fails on an unknown `hook:` and on a hook no venue uses.
- A hook that raises or returns the wrong type is reported (`hook.<stage>.raised`, one error
  line) and its input passes through. Add a fixture test in tests/test_venue_hooks.py style.
