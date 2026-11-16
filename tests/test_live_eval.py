"""§1.7 live fill-rate check: src/eval/live.py, eval --live, digest health footer (offline)."""
from datetime import date, timedelta

from src.digest import health_footer
from src.eval import live
from src.eval.cli import main as eval_main
from src.models import Event

TODAY = date.today()
LIST = "https://venue.example/whats-on/"


def ev(source="V", time=None, price=None, url=LIST, sk=True):
    return Event(title="t", venue=source, date=TODAY + timedelta(days=1), time=time, price=price,
                 url=url, source=source, sessions_known=sk)


def test_fill_rates():
    evs = [ev(time="19:30", price="£5", url="https://venue.example/e/1"),
           ev(url="https://venue.example/whats-on"), ev(sk=False, time="10:00"), ev(price="Free")]
    f = live.fill_rates(evs, LIST)
    assert f == {"time": 0.5, "price": 0.5, "url_own": 0.25, "sessions_known": 0.75}
    assert live.fill_rates([], LIST) == {k: None for k in live.FILL_FIELDS}


def test_flag_reason():
    assert live.flag_reason(0, 12) == "0 (was 12)"
    assert live.flag_reason(5, 10) == "-50% (10→5)"
    assert live.flag_reason(6, 10) is None
    assert live.flag_reason(0, None) is None and live.flag_reason(0, 0) is None
    assert live.flag_reason(30, 10) is None


def test_rows_prev_line_wins_over_db_and_api_sources(tmp_path):
    p = tmp_path / "eval.jsonl"
    summary = {"A": {"fetch": "ok", "source": "jsonld"}, "B": {"fetch": "blocked", "source": None},
               "C": {"fetch": "ok", "source": "llm"}}
    evs = [ev("A", time="19:00")] * 4 + [ev("C")] + [ev("Skiddle", url="https://s/1")]
    rows = live.venue_rows("london", summary, evs, {"A": LIST, "C": LIST}, prev={}, prev_db={"A": 4, "B": 9})
    by = {r["venue"]: r for r in rows}
    assert set(by) == {"A", "B", "C", "Skiddle"}
    assert by["B"]["flag"] == "0 (was 9)" and by["B"]["fetch"] == "blocked"
    assert by["A"]["flag"] is None and by["A"]["delta_pct"] == 0.0 and by["A"]["fill"]["time"] == 1.0
    assert by["C"]["prev_events"] is None and by["C"]["flag"] is None
    assert by["Skiddle"]["fetch"] == "api"
    live.append(rows, p)
    assert live.load_prev("london", p) == {"A": 4, "B": 0, "C": 1, "Skiddle": 1}
    # next crawl: previous line (A=4) beats the db count; -75% flags
    rows2 = live.venue_rows("london", summary, [ev("A")], {}, live.load_prev("london", p),
                            prev_db={"A": 100}, ts="2999-01-01T00:00:00+00:00")
    a = next(r for r in rows2 if r["venue"] == "A")
    assert a["prev_events"] == 4 and a["flag"] == "-75% (4→1)"
    live.append(rows2, p)
    last = live.latest("london", p)
    assert {r["ts"] for r in last} == {"2999-01-01T00:00:00+00:00"}
    assert live.latest("moscow", p) == []
    rep = live.report(last)
    assert "london" in rep and "A | ok | jsonld | -75% (4→1)" in rep


def test_city_fill_is_event_weighted():
    rows = [{"events": 3, "fill": {"time": 1.0, "price": 0.0, "url_own": 1.0, "sessions_known": 1.0}},
            {"events": 1, "fill": {"time": 0.0, "price": 1.0, "url_own": 0.0, "sessions_known": 1.0}},
            {"events": 0, "fill": {k: None for k in live.FILL_FIELDS}}]
    f = live.city_fill(rows)
    assert f["venues"] == 3 and f["with_events"] == 2 and f["events"] == 4
    assert f["time"] == 0.75 and f["price"] == 0.25


def test_eval_live_cli(tmp_path, monkeypatch, capsys):
    p = tmp_path / "eval.jsonl"
    monkeypatch.setattr(live, "EVAL_LOG", p)
    live.append(live.venue_rows("london", {"A": {"fetch": "ok", "source": "llm"}}, [], {}, {"A": 3}), p)
    assert eval_main(["--live", "--city", "london"]) == 0
    out = capsys.readouterr().out
    assert "A | ok | llm | 0 (was 3)" in out
    monkeypatch.setattr(live, "EVAL_LOG", tmp_path / "none.jsonl")
    assert eval_main(["--live"]) == 0
    assert "no live eval rows" in capsys.readouterr().out


def test_health_footer():
    assert health_footer([], []) == ""
    out = health_footer([("Old Vic", "9.0d")], [("Barbican", "0 (was 40)")])
    assert "## Data health" in out and "Old Vic (9.0d)" in out and "Barbican (0 (was 40))" in out
    assert "Состояние данных" in health_footer([], [("X", "0 (was 1)")], "ru")


def test_main_live_eval_writes_and_prints(tmp_path, monkeypatch, capsys):
    import main
    p = tmp_path / "eval.jsonl"
    monkeypatch.setattr(live, "EVAL_LOG", p)
    main._live_eval("london", {"Gone": {"fetch": "error", "source": None}}, [ev("Kept")], {"Gone": 7})
    rows = live.latest("london", p)
    assert {r["venue"] for r in rows} == {"Gone", "Kept"}
    assert "⚑ Gone: 0 (was 7) [fetch=error]" in capsys.readouterr().out
