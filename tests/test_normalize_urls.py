"""src.normalize.urls.pick_event_url."""
from src.normalize import pick_event_url

L = "https://www.kingsplace.co.uk/whats-on/"


def test_arrow_link_relative():
    assert pick_event_url(["Jazz | Sun 8 Nov 7.30pm | desc -> /whats-on/jazz/foo/"], L) == \
        "https://www.kingsplace.co.uk/whats-on/jazz/foo/"


def test_never_listing_or_junk():
    lines = ["Title -> /whats-on/", "more -> /whats-on/?page=2", "x -> #top", "mailto:a@b.c",
             "y -> javascript:void(0)", "z -> /contact"]
    assert pick_event_url(lines, L) is None


def test_listing_with_trailing_slash_difference():
    assert pick_event_url(["t -> https://kingsplace.co.uk/whats-on"], L) is None


def test_pattern_wins():
    assert pick_event_url(["A -> /book/1", "B -> /events/123"], L, pattern=r"/events/\d+") == \
        "https://www.kingsplace.co.uk/events/123"


def test_same_host_beats_external_ticketing():
    lines = ["A -> https://ticketing.example.com/x", "B -> /events/123"]
    assert pick_event_url(lines, L) == "https://www.kingsplace.co.uk/events/123"


def test_external_used_when_only_option():
    assert pick_event_url(["A -> https://www.ticketweb.uk/event/xyz"], L) == "https://www.ticketweb.uk/event/xyz"


def test_nearest_first_link_and_fragment_stripped():
    lines = ["Show | 12 Nov -> /whats-on/show-a#tickets", "Other -> /whats-on/show-b"]
    assert pick_event_url(lines, L) == "https://www.kingsplace.co.uk/whats-on/show-a"


def test_markdown_and_bare_links():
    assert pick_event_url(["[More](/whats-on/a-show)"], L) == "https://www.kingsplace.co.uk/whats-on/a-show"
    assert pick_event_url(["see https://www.kingsplace.co.uk/events/9."], L) == "https://www.kingsplace.co.uk/events/9"


def test_images_skipped():
    assert pick_event_url(["img -> /media/poster.jpg", "t -> /whats-on/x"], L) == "https://www.kingsplace.co.uk/whats-on/x"


def test_filter_query_is_listing_but_id_query_is_event():
    assert pick_event_url(["t -> /whats-on/?category=kids"], L) is None
    assert pick_event_url(["t -> /whats-on/?event_id=77"], L) == "https://www.kingsplace.co.uk/whats-on/?event_id=77"


def test_empty_and_string_input():
    assert pick_event_url([], L) is None
    assert pick_event_url("t -> /whats-on/x", L) == "https://www.kingsplace.co.uk/whats-on/x"
