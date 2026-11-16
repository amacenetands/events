"""src/venues/fixes.apply_fixes: every `fix:` key, through build_events, frozen TODAY."""
from datetime import date

import pytest
from pydantic import ValidationError

from src.config import VenueConfig, get_city
from src.models import RawEvent
from src.sources.base import SourceCtx
from src.sources.build import build_events
from src.venues.fixes import apply_fixes

TODAY = date(2026, 11, 15)   # a Sunday
LISTING = "https://example.org/whats-on/"


def _cfg(**fix) -> VenueConfig:
    return VenueConfig(name="Fix Venue", url=LISTING, fix=fix)


def _ctx() -> SourceCtx:
    return SourceCtx(http=None, today=TODAY, window_days=365, city=get_city("london"))


def _build(cfg, *raws, ctx=None):
    return build_events(list(raws), cfg, ctx or _ctx())


def test_no_fix_is_identity():
    evs = _build(_cfg(), RawEvent(title="A", date_text="20 Nov 2026"))
    assert apply_fixes(_cfg(), evs, _ctx()) is evs


def test_drop_title_re():
    ctx = _ctx()
    evs = _build(_cfg(drop_title_re="^gift vouchers|members only"),
                 RawEvent(title="Gift Vouchers", date_text="20 Nov 2026"),
                 RawEvent(title="Talk (Members only)", date_text="21 Nov 2026"),
                 RawEvent(title="Gift of the Magi", date_text="22 Nov 2026"), ctx=ctx)
    assert [e.title for e in evs] == ["Gift of the Magi"] and ctx.stats["fix.drop_title_re"] == 2


def test_url_pattern():
    ctx = _ctx()
    evs = _build(_cfg(url_pattern=r"/events/[^/]+/$"),
                 RawEvent(title="A", date_text="20 Nov 2026", url="/events/a/"),
                 RawEvent(title="B", date_text="20 Nov 2026", url="/shop/b/"),
                 RawEvent(title="C", date_text="20 Nov 2026"), ctx=ctx)
    assert [e.url for e in evs] == ["https://example.org/events/a/", LISTING, LISTING]
    assert ctx.stats["fix.url_pattern"] == 1                     # listing fallback is not a fix


def test_time_default():
    ctx = _ctx()
    evs = _build(_cfg(time_default="19:30"), RawEvent(title="A", date_text="20 Nov 2026"),
                 RawEvent(title="B", date_text="20 Nov 2026", time_text="2.30pm"), ctx=ctx)
    assert [e.time for e in evs] == ["19:30", "14:30"] and ctx.stats["fix.time_default"] == 1


def test_date_order_reaches_parser():
    assert _build(_cfg(date_order="mdy"), RawEvent(title="A", date_text="12/01/2026"))[0].date == date(2026, 12, 1)
    assert _build(_cfg(), RawEvent(title="A", date_text="12/01/2027"))[0].date == date(2027, 1, 12)


def test_range_means_weekly_weekday_in_text():
    ctx = _ctx()
    [e] = _build(_cfg(range_means_weekly=True), RawEvent(title="WordPlay", date_text="Fri 20 Nov – Fri 18 Dec"), ctx=ctx)
    assert (e.kind, e.byday, e.sessions_known, e.end_date) == ("recurring", [4], True, date(2026, 12, 18))
    assert e.recurrence == "Fridays" and ctx.stats["fix.range_means_weekly"] == 1


def test_range_means_weekly_bare_same_weekday_ends_need_opt_in():
    raw = RawEvent(title="Family Freestyle", date_text="19 November 2026 – 17 December 2026")   # Thu..Thu
    [e] = _build(_cfg(range_means_weekly=True), raw)
    assert (e.byday, e.sessions_known) == (None, False)
    [e] = _build(_cfg(range_means_weekly={"min_days": 28, "title_re": "freestyle|culture club"}), raw)
    assert (e.byday, e.sessions_known) == ([3], True)
    [e] = _build(_cfg(range_means_weekly={"min_days": 35}), raw)                              # 28 days: too short
    assert e.byday is None and not e.sessions_known
    [e] = _build(_cfg(range_means_weekly={"title_re": "^Culture"}), raw)
    assert e.byday is None


