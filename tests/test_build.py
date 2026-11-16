"""sources.build: RawEvent -> Event (dates, kinds, times, prices, urls, drops)."""
from datetime import date

import pytest

from src.config import VenueConfig, get_city
from src.models import RawEvent
from src.sources.base import SourceCtx
from src.sources.build import MAX_INSTANCES, build_event, build_events

TODAY = date(2026, 11, 15)   # a Sunday
LISTING = "https://example.org/whats-on/"


def _cfg(**kw) -> VenueConfig:
    return VenueConfig(name="Test Venue", url=LISTING, **kw)


def _ctx(today: date = TODAY, window: int = 365) -> SourceCtx:
    return SourceCtx(http=None, today=today, window_days=window, city=get_city("london"))


def _one(raw: RawEvent, cfg: VenueConfig | None = None, ctx: SourceCtx | None = None):
    evs = build_events([raw], cfg or _cfg(), ctx or _ctx())     # incl. the `fix:` block
    assert len(evs) == 1, evs
    return evs[0]


def test_range_with_weekday_is_weekly_run():
    e = _one(RawEvent(title="Family Freestyle", date_text="13 Nov – 18 Dec, Fridays"))
    assert (e.kind, e.date, e.end_date, e.byday, e.sessions_known) == (
        "run", date(2026, 11, 13), date(2026, 12, 18), [4], True)


def test_bare_range_sessions_unknown():
    e = _one(RawEvent(title="Workshop", date_text="12 November 2026 – 17 December 2026"))
    assert (e.kind, e.byday, e.sessions_known) == ("run", None, False)


def test_range_means_weekly_derives_byday():
    cfg = _cfg(fix={"range_means_weekly": {"min_days": 28}})       # bare same-weekday ends: opt-in
    e = _one(RawEvent(title="Workshop", date_text="12 November 2026 – 17 December 2026"), cfg)
    assert (e.kind, e.byday, e.sessions_known) == ("run", [3], True)   # both Thursdays
    e = _one(RawEvent(title="Workshop", date_text="12 November 2026 – 17 December 2026"),
             _cfg(fix={"range_means_weekly": True}))
    assert (e.byday, e.sessions_known) == (None, False)


def test_range_means_weekly_needs_matching_weekdays():
    cfg = _cfg(fix={"range_means_weekly": True})
    e = _one(RawEvent(title="Exhibition", date_text="12 November 2026 – 18 December 2026"), cfg)
    assert (e.byday, e.sessions_known) == (None, False)


def test_on_now_until():
    e = _one(RawEvent(title="Tour", date_text="On now until 31 Dec"))
    assert (e.kind, e.date, e.end_date, e.start_known) == ("run", TODAY, date(2026, 12, 31), False)


def test_weekday_date_time_one_off():
    e = _one(RawEvent(title="Talk", date_text="Thursday 19 November 10 am"))
    assert (e.kind, e.date, e.time, e.end_date) == ("one_off", date(2026, 11, 19), "10:00", None)


def test_instances_become_recurring_events():
    evs = build_event(RawEvent(title="Storytime", date_text="19 Nov, 26 Nov, 3 Dec", time_text="10.30am"),
                      _cfg(), _ctx())
    assert [e.date for e in evs] == [date(2026, 11, 19), date(2026, 11, 26), date(2026, 12, 3)]
    assert {(e.kind, e.recurrence, e.time) for e in evs} == {("recurring", "Thursdays", "10:30")}


def test_instances_capped():
    text = ", ".join(f"{d} {m}" for m in ("Dec", "Jan", "Feb") for d in range(1, 29))
    evs = build_event(RawEvent(title="Daily tour", date_text=text), _cfg(), _ctx())
    assert len(evs) == MAX_INSTANCES


def test_time_default_and_field_time_precedence():
    cfg = _cfg(fix={"time_default": "19:30"})
    assert _one(RawEvent(title="Play", date_text="20 November 2026"), cfg).time == "19:30"
    assert _one(RawEvent(title="Play", date_text="20 November 2026", time_text="14.30"), cfg).time == "14:30"
    assert _one(RawEvent(title="Play", date_text="20 November 2026 7pm"), cfg).time == "19:00"


def test_drop_title_re_and_generic_titles():
    cfg = _cfg(fix={"drop_title_re": "^Gift Vouchers"})
    assert build_events([RawEvent(title="Gift vouchers for Xmas", date_text="20 Nov 2026")], cfg, _ctx()) == []
    assert build_event(RawEvent(title="Concert - Sat 21 Nov", date_text="21 Nov 2026"), cfg, _ctx()) == []
    assert build_event(RawEvent(title="Gift of the Magi", date_text="20 Nov 2026"), cfg, _ctx())


