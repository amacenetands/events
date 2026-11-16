"""src.normalize.dates — corpus mined from cache/memory.db scrapes (venue in comment)."""
from datetime import date as D, timedelta

import pytest

from src.normalize import DateSpec, occurrences, parse_date_specs, parse_date_text, weekday_of
from src.normalize.dates import series_kind

T = D(2026, 11, 15)   # Sunday
ALL = frozenset(range(7))

# (text, kind, start, end, byday, times, sessions_known)
CASES = [
    # --- spec cases
    ("Sun 15th Nov 7:30pm", "one_off", D(2026, 11, 15), None, None, ("19:30",), True),          # Green Note
    ("12 November 2026 – 17 December 2026", "run", D(2026, 11, 12), D(2026, 12, 17), None, (), False),  # Sadler's Wells
    ("Thursday 19 November 10 am", "one_off", D(2026, 11, 19), None, None, ("10:00",), True),
    ("15th November 2026", "one_off", D(2026, 11, 15), None, None, (), True),
    ("17-11-2026", "one_off", D(2026, 11, 17), None, None, (), True),                          # ATG JSON-LD
    ("17 Jun – 20 Sep", "run", D(2026, 6, 17), D(2026, 9, 20), None, (), False),
    ("15–17 Nov", "run", D(2026, 11, 15), D(2026, 11, 17), None, (), False),
    ("Nov 15 - Dec 20", "run", D(2026, 11, 15), D(2026, 12, 20), None, (), False),
    ("Thursdays, 19 Nov – 17 Dec", "recurring", D(2026, 11, 19), D(2026, 12, 17), frozenset({3}), (), True),
    ("19 Nov – 17 Dec, every Thursday", "recurring", D(2026, 11, 19), D(2026, 12, 17), frozenset({3}), (), True),
    ("Fridays 13 Nov to 18 Dec", "recurring", D(2026, 11, 13), D(2026, 12, 18), frozenset({4}), (), True),
    ("every Saturday", "recurring", None, None, frozenset({5}), (), True),
    ("Saturdays", "recurring", None, None, frozenset({5}), (), True),
    ("weekly on Sundays", "recurring", None, None, frozenset({6}), (), True),
    ("daily", "run", None, None, ALL, (), True),
    ("every day", "run", None, None, ALL, (), True),
    ("weekends", "recurring", None, None, frozenset({5, 6}), (), True),
    ("15 November 2026", "one_off", D(2026, 11, 15), None, None, (), True),
    ("15th Nov", "one_off", D(2026, 11, 15), None, None, (), True),
    ("Nov 15", "one_off", D(2026, 11, 15), None, None, (), True),
    ("15/11/2026", "one_off", D(2026, 11, 15), None, None, (), True),
    ("2026-11-15", "one_off", D(2026, 11, 15), None, None, (), True),
    ("2026-11-15T19:30:00+00:00", "one_off", D(2026, 11, 15), None, None, ("19:30",), True),
    ("15 ноября", "one_off", D(2026, 11, 15), None, None, (), True),
    ("15 ноя 2026", "one_off", D(2026, 11, 15), None, None, (), True),
    ("Сб 15 ноя 19:00", "one_off", D(2026, 11, 15), None, None, ("19:00",), True),
    ("с 12 ноября по 17 декабря", "run", D(2026, 11, 12), D(2026, 12, 17), None, (), False),
    # --- mined from cached venue HTML
    ("16 September – 8 November 2026", "run", D(2026, 9, 16), D(2026, 11, 8), None, (), False),   # White Cube
    ("10 Nov 2026 - 03 Jan 2027", "run", D(2026, 11, 10), D(2027, 1, 3), None, (), False),       # Criterion
    ("23 Nov 2026 - 23 Nov 2026", "one_off", D(2026, 11, 23), None, None, (), True),             # Criterion
    ("Date: Tuesday 3 November 2026", "one_off", D(2026, 11, 3), None, None, (), True),           # Benjamin Franklin House
    ("8 November 2026", "one_off", D(2026, 11, 8), None, None, (), True),                        # Union Theatre
    ("Thu 24 Jun", "one_off", D(2027, 6, 24), None, None, (), True),                             # yearless, rolls
    ("Fri 1 Jan 2027", "one_off", D(2027, 1, 1), None, None, (), True),                          # Barbican
    ("Tue, 10 Nov 2026", "one_off", D(2026, 11, 10), None, None, (), True),                      # Dublin Castle
    ("18th November 2026", "one_off", D(2026, 11, 18), None, None, (), True),                    # Hunterian
    ("13 \n\n Nov 2026", "one_off", D(2026, 11, 13), None, None, (), True),                     # Royal Astronomical Soc
    ("Friday, 27th November 2026", "one_off", D(2026, 11, 27), None, None, (), True),            # Westminster Abbey
    ("9 – 28 Nov 2026", "run", D(2026, 11, 9), D(2026, 11, 28), None, (), False),               # Park Theatre
    ("Fri 26 Jun 2026, 12:00", "one_off", D(2026, 6, 26), None, None, ("12:00",), True),         # Barbican
    ("Monday 9th November", "one_off", D(2026, 11, 9), None, None, (), True),                    # St Bride's
    ("Wed 11 Nov 26", "one_off", D(2026, 11, 11), None, None, (), True),                         # Tower Hamlets Arts
    ("Sun 8 Nov 7.30pm", "one_off", D(2026, 11, 8), None, None, ("19:30",), True),              # Kings Place
    ("7:30 pm GMT  8th September 2026 - 21st February 2027", "run", D(2026, 9, 8), D(2027, 2, 21), None, ("19:30",), False),  # How To Academy
    ("11 December - 24 January 2027", "run", D(2026, 12, 11), D(2027, 1, 24), None, (), False),   # Globe
    ("20 October, 2026, 5:30 pm", "one_off", D(2026, 10, 20), None, None, ("17:30",), True),     # Freud Museum
    ("12 Nov, 7:00pm", "one_off", D(2026, 11, 12), None, None, ("19:00",), True),                # Questors
    ("Thu 18 Jun 2026, 19:00", "one_off", D(2026, 6, 18), None, None, ("19:00",), True),         # Barbican
    ("20 – 21 November 2026", "run", D(2026, 11, 20), D(2026, 11, 21), None, (), False),        # Leicester Sq Theatre
    ("Tue 10 Nov – Wed 23 Dec 2026", "run", D(2026, 11, 10), D(2026, 12, 23), None, (), False),  # Almeida
    ("3rd - 24th November 2026", "run", D(2026, 11, 3), D(2026, 11, 24), None, (), False),       # Westminster Abbey
    ("1 - 4 December 2026, 7.30 pm", "run", D(2026, 12, 1), D(2026, 12, 4), None, ("19:30",), False),  # Barons Court
    ("Tuesday 10th November - 7.00pm", "one_off", D(2026, 11, 10), None, None, ("19:00",), True),  # The Cockpit
    ("10 Nov, 1:00pm", "one_off", D(2026, 11, 10), None, None, ("13:00",), True),               # Goldsmiths
    ("6:00pm, 3 Nov 2026", "one_off", D(2026, 11, 3), None, None, ("18:00",), True),            # Courtauld
    ("Tue 10 Nov, 7pm", "one_off", D(2026, 11, 10), None, None, ("19:00",), True),              # Wilton's
    ("November 10 at 1:00 pm", "one_off", D(2026, 11, 10), None, None, ("13:00",), True),        # St James's Paddington
    ("10 November 8:30pm", "one_off", D(2026, 11, 10), None, None, ("20:30",), True),           # Exchange Twickenham
    ("Mon 09 th Nov - 8:00pm", "one_off", D(2026, 11, 9), None, None, ("20:00",), True),        # 606 Club
    ("Sat 7th Nov - 7:30pm", "one_off", D(2026, 11, 7), None, None, ("19:30",), True),          # Bush Hall
    ("Sun 8th Nov @ 09:55 AM - 11:05 AM", "one_off", D(2026, 11, 8), None, None, ("09:55", "11:05"), True),  # St Michael's
    ("Wednesday 23rd December, 7pm", "one_off", D(2026, 12, 23), None, None, ("19:00",), True),  # The Glitch
    ("On from 18/11/2026 to 22/11/2026", "run", D(2026, 11, 18), D(2026, 11, 22), None, (), False),  # Gunnersbury
    ("On 22/11/2026", "one_off", D(2026, 11, 22), None, None, (), True),                        # Gunnersbury
    ("09/11/2026", "one_off", D(2026, 11, 9), None, None, (), True),                            # Angel Comedy
    ("Wednesdays at 1.15 pm", "recurring", None, None, frozenset({2}), ("13:15",), True),        # Temple Church
    ("Open Wednesday to Sunday 10am - 5pm", "run", None, None, frozenset({2, 3, 4, 5, 6}), ("10:00", "17:00"), True),  # Dickens Museum
    ("Daily 11am and 1.30pm", "run", None, None, ALL, ("11:00", "13:30"), True),           # Kew
    ("Monday to Saturday, 4pm - 8pm", "run", None, None, frozenset({0, 1, 2, 3, 4, 5}), ("16:00", "20:00"), True),  # Bush Theatre
    ("26, 27 June and 3, 4 July 2026", "recurring", D(2026, 6, 26), D(2026, 7, 4), None, (), True),  # Kew
    # Russian
    ("24 июня — 30 августа", "run", D(2027, 6, 24), D(2027, 8, 30), None, (), False),          # Музей Москвы
    ("18 декабря 2025 – 24 января 2026", "run", D(2025, 12, 18), D(2026, 1, 24), None, (), False),
    ("C 15 октября 2024 г. по 15 февраля 2025 г.", "run", D(2024, 10, 15), D(2025, 2, 15), None, (), False),  # Музей Маяковского
    ("25 августа — 1 ноября 2026", "run", D(2026, 8, 25), D(2026, 11, 1), None, (), False),     # Дарвиновский
    ("22 июля 2026 19:30", "one_off", D(2026, 7, 22), None, None, ("19:30",), True),            # Polytechnic
    ("13 сентября 2026 Вс 19:00", "one_off", D(2026, 9, 13), None, None, ("19:00",), True),     # ММДМ
    ("19-20 сентября", "run", D(2026, 9, 19), D(2026, 9, 20), None, (), False),                 # Хлебозавод
    ("9–11 октября, очно и онлайн", "run", D(2026, 10, 9), D(2026, 10, 11), None, (), False),   # Прямая речь
    ("27 августа c 19:00 до 21:00", "one_off", D(2027, 8, 27), None, None, ("19:00", "21:00"), True),  # Гутенберг
    ("13 Сент", "one_off", D(2027, 9, 13), None, None, (), True),                               # Эссе
    ("по субботам", "recurring", None, None, frozenset({5}), (), True),
    ("каждый день", "run", None, None, ALL, (), True),
    # more shapes
    ("Dec 20 - Jan 5", "run", D(2026, 12, 20), D(2027, 1, 5), None, (), False),
    ("1 Oct – 20 Nov", "run", D(2026, 10, 1), D(2026, 11, 20), None, (), False),
    ("Nov 15–20", "run", D(2026, 11, 15), D(2026, 11, 20), None, (), False),
    ("Every Thursday from 19 Nov", "recurring", D(2026, 11, 19), None, frozenset({3}), (), True),
    ("Sat 12 Dec, Sat 9 Jan", "recurring", D(2026, 12, 12), D(2027, 1, 9), frozenset({5}), (), True),
    ("Sat & Sun, 21 Nov – 20 Dec", "recurring", D(2026, 11, 21), D(2026, 12, 20), frozenset({5, 6}), (), True),
    ("Weekends 21 Nov – 20 Dec", "recurring", D(2026, 11, 21), D(2026, 12, 20), frozenset({5, 6}), (), True),
    ("15 Nov 2026 — 19:30", "one_off", D(2026, 11, 15), None, None, ("19:30",), True),
    ("15 November 2026 at 12 noon", "one_off", D(2026, 11, 15), None, None, ("12:00",), True),
    ("1st of December 2026", "one_off", D(2026, 12, 1), None, None, (), True),
]