def test_range_means_weekly_recurrence_cue_with_same_weekday_ends():
    [e] = _build(_cfg(range_means_weekly=True), RawEvent(title="Club", date_text="every week, 20 Nov – 18 Dec 2026"))
    assert (e.byday, e.sessions_known) == ([4], True)


def test_range_means_weekly_never_reads_end_labels_as_sessions():
    [e] = _build(_cfg(range_means_weekly=True), RawEvent(title="Fest", date_text="Thu 19 Nov – Sat 21 Nov 2026"))
    assert (e.kind, e.byday, e.sessions_known) == ("run", None, False)
    [e] = _build(_cfg(range_means_weekly=True), RawEvent(title="Fest", date_text="Fri 20 Nov – Sat 19 Dec 2026"))
    assert e.byday is None                                           # ends on different weekdays


def test_range_means_weekly_no_weekday_sessions_unknown():
    ctx = _ctx()
    [e] = _build(_cfg(range_means_weekly=True), RawEvent(title="Expo", date_text="19 November 2026 – 18 December 2026"),
                 ctx=ctx)
    assert (e.kind, e.byday, e.sessions_known) == ("run", None, False)
    assert "fix.range_means_weekly" not in ctx.stats


def test_range_means_weekly_leaves_one_offs_and_explicit_days():
    cfg = _cfg(range_means_weekly=True)
    [one] = _build(cfg, RawEvent(title="Gig", date_text="Fri 20 Nov 2026"))
    assert (one.kind, one.byday) == ("one_off", None)
    [run] = _build(cfg, RawEvent(title="Class", date_text="20 Nov – 18 Dec, Tuesdays & Thursdays"))
    assert run.byday == [1, 3]


def test_weekly_title_regex():
    ctx = _ctx()
    evs = {e.title: e for e in _build(_cfg(weekly={"^WordPlay": [4]}),
                                      RawEvent(title="WordPlay: Ages 0 – 2", date_text="13 November 2026 – 18 December 2026"),
                                      RawEvent(title="Nan Goldin", date_text="24 November 2026 – 14 March 2027"),
                                      RawEvent(title="WordPlay special", date_text="21 Nov 2026"), ctx=ctx)}
    w = evs["WordPlay: Ages 0 – 2"]
    assert (w.byday, w.sessions_known, w.kind, w.recurrence) == ([4], True, "recurring", "Fridays")
    assert evs["Nan Goldin"].byday is None
    assert evs["WordPlay special"].kind == "one_off" and evs["WordPlay special"].byday is None
    assert ctx.stats["fix.weekly"] == 1


def test_byday_forced_moves_start_and_skips_one_offs():
    ctx = _ctx()
    evs = _build(_cfg(byday=[5, 6]), RawEvent(title="Tour", date_text="17 Nov 2026 – 31 Jan 2027"),   # Tue start
                 RawEvent(title="Gig", date_text="21 Nov 2026"), ctx=ctx)
    assert (evs[0].kind, evs[0].byday, evs[0].date) == ("run", [5, 6], date(2026, 11, 21))   # forced opening days
    assert evs[1].byday is None and evs[1].kind == "one_off"                  # one_off: no-op
    assert ctx.stats["fix.byday"] == 1


def test_byday_no_session_in_range():
    ctx = _ctx()
    [e] = _build(_cfg(byday=[6]), RawEvent(title="Short", date_text="17 Nov 2026 – 19 Nov 2026"), ctx=ctx)
    assert e.byday is None and e.date == date(2026, 11, 17) and ctx.stats["fix.byday.no_session"] == 1


def test_weekly_skips_undated():
    [e] = _build(_cfg(weekly={"Jazz Jam": [0]}), RawEvent(title="Jazz Jam", date_text="Every week", recurrence_text="weekly"))
    assert e.date_undetermined and e.byday is None