def test_past_event_dropped():
    ctx = _ctx()
    assert build_event(RawEvent(title="Gone", date_text="1 November 2026"), _cfg(), ctx) == []
    assert ctx.stats["build.dropped_past"] == 1
    assert build_event(RawEvent(title="Gone run", date_text="1 Oct 2026 – 10 Nov 2026"), _cfg(), ctx) == []
    # a run that started in the past but is still on is kept, start as given
    e = _one(RawEvent(title="Still on", date_text="1 Oct 2026 – 10 Jan 2027"))
    assert (e.date, e.end_date) == (date(2026, 10, 1), date(2027, 1, 10))


def test_no_window_pruning_in_build():
    e = _one(RawEvent(title="Spirited Away", date_text="3 March 2028"), ctx=_ctx(window=3))
    assert e.date == date(2028, 3, 3)


def test_open_ended_series_with_past_start_kept():
    e = _one(RawEvent(title="Gallery tours", date_text="from 1 Oct 2026"))
    assert (e.kind, e.date, e.end_date) == ("run", TODAY, None)
    e = _one(RawEvent(title="Rhyme time", date_text="2026-09-03", recurrence_text="Thursdays 10:00"))
    assert (e.kind, e.date, e.byday, e.time) == ("recurring", date(2026, 11, 19), [3], "10:00")   # next Thursday


def test_all_instances_past_counted():
    ctx = _ctx()
    assert build_event(RawEvent(title="Old", date_text="1 Nov 2026, 5 Nov 2026, 8 Nov 2026"), _cfg(), ctx) == []
    assert ctx.stats["build.dropped_past"] == 1


def test_undated_kept_flagged():
    e = _one(RawEvent(title="Mystery night", date_text="Dates TBC"))
    assert (e.date, e.date_undetermined) == (TODAY, True)
    assert _one(RawEvent(title="No date at all")).date_undetermined


def test_price_from_field_then_desc():
    assert _one(RawEvent(title="Gig", date_text="20 Nov 2026", price_text="£15 / £10 conc")).price == "£15"
    e = _one(RawEvent(title="Gig", date_text="20 Nov 2026", desc="Late show.\nTickets £12.50"))
    assert e.price == "£12.50"
    assert _one(RawEvent(title="Gig", date_text="20 Nov 2026")).price is None


def test_sold_out_passthrough():
    assert _one(RawEvent(title="Hot", date_text="20 Nov 2026", sold_out=True)).sold_out is True
    assert _one(RawEvent(title="Cold", date_text="20 Nov 2026")).sold_out is False


def test_atg_dd_mm_yyyy():
    e = _one(RawEvent(title="Sinatra", date_text="17-11-2026", end_text="10-04-2027"))
    assert (e.date, e.end_date, e.kind) == (date(2026, 11, 17), date(2027, 4, 10), "run")
    assert _one(RawEvent(title="Sinatra", date_text="17-11-2026")).date == date(2026, 11, 17)


def test_mdy_order():
    e = _one(RawEvent(title="US date", date_text="12/01/2026"), _cfg(fix={"date_order": "mdy"}))
    assert e.date == date(2026, 12, 1)


def test_url_absolutised_fragment_stripped_and_listing_fallback():
    e = _one(RawEvent(title="A", date_text="20 Nov 2026", url="/events/a/#20261120"))
    assert e.url == "https://example.org/events/a/"
    assert _one(RawEvent(title="B", date_text="20 Nov 2026")).url == LISTING
    assert _one(RawEvent(title="M", date_text="20 Nov 2026", url="mailto:box@example.org")).url == LISTING
    assert _one(RawEvent(title="J", date_text="20 Nov 2026", url="javascript:void(0)")).url == LISTING
    cfg = _cfg(fix={"url_pattern": r"/events/[^/]+/$"})
    assert _one(RawEvent(title="C", date_text="20 Nov 2026", url="/events/c/"), cfg).url == "https://example.org/events/c/"
    assert _one(RawEvent(title="D", date_text="20 Nov 2026", url="/shop/gift/"), cfg).url == LISTING


def test_build_never_raises_one_error_line_per_venue():
    ctx = _ctx()
    raws = [RawEvent(title="Bad", date_text="20 Nov 2026", url="http://[bad")] * 3
    assert build_events(raws, _cfg(), ctx) == []
    assert ctx.stats["build.failed"] == 3 and len(ctx.errors) == 1
    assert ctx.errors[0].startswith("Test Venue: build failed")


def test_invalid_regex_rejected_at_config_load():
    with pytest.raises(ValueError, match="invalid regex"):
        _cfg(fix={"drop_title_re": "(unclosed"})
    with pytest.raises(ValueError, match="invalid regex"):
        _cfg(follow={"link_pattern": "[x"})


def test_iso_midnight_is_no_time():
    assert _one(RawEvent(title="Expo", date_text="2026-11-20T00:00:00")).time is None
    assert _one(RawEvent(title="Late", date_text="2026-11-20", time_text="midnight")).time == "00:00"
    assert _one(RawEvent(title="Gig", date_text="2026-11-20T19:30:00")).time == "19:30"