@pytest.mark.parametrize("text,kind,start,end,byday,times,sk", CASES, ids=[c[0][:40] for c in CASES])
def test_parse(text, kind, start, end, byday, times, sk):
    s = parse_date_text(text, T)
    assert s is not None
    assert (s.kind, s.start, s.end, s.byday, s.times, s.sessions_known) == (kind, start, end, byday, times, sk)
    assert s.raw == text
    assert s.start_known == (start is not None)


# weekday-bearing year-less dates, parsed with today = the page's fetch date
FETCHED = [
    ("Wed 24 Jun at 7.30pm", D(2026, 6, 20), "one_off", D(2026, 6, 24), None, None, ("19:30",), True),          # Barbican: weekday matches
    ("Saturday 9th May @ 2:00 pm", D(2026, 5, 2), "one_off", D(2026, 5, 9), None, None, ("14:00",), True),    # Ealing Club: weekday picks 2026
    ("Tuesday 16 June at 2pm, Wednesday 17 June", D(2026, 6, 12), "recurring", D(2026, 6, 16), D(2026, 6, 17), None, ("14:00",), True),  # Barbican
    ("14 сентября, ПН, 20:30", D(2026, 9, 10), "one_off", D(2026, 9, 14), None, None, ("20:30",), True),         # Джаз-клуб
    ("4 июля • СБ, 13:00", D(2026, 7, 1), "one_off", D(2026, 7, 4), None, None, ("13:00",), True),             # Ленком
    ("05 сентября, Суббота, 12:00", D(2026, 9, 1), "one_off", D(2026, 9, 5), None, None, ("12:00",), True),    # МУАР 
]