def test_time_default_skips_invented_times():
    ctx = _ctx()
    cfg = VenueConfig(name="Fix Venue", url=LISTING, category_hint="exhibition", fix={"time_default": "19:30"})
    [ex] = _build(cfg, RawEvent(title="Expo", date_text="20 Nov 2026"))
    assert ex.time is None                                                        # exhibition
    evs = _build(_cfg(time_default="19:30"), RawEvent(title="Run", date_text="19 November 2026 – 18 December 2026"),
                 RawEvent(title="TBC", date_text="dates tbc"),
                 RawEvent(title="Weekly", date_text="20 Nov – 18 Dec 2026, Fridays"), ctx=ctx)
    assert [e.time for e in evs] == [None, None, "19:30"] and ctx.stats["fix.time_default"] == 1


def test_fix_failure_is_isolated(monkeypatch):
    from src.venues import fixes
    monkeypatch.setattr(fixes, "_one", lambda *a: 1 / 0)
    ctx = _ctx()
    evs = _build(_cfg(time_default="19:00"), RawEvent(title="A", date_text="20 Nov 2026"), ctx=ctx)
    assert [e.title for e in evs] == ["A"] and ctx.stats["fix.failed"] == 1
    assert ctx.errors[0].startswith("Fix Venue: fix failed")


@pytest.mark.parametrize("bad", [{"byday": [7]}, {"byday": []}, {"weekly": {"(": [1]}},
                                 {"weekly": {"x": [9]}}, {"time_default": "7pm"}])
def test_fix_validation(bad):
    with pytest.raises(ValidationError):
        _cfg(**bad)


def test_byday_leaves_tbc_alone():
    [e] = _build(_cfg(byday=[2]), RawEvent(title="Something", date_text="dates tbc"))
    assert e.date_undetermined and e.byday is None


def test_apply_fixes_stored_pins_kept_rows_and_is_idempotent():
    """Rows of a venue not rebuilt this crawl (disabled) still get the current `fix:` block."""
    from datetime import timedelta
    from src.models import Event
    from src.venues.fixes import apply_fixes_stored
    today = date.today()
    start = today + timedelta(days=(4 - today.weekday()) % 7 + 1)      # a Saturday, after today
    cfg = VenueConfig(name="SB", url="https://sb.example/whats-on", fix={"weekly": {"^WordPlay": [4]}})
    row = Event(title="WordPlay: Ages 0 – 2", venue="SB", source="SB", date=start, end_date=start + timedelta(days=34),
                kind="run", url="https://sb.example/whats-on")
    other = Event(title="X", venue="Y", source="Unknown", date=start, url="https://y.example/")
    once = apply_fixes_stored([row, other], [cfg], today)
    wp = next(e for e in once if e.source == "SB")
    assert wp.byday == [4] and wp.kind == "recurring" and wp.date.weekday() == 4 and wp.date > start
    assert next(e for e in once if e.source == "Unknown") is other
    assert apply_fixes_stored(once, [cfg], today) == once


def test_apply_fixes_stored_skips_rows_the_latest_crawl_built():
    from datetime import timedelta
    from src.models import Event
    from src.venues.fixes import apply_fixes_stored
    today = date.today()
    fri = today + timedelta(days=(4 - today.weekday()) % 7 + 7)
    live_cfg = VenueConfig(name="SW", url="https://sw.example/", fix={"weekly": {"^Freestyle": [5]}})
    off_cfg = VenueConfig(name="SB", url="https://sb.example/", disabled=True, fix={"weekly": {"^WordPlay": [4]}})
    enriched = Event(title="Freestyle", venue="SW", source="SW", date=fri - timedelta(days=1), kind="recurring",
                     byday=[3], url="https://sw.example/f", end_date=fri + timedelta(days=27))
    stale = Event(title="WordPlay", venue="SB", source="SB", date=fri + timedelta(days=1), kind="run",
                  end_date=fri + timedelta(days=36), url="https://sb.example/")
    old_sw = enriched.model_copy(update={"title": "Freestyle old"})
    out = apply_fixes_stored([enriched, stale, old_sw], [live_cfg, off_cfg], today,
                             crawl_ids=["new", "new", "old"], latest_crawl="new")
    assert out[0] is enriched                               # built by the latest crawl: untouched
    assert out[1].byday == [4] and out[1].kind == "recurring"   # disabled venue: fixed even with new id
    assert out[2].byday == [5]                              # older crawl's row: fixed
