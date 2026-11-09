# Venue adapters

The reducer turns a venue's raw HTML into compact event records before the LLM.
Default is `auto` (JSON harvest -> repeated cards -> flat date lines). For venues
where that is lossy, add an `adapter:` block to the site in `sites.yaml`.

## 1. YAML only (cards)
```yaml
- name: Some Gallery
  url: https://example.org/events
  adapter: {kind: cards, card: "li.event", title: "h3", date: ".when", time: ".t", desc: ".type", link: "a"}
```
## 2. YAML only (inline JSON / JS literal)
```yaml
  adapter: {kind: json, script: "script:-soup-contains('Calendar')", js_after: "new Calendar(",
            path: "a.b.*", title: title, date: start, url: url}
```
`cards`/`json` are `direct`: if every record has a title and a parseable date, Events are
built straight from them (no LLM). Add `llm: true` under `adapter:` to force the LLM anyway.
If the adapter returns 0 records the site silently falls back to `auto`.

## 3. Python subclass
`src/sources/adapters/venues/<slug>.py`:
```python
from ..base import Adapter, Rec, register
@register("my_venue")
class MyVenue(Adapter):
    direct = True                                   # records are trustworthy -> skip LLM
    def records(self, html, site_cfg):
        return [Rec(title=t, date="2026-12-01", url="/x") for t in ...]
```
then `adapter: {kind: my_venue}` in sites.yaml. Modules in `venues/` auto-import.

Check results with `venv/bin/python scripts/reduce_regress.py` (read-only over cache/memory.db).
