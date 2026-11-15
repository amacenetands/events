"""Occurrence-aware day matching (audit: weekly runs were matched as every day)."""
from datetime import date, timedelta

import json

import pytest

from src.models import Event
from src.query import Filter, event_days, parse_days, query, resolve_when, sessions_uncertain

TODAY = date(2026, 11, 16)          # Monday (frozen; query() always gets today=)
FRI, SAT, SUN = date(2026, 11, 20), date(2026, 11, 21), date(2026, 11, 22)


def ev(title="X", d=TODAY, **kw):
    base = dict(title=title, venue="V", date=d, url="https://x.test/a", source="V")
    base.update(kw)
    return Event(**base)


def titles(evs):
    return sorted(e.title for e in evs)


def q(events, stats=None, **kw):
    kw.setdefault("when", "next:14")
    return query(events, Filter(**kw), today=TODAY, stats=stats)


WORDPLAY = ev("WordPlay", FRI, kind="run", end_date=FRI + timedelta(days=28), byday=[4], time="19:30")
FREESTYLE = ev("Family Freestyle", SAT, kind="run", end_date=SAT + timedelta(days=35), byday=[5])


def test_weekly_run_only_on_its_weekday():
    days = event_days(WORDPLAY, TODAY, TODAY + timedelta(days=20))
    assert days == [FRI, FRI + timedelta(days=7), FRI + timedelta(days=14)]
    assert all(d.weekday() == 4 for d in days)
    assert q([WORDPLAY], when="today") == []
    assert q([WORDPLAY], when="tomorrow") == []
    assert titles(q([WORDPLAY], when=f"range:{FRI}..{FRI}")) == ["WordPlay"]
    assert q([WORDPLAY], when=f"range:{SAT}..{SUN}") == []
    assert q([WORDPLAY], days="weekend") == []
    assert titles(q([WORDPLAY], days="fri")) == ["WordPlay"]
    assert q([WORDPLAY], days="sat,sun") == []


def test_weekly_run_weekday_filter_and_weekend_filter():
    assert q([FREESTYLE], days="weekday") == []
    assert titles(q([FREESTYLE], days="weekend")) == ["Family Freestyle"]
    assert q([FREESTYLE], when=f"range:{FRI}..{FRI}") == []
    assert resolve_when("weekend", TODAY) == (SAT, SUN)
    assert titles(q([FREESTYLE], when="weekend")) == ["Family Freestyle"]
    assert q([FREESTYLE], when=f"range:{SUN}..{SUN}") == []      # Saturdays only, not Sunday


def test_weekend_byday_5_6():
    e = ev("Weekend workshop", SAT, kind="run", end_date=SAT + timedelta(days=30), byday=[5, 6])
    assert event_days(e, TODAY, SUN + timedelta(days=1)) == [SAT, SUN]
    assert titles(q([e], when="weekend")) == ["Weekend workshop"]
    assert q([e], days="weekday") == []
    assert q([e], when=f"range:{FRI}..{FRI}") == []


def test_range_only_run_matches_every_day_but_is_flagged():
    e = ev("Range only", TODAY - timedelta(days=3), kind="run", end_date=TODAY + timedelta(days=40),
           sessions_known=False, category="workshop")
    assert sessions_uncertain(e)
    assert len(event_days(e, TODAY, TODAY + timedelta(days=6))) == 7
    st: dict = {}
    assert titles(q([e], st, when="today")) == ["Range only"]
    assert st["check_days"] == 1
    assert titles(q([e], days="weekend")) == ["Range only"]
    assert not sessions_uncertain(WORDPLAY)


def test_open_ended_recurring_series():
    e = ev("Every Saturday", SAT, kind="recurring", byday=[5], start_known=False)
    far = TODAY + timedelta(days=60)
    days = event_days(e, TODAY, far, today=TODAY)
    assert days[0] == SAT and all(d.weekday() == 5 for d in days) and len(days) == 8
    assert titles(q([e], when="range:" + f"{far - timedelta(days=6)}..{far}")) == ["Every Saturday"]
    assert q([e], days="weekday") == []


