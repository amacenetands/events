import json, datetime as dt
from src.sources.adapters import get_adapter
from src.models import RawEvent
from src.sources.llm_source import fill_urls


def test_coliseum_one_rec_per_performance():
    today = dt.date.today()
    d = today + dt.timedelta(days=5)
    bk = [{"event_name": "Tosca", "start": (d + dt.timedelta(days=i)).isoformat(), "title": t}
          for i, t in ((4, "6.00pm"), (0, "7.30pm"), (2, "7.30pm"), (-30, "7.30pm"))]
    html = f'<script id="json-data">{json.dumps([{"id": "tosca", "bookings": bk}])}</script>'
    recs = get_adapter({"adapter": {"kind": "coliseum"}}).records(html, {}, today=today)
    assert [(r.title, r.date, r.time, r.url) for r in recs] == [
        ("Tosca", (d + dt.timedelta(days=i)).strftime("%-d %B %Y"), t, "/events/tosca/")
        for i, t in ((0, "7.30pm"), (2, "7.30pm"), (4, "6.00pm"))]


def test_fill_urls_uses_card_link():
    raws = [RawEvent(title="Foo Gig", url="https://x.org/ev/"), RawEvent(title="Bar", url="https://x.org/b"),
            RawEvent(title="Baz Night")]
    text = "Foo Gig | 1 Dec -> /ev/foo-gig\nBar | 2 Dec -> /ev/bar\nBaz Night | 3 Dec -> /ev/baz"
    out = fill_urls(raws, text, "https://x.org/ev")
    assert [r.url for r in out] == ["https://x.org/ev/foo-gig", "https://x.org/b", "https://x.org/ev/baz"]
