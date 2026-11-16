"""Deterministic pre-LLM reducer: html -> records -> chunks (input of the `llm` source)."""
from __future__ import annotations
import dataclasses as dc
import re
from .adapters import get_adapter
from .adapters.auto import AutoAdapter, text_records
from .adapters.textutil import set_site_host

CHUNK_CHARS = 6000
MAX_CHUNKS = 8


@dc.dataclass
class Reduced:
    lines: list[str]
    chunks: list[str]
    thin: bool = False
    adapter: str = "auto"
    n_records: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def chunk_lines(lines: list[str], size: int = CHUNK_CHARS) -> list[str]:
    out, cur, n = [], [], 0
    for l in lines:
        if n + len(l) + 1 > size and cur:
            out.append("\n".join(cur)); cur, n = [], 0
        cur.append(l); n += len(l) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def reduce_site(html: str, site: dict, pre_text: str | None = None, today=None) -> Reduced:
    name = site.get("name", "?")
    set_site_host(site.get("url"))
    if not html:
        recs = text_records(pre_text or "", today=today)
        adapter_name = "text"
    else:
        try:
            adapter = get_adapter(site)
        except KeyError:              # kind served by a source, not an adapter (e.g. spektrix)
            adapter = AutoAdapter()
        adapter_name = adapter.kind
        recs = adapter.dedupe([r for r in adapter.records(html, site, today=today)])
        if not recs and adapter.kind != "auto":
            print(f"  [reduce] {name}: adapter {adapter.kind!r} found 0 records — falling back to auto")
            adapter, adapter_name = AutoAdapter(), "auto"
            recs = adapter.records(html, site, today=today)
    lines = [r.line() for r in recs]
    chunks = chunk_lines(lines)
    if len(chunks) > MAX_CHUNKS:
        print(f"  [reduce] {name}: {len(chunks)} chunks > {MAX_CHUNKS}; truncating")
        chunks = chunks[:MAX_CHUNKS]
        lines = "\n".join(chunks).split("\n")
    thin = not recs
    if thin:
        print(f"  [reduce] {name}: thin page — needs js_required or different URL")
    return Reduced(lines, chunks, thin, adapter_name, len(recs))


# ── page cleaner (scripts/gen_selectors.py; not on the extraction path) ──────

# Word-boundaried so a boilerplate word can't match as a substring of a real
# content class — e.g. bare "promo" used to strip St Paul's event cards whose
# class is "node--promoted". (Compound-token false matches like "sidebar" inside
# "page-template-right-sidebar" are handled separately by the >40%-of-page guard
# in _clean_html.)
_BOILERPLATE_PATTERNS = re.compile(
    r"\b(?:cookie|consent|gdpr|banner|popup|modal|newsletter|subscribe|overlay|"
    r"sticky|announcement|alert|notification|skip-link|breadcrumb|pagination|"
    r"social-share|share-button|follow-us|sidebar|widget|advertisement|promo)\b",
    re.IGNORECASE,
)


def _clean_html(html: str, extra_selectors: list[str] | None = None) -> str:
    try:
        from bs4 import BeautifulSoup, Comment
    except ImportError:
        return html

    soup = BeautifulSoup(html, "html.parser")

    if extra_selectors:
        for sel in extra_selectors:
            try:
                for tag in soup.select(sel):
                    tag.decompose()
            except Exception:
                # malformed selector (e.g. Tailwind "md:hidden" reads as a
                # pseudo-class) — skip it rather than abort the whole page
                continue

    for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"]):
        tag.decompose()

    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    body_len = len(soup.get_text(strip=True)) or 1
    to_remove = []
    for tag in soup.find_all(True):
        try:
            attrs = " ".join([
                " ".join(tag.get("class") or []),
                tag.get("id") or "",
                tag.get("role") or "",
                tag.get("aria-label") or "",
            ])
        except Exception:
            continue
        if _BOILERPLATE_PATTERNS.search(attrs):
            # Guard against false positives: a layout wrapper whose class merely
            # contains a boilerplate word (e.g. Concrete5's
            # "page-template-right-sidebar") can hold most of the page. Only
            # strip genuinely peripheral nodes, never the main content body.
            if len(tag.get_text(strip=True)) > 0.4 * body_len:
                continue
            to_remove.append(tag)
    for tag in to_remove:
        try:
            tag.decompose()
        except Exception:
            pass

    main = soup.find("main") or soup.find("article") or soup.find(id=re.compile(r"main|content", re.I))
    # Only trust the main/content container if it actually holds most of the
    # page. Some sites put the real listing outside a tiny <main>/<article>
    # teaser (e.g. Two Temple Place: a ~100-char <article> beside 34k of content)
    # — selecting it would throw the page away. Fall back to the full document.
    if main:
        body_text = len(soup.get_text(strip=True)) or 1
        if len(main.get_text(strip=True)) >= 0.5 * body_text:
            return str(main)
    return str(soup)
