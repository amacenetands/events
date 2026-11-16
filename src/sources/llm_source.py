"""`llm` source: reduced listing text -> LLM copies raw strings -> RawEvent.

The model only finds events and copies title/date/time/price/link/etc. verbatim
(src/extract.py holds the prompt + batching/caching engine); sources/build.py does
every date/time/price conversion. Requests from concurrently parsed venues are
coalesced into multi-site batches (extract.BATCH_SIZE) under one shared semaphore.
"""
from __future__ import annotations

import asyncio
import re
import weakref
from datetime import date
from urllib.parse import urljoin, urlsplit

from .. import extract as ex
from ..config import VenueConfig
from ..fetch.types import FetchResult
from ..models import RawEvent
from ..normalize import price_amounts
from ..normalize.urls import match_title_url
from .base import SourceCtx, base_url, register_source
from .reduce import reduce_site

__all__ = ["LlmSource", "site_data", "fill_urls", "verify_prices"]

IDLE_FLUSH = 0.3          # s without a new request before a partial batch is sent
_HTML_TAG = re.compile(r"<\s*(?:!doctype|html|head|body|div|span|a|p|ul|ol|li|script|article|section|main"
                       r"|h[1-6]|meta|link|img|table|nav|header|footer)\b", re.I)


def _bump(ctx: SourceCtx, key: str, n: int = 1) -> None:
    ctx.stats[key] = ctx.stats.get(key, 0) + n


def site_data(cfg: VenueConfig, fr: FetchResult, ctx: SourceCtx) -> dict | None:
    """Engine input for one venue (reduced records + prompt header fields); None if thin.
    A cached body without HTML tags is legacy browser markdown and is reduced as text
    (kept until those scrapes rows age out)."""
    listing = cfg.resolved_url(ctx.today.year)
    site = cfg.to_legacy_dict()
    site["url"] = base_url(cfg, fr, ctx)
    html = fr.body if _HTML_TAG.search(fr.body or "") else ""
    red = reduce_site(html, site, pre_text=None if html else fr.body, today=ctx.today)
    if red.thin:
        return None
    return {
        "name": cfg.name, "url": listing, "base": site["url"], "text": red.text, "chunks": red.chunks,
        "category_hint": cfg.category_hint, "hint": cfg.hint or "",
        "lang": ctx.city.lang, "must_determine_venue": cfg.must_determine_venue,
        "cache_version": cfg.cache_version,
        "fetched_at": fr.fetched_at.isoformat() if fr.fetched_at else None,
    }


def _on_page(url: str, text: str) -> bool:
    """The model's link occurs in the reduced text (as given, or its path)."""
    if url in text:
        return True
    path = urlsplit(url).path
    return len(path) > 1 and path in text


def fill_urls(raws: list[RawEvent], text: str, base: str, pattern: str | None = None,
              listing: str | None = None) -> list[RawEvent]:
    """Absolute per-event links: a link not found in `text` (invented) or missing / equal
    to the listing is re-derived from the line uniquely labelled with the title, else dropped
    (build falls back to the listing page)."""
    lines = [ln for ln in (text or "").splitlines() if "->" in ln or "](" in ln or "http" in ln]
    own = {(listing or base).rstrip("/"), base.rstrip("/")}
    out = []
    for r in raws:
        u = (r.url or "").strip()
        keep = u and _on_page(u, text or "") and urljoin(base, u).rstrip("/") not in own
        if keep:
            new = urljoin(base, u)
        else:
            new = match_title_url(r.title, lines, base, pattern=pattern)
        out.append(r.model_copy(update={"url": new or None}) if new != r.url else r)
    return out


_CARD = re.compile(r"^\s*\[C\]\s")       # reducer's visible-card line ("[C] …"); [J] = JSON-LD only


def _squash(s: str) -> str:
    """Case-folded alphanumerics only: '£19 | £17 Advance' -> '1917advance'."""
    return re.sub(r"[\W_]+", "", (s or "").casefold())


def _body(line: str) -> str:
    return line.split(" -> ", 1)[0]


def _own_card(title: str, cards: list[str]) -> str | None:
    """The single [C] card line naming `title`; None when not uniquely identifiable."""
    t = _squash(title)
    if len(t) < 4:
        return None
    hits = [ln for ln in cards if t in _squash(_body(ln))]
    return hits[0] if len(hits) == 1 else None


