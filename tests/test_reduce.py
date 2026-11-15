import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sources.reduce import reduce_site, chunk_lines

Y = dt.date.today().year + 1  # always-future year


def _cards(n=4, past=False):
    y = 2001 if past else Y
    items = "".join(
        f'<div class="ev"><h3>Show number {i} here</h3><span>{i + 1} March {y}</span>'
        f'<a href="/e/{i}">more</a></div>' for i in range(n))
    return f"<html><body><nav>Home</nav><main>{items}</main></body></html>"


def test_card_list_auto():
    r = reduce_site(_cards(), {"name": "X", "url": "https://x.org/"})
    assert not r.thin and r.n_records >= 4
    assert any(l.startswith("[C]") and f"March {Y}" in l and "-> /e/1" in l for l in r.lines)
    assert "Home" not in r.text


def test_past_cards_dropped():
    r = reduce_site(_cards(past=True), {"name": "X", "url": "https://x.org/"})
    assert not any(l.startswith("[C]") for l in r.lines)


def test_js_literal_json_adapter_direct_events():
    js = ('new Cal([{"title":"Hamlet 19:30","start":"%d-03-05T19:30","url":"/w/ham#x"},'
          '{"title":"Lear","start":"%d-03-06T14:00","url":"/w/lear"},'
          '{"title":"Macbeth","start":"%d-03-07","url":"/w/mac"}])' % (Y, Y, Y))
    html = f"<html><body><script>{js}</script></body></html>"
    site = {"name": "T", "url": "https://t.org/", "category_hint": "theatre",
            "adapter": {"kind": "json", "script": "script", "js_after": "new Cal(",
                        "title": "title", "date": "start", "url": "url"}}
    r = reduce_site(html, site)                       # the llm source still gets its records
    assert len(r.lines) == 3 and "-> /w/lear" in r.lines[1] and not r.thin
    from src.config import VenueConfig, get_city
    from src.sources.adapter_source import raws_from_html
    from src.sources.base import SourceCtx
    from src.sources.build import build_events
    cfg = VenueConfig(name="T", url="https://t.org/", category_hint="theatre", adapter=site["adapter"])
    ctx = SourceCtx(http=None, today=dt.date.today(), window_days=365, city=get_city("london"))
    evs = build_events(raws_from_html(html, cfg, ctx), cfg, ctx)   # direct adapter: no LLM
    assert len(evs) == 3
    ham = next(e for e in evs if e.title == "Hamlet")
    assert ham.time == "19:30" and ham.url == "https://t.org/w/ham" and ham.is_one_off
    assert ham.category == "theatre"


def test_auto_harvests_embedded_json():
    items = ",".join('{"title":"Gig %d","date":"%d-04-%02d"}' % (i, Y, i + 1) for i in range(5))
    html = f'<html><body><script>var d = {{"events":[{items}]}};</script><p>hi</p></body></html>'
    r = reduce_site(html, {"name": "J", "url": "https://j.org/"})
    assert sum(l.startswith("[J] Gig") for l in r.lines) == 5


def test_thin_page_and_chunking():
    r = reduce_site("<html><body><div id='app'></div><script>x=1</script></body></html>",
                    {"name": "Thin", "url": "https://t.org/"})
    assert r.thin and r.chunks == []
    ch = chunk_lines(["a" * 100] * 200, size=6000)
    assert all(len(c) <= 6000 for c in ch) and len(ch) == 4