@pytest.mark.parametrize("text,today,kind,start,end,byday,times,sk", FETCHED, ids=[c[0][:40] for c in FETCHED])
def test_parse_at_fetch_date(text, today, kind, start, end, byday, times, sk):
    s = parse_date_text(text, today)
    assert (s.kind, s.start, s.end, s.byday, s.times, s.sessions_known) == (kind, start, end, byday, times, sk)
    assert not s.inconsistent


def test_weekday_never_picks_long_past_year():
    s = parse_date_text("Wed 24 Jun at 7.30pm", T)        # Wed in 2026, but that is 144 days ago
    assert s.start == D(2027, 6, 24) and s.inconsistent
    s = parse_date_text("Sat 3 Jan", T)
    assert s.start == D(2027, 1, 3) and s.inconsistent


UNTIL = [
    ("until 4 Oct", D(2026, 10, 4)),         # 42 days past: within ROLL_DAYS
    ("until 4 Sep", D(2027, 9, 4)),
    ("Closes 17 Jan 2027", D(2027, 1, 17)),
    ("through 31 Dec", D(2026, 12, 31)),
    ("On now until 31 Dec", D(2026, 12, 31)),       # V&A
    ("to 22 Nov", D(2026, 11, 22)),
    ("On until Thursday, 17 December 2026", D(2026, 12, 17)),   # V&A East
    ("Now on until 29 November 2026", D(2026, 11, 29)),         # Museum of Brands
    ("Until Sat 29 May 2027", D(2027, 5, 29)),                  # Fortune Theatre
    ("Booking Until Sunday 12 December, 2027", D(2027, 12, 12)),  # Lyric Theatre
    ("Ends: 3 January 2027", D(2027, 1, 3)),
    ("До 4 октября", D(2026, 10, 4)),                           # Музей русского импрессионизма
]


