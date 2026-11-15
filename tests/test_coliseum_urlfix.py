import json, datetime as dt
from src.sources.adapters import get_adapter
from src.models import RawEvent
from src.sources.llm_source import fill_urls


def test_coliseum_one_rec_per_production():
    d = dt.date.today() + dt.timedelta(days=5)
    bk = [{"event_name": "Tosca", "start": (d + dt.timedelta(days=i)).isoformat(), "title": "7.30pm"} for i in (0, 2, 4)]
    html = f'<script id="json-data">{json.dumps([{"id": "tosca", "bookings": bk}])}</script>'
    recs = get_adapter({"adapter": {"kind": "coliseum"}}).records(html, {})
    assert len(recs) == 1 and recs[0].title == "Tosca" and recs[0].url == "/events/tosca/"


def test_fill_urls_uses_card_link():
    raws = [RawEvent(title="Foo Gig", url="https://x.org/ev/"), RawEvent(title="Bar", url="https://x.org/b"),
            RawEvent(title="Baz Night")]
    text = "Foo Gig | 1 Dec -> /ev/foo-gig\nBar | 2 Dec -> /ev/bar\nBaz Night | 3 Dec -> /ev/baz"
    out = fill_urls(raws, text, "https://x.org/ev")
    assert [r.url for r in out] == ["https://x.org/ev/foo-gig", "https://x.org/b", "https://x.org/ev/baz"]
