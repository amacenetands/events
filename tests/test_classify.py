import datetime as dt
from src.models import Event
from src.classify import classify


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


def test_adult_age_wording_not_kids():
    from datetime import date as _d
    for t, d in [("Salsa night", "ages 18-25"), ("Walking club", "ages 50+"), ("Careers", "for 16-25 year olds"),
                 ("Revision", "GCSE and A-level students aged 14-19")]:
        e = classify([ev(t, description=d)])[0]
        assert e.audience != "kids", (t, e.audience)
    assert classify([ev("Walking club", description="ages 50+")])[0].audience == "adult"
    assert classify([ev("Rhyme time", description="ages 2-4")])[0].audience == "kids"


def test_livestream_online_and_venue_sanity():
    assert classify([ev("Couture (Livestream)")])[0].online is True
    e = classify([ev("X", venue="1 High St, London N1 9AB", source="Src")])[0]
    assert e.venue == "Src"


def test_html_entities_cleaned():
    e = ev("Tom &#038; Jerry<br>Live", description="a &amp;amp; b <b>c</b>")
    assert e.title == "Tom & Jerry / Live" and e.description == "a & b c"


def test_tidy_collapse_noise_dedupe():
    from datetime import date as _d
    from src.tidy import tidy
    T = _d(2026, 11, 9)
    play = [ev("Cranford", f"2026-11-{d}", source="OT", venue="OT", kind="recurring") for d in (10, 11, 13, 14, 17)]
    junk = [ev("Performance", "2026-11-10", source="OT"), ev("Museum Admission", "2026-11-10", source="OT"),
            ev("Old", "2026-11-01", source="OT")]
    a = ev("Talk", "2026-11-12", time="19:00", source="BM", venue="BM", url="https://x.test/")
    b = ev("Talk", "2026-11-12", time="19:00", source="Linnean", venue="BM", url="https://x.test/events/talk-1")
    out = tidy(play + junk + [a, b], T)
    runs = [e for e in out if e.title == "Cranford"]
    assert len(runs) == 1 and runs[0].kind == "run" and str(runs[0].end_date) == "2026-11-17"
    assert [e.title for e in out if e.title in ("Performance", "Museum Admission", "Old")] == []
    assert [e.source for e in out if e.title == "Talk"] == ["Linnean"]


def test_members_views_private():
    (e,) = classify([ev("Schiaparelli Members' Closing Weekend Morning Views")])
    assert e.public is False


def test_student_admin_private():
    (e,) = classify([ev("Disabled Students Allowance: Q&A Session")])
    assert e.public is False


def test_lecture_recording_online():
    (e,) = classify([ev("Taster Lecture Recording: Aphrodite", description="Online taster lecture recording available")])
    assert e.online is True


def test_touring_town_list_elsewhere():
    from src.classify import is_elsewhere
    assert is_elsewhere(ev("Paula Rego", venue="Cheltenham, Petworth, Rugby", source="Southbank Centre"))
    assert not is_elsewhere(ev("X", venue="Kings Place", source="Kings Place"))


def test_rank_prompt_rules_and_compact_rows():
    from src.rank import build_prompt, parse_llm
    from src.query import Filter
    today = dt.date(2026, 11, 15)
    pool = [ev("Kids Concert", d="2026-11-15", time="10:15", audience="kids"),
            ev("Play Days (Nov-Dec 2026)", d="2026-11-01", kind="run", end_date=dt.date(2026, 12, 18), audience="family")]
    p = build_prompt(pool, {"kids_ages": [6]}, 2, Filter(city="london", when="today", audience="family"), today)
    assert "kind=one" in p and "prioritise events made for kids" in p and "Kids ages: 6" in p
    assert "E2|Play Days|" in p and "|run|to 18Dec|" in p and "E1|Kids Concert|" in p
    adult = build_prompt(pool, {"kids_ages": [6]}, 2, Filter(city="london", when="today", audience="adult"), today)
    assert "Kids ages" not in adult and "made for kids" not in adult
    assert [e.title for e, _ in parse_llm("1. E2|closing\nE1|why", pool)] == ["Play Days (Nov-Dec 2026)", "Kids Concert"]


def test_is_elsewhere_uses_the_city_with_word_boundaries():
    from src.classify import is_elsewhere
    from src.models import Event
    def E(v):
        return Event(title="t", venue=v, date="2026-12-01", url="u", source="S")
    assert is_elsewhere(E("Cheltenham, Petworth, Rugby"))
    assert not is_elsewhere(E("Bath, Rugby, London"))
    assert is_elsewhere(E("Bath, Londonderry, Rugby"))                          # word boundary
    assert is_elsewhere(E("Bath, Rugby, London"), "moscow")                    # current city only


def test_recurring_rule_cyrillic_titles():
    # distinct Cyrillic titles (same prefix, «…» differs) on 3 dates: still one-offs
    titles = ["Круглый стол «Автономные поселения»", "Круглый стол «Нейроархитектура»", "Круглый стол «Город»"]
    evs = classify([ev(t, f"2026-12-1{i}") for i, t in enumerate(titles)])
    assert [e.kind for e in evs] == ["one_off"] * 3
    # the same Cyrillic title on 3 dates is a series
    evs = classify([ev("Экскурсия «Дом»", f"2026-12-1{i}") for i in range(3)])
    assert all(e.kind == "recurring" and e.recurrence == "multiple dates" for e in evs)


def test_recurring_rule_ignores_empty_normalised_title():
    evs = classify([ev("«»", f"2026-12-1{i}") for i in range(3)])
    assert [e.kind for e in evs] == ["one_off"] * 3


def test_recurring_rule_english_unchanged():
    evs = classify([ev("Jazz Night!", f"2026-12-1{i}") for i in range(2)] + [ev("jazz night", "2026-12-13")])
    assert all(e.kind == "recurring" for e in evs)
    evs = classify([ev(f"Talk {n}", f"2026-12-1{i}") for i, n in enumerate("ABC")])
    assert all(e.kind == "one_off" for e in evs)


def test_tidy_collapse_keeps_session_time():
    from datetime import date as _d, timedelta as _td
    from src.tidy import collapse_series
    T = _d.today()
    d = lambda n: str(T + _td(days=n))                      # noqa: E731
    mixed = [ev("Bubble Show", d(n), time=t, source="JL", venue="JL", kind="recurring")
             for n, t in ((1, "15:00"), (2, "11:00"), (3, "15:00"))]
    one = [ev("Elf", d(n), time="10:30", source="JL", venue="JL", kind="recurring") for n in (1, 2, 4)]
    none = [ev("Pigs", d(n), source="JL", venue="JL", kind="recurring") for n in (1, 2, 4)]
    out = {e.title: e for e in collapse_series(mixed + one + none)}
    assert (out["Bubble Show"].kind, out["Bubble Show"].time, out["Bubble Show"].times_vary) == ("run", "11:00", True)
    assert (out["Elf"].time, out["Elf"].times_vary) == ("10:30", False)
    assert (out["Pigs"].time, out["Pigs"].times_vary) == (None, False)


def test_tidy_keeps_cyrillic_titles():
    from datetime import date as _d, timedelta as _td
    from src.tidy import is_noise, norm_title, tidy
    T = _d.today()
    a = ev("Концерт «Времена года»", str(T + _td(days=1)), source="МФ", venue="МФ")
    b = ev("Лекция о Малевиче", str(T + _td(days=1)), source="МФ", venue="МФ")
    assert norm_title(a.title) == "концерт времена года" and not is_noise(a)
    assert {e.title for e in tidy([a, b], T)} == {a.title, b.title}