@pytest.mark.parametrize("text,end", UNTIL)
def test_until_has_unknown_start(text, end):
    s = parse_date_text(text, T)
    assert s.kind == "run" and s.start is None and not s.start_known and s.end == end
    assert s.sessions_known is False


def test_daily_until_is_run_with_all_days():
    s = parse_date_text("Daily until 3 January 2027", T)       # National Maritime Museum
    assert (s.kind, s.start, s.end, s.byday, s.sessions_known) == ("run", None, D(2027, 1, 3), ALL, True)


@pytest.mark.parametrize("text", ["TBC", "Various dates", "Date to be announced", "", "   ", "Dates TBC",
                                  "garbage %%% 99", "Mon: 11am – 11pm", "Thu", "Book now", None, 123])
def test_none(text):
    assert parse_date_text(text, T) is None


@pytest.mark.parametrize("text,inc", [
    ("Friday 19 November 2026", True),       # 19 Nov 2026 is a Thursday
    ("Thursday 19 November 2026", False),
    ("Сб 15 ноя 19:00", True),               # 15 Nov 2026 is a Sunday
    ("Sun 15 Nov", False),
    ("Tue 10 Nov – Wed 23 Dec 2026", False),
    ("Mon 10 Nov 2026", True),
])
def test_weekday_consistency(text, inc):
    s = parse_date_text(text, T)
    assert s.inconsistent is inc
    if text.startswith("Friday"):
        assert s.start == D(2026, 11, 19)    # date trusted over weekday


def test_instances_list():
    s = parse_date_text("15 Nov, 22 Nov, 29 Nov", T)
    assert s.kind == "recurring" and s.instances == (D(2026, 11, 15), D(2026, 11, 22), D(2026, 11, 29))
    assert s.byday == frozenset({6}) and s.sessions_known


def test_instances_with_times():
    s = parse_date_text("Thu 19 Nov 10am / Thu 26 Nov 10am", T)
    assert s.instances == (D(2026, 11, 19), D(2026, 11, 26)) and s.byday == frozenset({3}) and s.times == ("10:00",)


def test_year_inference_keeps_recent_past():
    assert parse_date_text("10 Nov", T).start == D(2026, 11, 10)       # 5 days ago: kept
    assert parse_date_text("16 Sep", T).start == D(2026, 9, 16)        # 60 days ago: kept
    assert parse_date_text("15 Sep", T).start == D(2027, 9, 15)        # 61 days ago: next year
    assert parse_date_text("10 Nov", D(2027, 1, 5)).start == D(2027, 11, 10)


def test_order_mdy():
    assert parse_date_text("11/12/2026", T).start == D(2026, 12, 11)
    assert parse_date_text("11/12/2026", T, order="mdy").start == D(2026, 11, 12)
    assert parse_date_text("06/22/2016", T).start == D(2016, 6, 22)    # impossible dmy -> swapped