def test_inconsistent_weekday_flagged():
    assert _one(RawEvent(title="Talk", date_text="Tue 16 Nov 2026")).date_inconsistent is True
    assert _one(RawEvent(title="Talk", date_text="Mon 16 Nov 2026")).date_inconsistent is False


@pytest.mark.parametrize("text,age", [("18+", 18), ("5-11", 5), ("3+", 3), ("Ages 7 to 12", 7), ("8", 8), ("all ages", None)])
def test_min_age_of(text, age):
    from src.sources.build import min_age_of
    assert min_age_of(text) == age


def test_category_audience_age_texts():
    e = _one(RawEvent(title="Gig", date_text="20 Nov 2026", category_text="music"), _cfg(category_hint="theatre"))
    assert (e.category, e.audience) == ("concert", "all")
    e = _one(RawEvent(title="Show", date_text="20 Nov 2026", category_text="kids", audience_text="kids"))
    assert (e.category, e.audience) == ("other", "kids")
    e = _one(RawEvent(title="Play", date_text="20 Nov 2026", age_text="13+"))
    assert (e.min_age, e.audience) == (13, "family")
    e = _one(RawEvent(title="Cabaret", date_text="20 Nov 2026", age_text="18+"))
    assert (e.min_age, e.audience) == (18, "adult")
    assert _one(RawEvent(title="Talk", date_text="20 Nov 2026", category_text="unknownword")).category == "other"


def test_venue_source_raw_date_and_category():
    cfg = _cfg(category_hint="kids")
    e = _one(RawEvent(title="Puppets", date_text="Sat 21 Nov 2026", venue="Church Hall"), cfg)
    assert (e.venue, e.source, e.raw_date, e.category, e.audience) == (
        "Church Hall", "Test Venue", "Sat 21 Nov 2026", "other", "kids")
    e = _one(RawEvent(title="Gig", date_text="21 Nov 2026"), _cfg(category_hint="lecture"))
    assert (e.venue, e.category) == ("Test Venue", "talk")


def test_weekday_pattern_without_dates():
    e = _one(RawEvent(title="Baby rhyme time", recurrence_text="Thursdays 10:00"))
    assert (e.kind, e.byday, e.recurrence, e.time, e.date) == ("recurring", [3], "Thursdays", "10:00", date(2026, 11, 19))


def test_build_events_dedupes():
    raws = [RawEvent(title="A", date_text="20 Nov 2026 7pm")] * 2 + [RawEvent(title="A", date_text="20 Nov 2026 2pm")]
    assert len(build_events(raws, _cfg(), _ctx())) == 2


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_title_dropped(bad):
    assert build_event(RawEvent(title=bad, date_text="20 Nov 2026"), _cfg(), _ctx()) == []


def test_source_registry():
    from src.sources.base import SOURCES, Source, get_source, load_builtin_sources
    load_builtin_sources()
    load_builtin_sources()
    assert {"jsonld", "adapter", "follow_links"} <= set(SOURCES)
    assert all(isinstance(s, Source) for s in SOURCES.values())
    with pytest.raises(KeyError, match="known:"):
        get_source("nope")


def test_atg_range_day_after_year_dash():   # Dominion: was split as 9–25 Nov + 25 Sep
    e = _one(RawEvent(title="The Devil Wears Prada", date_text="09-11-2026", end_text="25-09-2027"))
    assert (e.date, e.end_date) == (date(2026, 11, 9), date(2027, 9, 25))


def test_safe_records_swallows_and_reports():
    import asyncio
    from src.sources.base import safe_records

    class Boom:
        kind = "boom"

        async def records(self, cfg, fr, ctx):
            raise RuntimeError("bad page")
    ctx = _ctx()
    assert asyncio.run(safe_records(Boom(), _cfg(), None, ctx)) == []
    assert ctx.stats == {"boom.raised": 1}
    assert ctx.errors == ["Test Venue: source boom raised RuntimeError: bad page"]


def test_same_key_variants_kept_within_one_source():
    """Matinée + evening cards with no times (Green Note 2026-11-09): same title/date, other description."""
    t = "Folk and Roots Presents: The Rheingans Sisters"
    raws = [RawEvent(title=t, date_text="15 November 2026", desc="(Matinée)", source_kind="llm"),
            RawEvent(title=t, date_text="15 November 2026", desc="Evening", source_kind="llm"),
            RawEvent(title=t, date_text="15 November 2026", desc="Evening", source_kind="llm"),     # exact repeat
            RawEvent(title=t, date_text="15 November 2026", desc="from JSON-LD", source_kind="jsonld")]
    ctx = _ctx()
    evs = build_events(raws, _cfg(), ctx)
    assert [e.description for e in evs] == ["(Matinée)", "Evening"] and ctx.stats["build.kept_variant"] == 1
