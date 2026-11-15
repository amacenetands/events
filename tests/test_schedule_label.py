"""digest.schedule_label / days_label — deterministic schedule text (EN + RU)."""
from datetime import date, timedelta

import pytest

from src.digest import days_label, generate_digest_v2, schedule_label
from src.models import Event
from src.query import Filter

TODAY = date(2026, 11, 16)    # Monday, frozen


def ev(d=TODAY, **kw):
    base = dict(title="T", venue="V", date=d, url="https://x.test/a", source="V")
    base.update(kw)
    return Event(**base)


@pytest.mark.parametrize("byday,en,ru", [
    (None, "", ""),
    ([], "", ""),
    ([4], "Fridays", "по пятницам"),
    ([0], "Mondays", "по понедельникам"),
    ([6], "Sundays", "по воскресеньям"),
    ([5, 6], "Sat & Sun", "сб и вс"),
    ([0, 1, 2, 3, 4], "Mon–Fri", "пн–пт"),
    ([1, 2, 3, 4, 5], "Tue–Sat", "вт–сб"),
    (list(range(7)), "daily", "ежедневно"),
    ([1, 3], "Tue & Thu", "вт и чт"),
    ([1, 3, 5], "Tue, Thu & Sat", "вт, чт и сб"),
    ([0, 6], "Sun & Mon", "вс и пн"),
    ([4, 5, 6], "Fri–Sun", "пт–вс"),
    ([5, 6, 0], "Sat–Mon", "сб–пн"),
    ([4, 5, 6, 0], "Fri–Mon", "пт–пн"),
    ([6, 0, 1, 2], "Sun–Wed", "вс–ср"),
    ([6, 0], "Sun & Mon", "вс и пн"),
    ([3, 5], "Thu & Sat", "чт и сб"),
    ([0, 2, 4], "Mon, Wed & Fri", "пн, ср и пт"),
])
def test_days_label(byday, en, ru):
    assert days_label(byday) == en
    assert days_label(byday, "ru") == ru


def test_weekly_run_with_time_future_start():
    e = ev(TODAY + timedelta(days=4), kind="run", end_date=TODAY + timedelta(days=32), byday=[4], time="19:30")
    assert schedule_label(e, TODAY) == "Fridays 19:30 from 20 Nov until 18 Dec"
    assert schedule_label(e, TODAY, "ru") == "по пятницам 19:30 с 20 ноября до 18 декабря"


def test_weekly_run_already_on():
    e = ev(TODAY - timedelta(days=3), kind="run", end_date=TODAY + timedelta(days=19), byday=[5, 6])
    assert schedule_label(e, TODAY) == "Sat & Sun until 5 Dec"


def test_daily_until():
    e = ev(TODAY - timedelta(days=30), kind="run", end_date=date(2026, 12, 7), byday=list(range(7)))
    assert schedule_label(e, TODAY) == "daily until 7 Dec"
    assert schedule_label(e, TODAY, "ru") == "ежедневно до 7 декабря"


def test_check_days_badge():
    e = ev(TODAY - timedelta(days=3), kind="run", end_date=TODAY + timedelta(days=30), sessions_known=False)
    assert schedule_label(e, TODAY) == "until 16 Dec · check days"
    assert schedule_label(e, TODAY, "ru") == "до 16 декабря · уточняйте дни"
    no_end = ev(kind="run", sessions_known=False, start_known=False)
    assert schedule_label(no_end, TODAY) == "check days"


def test_start_unknown_never_shows_from():
    e = ev(TODAY + timedelta(days=5), kind="run", end_date=TODAY + timedelta(days=40), start_known=False,
           byday=[2])
    assert schedule_label(e, TODAY) == "Wednesdays until 26 Dec"


def test_far_end_shows_year():
    e = ev(TODAY, kind="run", end_date=date(2027, 3, 1), byday=[0, 1, 2, 3, 4])
    assert schedule_label(e, TODAY) == "Mon–Fri until 1 March 2027"
    assert schedule_label(e, TODAY, "ru") == "пн–пт до 1 марта 2027"


def test_open_series_and_one_off():
    s = ev(TODAY + timedelta(days=5), kind="recurring", byday=[5], time="11:00", start_known=False)
    assert schedule_label(s, TODAY) == "Saturdays 11:00"
    assert schedule_label(ev(time="20:00"), TODAY) == "20:00"
    assert schedule_label(ev(), TODAY) == ""


def test_raw_recurrence_never_leaks():
    e = ev(TODAY, kind="run", end_date=TODAY + timedelta(days=10), byday=[3], recurrence="multiple dates")
    assert "multiple" not in schedule_label(e, TODAY)


def test_digest_uses_label_and_drops_sold_out():
    run = ev(TODAY + timedelta(days=4), title="WordPlay", kind="run", end_date=TODAY + timedelta(days=32),
             byday=[4], time="19:30", recurrence="multiple dates")
    rng = ev(TODAY - timedelta(days=2), title="Range", kind="run", end_date=TODAY + timedelta(days=30),
             sessions_known=False)
    series = ev(TODAY + timedelta(days=5), title="Every Sat", kind="recurring", byday=[5], start_known=False)
    gone = ev(TODAY + timedelta(days=1), title="Gone", sold_out=True)
    series.price = "£5"
    md = generate_digest_v2([run, rng, series, gone], Filter(when="next:14"), today=TODAY)
    assert "Saturdays · £5" in md
    assert "WordPlay" in md and "Fridays 19:30 from 20 Nov until 18 Dec" in md
    assert "multiple dates" not in md
    assert "until 16 Dec · check days" in md
    assert "Every Sat" in md and "Saturdays" in md
    assert "Sold out (check returns): [Gone](https://x.test/a)" in md and "1 sold out" in md
    assert md.count("Gone") == 1
    md2 = generate_digest_v2([gone], Filter(when="next:14"), picks=[gone], today=TODAY)
    assert "Top picks" not in md2 and "Sold out (check returns)" in md2
