"""events table: v3 Event fields round-trip, old rows load, past rows pruned (A8). In-memory db only."""
import json
import sqlite3
from datetime import date, timedelta

import pytest

from src import events_db
from src.models import Event

TODAY = date(2026, 11, 16)


def ev(title="X", d=TODAY, **kw):
    base = dict(title=title, venue="V", date=d, url="https://x.test/a", source="S1")
    base.update(kw)
    return Event(**base)


@pytest.fixture
def db(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    monkeypatch.setattr(events_db, "_conn", c)
    monkeypatch.setattr(events_db, "_ready", False)
    monkeypatch.setattr(events_db, "_today", lambda city: TODAY)
    return c


def by_title(city="london"):
    return {e.title: e for e in events_db.load_events(city).events}


def test_v3_fields_round_trip(db):
    e = ev("WordPlay", TODAY + timedelta(days=4), kind="run", end_date=TODAY + timedelta(days=32),
           byday=[4], sessions_known=True, start_known=False, sold_out=True,
           raw_date="Fridays until 18 Dec", date_inconsistent=True, time="19:30")
    events_db.save_crawl("london", [e], {"S1": ["arts"]})
    got = by_title()["WordPlay"]
    assert got.byday == [4] and got.start_known is False and got.sold_out is True
    assert got.raw_date == "Fridays until 18 Dec" and got.date_inconsistent is True
    assert got.model_dump() == e.model_dump()
    row = db.execute("SELECT event_json FROM events").fetchone()
    assert json.loads(row["event_json"])["byday"] == [4]


def test_old_rows_load_with_defaults(db):
    events_db.init_db()
    old = {"title": "Legacy", "venue": "V", "date": str(TODAY), "url": "https://x.test/l",
           "is_one_off": False, "end_date": str(TODAY + timedelta(days=9)), "source": "S1"}
    db.execute("INSERT INTO events(city,event_key,fingerprint,source,event_json,tags,first_seen,extracted_at,crawl_id)"
               " VALUES('london','k','f','S1',?,'[]','t','t','c')", (json.dumps(old),))
    e = by_title()["Legacy"]
    assert e.kind == "run" and e.byday is None and e.sessions_known and e.start_known
    assert not e.sold_out and e.raw_date is None and not e.date_inconsistent


def test_prune_past_rows_on_save(db):
    keep = [
        ev("yesterday", TODAY - timedelta(days=1)),                       # grace day
        ev("today", TODAY),
        ev("run_on", TODAY - timedelta(days=20), kind="run", end_date=TODAY + timedelta(days=3)),
        ev("run_open", TODAY - timedelta(days=200), kind="run"),          # open-ended run
        ev("series", TODAY - timedelta(days=30), kind="recurring", byday=[5], start_known=False),
        ev("series_from", TODAY - timedelta(days=30), kind="recurring", byday=[5]),   # (c) known start
        ev("tbc", TODAY - timedelta(days=30), date_undetermined=True),                 # (d)
    ]
    dead = [
        ev("old_one_off", TODAY - timedelta(days=2), source="S2"),
        ev("old_run", TODAY - timedelta(days=40), kind="run", end_date=TODAY - timedelta(days=5), source="S2"),
        ev("old_instance", TODAY - timedelta(days=7), kind="recurring", recurrence="Mondays", source="S2"),
    ]
    events_db.save_crawl("london", keep + dead, {})
    events_db.save_crawl("moscow", [ev("moscow_old", TODAY - timedelta(days=9))], {}, today=TODAY - timedelta(days=30))
    # S2 errors this time: its rows are not replaced but the past ones are still pruned
    st: dict = {}
    events_db.save_crawl("london", [ev("fresh", TODAY + timedelta(days=1), source="S3")], {},
                         errors=["S2"], stats=st)
    assert st["pruned"] == 0       # first london save already pruned them
    assert sorted(by_title()) == sorted([e.title for e in keep] + ["fresh"])
    assert "moscow_old" in by_title("moscow")       # other city untouched by london saves
    assert db.execute("SELECT n_events FROM crawls WHERE city='london' ORDER BY rowid DESC").fetchone()[0] == 8


def test_prune_count_reported(db):
    events_db.save_crawl("london", [ev("a", TODAY - timedelta(days=3)), ev("b", TODAY)], {},
                         today=TODAY - timedelta(days=5))
    st: dict = {}
    events_db.save_crawl("london", [ev("c", TODAY, source="S9")], {}, stats=st)
    assert st["pruned"] == 1 and sorted(by_title()) == ["b", "c"]
    assert events_db.prune_past("london", TODAY + timedelta(days=10)) == 2