def test_ru_dotted_only_with_lang_ru():
    assert parse_date_text("19.06", T) is None
    assert parse_date_text("19.06", T, lang="ru").start == D(2027, 6, 19)
    assert parse_date_text("19.06.2026", T).start == D(2026, 6, 19)


def test_frozen():
    s = parse_date_text("15 Nov", T)
    assert isinstance(s, DateSpec)
    with pytest.raises(Exception):
        s.start = None


@pytest.mark.parametrize("text,days", [
    ("Thursdays", {3}), ("Thu", {3}), ("every Sat & Sun", {5, 6}), ("weekends", {5, 6}),
    ("Сб/Вс", {5, 6}), ("Mon–Fri", {0, 1, 2, 3, 4}), ("Wednesday to Sunday", {2, 3, 4, 5, 6}),
    ("Fri – Mon", {4, 5, 6, 0}), ("daily", set(range(7))), ("weekdays", {0, 1, 2, 3, 4}),
    ("Tues and Weds", {1, 2}), ("по четвергам", {3}), ("Saturday", {5}),
])
def test_weekday_of(text, days):
    assert weekday_of(text) == frozenset(days)


@pytest.mark.parametrize("text", ["", "15 Nov", "Sunderland Monsoon Saturn", None])
def test_weekday_of_none(text):
    assert weekday_of(text) is None


def test_occurrences_run_byday():
    s = parse_date_text("Thursdays, 19 Nov – 17 Dec", T)
    assert occurrences(s, D(2026, 11, 15), D(2026, 11, 30)) == [D(2026, 11, 19), D(2026, 11, 26)]


def test_occurrences_bare_run_every_day():
    s = parse_date_text("15–17 Nov", T)
    assert occurrences(s, D(2026, 11, 1), D(2026, 11, 30)) == [D(2026, 11, 15), D(2026, 11, 16), D(2026, 11, 17)]


def test_occurrences_recurring_no_dates():
    s = parse_date_text("every Saturday", T)
    assert occurrences(s, D(2026, 11, 15), D(2026, 11, 29)) == [D(2026, 11, 21), D(2026, 11, 28)]
    w = parse_date_text("weekly", T)
    assert w.kind == "recurring" and w.byday is None
    assert occurrences(w, D(2026, 11, 15), D(2026, 11, 29)) == []      # window holds Saturdays


def test_occurrences_instances_and_one_off():
    s = parse_date_text("15 Nov, 22 Nov, 29 Nov", T)
    assert occurrences(s, D(2026, 11, 16), D(2026, 11, 30)) == [D(2026, 11, 22), D(2026, 11, 29)]
    o = parse_date_text("Sun 15th Nov 7:30pm", T)
    assert occurrences(o, T, T) == [T] and occurrences(o, D(2026, 11, 16), D(2026, 12, 1)) == []


def test_occurrences_unknown_start_run():
    s = parse_date_text("On now until 18 Nov", T)
    assert occurrences(s, T, D(2026, 11, 30)) == [D(2026, 11, d) for d in (15, 16, 17, 18)]


def test_weekly_without_day_is_sessions_unknown():
    s = parse_date_text("weekly", T)
    assert s.kind == "recurring" and s.byday is None and s.sessions_known is False



# --- review regressions -----------------------------------------------------------
def _k(s):
    return (s.kind, s.start, s.end)


def test_age_range_is_not_dates():
    assert _k(parse_date_text("Ages 5-7, 21 Nov", T)) == ("one_off", D(2026, 11, 21), None)


def test_time_range_dash_does_not_bridge_dates():
    s = parse_date_text("3 December 9.45am – 10.30am 29 October – sold out 3 December", D(2026, 11, 9))
    assert s.kind != "run"
    assert set(s.instances) <= {D(2026, 10, 29), D(2026, 12, 3)} and D(2026, 12, 3) in s.instances


@pytest.mark.parametrize("text", ["3 Dec, 29 Oct", "20 Nov, 18 Nov, 3 Dec"])
def test_no_borrowing_from_inferred_years(text):
    s = parse_date_text(text, T)
    assert all(d.year == 2026 for d in s.instances)


def test_unordered_instances():
    assert parse_date_text("20 Nov, 18 Nov, 3 Dec", T).instances == (D(2026, 11, 18), D(2026, 11, 20), D(2026, 12, 3))


def test_end_text_kwarg():
    s = parse_date_text("19 Nov 2026", T, end_text="17 Dec 2026")
    assert _k(s) == ("run", D(2026, 11, 19), D(2026, 12, 17)) and s.raw == "19 Nov 2026"


