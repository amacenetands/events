"""Gold-set eval harness (src/eval): offline run over tests/gold/, known-failure ledger, drift.

Every gold venue carries its own capture date (meta.yaml `today`), so results never depend on
the wall clock; the LLM venues replay their recorded llm_reply.json; the network is blocked."""
import json
import shutil
import socket
import warnings
from collections import Counter
from datetime import date

import pytest
import yaml

from src.eval import FIELDS, run_gold
from src.eval.capture import MAX_BYTES
from src.eval.cli import main as eval_main
from src.eval.drift import F1_TOLERANCE, baseline_dict, compare, summary_line
from src.eval.run import (GOLD_DIR, Counts, StaleReply, _event_id, _match, load_gold, load_known_failures,
                          meta_divergence, norm_price, title_ratio)
from src.models import Event

BASELINE = GOLD_DIR / "baseline.json"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise RuntimeError("tests/test_eval.py must not touch the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(scope="module")
def report():
    return run_gold()


# ── gold set shape ───────────────────────────────────────────────────────────

def test_gold_set_shape():
    gold = load_gold()
    assert len(gold) >= 14
    assert sum(len(g.expected) for g in gold) >= 60
    assert {"jsonld", "adapter", "follow_links", "spektrix", "llm"} <= {g.source for g in gold}
    for g in gold:
        assert {"name", "url", "extract", "today", "source"} <= set(g.meta), g.slug
        assert g.meta.get("notes"), f"{g.slug}: meta notes (source of truth) missing"
        files = [g.dir / "page.html"] if g.source != "spektrix" else []
        files += sorted((g.dir / "details").glob("*.html"))
        for f in files:
            assert f.exists() and len(f.read_bytes()) <= MAX_BYTES, f
        if g.source == "llm":
            rep = json.loads((g.dir / "llm_reply.json").read_text())
            assert rep["text_hash"] and isinstance(rep["records"], list), g.slug
        for e in g.expected:
            assert e.get("evidence"), f"{g.slug} | {e['title']}: no evidence"
            assert e.get("kind") in ("one_off", "run", "recurring"), (g.slug, e["title"])


def test_gold_ids_unique():
    ids = Counter(_event_id(g.slug, e) for g in load_gold() for e in g.expected)
    assert [i for i, n in ids.items() if n > 1] == []


def test_gold_covers_the_audit_cases():
    exp = [e for g in load_gold() for e in g.expected]
    assert any(e["sessions_known"] is False for e in exp)               # range-only run
    assert any(e["start_known"] is False for e in exp)                  # "until" / "On now until"
    assert any(e["kind"] == "recurring" and e.get("byday") for e in exp)   # weekday series
    assert any(norm_price(e.get("price")) == "0" for e in exp)          # free
    assert any("–" in str(e.get("price") or "") or " - " in str(e.get("price") or "") for e in exp)
    assert any("DD-MM-YYYY" in e["evidence"] for e in exp)


def test_meta_overrides_vs_sites_entries():
    """Meta `venue:` overrides that differ from sites/<city>.yaml: a warning, not a failure."""
    div = {g.slug: d for g in load_gold() if (d := meta_divergence(g))}
    if div:
        warnings.warn(f"gold meta overrides differ from sites entries: {div}")


# ── harness ──────────────────────────────────────────────────────────────────

def test_every_venue_produces(report):
    assert set(report.venues) == {g.slug for g in load_gold()}
    for slug, v in report.venues.items():
        assert v["won"], f"{slug}: no source produced events ({v['errors']})"


def _diff(report, known, slugs=None):
    got = {m.key: m.got for m in report.mismatches if slugs is None or m.venue in slugs}
    want = {k: v["got"] for k, v in known.items() if slugs is None or k[0] in slugs}
    new = sorted((k for k in got if k not in want or want[k] != got[k]), key=str)
    fixed = sorted((k for k in want if k not in got), key=str)
    return new, fixed, got


def test_failures_equal_known_failures(report):
    """Every gold mismatch is a known, explained bug with the same wrong value; a fixed bug
    shows up as an unexpected pass."""
    known = load_known_failures()
    new, fixed, got = _diff(report, known)
    lines = {m.key: m.line() for m in report.mismatches}
    msg = ""
    if new:
        msg += "NEW failures (fix, or list in tests/gold/KNOWN_FAILURES.yaml with got):\n  " + \
            "\n  ".join(f"{lines[k]}  [known got: {known.get(k, {}).get('got', '-')}]" for k in new)
    if fixed:
        msg += "\nUNEXPECTED PASS (remove from KNOWN_FAILURES.yaml, --update-baseline):\n  " + \
            "\n  ".join(" | ".join(map(str, k)) for k in fixed)
    assert not new and not fixed, msg
    assert all(v["reason"] for v in known.values()), "every known failure needs a reason"


def test_known_failure_with_changed_value_is_new(report):
    known = load_known_failures()
    if not known:
        pytest.skip("KNOWN_FAILURES.yaml is empty: nothing to tamper with")
    k = next((k for k in known if known[k]["got"] is not None), next(iter(known)))
    tampered = {**known, k: {**known[k], "got": "999"}}
    new, _, _ = _diff(report, tampered)
    assert new == [k]


def test_llm_venues_replay_recorded_reply(report):
    llm = [s for s, v in report.venues.items() if v["source"] == "llm"]
    assert len(llm) == 3
    for slug in llm:
        assert report.venues[slug]["won"] == "llm", slug


def test_stale_reply_fails(tmp_path):
    shutil.copytree(GOLD_DIR / "jazz-esse", tmp_path / "jazz-esse")
    p = tmp_path / "jazz-esse" / "llm_reply.json"
    rep = json.loads(p.read_text())
    p.write_text(json.dumps({**rep, "text_hash": "0" * 16}))
    with pytest.raises(StaleReply, match="re-record"):
        run_gold(gold_dir=tmp_path)


def test_today_override_changes_window():
    """`today` drives past-dropping: a far-future today leaves nothing of a dated venue."""
    rep = run_gold(only="conway-hall", today=date(2030, 1, 1))
    assert rep.venues["conway-hall"]["found"] == 0


# ── matching / scoring helpers ───────────────────────────────────────────────

def _ev(title, d, t=None):
    return Event(title=title, venue="V", date=date.fromisoformat(d), time=t, url="https://x.org/")


def test_match_prefers_same_time_for_same_day_sessions():
    exp = [{"title": "Show", "date": "2026-12-19", "time": "10:00"},
           {"title": "Show", "date": "2026-12-19", "time": "11:30"}]
    got = [_ev("Show", "2026-12-19", "11:30"), _ev("Show", "2026-12-19", "10:00")]
    pairs, missed, extra, wrong = _match(exp, got)
    assert pairs == [(0, 1), (1, 0)] and not missed and not extra and not wrong


def test_match_title_only_within_seven_days():
    exp = [{"title": "Talk", "date": "2026-11-09"}, {"title": "Late", "date": "2026-11-09"}]
    got = [_ev("Talk", "2026-11-12"), _ev("Late", "2026-12-28")]
    pairs, missed, extra, wrong = _match(exp, got)
    assert pairs == [(0, 0)] and wrong == {0} and missed == [1] and extra == [1]


@pytest.mark.parametrize("text,want", [
    ("£10 – £30", "10"), ("From £12", "12"), ("Free", "0"), ("вход свободный", "0"),
    ("Бесплатно", "0"), ("1 500 ₽", "1500"), ("£28.75", "28.75"), (None, None), ("Pay what you can", None),
    ("£15 | £12 Advance | £8 MEMBERS", "12"),
    ("Standard £15.50 (£14 + £1.50 transaction fee) | Members £11.20 | Under 18s £7.50 (£6 + £1.50 transaction fee)",
     "15.5"),
    ("From £24 (£20 + £4 transaction fee) | Young Barbican £18", "24"),
    ("£12 | £10 Advance | FREE FOR MEMBERS", "10"),
    ("£12.50 concessions, £20 standard", "20"),
])
def test_norm_price(text, want):
    assert norm_price(text) == want


def test_price_min_override():
    from src.eval.run import expected_fields
    assert expected_fields({"title": "x", "price": "two for one, ask", "price_min": 7})["price"] == "7"


def test_title_ratio_and_counts():
    assert title_ratio("Raphael Wallfisch & Simon Callaghan", "Raphael Wallfisch and Simon Callaghan") == 1.0
    assert title_ratio("Tosca", "Adriana Mater") < 0.9
    c = Counts()
    assert c.add(None, None) and c.add("a", "a") and not c.add("a", None) and not c.add(None, "b")
    assert (c.tp, c.fp, c.fn, c.tn) == (1, 1, 1, 1)
    assert c.precision == c.recall == c.f1 == 0.5


# ── drift ────────────────────────────────────────────────────────────────────

def test_baseline_is_current(report):
    assert compare(report, json.loads(BASELINE.read_text())) == []


def test_drift_detects_field_event_and_extra_regression(report):
    base = baseline_dict(report)
    cur = json.loads(json.dumps(base))
    ok_id = next(i for i, e in cur["events"].items() if e["found"] and not e["wrong"])
    cur["events"][ok_id]["wrong"] = ["time"]
    lost_id = next(i for i, e in cur["events"].items() if e["found"] and i != ok_id)
    cur["events"][lost_id] = {"found": False, "wrong": list(FIELDS)}
    cur["fields"]["time"]["f1"] -= F1_TOLERANCE + 0.01
    spurious = f"{ok_id.split('|')[0]}|Spurious Event|2026-12-01"
    cur["extras"] = cur["extras"] + [spurious]
    drift = compare(cur, base)
    assert f"event regressed: {ok_id}: time" in drift
    assert f"event lost: {lost_id}" in drift
    assert f"new extra event: {spurious}" in drift
    assert any(d.startswith("F1 time:") for d in drift)
    cur["fields"]["time"]["f1"] = base["fields"]["time"]["f1"] - F1_TOLERANCE / 2   # within tolerance
    assert not any(d.startswith("F1 time:") for d in compare(cur, base))


def test_drift_from_injected_pipeline_regression(tmp_path, report):
    """Gold copy whose page loses a time: the harness run itself reports the regression."""
    shutil.copytree(GOLD_DIR / "conway-hall", tmp_path / "conway-hall")
    page = tmp_path / "conway-hall" / "page.html"
    page.write_text(page.read_text().replace('"startDate": "2026-11-18 13:10:00"', '"startDate": "2026-11-18"'))
    drift = compare(run_gold(gold_dir=tmp_path), baseline_dict(report))
    assert "event regressed: conway-hall|Edward Lloyd|2026-11-18T13:10: time" in drift


def test_new_spurious_event_fails_baseline(tmp_path, report):
    shutil.copytree(GOLD_DIR / "young-vic", tmp_path / "young-vic")
    exp = tmp_path / "young-vic" / "expected.yaml"
    rows = yaml.safe_load(exp.read_text())
    exp.write_text(yaml.safe_dump(rows[1:], allow_unicode=True))     # gold loses one -> pipeline extra
    drift = compare(run_gold(gold_dir=tmp_path), baseline_dict(report))
    assert any(d.startswith("new extra event: young-vic|") for d in drift)


def test_cli_baseline_exit_codes(tmp_path, capsys):
    assert eval_main(["--quiet", "--baseline", str(BASELINE)]) == 0
    bad = json.loads(BASELINE.read_text())
    bad["fields"]["date"]["f1"] = 1.5
    p = tmp_path / "b.json"
    p.write_text(json.dumps(bad))
    assert eval_main(["--quiet", "--baseline", str(p)]) == 1
    out = capsys.readouterr().out
    assert "DRIFT" in out and "F1 date" in out
    j = tmp_path / "r.json"
    assert eval_main(["--quiet", "--only", "young-vic", "--json", str(j)]) == 0
    assert json.loads(j.read_text())["coverage"]["expected"] == 8
    with pytest.raises(SystemExit):
        eval_main(["--today", "2026-11-20", "--baseline", str(BASELINE)])


def test_cli_capture_and_record_refuse_overwrite():
    with pytest.raises(SystemExit, match="--force"):
        eval_main(["--capture", "conway-hall", "--url", "https://www.conwayhall.org.uk/upcomingconcerts/"])
    with pytest.raises(SystemExit, match="--force"):
        eval_main(["--record-llm", "jazz-esse"])


def test_summary_line(report):
    line = summary_line(report)
    assert line.startswith("eval: ") and "date=1.00" in line and "F1 title=" in line and "mismatches" in line


def test_known_failures_file_is_valid_yaml():
    rows = yaml.safe_load((GOLD_DIR / "KNOWN_FAILURES.yaml").read_text())
    slugs = {g.slug for g in load_gold()}
    assert all(r["venue"] in slugs and r["got"] and r["reason"] for r in rows)
