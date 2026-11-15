import pytest
from pydantic import ValidationError

from src.models import Event, RawEvent


def test_old_event_json_loads_with_defaults():
    old = {"title": "X", "venue": "V", "date": "2030-01-05", "url": "https://v/x", "is_one_off": False}
    e = Event.model_validate(old)
    assert e.kind == "run"
    assert (e.byday, e.sessions_known, e.start_known, e.sold_out, e.raw_date) == (None, True, True, False, None)
    assert Event.model_validate_json(e.model_dump_json()) == e


def test_byday_normalised_and_validated():
    e = Event(title="X", venue="V", date="2030-01-05", url="u", byday=[5, 1, 5])
    assert e.byday == [1, 5]
    with pytest.raises(ValidationError):
        Event(title="X", venue="V", date="2030-01-05", url="u", byday=[7])


def test_raw_event_strings_only():
    r = RawEvent(title="Nutcracker", date_text="Sat 22 Nov – 4 Jan", source_kind="adapter:cards")
    assert r.time_text is None and r.source_kind == "adapter:cards"
    with pytest.raises(ValidationError):
        RawEvent(date_text="x")