def test_recurrence_text_kwarg():
    s = parse_date_text("19 Nov 2026", T, end_text="17 Dec 2026", recurrence_text="Thursdays")
    assert _k(s) == ("recurring", D(2026, 11, 19), D(2026, 12, 17)) and s.byday == frozenset({3})


def test_iso_offset_converted_to_tz():
    s = parse_date_text("2026-07-20T23:30:00Z", T, tz="Europe/London")
    assert s.start == D(2026, 7, 21) and s.times == ("00:30",)
    s = parse_date_text("2026-07-20T23:30:00Z", T)
    assert s.start == D(2026, 7, 20) and s.times == ("23:30",)
    assert parse_date_text("2026-07-20T23:30:00Z", T, tz="Nowhere/Bogus").start == D(2026, 7, 20)


def test_every_needs_a_period():
    assert parse_date_text("…not available from every sales outlet. Booking fees may apply…", T) is None
    assert parse_date_text("select performances", T) is None


def test_disjoint_runs_split():
    specs = parse_date_specs("Semester One 21 September – 11 December 2026 Semester Two 18 January – 9 April", T)
    assert [_k(s) for s in specs] == [("run", D(2026, 9, 21), D(2026, 12, 11)), ("run", D(2027, 1, 18), D(2027, 4, 9))]
    m = parse_date_text("21 Sep–11 Dec 2026, 18 Jan–9 Apr", T)
    assert _k(m) == ("run", D(2026, 9, 21), D(2027, 4, 9)) and m.sessions_known is False


def test_run_then_one_off():
    specs = parse_date_specs("1 - 2 Dec 2026 7:30pm, 3 Dec 2pm", T)
    assert [(_k(s), s.times) for s in specs] == [(("run", D(2026, 12, 1), D(2026, 12, 2)), ("19:30",)),
                                                (("one_off", D(2026, 12, 3), None), ("14:00",))]
    assert parse_date_text("1 - 2 Dec 2026 7:30pm, 3 Dec 2pm", T).sessions_known is False


def test_two_ranges_same_month():
    specs = parse_date_specs("1-3 Dec & 8-10 Dec", T)
    assert [_k(s) for s in specs] == [("run", D(2026, 12, 1), D(2026, 12, 3)), ("run", D(2026, 12, 8), D(2026, 12, 10))]
    assert parse_date_text("1-3 Dec & 8-10 Dec", T).sessions_known is False


def test_single_spec_list():
    assert [_k(s) for s in parse_date_specs("15–17 Nov", T)] == [("run", D(2026, 11, 15), D(2026, 11, 17))]
    assert parse_date_specs("TBC", T) == [] and parse_date_specs(None, T) == []


@pytest.mark.parametrize("text", ["Thu 18 Jun → Tue 1 Sep", "Thu 18 Jun › Tue 1 Sep", "18 Jun » 1 Sep"])
def test_arrow_range(text):
    assert parse_date_text(text, T).kind == "run"


@pytest.mark.parametrize("text,start,end,byday", [
    ("September – November 2026", D(2026, 9, 1), D(2026, 11, 30), None),
    ("November 2026", D(2026, 11, 1), D(2026, 11, 30), None),
    ("every Friday in December", D(2026, 12, 1), D(2026, 12, 31), frozenset({4})),
    ("February 2028", D(2028, 2, 1), D(2028, 2, 29), None),
])
def test_month_only(text, start, end, byday):
    s = parse_date_text(text, T)
    assert (s.kind, s.start, s.end, s.byday) == ("run", start, end, byday)


@pytest.mark.parametrize("text", ["May contain nuts", "March on, comrades", "the October Revolution"])
def test_bare_month_word_is_not_a_date(text):
    assert parse_date_text(text, T) is None


@pytest.mark.parametrize("text,days", [("June 27 & 28", (D(2027, 6, 27), D(2027, 6, 28))),
                                       ("Nov 27 & 28", (D(2026, 11, 27), D(2026, 11, 28))),
                                       ("Nov 27, 28 and 29", (D(2026, 11, 27), D(2026, 11, 28), D(2026, 11, 29)))])
def test_partial_after_list(text, days):
    assert parse_date_text(text, T).instances == days


@pytest.mark.parametrize("text,start", [("From Mon 3 Aug 2026", D(2026, 8, 3)), ("From 3 Dec", D(2026, 12, 3)),
                                        ("Starts 19 Nov", D(2026, 11, 19)), ("3 Dec onwards", D(2026, 12, 3))])
def test_from_cue_open_run(text, start):
    s = parse_date_text(text, T)
    assert (s.kind, s.start, s.end, s.start_known) == ("run", start, None, True)


