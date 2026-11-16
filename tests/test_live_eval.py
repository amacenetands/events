"""§1.7 live fill-rate check: src/eval/live.py, eval --live, digest health footer (offline)."""
import json
from datetime import date, timedelta

from src.digest import health_footer
from src.eval import live
from src.eval.cli import main as eval_main
from src.models import Event

TODAY = date.today()
LIST = "https://venue.example/whats-on/"
OK = {"fetch": "ok", "source": "llm"}


def ev(source="V", time=None, price=None, url=LIST, sk=True):
    return Event(title="t", venue=source, date=TODAY + timedelta(days=1), time=time, price=price,
                 url=url, source=source, sessions_known=sk)


def evs(source, n):
    return [ev(source)] * n


def rows_for(summary, events, path, prev_db=None, stored=None, ts="2000-01-01T00:00:00+00:00", **kw):
    r = live.venue_rows("london", summary, events, {}, live.load_prev("london", path), prev_db, stored, ts=ts, **kw)
    live.append(r, path)
    return {x["venue"]: x for x in r}


def test_fill_rates():
    e = [ev(time="19:30", price="£5", url="https://venue.example/e/1"),
         ev(url="https://venue.example/whats-on"), ev(sk=False, time="10:00"), ev(price="Free")]
    assert live.fill_rates(e, LIST) == {"time": 0.5, "price": 0.5, "url_own": 0.25, "sessions_known": 0.75}
    assert live.fill_rates([], LIST) == {k: None for k in live.FILL_FIELDS}


def test_flag_reason_needs_prev_of_five():
    assert live.flag_reason(0, 12) == "0 (was 12)"
    assert live.flag_reason(5, 10) == "-50% (10→5)"
    assert live.flag_reason(6, 10) is None and live.flag_reason(30, 10) is None
    assert live.flag_reason(0, 4) is None and live.flag_reason(0, None) is None


def test_rows_fields_and_api_sources(tmp_path):
    p = tmp_path / "eval.jsonl"
    by = rows_for({"A": OK}, [ev("A", time="19:00")] * 6 + [ev("Skiddle", url="https://s/1")], p,
                  prev_db={"A": 6}, stored={"A": 6, "Skiddle": 1}, tag=None, mode="parse_only")
    a = by["A"]
    assert (a["events"], a["stored"], a["prev_events"], a["delta_pct"], a["flag"]) == (6, 6, 6, 0.0, None)
    assert (a["tag"], a["mode"], a["fill"]["time"]) == (None, "parse_only", 1.0)
    assert by["Skiddle"]["fetch"] == "api"


def test_blocked_crawl_keeps_prev_and_recovery_flags(tmp_path):
    p = tmp_path / "eval.jsonl"
    rows_for({"V": OK}, evs("V", 40), p, stored={"V": 40}, ts="2000-01-01T00:00:00+00:00")
    b = rows_for({"V": {"fetch": "blocked", "source": None}}, [], p, stored={"V": 40},
                 ts="2000-01-02T00:00:00+00:00")["V"]
    assert (b["events"], b["stored"], b["flag"]) == (0, 40, "fetch failed, rows kept")
    assert live.load_prev("london", p) == {"V": 40}            # the blocked line is not a baseline
    r = rows_for({"V": OK}, evs("V", 5), p, stored={"V": 5}, ts="2000-01-03T00:00:00+00:00")["V"]
    assert r["prev_events"] == 40 and r["flag"] == "-88% (40→5)"


def test_thin_source_rows_kept_is_not_a_plain_drop(tmp_path):
    p = tmp_path / "eval.jsonl"
    rows_for({"V": OK}, evs("V", 10), p)
    r = rows_for({"V": OK}, [], p, stored={"V": 10}, ts="2000-01-02T00:00:00+00:00")["V"]
    assert r["flag"] == "no events this crawl, 10 rows kept (was 10)"


