"""rank: range-only runs sink ('check days'); sold out excluded from picks."""
from datetime import date, timedelta

from src.models import Event
from src.query import Filter
from src.rank import RankCtx, build_prompt, candidates, score, score_parts

TODAY = date(2026, 11, 16)
CTX = RankCtx(today=TODAY, profile={}, weights={})


def ev(title="X", **kw):
    base = dict(title=title, venue="V", date=TODAY - timedelta(days=2), url="https://x.test/a",
                source="V", kind="run", end_date=TODAY + timedelta(days=30), category="workshop")
    base.update(kw)
    return Event(**base)


def test_range_only_run_sinks_below_concrete_sessions():
    vague = ev("vague", sessions_known=False)
    concrete = ev("concrete", byday=[4], time="19:30")
    weekly_no_time = ev("weekly", byday=[4])
    assert "check days" in score_parts(vague, Filter(), CTX)
    assert score(vague, Filter(), CTX) < score(weekly_no_time, Filter(), CTX) < score(concrete, Filter(), CTX)


def test_short_range_not_penalised():
    short = ev("short", end_date=TODAY + timedelta(days=3), date=TODAY - timedelta(days=1), sessions_known=False)
    assert "check days" not in score_parts(short, Filter(), CTX)


def test_sold_out_excluded_and_penalised():
    so = ev("so", kind="one_off", end_date=None, date=TODAY + timedelta(days=1), sold_out=True)
    assert candidates([so]) == []
    assert score_parts(so, Filter(), CTX)["sold out"] < -10


def test_prompt_uses_days_label_not_raw_recurrence():
    e = ev("w", byday=[5, 6], recurrence="multiple dates")
    p = build_prompt([e], {}, 1, Filter(), TODAY)
    assert "Sat & Sun" in p and "multiple dates" not in p


def test_prompt_falls_back_to_recurrence_and_check_days_only_long():
    inst = ev("i", kind="recurring", end_date=None, date=TODAY + timedelta(days=5), recurrence="Saturdays")
    assert "Saturdays" in build_prompt([inst], {}, 1, Filter(), TODAY)
    short = ev("s", sessions_known=False, date=TODAY, end_date=TODAY + timedelta(days=3))
    assert "check days" not in build_prompt([short], {}, 1, Filter(), TODAY)
    assert "check days" in build_prompt([ev("l", sessions_known=False)], {}, 1, Filter(), TODAY)
