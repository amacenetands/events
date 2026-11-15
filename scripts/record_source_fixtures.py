"""Record trimmed HTML fixtures for tests/test_sources_*.py from cache/memory.db (no network).

  venv/bin/python scripts/record_source_fixtures.py

Writes tests/fixtures/sources/: jsonld_<slug>.html (ld+json blocks only), follow_barbican_*.html
(listing anchors + 2 detail pages without scripts/styles), adapter_<slug>.html (the adapter's
script/cards, item list cut to a few records) and manifest.json {recorded_on, files}.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "cache" / "memory.db"
OUT = ROOT / "tests" / "fixtures" / "sources"
MAX_BYTES = 30_000

JSONLD_VENUES = {
    "aldwych": "Aldwych Theatre", "conway_hall": "Conway Hall", "camden_peoples": "Camden People's Theatre",
    "apollo": "Apollo Theatre", "garrick": "Garrick Theatre", "lyric": "Lyric Theatre",
    "bach_to_baby": "Bach to Baby Ealing",
}
BARBICAN_LISTING = "https://www.barbican.org.uk/whats-on"
BARBICAN_DETAILS = ("naza", "ensemble-pygmalion-mozarts-great-mass")
_LD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>.*?</script>', re.S | re.I)


def _html(conn: sqlite3.Connection, *, site: str | None = None, url: str | None = None) -> str:
    """Stored html for a site's listing url (first match) or an exact url."""
    if url:
        row = conn.execute("select html from scrapes where url=?", (url,)).fetchone()
    else:
        from src.config import all_venues
        v = next((v for v in all_venues() if v.name == site), None)
        if v is None:
            raise SystemExit(f"no venue named {site!r} in sites.yaml")
        row = conn.execute("select html from scrapes where url=?", (v.resolved_url(date.today().year),)).fetchone()
    if not row:
        raise SystemExit(f"not cached: {site or url}")
    return row[0]


def _page(body: str, head: str = "") -> str:
    return f"<!doctype html>\n<html><head>{head}</head><body>\n{body}\n</body></html>\n"


def _write(name: str, text: str, files: list[str]) -> None:
    if len(text.encode()) > MAX_BYTES:
        print(f"WARN {name}: {len(text.encode())} bytes > {MAX_BYTES}")
    (OUT / name).write_text(text)
    files.append(name)


def _jsonld(conn: sqlite3.Connection, files: list[str]) -> None:
    for slug, site in JSONLD_VENUES.items():
        blocks = _LD.findall(_html(conn, site=site))
        _write(f"jsonld_{slug}.html", _page("\n".join(blocks)), files)


def _strip(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "svg", "noscript", "iframe", "link", "footer", "nav", "img", "picture"]):
        t.decompose()
    return soup


def _barbican(conn: sqlite3.Connection, files: list[str]) -> None:
    soup = BeautifulSoup(_html(conn, url=BARBICAN_LISTING), "html.parser")
    links = [f'<a href="{a["href"]}">{a.get_text(" ", strip=True)[:80]}</a>'
             for a in soup.find_all("a", href=True)
             if re.search(r"/whats-on/\d{4}/event/", a["href"])]
    _write("follow_barbican_listing.html",
           _page('<a href="/whats-on/cinema">Cinema</a>\n' + "\n".join(links)), files)
    for d in BARBICAN_DETAILS:
        row = conn.execute("select url from scrapes where url like ?", (f"%/event/{d}%",)).fetchone()
        if row is None:
            raise SystemExit(f"Barbican detail page {d!r} not cached; pick another slug from the listing")
        url = row[0]
        s = _strip(_html(conn, url=url))
        head = "".join(str(m) for m in s.find_all("meta", attrs={"property": "og:title"}))
        main = s.find("main") or s.body
        for el in main.find_all(attrs={"class": re.compile(r"newsletter|carousel", re.I)}):
            el.decompose()
        body = re.sub(r"\n\s*\n+", "\n", str(main))
        _write(f"follow_barbican_{d.split('-')[0]}.html", _page(body[:MAX_BYTES - 2000], head), files)


def _js_literal(conn: sqlite3.Connection, files: list[str]) -> None:
    """Orange Tree: `new JupiterCalendar([...])` cut to 12 events with the mapped keys only."""
    soup = BeautifulSoup(_html(conn, site="Orange Tree Theatre"), "html.parser")
    txt = soup.select_one("script:-soup-contains('JupiterCalendar')").get_text()
    marker = "new JupiterCalendar("
    data, _ = json.JSONDecoder().raw_decode(txt[txt.index(marker) + len(marker):])
    items = [{k: e.get(k) for k in ("title", "start", "url")} for e in data[:12]]
    _write("adapter_orange_tree.html",
           _page(f"<script>var cal = {marker}{json.dumps(items, ensure_ascii=False)}, {{}});</script>"), files)


def _coliseum(conn: sqlite3.Connection, files: list[str]) -> None:
    """Coliseum: script#json-data cut to 4 productions x 6 bookings."""
    soup = BeautifulSoup(_html(conn, site="London Coliseum"), "html.parser")
    data = json.loads(soup.select_one("script#json-data").get_text())
    prods = [p for p in data if p.get("bookings")][:4]
    keep = [{"id": p.get("id"), "bookings": [{k: b.get(k) for k in ("event_name", "start", "title")}
                                             for b in p["bookings"][:6]]} for p in prods]
    _write("adapter_coliseum.html",
           _page(f'<script id="json-data" type="application/json">{json.dumps(keep, ensure_ascii=False)}</script>'),
           files)


def _cards(conn: sqlite3.Connection, files: list[str]) -> None:
    """V&A: 10 li.b-event-teaser cards."""
    soup = BeautifulSoup(_html(conn, site="V&A"), "html.parser")
    cards = soup.select("li.b-event-teaser")[:10]
    for c in cards:
        for t in c(["img", "picture", "svg", "source"]):
            t.decompose()
    _write("adapter_vam.html", _page("<ul>" + "\n".join(str(c) for c in cards) + "</ul>"), files)


def main() -> int:
    sys.path.insert(0, str(ROOT))
    OUT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    files: list[str] = []
    _jsonld(conn, files)
    _barbican(conn, files)
    _js_literal(conn, files)
    _coliseum(conn, files)
    _cards(conn, files)
    (OUT / "manifest.json").write_text(json.dumps({"recorded_on": date.today().isoformat(), "files": files}, indent=1))
    print(f"wrote {len(files)} fixtures to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