def test_latest_ignores_tagged_runs_and_trim(tmp_path):
    p = tmp_path / "eval.jsonl"
    rows_for({"A": OK, "B": OK}, evs("A", 9) + evs("B", 9), p, ts="2000-01-01T00:00:00+00:00")
    rows_for({"A": OK}, evs("A", 1), p, ts="2000-01-02T00:00:00+00:00", tag="family")
    rows_for({"B": OK}, evs("B", 8), p, ts="2000-01-03T00:00:00+00:00")
    got = {r["venue"]: (r["events"], r["ts"][:10]) for r in live.latest("london", p)}
    assert got == {"A": (9, "2000-01-01"), "B": (8, "2000-01-03")}
    assert live.latest("moscow", p) == []
    live.append([], p, keep=2)
    assert sorted({r["ts"][:10] for r in live._read(p)}) == ["2000-01-02", "2000-01-03"]


def test_read_skips_bad_lines(tmp_path):
    p = tmp_path / "eval.jsonl"
    p.write_text('not json\n{"city": "london"}\n' + json.dumps({"city": "london", "venue": "A", "ts": "1",
                                                                 "events": 3, "fetch": "ok"}) + "\n")
    assert [r["venue"] for r in live._read(p)] == ["A"] and live.load_prev("london", p) == {"A": 3}


def test_city_fill_is_event_weighted():
    rows = [{"events": 3, "fill": {"time": 1.0, "price": 0.0, "url_own": 1.0, "sessions_known": 1.0}},
            {"events": 1, "fill": {"time": 0.0, "price": 1.0, "url_own": 0.0, "sessions_known": 1.0}},
            {"events": 0, "fill": {k: None for k in live.FILL_FIELDS}}]
    f = live.city_fill(rows)
    assert (f["venues"], f["with_events"], f["events"], f["time"], f["price"]) == (3, 2, 4, 0.75, 0.25)


def test_eval_live_cli(tmp_path, monkeypatch, capsys):
    p = tmp_path / "eval.jsonl"
    monkeypatch.setattr(live, "EVAL_LOG", p)
    live.append(live.venue_rows("london", {"A": OK}, [], {}, {"A": 30}), p)
    assert eval_main(["--live", "--city", "london"]) == 0
    assert "A | ok | llm | 0 (was 30)" in capsys.readouterr().out
    monkeypatch.setattr(live, "EVAL_LOG", tmp_path / "none.jsonl")
    assert eval_main(["--live"]) == 0
    assert "no live eval rows" in capsys.readouterr().out


def test_health_footer_caps_stale():
    assert health_footer([], []) == ""
    out = health_footer([("Old Vic", "9.0d")], [("Barbican", "0 (was 40)")])
    assert "## Data health" in out and "Old Vic (9.0d)" in out and "Barbican (0 (was 40))" in out
    many = health_footer([(f"V{i}", "8d") for i in range(8)], [])
    assert "V4 (8d)" in many and "V5" not in many and "+3 more" in many and "Dropped" not in many
    assert "Состояние данных" in health_footer([], [("X", "0 (was 9)")], "ru")


def test_main_live_eval_writes_and_prints(tmp_path, monkeypatch, capsys):
    import main
    p = tmp_path / "eval.jsonl"
    monkeypatch.setattr(live, "EVAL_LOG", p)
    main._live_eval("london", {"Gone": {"fetch": "error", "source": None}, "Drop": OK}, [ev("Kept")],
                    {"Gone": 7, "Drop": 12}, {"Gone": 7, "Kept": 1}, tag="family", mode="crawl")
    rows = {r["venue"]: r for r in live._read(p)}
    assert set(rows) == {"Gone", "Drop", "Kept"} and rows["Kept"]["tag"] == "family"
    out = capsys.readouterr().out
    assert "⚑ Gone: fetch failed, rows kept [fetch=error]" in out and "⚑ Drop: 0 (was 12)" in out


def test_api_fed_from_latest_lines(tmp_path, monkeypatch):
    import main
    p = tmp_path / "eval.jsonl"
    monkeypatch.setattr(live, "EVAL_LOG", p)
    live.append(live.venue_rows("london", {"S": {"fetch": "ok", "source": "spektrix"}, "L": OK},
                                [ev("S"), ev("L"), ev("Skiddle")], {}, {}), p)
    assert main._api_fed("london") == {"S", "Skiddle"}
