import json, datetime as dt
from src.sources.adapters import get_adapter
from src.extract import _fix_urls


def test_coliseum_one_rec_per_production():
    d = dt.date.today() + dt.timedelta(days=5)
    bk = [{"event_name": "Tosca", "start": (d + dt.timedelta(days=i)).isoformat(), "title": "7.30pm"} for i in (0, 2, 4)]
    html = f'<script id="json-data">{json.dumps([{"id": "tosca", "bookings": bk}])}</script>'
    recs = get_adapter({"adapter": {"kind": "coliseum"}}).records(html, {})
    assert len(recs) == 1 and recs[0].title == "Tosca" and recs[0].url == "/events/tosca/"


def test_fix_urls_uses_card_link():
    raw = [{"title": "Foo Gig", "url": "https://x.org/ev/"}, {"title": "Bar", "url": "https://x.org/b"}]
    out = _fix_urls(raw, "Foo Gig | 1 Dec -> /ev/foo-gig\nBar | 2 Dec -> /ev/bar", "https://x.org/ev")
    assert out[0]["url"] == "/ev/foo-gig" and out[1]["url"] == "https://x.org/b"