def test_weekday_after_long_range_is_byday():
    s = parse_date_text("19 Nov – 17 Dec, Thursday", T)
    assert s.kind == "recurring" and s.byday == frozenset({3}) and s.sessions_known and not s.inconsistent
    s = parse_date_text("19 Nov – 17 Dec, Saturday", T)
    assert s.byday == frozenset({5}) and not s.inconsistent


@pytest.mark.parametrize("text,days", [
    ("Daily 10am-5pm, closed Mondays", {1, 2, 3, 4, 5, 6}),
    ("Open daily except Monday", {1, 2, 3, 4, 5, 6}),
    ("Ежедневно, кроме понедельника", {1, 2, 3, 4, 5, 6}),
    ("Every day except Mon & Tue", {2, 3, 4, 5, 6}),
])
def test_closed_days_subtracted(text, days):
    assert parse_date_text(text, T).byday == frozenset(days)


def test_closed_days_on_run():
    s = parse_date_text("19 Nov – 17 Dec, closed Mondays", T)
    assert s.kind == "run" and s.byday == frozenset({1, 2, 3, 4, 5, 6})


@pytest.mark.parametrize("text,days,kind", [("Weekdays 9am", {0, 1, 2, 3, 4}, "run"), ("Sat & Sun 11am", {5, 6}, "recurring"),
                                            ("Сб, Вс 12:00", {5, 6}, "recurring")])
def test_weekday_lists_follow_series_kind(text, days, kind):
    s = parse_date_text(text, T)
    assert (s.kind, s.byday, s.sessions_known) == (kind, frozenset(days), True)


@pytest.mark.parametrize("text,days", [("First Thursday of every month", {3}), ("Every other Saturday", {5}),
                                       ("Fortnightly on Fridays", {4}), ("2nd Sunday of the month", {6})])
def test_irregular_recurrence(text, days):
    s = parse_date_text(text, T)
    assert (s.kind, s.byday, s.sessions_known) == ("recurring", frozenset(days), False)


def test_span_offsets():
    raw = "Jazz Night  Sun 15th Nov 7:30pm"
    s = parse_date_text(raw, T)
    assert raw[s.span[0]:s.span[1]] == "Sun 15th Nov"
    raw = "Talk – Thursday 19 November 2026"
    s = parse_date_text(raw, T)
    assert raw[s.span[0]:s.span[1]] == "Thursday 19 November 2026"
    assert parse_date_text("19 Nov 2026", T, end_text="17 Dec 2026").span == (0, 11)
    assert parse_date_text("every Saturday", T).span is None


def test_occurrences_max_n_and_open_start_clamp():
    s = parse_date_text("until 31 Dec 2027", T)
    occ = occurrences(s, D(2020, 1, 1), D(2027, 12, 31))
    assert occ[0] == D(2026, 12, 31) and len(occ) == 366
    assert len(occurrences(s, D(2020, 1, 1), D(2027, 12, 31), max_n=10)) == 10
    assert occurrences(None, T, T) == []


def test_calendar_widget_numbers_are_not_two_digit_years():
    specs = parse_date_specs("Sunday 26 26 October 27 27 October 28 28 October", T)
    assert all(d.year == 2026 for sp in specs for d in (sp.start, sp.end, *sp.instances) if d)
    assert parse_date_text("Wed 11 Nov 26", T).start == D(2026, 11, 11)


def test_reversed_partial_is_not_a_range():
    s = parse_date_text("30 July 2026 — 16", T)
    assert (s.kind, s.start) == ("one_off", D(2026, 7, 30))


def test_list_order_does_not_shift_borrowed_year():
    s = parse_date_text("Sunday 21st June, 3pm; Sunday 17th May 2026, 3pm", T)
    assert {d.year for d in s.instances} == {2026}


def test_range_borrowing_from_later_anchor():
    s = parse_date_specs("Closure: Monday 21st December - Monday 4th January inclusive. Back: Tuesday 5th January 2027", T)[0]
    assert (s.kind, s.start, s.end, s.inconsistent) == ("run", D(2026, 12, 21), D(2027, 1, 4), False)


def test_range_borrow_never_moves_start_into_long_past():
    s = parse_date_specs("Booking Bands for Nov - Feb, Click to Apply ALL EVENTS November 2026", T)[0]
    assert (s.kind, s.start, s.end) == ("run", D(2026, 11, 1), D(2027, 2, 28))


