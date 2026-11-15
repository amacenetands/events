from datetime import date

import pytest

from src.models import Event
from src.query import Filter, query, resolve_when, describe, preset_filter, filter_from_dict, parse_time_arg
from src.digest import generate_digest_v2
from src.email_sender import markdown_to_plain, render_html
import re

TODAY = date(2026, 11, 11)      # Wednesday
SAT, SUN = date(2026, 11, 14), date(2026, 11, 15)


def ev(title="X", d="2026-11-11", **kw):
    base = dict(title=title, venue="V", date=d, url="https://x.test/a", source="V")
    base.update(kw)
    return Event(**base)


def titles(evs):
    return sorted(e.title for e in evs)


def q(events, **kw):
    kw.setdefault("when", "next:14")
    return query(events, Filter(**kw), today=TODAY)


def test_resolve_when():
    assert resolve_when("today", TODAY) == (TODAY, TODAY)
    assert resolve_when("tomorrow", TODAY) == (date(2026, 11, 12),) * 2
    assert resolve_when("weekend", TODAY) == (SAT, SUN)
    assert resolve_when("weekend", SAT) == (SAT, SUN)
    assert resolve_when("weekend", SUN) == (SUN, SUN)
    assert resolve_when("weekend", date(2026, 11, 9)) == (SAT, SUN)  # Monday
    assert resolve_when("next:7", TODAY) == (TODAY, date(2026, 11, 17))   # 7 days incl. today
    assert resolve_when("next:1", TODAY) == (TODAY, TODAY)
    assert resolve_when("next:3", TODAY) == (TODAY, date(2026, 11, 13))
    assert resolve_when("range:2026-12-01..2026-12-05") == (date(2026, 12, 1), date(2026, 12, 5))
    with pytest.raises(ValueError):
        resolve_when("range:2026-12-05..2026-12-01")
    with pytest.raises(ValueError):
        resolve_when("someday")


def test_overlap_semantics():
    evs = [
        ev("one_in", "2026-11-12"), ev("one_out", "2026-11-20"), ev("one_past", "2026-11-01"),
        ev("run_ongoing", "2026-10-01", end_date="2026-12-01", kind="run"),
        ev("run_ended", "2026-10-01", end_date="2026-11-05", kind="run"),
        ev("run_future", "2026-12-20", end_date="2027-01-20", kind="run"),
        ev("run_starts_in_window", "2026-11-13", end_date="2027-01-20", kind="run"),
        ev("rec_in", "2026-11-14", kind="recurring"), ev("rec_out", "2026-11-28", kind="recurring"),
    ]
    got = titles(q(evs, when="next:7"))
    assert got == ["one_in", "rec_in", "run_ongoing", "run_starts_in_window"]
    assert titles(q(evs, when="next:7", kinds=frozenset({"one_off"}))) == ["one_in"]
    assert titles(q(evs, when="today")) == ["run_ongoing"]


def test_audience_family_includes_kids():
    evs = [ev("k", audience="kids"), ev("f", audience="family"), ev("a", audience="adult"), ev("n")]
    assert titles(q(evs, audience="family")) == ["f", "k"]
    assert titles(q(evs, audience="kids")) == ["k"]
    assert titles(q(evs, audience="adult")) == ["a", "n"]
    assert len(q(evs)) == 4


def test_min_age_max():
    evs = [ev("a", audience="kids", min_age=3), ev("b", audience="kids", min_age=9), ev("c", audience="kids")]
    assert titles(q(evs, audience="family", min_age_max=5)) == ["a", "c"]


def test_time_buckets():
    evs = [ev("m", time="09:30"), ev("a", time="12:00"), ev("a2", time="16:59"), ev("e", time="17:00"),
           ev("none"), ev("run", d="2026-11-01", end_date="2026-12-01", kind="run")]
    f = dict(time_of_day=frozenset({"evening"}))
    assert titles(q(evs, **f)) == ["e", "run"]   # untimed one-off with no inferable bucket is hidden
    assert titles(q(evs, strict_time=True, **f)) == ["e", "run"]
    assert titles(q(evs, time_of_day=parse_time_arg("daytime"), strict_time=True)) == ["a", "a2", "m", "run"]
    assert titles(q(evs, time_of_day=frozenset({"morning"}))) == ["m", "run"]


def test_days():
    evs = [ev("wed", "2026-11-11"), ev("sat", "2026-11-14"), ev("sun", "2026-11-15"),
           ev("run_wk", "2026-11-14", end_date="2026-11-15", kind="run")]
    assert titles(q(evs, days="weekday")) == ["wed"]
    assert titles(q(evs, days="weekend")) == ["run_wk", "sat", "sun"]
    # run covering only a weekend has no weekday in window
    assert "run_wk" not in titles(q(evs, days="weekday"))