def _on_card(price: str, line: str) -> bool:
    """The price occurs on the card: same amounts ('£10.00' == '£10'), or the same squashed text."""
    want = price_amounts(price)
    if want:
        return want <= price_amounts(_body(line))
    return bool(_squash(price)) and _squash(price) in _squash(_body(line))


def verify_prices(raws: list[RawEvent], text: str, ctx: SourceCtx) -> list[RawEvent]:
    """Drop a price the model copied from a neighbouring card: only when it is absent from the
    event's own [C] card and present on a different one (stat llm.price_dropped). Anything
    else is kept; when not confirmed on the own card it counts as llm.price_unverified."""
    cards = [ln for ln in (text or "").splitlines() if _CARD.match(ln)]
    out = []
    for r in raws:
        if r.price_text:
            own = _own_card(r.title, cards)
            if own is None or not _on_card(r.price_text, own):
                other = [ln for ln in cards if ln is not own and _on_card(r.price_text, ln)]
                if own is not None and other and price_amounts(r.price_text):
                    _bump(ctx, "llm.price_dropped")
                    r = r.model_copy(update={"price_text": None})
                else:
                    _bump(ctx, "llm.price_unverified")
        out.append(r)
    return out


class _Batcher:
    """Coalesces concurrent per-venue requests into engine calls on one event loop."""

    def __init__(self) -> None:
        self.pending: list[tuple[dict, bool, date, asyncio.Future]] = []
        self.timer: asyncio.TimerHandle | None = None
        self.sem = asyncio.Semaphore(ex._CONCURRENCY)
        self.tasks: set[asyncio.Task] = set()

    async def submit(self, site: dict, force: bool, today: date) -> tuple[list[dict], list[str]]:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.pending.append((site, force, today, fut))
        if self.timer:
            self.timer.cancel()
        if len(self.pending) >= ex.BATCH_SIZE:
            self._flush()
        else:
            self.timer = loop.call_later(IDLE_FLUSH, self._flush)
        return await fut

    def _flush(self) -> None:
        if self.timer:
            self.timer.cancel()
            self.timer = None
        batch, self.pending = self.pending, []
        groups: dict[tuple[bool, date], list] = {}
        for item in batch:
            groups.setdefault((item[1], item[2]), []).append(item)
        for (force, today), items in groups.items():
            t = asyncio.ensure_future(self._run(items, force, today))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)

    async def _run(self, items: list, force: bool, today: date) -> None:
        errors: list[tuple[str, str]] = []
        try:
            res = await ex.extract_raw_batch_async([s for s, *_ in items], errors=errors, force_parse=force,
                                                   today=today, sem=self.sem)
        except Exception as e:   # noqa: BLE001 — delivered to each waiting venue (safe_records catches)
            for *_, fut in items:
                if not fut.done():
                    fut.set_exception(e)
            return
        for s, *_, fut in items:
            name = s["name"]
            errs = [msg for n, msg in errors if n == name]
            if not fut.done():
                fut.set_result((res.get(name, []), errs))


_batchers: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Batcher]" = weakref.WeakKeyDictionary()


def _batcher() -> _Batcher:
    loop = asyncio.get_running_loop()
    b = _batchers.get(loop)
    if b is None:
        b = _batchers[loop] = _Batcher()
    return b


class LlmSource:
    """LLM over the reduced listing page (network: the LLM backend only)."""
    kind = "llm"

    async def records(self, cfg: VenueConfig, fr: FetchResult | None, ctx: SourceCtx) -> list[RawEvent]:
        if fr is None or not fr.body:
            return []
        site = site_data(cfg, fr, ctx)
        if site is None:
            _bump(ctx, "llm.thin")
            return []
        hit = ex.cached_records(site, ctx.force_parse)      # cache hits skip the batch queue
        if hit is not None:
            dicts, errs = hit[0], []
            _bump(ctx, "llm.cache_hits")
        else:
            dicts, errs = await _batcher().submit(site, ctx.force_parse, ctx.today)
        for msg in errs:
            _bump(ctx, "llm.failed" if msg.startswith("failed") else "llm.partial")
            ctx.errors.append(f"{cfg.name}: llm {msg}")
        raws = ex.to_raws(dicts, ctx.stats)
        if not cfg.must_determine_venue:
            raws = [r.model_copy(update={"venue": None}) if r.venue else r for r in raws]
        raws = fill_urls(raws, site["text"], site["base"], cfg.fix.url_pattern, site["url"])
        raws = verify_prices(raws, site["text"], ctx)
        _bump(ctx, "llm.records", len(raws))
        return raws


SOURCE = register_source(LlmSource())
