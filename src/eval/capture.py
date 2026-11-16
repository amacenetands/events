"""Gold-page capture: cached HTML (cache/memory.db `scrapes`) -> trimmed page.html.

Keeps the DOM shape and class/id/href attributes the reducers and adapters read, plus every
JSON-LD block; drops other scripts, styles, svg, media and boilerplate attributes. `keep`
(CSS selector) cuts the body down to the listing region; `limit` keeps the first N matches
of `items` inside it.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from bs4 import BeautifulSoup, Comment

__all__ = ["MAX_BYTES", "cached_html", "trim_html"]

MAX_BYTES = 40_000
DB = Path(__file__).resolve().parents[2] / "cache" / "memory.db"
_DROP_TAGS = ("style", "svg", "noscript", "iframe", "link", "meta", "img", "picture", "source", "video",
              "audio", "canvas", "form", "input", "button", "select", "template")
_KEEP_ATTRS = {"href", "class", "id", "datetime", "type", "content", "itemprop", "itemtype", "lang"}


def cached_html(url: str, db: Path | None = None) -> str:
    with sqlite3.connect(db or DB) as conn:
        row = conn.execute("select html from scrapes where url=?", (url,)).fetchone()
    if row is None:
        raise KeyError(f"no cached scrape for {url}")
    return row[0]


def trim_html(html: str, keep: str | None = None, items: str | None = None, limit: int | None = None,
              json_scripts: str | None = None) -> str:
    """Trimmed page. `json_scripts`: regex; non-LD <script> blocks whose text matches are kept
    (adapter payloads such as `new JupiterCalendar(`)."""
    soup = BeautifulSoup(html, "html.parser")
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    ld = []
    for s in soup.find_all("script"):
        typ = (s.get("type") or "").lower()
        if "ld+json" in typ or (json_scripts and re.search(json_scripts, s.string or "")):
            ld.append(s.extract())
        else:
            s.decompose()
    for t in soup.find_all(_DROP_TAGS):
        t.decompose()
    body = soup.body or soup
    if keep:
        region = soup.select(keep)
        if not region:
            raise ValueError(f"selector {keep!r} matched nothing")
        if items and limit is not None:
            for r in region:
                for extra in r.select(items)[limit:]:
                    extra.decompose()
        inner = "".join(str(r) for r in region)
    else:
        inner = "".join(str(c) for c in body.contents)
    out = BeautifulSoup(f"<!doctype html><html><head></head><body>{inner}</body></html>", "html.parser")
    for t in out.find_all(True):
        t.attrs = {k: v for k, v in t.attrs.items() if k in _KEEP_ATTRS}
    for s in ld:
        out.head.append(s)
    text = str(out)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text
