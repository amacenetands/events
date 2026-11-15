"""`adapter` source: direct (structured) venue adapters -> RawEvent. Non-direct (auto) -> []."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from ..config import VenueConfig
from ..fetch.types import FetchResult
from ..models import RawEvent
from ..normalize import parse_date_specs
from .adapters import Adapter, Rec, get_adapter
from .adapters.textutil import _SITE_HOST, set_site_host
from .base import SourceCtx, base_url, register_source

__all__ = ["AdapterSource", "rec_to_raw", "raws_from_html"]

_TRAILING_TIME = re.compile(r"\s+\d{1,2}[:.]\d\d$")   # "Cranford 20:00" (Orange Tree)


def rec_to_raw(rec: Rec, kind: str, base: str = "") -> RawEvent:
    """Structured Rec -> RawEvent (strings as found; url absolute against `base`)."""
    title = _TRAILING_TIME.sub("", rec.title).strip() or rec.title
    url = urljoin(base, rec.url) if rec.url and base else rec.url
    return RawEvent(title=title, date_text=rec.date or None, time_text=rec.time or None,
                    url=url or None, desc=rec.desc or None, source_kind=f"adapter:{kind}")


def _parses(rec: Rec, cfg: VenueConfig, ctx: SourceCtx) -> bool:
    order = "mdy" if cfg.fix.date_order == "mdy" else "dmy"
    return bool(parse_date_specs(rec.date, ctx.today, order=order, lang=ctx.city.lang, tz=ctx.city.tz))


def raws_from_html(html: str, cfg: VenueConfig, ctx: SourceCtx, base: str = "") -> list[RawEvent]:
    """RawEvents when the venue's adapter is direct and every titled record has a parseable date.
    Links resolve against `base` (post-redirect page URL), else the listing URL."""
    site = cfg.to_legacy_dict()
    site["url"] = cfg.resolved_url(ctx.today.year)
    try:
        adapter = get_adapter(site)
    except KeyError:           # kind served by another source (e.g. spektrix)
        return []
    if not adapter.direct or (cfg.adapter or {}).get("llm"):
        return []
    token = set_site_host(site["url"])
    try:
        recs = [r for r in Adapter.dedupe(adapter.records(html, site)) if r.title]
    finally:
        _SITE_HOST.reset(token)
    if not recs:
        ctx.stats["adapter.no_records"] = ctx.stats.get("adapter.no_records", 0) + 1
        return []
    if not all(r.date and _parses(r, cfg, ctx) for r in recs):
        ctx.stats["adapter.unparsed_venues"] = ctx.stats.get("adapter.unparsed_venues", 0) + 1
        return []
    return [rec_to_raw(r, adapter.kind, base or site["url"]) for r in recs]


class AdapterSource:
    """Pure: runs the configured adapter over the fetched listing page."""
    kind = "adapter"

    async def records(self, cfg: VenueConfig, fr: FetchResult | None, ctx: SourceCtx) -> list[RawEvent]:
        if fr is None or not fr.body:
            return []
        out = raws_from_html(fr.body, cfg, ctx, base_url(cfg, fr, ctx))
        ctx.stats["adapter.records"] = ctx.stats.get("adapter.records", 0) + len(out)
        return out


SOURCE = register_source(AdapterSource())
