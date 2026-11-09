import json
import sqlite3
from datetime import date, datetime

import pytest

from src import events_db
from src.digest import generate_digest_v2
from src.email_sender import markdown_to_plain, render_html
from src.models import Event
from src.query import Filter, query, city_today, resolve_when, describe

TODAY = date(2026, 11, 11)


def ev(title="X", d="2026-11-12", **kw):
    base = dict(title=title, venue="V", date=d, url="https://x.test/a", source="S1")
    base.update(kw)
    return Event(**base)


@pytest.fixture
def db(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    monkeypatch.setattr(events_db, "_conn", c)
    monkeypatch.setattr(events_db, "_ready", False)
    return c


def titles(city="london"):
    return sorted(e.title for e in events_db.load_events(city).events)


def test_event_key_distinguishes_time_fingerprint_does_not():
    a, b = ev("Show", time="14:00"), ev("Show", time="19:30")
    assert a.fingerprint() == b.fingerprint() and a.event_key() != b.event_key()


def test_dedup_keeps_matinee_and_evening():
    import main
    out = main._dedup_by_fingerprint([ev("Show", time="14:00"), ev("Show", time="19:30"), ev("Show", time="19:30")])
    assert len(out) == 2


def test_save_crawl_partial_keeps_other_sources(db):
    events_db.save_crawl("london", [ev("a1", source="S1"), ev("b1", source="S2")], {})
    # S1 ok (new content), S2 errored w/ nothing, S3 new
    events_db.save_crawl("london", [ev("a2", source="S1"), ev("c1", source="S3")], {}, errors=["S2 (thin page)"])
    assert titles() == ["a2", "b1", "c1"]


def test_save_crawl_errored_source_keeps_old_rows(db):
    events_db.save_crawl("london", [ev("a1", source="S1", time="10:00")], {})
    events_db.save_crawl("london", [ev("a2", source="S1")], {}, errors=["S1"])
    assert titles() == ["a1", "a2"]


def test_matinee_evening_both_stored(db):
    events_db.save_crawl("london", [ev("Show", time="14:00"), ev("Show", time="19:30")], {})
    assert titles() == ["Show", "Show"]


def test_first_seen_preserved(db):
    events_db.save_crawl("london", [ev("a")], {})
    fp = ev("a").fingerprint()
    t0 = events_db.load_events("london").first_seen[fp]
    events_db.save_crawl("london", [ev("a")], {})
    assert events_db.load_events("london").first_seen[fp] == t0


def test_migrates_old_schema(db):
    db.executescript("""CREATE TABLE events (city TEXT NOT NULL, fingerprint TEXT NOT NULL, source TEXT NOT NULL,
        event_json TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]', first_seen TEXT NOT NULL,
        extracted_at TEXT NOT NULL, crawl_id TEXT NOT NULL, PRIMARY KEY (city, fingerprint));
        CREATE TABLE crawls (crawl_id TEXT PRIMARY KEY, city TEXT, crawled_at TEXT, n_events INTEGER);""")
    e = ev("old")
    db.execute("INSERT INTO events VALUES('london',?,?,?,'[]','t','t','c')", (e.fingerprint(), "S1", e.model_dump_json()))
    db.execute("INSERT INTO events VALUES('london','zz','S1','{bad','[]','t','t','c')")
    db.commit()
    events_db.init_db()
    assert "event_key" in [r[1] for r in db.execute("PRAGMA table_info(events)")]
    assert titles() == ["old"]
    events_db.save_crawl("london", [ev("old", time="9:00")], {})   # works on migrated table


def test_undecodable_rows_counted_and_old_crawls_pruned(db, capsys):
    events_db.save_crawl("london", [ev("a")], {})
    db.execute("INSERT INTO events VALUES('london','k','k','S1','{bad','[]','t','t','c')")
    db.execute("INSERT INTO crawls(crawl_id,city,crawled_at,n_events) VALUES('old','london','2020-01-01T00:00:00+00:00',1)")
    db.commit()
    assert events_db.load_events("london").n_bad == 1
    events_db.save_crawl("london", [ev("a")], {})
    assert db.execute("SELECT COUNT(*) FROM crawls WHERE crawl_id='old'").fetchone()[0] == 0


def test_no_ddl_at_import():
    import inspect
    src = inspect.getsource(events_db)
    assert "_conn.executescript(\"\"\"" not in src.split("def init_db")[0]


def test_city_timezone_today():
    f = Filter(city="moscow")
    assert isinstance(city_today(f), date)
    assert Filter(city="london").tz is None


def test_next_n_is_n_days_inclusive():
    assert resolve_when("next:3", TODAY) == (TODAY, date(2026, 11, 13))
    evs = [ev("in", d="2026-11-13"), ev("out", d="2026-11-14")]
    assert [e.title for e in query(evs, Filter(when="next:3"), today=TODAY)] == ["in"]
    assert "Wed 11 Nov" in describe(Filter(when="next:1"), TODAY)


def test_public_and_online_default_filters():
    evs = [ev("pub"), ev("priv", public=False), ev("web", online=True)]
    assert [e.title for e in query(evs, Filter(when="next:3"), today=TODAY)] == ["pub"]
    assert len(query(evs, Filter(when="next:3", public_only=False), today=TODAY)) == 2
    assert len(query(evs, Filter(when="next:3", include_online=True), today=TODAY)) == 2


def test_link_with_parens_and_spaces_and_html_escape():
    e = ev("Rock <b>&</b> Roll", url="https://x.test/a b/(c)", venue="A&B", description="<script>x</script>")
    md = generate_digest_v2([e], Filter(when="next:3"), today=TODAY)
    assert "https://x.test/a%20b/%28c%29" in md
    assert "<script>" not in md and "&lt;script&gt;" in md
    assert "A&amp;B" in md
    html = render_html(md)
    assert "<script>" not in html and 'href="https://x.test/a%20b/%28c%29"' in html
    plain = markdown_to_plain(md)
    assert "https://x.test/a%20b/%28c%29" in plain and "A&B" in plain


def test_russian_headings():
    evs = [ev("Концерт", d="2026-11-11"), ev("Завтра шоу", d="2026-11-12"),
           ev("Выст", kind="run", d="2026-11-01", end_date="2026-12-01"),
           ev("Выст2", kind="run", d="2026-11-01", end_date="2026-12-05"),
           ev("TBC", date_undetermined=True)]
    md = generate_digest_v2(evs, Filter(city="moscow", when="next:7", lang="ru"), picks=[evs[2]], today=TODAY)
    for w in ("Сегодня", "Завтра", "Лучшее", "Выставки", "Дата уточняется", "ноября"):
        assert w in md, w
    assert "Top picks" not in md and "Today" not in md
    assert "Today" in generate_digest_v2(evs, Filter(when="next:7"), today=TODAY)


def test_dead_code_removed():
    import src.digest as d
    for n in ("generate_digest", "generate_family_digest", "generate_daytime_digest", "generate_evening_digest",
              "get_digest_generator", "_RU"):
        assert not hasattr(d, n), n
    assert not hasattr(Event, "is_weekday_daytime")
