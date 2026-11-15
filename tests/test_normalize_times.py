"""src.normalize.times — spec cases + strings mined from cache/memory.db."""
import pytest

from src.normalize import all_times, parse_time_text

FIRST = [
    ("7.30pm", "19:30"), ("7:30 pm", "19:30"), ("19:30", "19:30"), ("7pm", "19:00"), ("10 am", "10:00"),
    ("doors 7pm", "19:00"), ("10:00-12:00", "10:00"), ("10am–12pm", "10:00"), ("noon", "12:00"),
    ("midday", "12:00"), ("midnight", "00:00"), ("from 11am", "11:00"), ("12 noon", "12:00"),
    ("12pm", "12:00"), ("12am", "00:00"), ("12:30am", "00:30"), ("7 P.M.", "19:00"),
    ("2026-11-15T19:30:00+00:00", "19:30"), ("starts 7.30", "07:30"), ("doors open 19.30", "19:30"),
    ("@ 8.15", "08:15"), ("6–8pm", "18:00"), ("12–8pm", "12:00"), ("10–12pm", "10:00"), ("11-1pm", "11:00"),
    # mined
    ("1 - 4 December 2026, 7.30 pm", "19:30"),          # Barons Court
    ("Tuesday 10th November - 7.00pm", "19:00"),         # The Cockpit
    ("Cafe Games/Books: from 10.30am we'll set out", "10:30"),  # Barbican
    ("Sat 12 Dec at 2.30pm", "14:30"),                   # Barbican
    ("10 Nov, 1:00pm", "13:00"),                         # Goldsmiths
    ("Horniman: 10am – 3pm", "10:00"),
    ("6.30pm - 7.30pm", "18:30"),                        # Westminster Abbey
    ("Wednesday 24 November 2026, 6–8pm", "18:00"),      # White Cube
    ("Sun 8th Nov @ 09:55 AM - 11:05 AM", "09:55"),      # St Michael and All Angels
    ("Starts: 6:00PM", "18:00"),                         # Top Secret Comedy Club
    ("7PM - 12AM", "19:00"),                             # Royal Vauxhall Tavern
    ("Time: 7:30pm", "19:30"),                           # Phoenix Arts Club
    ("10.45am", "10:45"),                                # Arts Depot
    ("Saturday 9th May @ 2:00 pm", "14:00"),             # Ealing Club
    ("7:30 pm GMT  8th September", "19:30"),             # How To Academy
    ("8am—11pm", "08:00"),                               # Somerset House
    ("27 августа c 19:00 до 21:00", "19:00"),            # Курилка Гутенберга
    ("4 июля • СБ, 13:00", "13:00"),                     # Ленком
]


@pytest.mark.parametrize("text,want", FIRST)
def test_first_time(text, want):
    assert parse_time_text(text) == want


@pytest.mark.parametrize("text", [
    "£7.30", "15.11", "15.11.2026", "Various times", "TBC", "", None, "Ages 5-7", "Room 101",
    "£15.50 – £30.00", "24/7", "17-11-2026", "2026-11-15", "19pm", "Programme £5", "7.30",
])
def test_no_time(text):
    assert parse_time_text(text) is None


@pytest.mark.parametrize("text,want", [
    ("10:00-12:00", ["10:00", "12:00"]),
    ("10am–12pm", ["10:00", "12:00"]),
    ("Daily 11am and 1.30pm", ["11:00", "13:30"]),       # Kew
    ("6–8pm", ["18:00", "20:00"]),
    ("12pm - 12am", ["12:00", "00:00"]),                 # Half Moon
    ("Matinee 2.30pm, evening 7.30pm, £12", ["14:30", "19:30"]),
    ("7pm, 7pm and 9pm", ["19:00", "21:00"]),
])
def test_all_times(text, want):
    assert all_times(text) == want


@pytest.mark.parametrize("text,want", [
    ("7.30-10pm", ["19:30", "22:00"]),
    ("Thu 25 Jun at 3.30–4.15pm", ["15:30", "16:15"]),
    ("at 3:30–4:15pm", ["15:30", "16:15"]),
])
def test_shared_meridiem_regressions(text, want):
    assert all_times(text) == want


@pytest.mark.parametrize("text,want", [("19.30", "19:30"), ("19:30", "19:30"), ("19", "19:00"), ("7.30pm", "19:30"),
                                       ("TBC", None), ("25", None)])
def test_field_mode(text, want):
    assert parse_time_text(text, field=True) == want


def test_field_mode_off_by_default():
    assert parse_time_text("19.30") is None and parse_time_text("19") is None
