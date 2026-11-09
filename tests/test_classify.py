import datetime as dt
from src.models import Event
from src.classify import classify
from src.sources.adapters import Rec
from src.sources.reduce import rec_to_event
from src.sources.html_sites import _jsonld_class


def ev(title="X", d="2026-12-01", **kw):
    base = dict(title=title, venue="V", date=d, url="http://x", source="V")
    base.update(kw)
    return Event(**base)


def test_compat_and_derived():
    e = ev(is_one_off=False, is_kids=True, category="kids", end_date="2026-12-09")
    assert e.kind == "run" and e.audience == "kids" and e.category == "other"
    assert not e.is_one_off and e.is_kids and e.is_family
    assert ev(is_family=True).audience == "family"
    assert ev().is_one_off and not ev().is_kids


def test_category_alias_unknown():
    assert ev(category="lecture").category == "talk"
    assert ev(category="gallery").category == "exhibition"
    assert ev(category="banana").category == "other"
    assert ev(category=None).category == "other"


def test_audience_regex_and_min_age():
    e = classify([ev("Storytime for ages 3-7")])[0]
    assert e.audience == "kids" and e.min_age == 3
    e = classify([ev("Clay workshop (family)")])[0]
    assert e.audience == "family"
    e = classify([ev("Half-term fun")])[0]
    assert e.audience == "family"
    assert classify([ev("Jazz Lates: wine tasting")])[0].audience == "adult"
    e = classify([ev("Drag night 18+")])[0]
    assert e.audience == "adult" and e.min_age == 18
    assert classify([ev("Show", description="Suitable for ages 5+")])[0].min_age == 5


def test_venue_hint_upgrade_not_override():
    assert classify([ev("Puppets", venue="Little Angel Theatre", source="Little Angel")])[0].audience == "family"
    e = classify([ev("Night", venue="Polka Theatre", source="Polka Theatre", audience="adult")])[0]
    assert e.audience == "adult"


def test_recurring():
    evs = classify([ev("Tour", d=f"2026-12-0{i}") for i in (1, 2, 3)] + [ev("Other", d="2026-12-05")])
    assert [e.kind for e in evs] == ["recurring"] * 3 + ["one_off"]
    assert evs[0].recurrence == "multiple dates" and evs[0].is_one_off  # dated instance
    assert classify([ev("Yoga every Saturday")])[0].kind == "recurring"
    assert classify([ev("Show", kind="run", end_date="2026-12-01")])[0].kind == "one_off"


def test_time_fill():
    assert classify([ev("Gig", description="Doors 19:00")])[0].time == "19:00"
    assert classify([ev("Gig 7.30pm")])[0].time == "19:30"
    assert classify([ev("Gig", description="Tickets £10.50")])[0].time is None
    assert classify([ev("Gig", time="20:00", description="at 7pm")])[0].time == "20:00"


def test_jsonld_types():
    c = _jsonld_class({"@type": "MusicEvent", "name": "a"})
    assert c["category"] == "concert"
    c = _jsonld_class({"@type": "ChildrensEvent", "name": "a"})
    assert c["audience"] == "kids"
    c = _jsonld_class({"@type": "EducationEvent", "name": "Pottery workshop", "typicalAgeRange": "8-12"})
    assert c["category"] == "workshop" and c["min_age"] == 8 and c["audience"] == "kids"
    c = _jsonld_class({"@type": "Event", "name": "a", "eventSchedule": {"repeatFrequency": "P1W", "byDay": "Saturday"}})
    assert c["kind"] == "recurring" and "Saturday" in c["recurrence"]
    assert _jsonld_class({"@type": "ScreeningEvent", "name": "a"})["category"] == "film"


SITE = {"name": "S", "url": "http://s.com", "category_hint": "other"}
TODAY = dt.date(2026, 11, 1)


def test_rec_multi_date():
    evs = rec_to_event(Rec(title="Tour", date="Sat 5 Dec, Sat 12 Dec, Sat 19 Dec"), SITE, TODAY)
    assert len(evs) == 3 and all(e.kind == "recurring" for e in evs)
    assert [e.date.day for e in evs] == [5, 12, 19]
    evs = rec_to_event(Rec(title="Tour", date="5 Dec, 12 Dec"), SITE, TODAY)
    assert len(evs) == 2 and all(e.kind == "one_off" for e in evs)


def test_rec_true_range():
    evs = rec_to_event(Rec(title="Expo", date="5 Dec 2026 - 20 Feb 2027"), SITE, TODAY)
    assert len(evs) == 1 and evs[0].kind == "run" and evs[0].end_date == dt.date(2027, 2, 20)
    evs = rec_to_event(Rec(title="Expo", date="Closes 17 Jan 2027"), SITE, TODAY)
    assert evs[0].kind == "run" and evs[0].date == TODAY


def test_baby_driver_not_kids_but_baby_class_is():
    assert classify([ev("Baby Driver")])[0].audience == "all"
    assert classify([ev("Baby sensory class")])[0].audience == "kids"
    assert classify([ev("Kids' club")])[0].audience == "kids"
    assert classify([ev("Film (family)")])[0].audience == "family"


def test_recur_description_not_flipped():
    assert classify([ev("Concert", description="Museum open daily 10-5. Weekly newsletter.")])[0].kind == "one_off"
    assert classify([ev("Craft", description="Join us every Saturday")])[0].kind == "recurring"
    assert classify([ev("Daily Yoga")])[0].kind == "recurring"


def test_time_fill_skips_runs_and_needs_cue():
    run = ev("Show", kind="run", end_date="2027-01-01", description="Open 10am-5pm")
    assert classify([run])[0].time is None
    assert classify([ev("Talk", description="Doors at 7pm")])[0].time == "19:00"
    assert classify([ev("Talk", description="Tickets 10:30 minimum")])[0].time is None
    assert classify([ev("Talk 7pm")])[0].time == "19:00"


def test_public_and_online_flags():
    assert classify([ev("GCSE Revision Day")])[0].public is False
    assert classify([ev("Teacher CPD session", description="for educators")])[0].public is False
    assert classify([ev("Schools & colleges workshop")])[0].public is False
    assert classify([ev("Royal College of Music Recital")])[0].public is True
    assert classify([ev("Talk on Zoom")])[0].online is True
    assert classify([ev("Webinar: Rembrandt")])[0].online is True
    assert classify([ev("Virtual Reality Experience")])[0].online is False
    assert classify([ev("Talk", description="hybrid, virtual or in person")])[0].online is False