def test_recurring_instance_is_single_day():
    inst = ev("Instance", SAT, kind="recurring", recurrence="Saturdays")   # contract: instance = byday None
    assert event_days(inst, TODAY, TODAY + timedelta(days=30)) == [SAT]
    assert q([inst], when=f"range:{SAT + timedelta(days=7)}..{SAT + timedelta(days=7)}") == []


def test_start_unknown_matches_from_today():
    # stored at crawl time a few days ago with date = crawl day; "until X" = on now
    e = ev("Until Jan", TODAY - timedelta(days=5), kind="run", end_date=TODAY + timedelta(days=50),
           start_known=False, sessions_known=False)
    assert titles(q([e], when="today")) == ["Until Jan"]
    # even if the stored date were later than today, an unknown start still counts as on now
    e2 = ev("Until Jan2", TODAY + timedelta(days=3), kind="run", end_date=TODAY + timedelta(days=50),
            start_known=False, byday=[0])
    assert titles(q([e2], when="today")) == ["Until Jan2"]


def test_past_never_matches():
    past_one = ev("Past", TODAY - timedelta(days=1))
    past_run = ev("Ended", TODAY - timedelta(days=30), kind="run", end_date=TODAY - timedelta(days=1), byday=[4])
    assert q([past_one, past_run], when=f"range:{TODAY - timedelta(days=10)}..{TODAY + timedelta(days=5)}") == []
    weekly = ev("Weekly", TODAY - timedelta(days=21), kind="run", end_date=TODAY + timedelta(days=21), byday=[0])
    # Monday run: earlier Mondays in a past-reaching range don't count, today does
    assert titles(q([weekly], when=f"range:{TODAY - timedelta(days=14)}..{TODAY}")) == ["Weekly"]
    assert q([weekly], when=f"range:{TODAY - timedelta(days=14)}..{TODAY - timedelta(days=1)}") == []


def test_parse_days_and_week_windows():
    assert parse_days("any") is None
    assert parse_days("weekday") == frozenset(range(5))
    assert parse_days("Fridays") == {4}
    assert parse_days("sat,sun") == {5, 6}
    with pytest.raises(ValueError):
        parse_days("someday")
    assert resolve_when("this-week", TODAY) == (TODAY, TODAY + timedelta(days=6))
    assert resolve_when("next-week", TODAY) == (TODAY + timedelta(days=7), TODAY + timedelta(days=13))
    assert resolve_when("this-week", SUN) == (SUN, SUN)


def test_stale_open_series_still_matches():
    # (a) stored weeks ago (first session then), byday [5]: still on every Saturday
    for sk in (True, False):
        e = ev("Every Sat", TODAY - timedelta(days=30), kind="recurring", byday=[5], start_known=sk)
        assert titles(q([e], when="next:14")) == ["Every Sat"]
        assert event_days(e, TODAY, TODAY + timedelta(days=13), today=TODAY) == [SAT, SAT + timedelta(days=7)]
        assert not e.is_one_off


def test_series_from_date_via_build():
    # (b) "Every Saturday from <date>" through build_events -> weekly days
    from src.config import VenueConfig, get_city
    from src.models import RawEvent
    from src.sources.base import SourceCtx
    from src.sources.build import build_events
    ctx = SourceCtx(http=None, today=TODAY, window_days=365, city=get_city("london"))
    cfg = VenueConfig(name="Test Venue", url="https://example.org/")
    raw = RawEvent(title="Sat club", date_text=f"Every Saturday from {SAT.day} {SAT:%B}",
                   url="https://example.org/e")
    (e,) = build_events([raw], cfg, ctx)
    assert e.kind == "recurring" and e.byday == [5] and e.end_date is None
    assert event_days(e, TODAY, TODAY + timedelta(days=20), today=TODAY) == [SAT + timedelta(days=7 * i) for i in range(3)]
    from src.events_db import _last_day
    assert _last_day(json.loads(e.model_dump_json())) is None


def test_bad_days_raises_without_events():
    with pytest.raises(ValueError):
        q([], days="someday")


def test_legacy_run_without_byday_still_spans():
    e = ev("Legacy", TODAY - timedelta(days=10), kind="run", end_date=TODAY + timedelta(days=10))
    assert titles(q([e], when="tomorrow")) == ["Legacy"]
