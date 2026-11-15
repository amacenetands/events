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
