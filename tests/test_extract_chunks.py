import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.extract as ex
from src.sources.reduce import reduce_site

Y = dt.date.today().year + 1


def _site(name="Chunky", n=3):
    chunks = [f"Show {i} line | 5 March {Y}" for i in range(n)]
    return {"name": name, "url": "https://c.org/", "text": "\n".join(chunks), "chunks": chunks,
            "category_hint": "other", "hint": "", "must_determine_venue": False}


def _patch(monkeypatch, handler):
    async def fake_run_llm(prompt, backend=None, timeout=0, retries=0):
        return handler(prompt, backend)
    monkeypatch.setattr(ex, "run_llm", fake_run_llm)
    monkeypatch.setattr(ex, "save_extraction", lambda *a, **k: saved.append((a, k)))
    monkeypatch.setattr(ex, "get_cached_parse", lambda *a, **k: None)
    monkeypatch.setattr(ex.timing, "log_runtime", lambda **k: None)
    monkeypatch.setattr(ex, "current_model_label", lambda: "deepseek")
    monkeypatch.setattr(ex, "BATCH_SIZE", 1)
    monkeypatch.setattr(ex, "_CONCURRENCY", 1)
    monkeypatch.setattr(ex, "_retry_target", lambda m: ("claude", "haiku") if m != "haiku" else None)
    saved.clear()


saved: list = []


def _ev(title):
    return {"title": title, "date_text": f"5 March {Y}", "time_text": None}


class _R:
    def __init__(self, d):
        self.title = d["title"]


def _flat(res: dict) -> list:
    return [_R(d) for ds in res.values() for d in ds]


def test_failed_chunk_keeps_other_chunks(monkeypatch):
    def handler(prompt, backend):
        if "Show 1 line" in prompt:
            raise RuntimeError("boom")
        i = 0 if "Show 0 line" in prompt else 2
        return json.dumps({"Chunky": [_ev(f"Event {i}")]})
    _patch(monkeypatch, handler)
    errors: list[str] = []
    evs = _flat(asyncio.run(ex.extract_raw_batch_async([_site()], errors=errors)))
    assert sorted(e.title for e in evs) == ["Event 0", "Event 2"]
    assert errors == [("Chunky", "chunk 2/3 failed")]
    assert not saved   # partial result must not be cached


def test_multi_chunk_zero_events_retried(monkeypatch):
    calls = []

    def handler(prompt, backend):
        calls.append(backend)
        if backend == "claude":
            return json.dumps({"Chunky": [_ev("Found by retry")]})
        return "{}"
    _patch(monkeypatch, handler)
    s = _site()
    s["chunks"] = [c + f" | {7 + i} March {Y}" for i, c in enumerate(s["chunks"])]   # >=3 distinct future dates
    s["text"] = "\n".join(s["chunks"])
    errors: list[str] = []
    evs = _flat(asyncio.run(ex.extract_raw_batch_async([s], errors=errors)))
    assert [e.title for e in evs] == ["Found by retry"] and not errors
    assert calls.count("claude") == 3


def test_flat_lines_not_duplicated_after_cards():
    items = "".join(
        f'<div class="ev"><h3>Talk number {i} here</h3><p>Sunday, 7:30 pm GMT {i + 1} March {Y}</p></div>'
        for i in range(4))
    r = reduce_site(f"<html><body><main>{items}</main></body></html>", {"name": "D", "url": "https://d.org/"})
    assert sum(1 for l in r.lines if l.startswith("[C]")) == 4
    assert not any(not l.startswith("[C]") and "7:30 pm" in l for l in r.lines)


def test_chunk_done_idempotent_and_finalize_crash_recovers(monkeypatch):
    # finalisation (save_extraction) crashes once on the last chunk; the haiku
    # fallback re-reports that unit: must not double-count nor lose events.
    state = {"n": 0}

    def handler(prompt, backend):
        i = next(k for k in range(3) if f"Show {k} line" in prompt)
        return json.dumps({"Chunky": [_ev(f"Event {i}")]})
    _patch(monkeypatch, handler)

    def flaky_save(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("db locked")
    monkeypatch.setattr(ex, "save_extraction", flaky_save)
    errors: list[str] = []
    evs = _flat(asyncio.run(ex.extract_raw_batch_async([_site()], errors=errors)))
    assert sorted(e.title for e in evs) == ["Event 0", "Event 1", "Event 2"]
    assert not errors


def test_chunked_qwen_empty_upgraded_to_haiku(monkeypatch):
    calls = []

    def handler(prompt, backend):
        calls.append(backend)
        return json.dumps({"Chunky": [_ev("Haiku found")]}) if backend == "claude" else "{}"
    _patch(monkeypatch, handler)
    monkeypatch.setattr(ex, "current_model_label", lambda: "qwen")
    evs = _flat(asyncio.run(ex.extract_raw_batch_async([_site()])))
    assert [e.title for e in evs] == ["Haiku found"]
    assert calls.count("claude") == 3
