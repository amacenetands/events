import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.extract as ex
import src.llm as llm
from src.sources.adapters.textutil import all_dates, set_site_host, strip_host
from src.sources.reduce import _time_of, rec_to_event
from src.sources.adapters import Rec
from src.sources import html_sites as hs

TODAY = dt.date(2026, 11, 8)
Y = dt.date.today().year + 1


def test_r2_does_not_read_hour_as_day():
    assert set(all_dates("Sat 14 Nov 19:00", TODAY)) == {dt.date(2026, 11, 14)}
    assert set(all_dates("Nov 19:00", TODAY)) == set()


def test_yearless_recent_past_dropped_old_rolls():
    assert all_dates("1 Nov", TODAY) == []                       # 7 days ago -> past
    assert all_dates("1 Jan", TODAY) == [dt.date(2027, 1, 1)]    # >60 days ago -> next year


def test_strip_host_keeps_external():
    set_site_host("https://www.x.org/whats-on")
    assert strip_host("https://x.org/e/1") == "/e/1"
    assert strip_host("https://tickets.other.com/e/1") == "https://tickets.other.com/e/1"
    set_site_host(None)


def test_time_of():
    assert _time_of(Rec(date="12.01.2027")) is None
    assert _time_of(Rec(date="17 Jan 2027, 7pm to 9pm")) == "19:00"
    assert _time_of(Rec(time="19:30", date="5 Mar")) == "19:30"


def test_closes_re_not_time_range():
    site = {"name": "V", "url": "https://v.org/", "category_hint": "other"}
    ev = rec_to_event(Rec(title="Talk here", date="17 Jan 2027, 7pm to 9pm"), site, TODAY)
    assert len(ev) == 1 and ev[0].kind == "one_off" and ev[0].end_date is None
    ev = rec_to_event(Rec(title="Show here", date="Closes 17 Jan 2027"), site, TODAY)
    assert ev[0].kind == "run" and ev[0].end_date == dt.date(2027, 1, 17)


def test_jsonld_iso_not_dayfirst():
    html = ('<script type="application/ld+json">{"@type":"Event","name":"Gig","startDate":"%d-03-05T19:00:00"}</script>' % Y)
    evs = hs._events_from_jsonld(html, "https://x.org", "X", 800)
    assert evs and evs[0]["date"] == f"{Y}-03-05"


def test_jsonld_recurring_past_start_kept():
    html = ('<script type="application/ld+json">{"@type":"Event","name":"Weekly","startDate":"2020-01-01",'
            '"eventSchedule":{"repeatFrequency":"P1W","byDay":"Saturday","endDate":"%d-12-31"}}</script>' % (Y + 1))
    evs = hs._events_from_jsonld(html, "https://x.org", "X", 365)
    assert evs and evs[0]["kind"] == "recurring"
    assert evs[0]["date"] == dt.date.today().isoformat() and evs[0]["end_date"] == f"{Y + 1}-12-31"


def test_markdown_only_detected():
    assert hs._is_markdown_only("# Title\n\n* 5 March 2027 show")
    assert not hs._is_markdown_only("<html><body><div>x</div></body></html>")


def test_parse_object_strict_prose_raises():
    with pytest.raises(ValueError):
        ex._parse_object_strict("Sorry, I cannot help with that.")
    assert ex._parse_object_strict("{}") == {}


def test_llm_config_error_not_retried(monkeypatch):
    monkeypatch.setattr(llm, "_DEEPSEEK_API_KEY", "")
    calls = []
    async def nosleep(_): calls.append(1)
    monkeypatch.setattr(llm.asyncio, "sleep", nosleep)
    with pytest.raises(llm.LLMConfigError):
        asyncio.run(llm.run_llm("x", backend="deepseek", retries=5))
    assert calls == []


def test_oldest_fetched_at():
    from src.dedup import oldest_fetched_at
    assert oldest_fetched_at([]) is None
    assert oldest_fetched_at(["https://nonexistent.invalid/zzz"]) is None