def test_categories_venues_tags_religious_newonly():
    evs = [ev("c", category="concert", venue="Oto"), ev("t", category="talk", venue="Other", source="S2"),
           ev("Choral Evensong", category="concert")]
    assert titles(q(evs, categories=frozenset({"talk"}))) == ["t"]
    assert titles(q(evs, venues=frozenset({"oto"}))) == ["c"]
    assert "Choral Evensong" not in titles(q(evs))
    assert "Choral Evensong" in titles(q(evs, drop_religious=False))
    tags = {"S2": {"museums"}, "V": set()}
    assert titles(query(evs, Filter(when="next:14", venue_tags=frozenset({"museums"})), TODAY, tags)) == ["t"]
    seen_c = lambda xs: [e for e in xs if e.title != "c"]
    got = query(evs, Filter(when="next:14", new_only=True), TODAY, unseen=seen_c)
    assert "c" not in titles(got)
    runs = [ev("r", "2026-11-01", end_date="2026-12-01", kind="run")]
    assert len(query(runs, Filter(when="today", new_only=True), TODAY, unseen=lambda xs: [])) == 1


def test_presets():
    fam = preset_filter("family")
    assert fam.audience == "family" and fam.when == "next:30" and fam.kinds == frozenset({"one_off", "run", "recurring"})
    eve = preset_filter("evening")
    assert eve.time_of_day == {"evening"} and eve.kinds == {"one_off"} and eve.when == "next:7" and eve.strict_time
    day = preset_filter("daytime")
    assert day.days == "weekday" and day.time_of_day == {"morning", "afternoon"} and day.when == "next:3"
    assert preset_filter("museums").venue_tags == {"museums"} and not preset_filter("museums").drop_religious
    assert preset_filter("moscow").city == "moscow" and preset_filter("moscow").lang == "ru"
    std = preset_filter("standard")
    assert std.when == "next:14" and std.rank is True


def test_today_override_applies_to_every_preset():
    for name in ("family", "daytime", "evening", "standard"):
        f = preset_filter(name, when="today")
        assert f.when == "today"
    evs = [ev("today", "2026-11-11", audience="family", time="10:00"), ev("later", "2026-11-12", audience="family", time="10:00"),
           ev("run", "2026-11-01", end_date="2026-12-01", kind="run", audience="family")]
    got = query(evs, preset_filter("family", when="today"), TODAY)
    assert titles(got) == ["run", "today"]


def test_filter_from_dict_rejects_unknown():
    with pytest.raises(ValueError):
        filter_from_dict({"nope": 1})
    with pytest.raises(ValueError):
        filter_from_dict({"kinds": ["bogus"]})


def test_describe():
    s = describe(Filter(when="weekend", audience="family", time_of_day=frozenset({"evening"})), TODAY)
    assert "Sat 14 Nov" in s and "family" in s and "evening" in s


def test_digest_v2_and_links():
    evs = [
        ev("Today show", "2026-11-11", time="19:30", url="https://a.test/x", audience="family", min_age=5),
        ev("Later", "2026-11-13", url="/rel/path"),
        ev("Yoga", "2026-11-12", kind="recurring", time="10:00"), ev("Yoga", "2026-11-14", kind="recurring", time="10:00"),
        ev("Expo", "2026-10-01", end_date="2026-11-15", kind="run", url="https://b.test/e"),
    ]
    md = generate_digest_v2(evs, Filter(when="next:7"), today=TODAY)
    assert "### Today" in md and "### Friday 13 Nov" in md
    assert "[family 5+]" in md and "closing soon" in md
    assert re.search(r"\[Yoga\]\(https://x.test/a\) — V · Thu 12, Sat 14 Nov · 10:00", md)
    assert "[link]" not in md
    plain = markdown_to_plain(md)
    assert "Today show — https://a.test/x" in plain and "](" not in plain and "**" not in plain
    hrefs = re.findall(r'href="([^"]+)"', render_html(md))
    assert hrefs and all(h.startswith("https://") for h in hrefs if not h.startswith("/rel"))


def test_untimed_evening_concert_hidden_in_daytime():
    from datetime import date
    from src.query import Filter, query, parse_time_arg, is_banned_url
    from src.models import Event
    mk = lambda **k: Event(**{"title": "x", "venue": "V", "date": date(2026, 11, 10), "url": "https://v.org/a", **k})
    gig = mk(title="Jazz night", category="concert")
    run = mk(title="Expo", category="exhibition", kind="run", end_date=date(2026, 12, 1))
    talk = mk(title="Talk", category="talk")
    timed = mk(title="Lunch", category="concert", time="13:00")
    f = Filter(when="range:2026-11-09..2026-11-11", time_of_day=parse_time_arg("daytime"))
    st = {}
    out = query([gig, run, talk, timed], f, today=date(2026, 11, 9), stats=st)
    assert {e.title for e in out} == {"Expo", "Lunch"} and st["untimed_hidden"] == 2
    out = query([gig, run, talk, timed], Filter(when=f.when), today=date(2026, 11, 9))
    assert len(out) == 4
    ev = query([gig], Filter(when=f.when, time_of_day=parse_time_arg("evening")), today=date(2026, 11, 9))
    assert ev == [gig]
    assert is_banned_url("https://www.eventbrite.co.uk/e/1") and not is_banned_url("https://v.org/eventbrite")
    assert query([mk(url="https://www.eventbrite.com/e/2")], Filter(when=f.when), today=date(2026, 11, 9)) == []