@pytest.mark.parametrize("text,lang", [
    ("Tue–Sun, 17 Nov – 20 Dec 2026", "en"), ("Tue-Sun 17 Nov - 20 Dec 2026", "en"),
    ("17 Nov – 20 Dec 2026, Tue–Sun", "en"),
    ("вт–вс, 17 ноября – 20 декабря 2026", "ru"), ("17 ноября – 20 декабря 2026, вт–вс", "ru"),
])
def test_weekday_range_before_or_after_date_range(text, lang):
    [s] = parse_date_specs(text, T, lang=lang)
    assert (s.kind, s.start, s.end, s.byday, s.sessions_known) == (
        "run", D(2026, 11, 17), D(2026, 12, 20), frozenset({1, 2, 3, 4, 5, 6}), True)


def test_weekday_labels_on_range_ends_stay_labels():
    [s] = parse_date_specs("Thu 19 Nov – Sat 21 Nov 2026", T)
    assert (s.start, s.end, s.byday) == (D(2026, 11, 19), D(2026, 11, 21), None)
    [s] = parse_date_specs("Sun 22 Nov", T)
    assert (s.kind, s.start) == ("one_off", D(2026, 11, 22))


# ── weekly programmes (series contract: recurring + byday) and exclusion dates ──

def test_weekday_until_is_recurring_from_unknown_start():
    s = parse_date_text("Every Monday until 28 Dec", T)
    assert (s.kind, s.start, s.end, s.byday, s.start_known, s.sessions_known) == (
        "recurring", None, D(2026, 12, 28), frozenset({0}), False, True)


@pytest.mark.parametrize("text", ["Daily until 3 January 2027", "Tue–Sun, 17 Nov – 20 Dec", "Mondays to Fridays, 19 Nov – 17 Dec",
                                  "Mon–Fri until 17 Dec"])
def test_not_a_weekly_programme(text):
    assert parse_date_text(text, T).kind == "run"


def test_excluded_dates_are_not_sessions():
    [s] = parse_date_specs("Every Monday until 28th December 2026, excluding 8th June and 31st August", T)
    assert (s.kind, s.start, s.end, s.byday) == ("recurring", None, D(2026, 12, 28), frozenset({0}))
    assert {(d.month, d.day) for d in s.exdates} == {(6, 8), (8, 31)}
    assert not s.instances


def test_closed_date_in_run_is_exdate():
    end = T + timedelta(days=40)
    closed = T + timedelta(days=20)
    text = f"{T.day} {T:%b} – {end.day} {end:%b} {end.year}, closed {closed.day} {closed:%b}"
    [s] = parse_date_specs(text, T)
    assert (s.kind, s.start, s.end, s.exdates) == ("run", T, end, (closed,))
    assert closed not in occurrences(s, T, end)
    assert len(occurrences(s, T, end)) == 40


@pytest.mark.parametrize("text", ["except Mondays, 19 Nov – 17 Dec", "Daily 10am, closed Mondays"])
def test_weekday_exclusion_unchanged(text):
    s = parse_date_text(text, T)
    assert s is not None and (s.byday is None or 0 not in s.byday)



@pytest.mark.parametrize("byday,cue,kind", [
    (None, True, "run"), (set(range(7)), True, "run"), ({0, 1, 2, 3, 4}, True, "run"), ({1, 2, 3, 4, 5, 6}, True, "run"),
    ({5, 6}, True, "recurring"), ({3}, True, "recurring"), ({0, 2, 4, 5}, True, "recurring"), ({3}, False, "run"),
])
def test_series_kind(byday, cue, kind):
    assert series_kind(byday, cue) == kind


@pytest.mark.parametrize("text,days", [("Weekends", {5, 6}), ("Saturdays and Sundays until 10 Jan", {5, 6})])
def test_weekend_series_recurring(text, days):
    s = parse_date_text(text, T)
    assert (s.kind, s.byday) == ("recurring", frozenset(days))


@pytest.mark.parametrize("text,day", [("No performance on 24 Dec. Thursdays 7pm until 31 Dec", (12, 24)),
                                      ("Not on 3 Dec. Thursdays until 17 Dec", (12, 3))])
def test_exclusion_cue_does_not_leak_onto_weekdays(text, day):
    [s] = parse_date_specs(text, T)
    assert (s.kind, s.byday) == ("recurring", frozenset({3}))
    assert [(d.month, d.day) for d in s.exdates] == [day]


def test_excluded_range_expands_to_each_day():
    [s] = parse_date_specs("Daily, closed 24-26 Dec", T)
    assert [(d.month, d.day) for d in s.exdates] == [(12, 24), (12, 25), (12, 26)]


def test_exclusion_list_stops_before_a_range():
    [s] = parse_date_specs("Open daily except 25 Dec, 17 Nov – 10 Jan", T)
    assert (s.kind, s.start, s.end) == ("run", D(2026, 11, 17), D(2027, 1, 10))
    assert [(d.month, d.day) for d in s.exdates] == [(12, 25)]
