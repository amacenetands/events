"""Cross-source dedupe, map URLs, cancelled rows, weekend first-day label, RU price labels."""
from datetime import date, timedelta
from types import SimpleNamespace

from src.digest import generate_digest_v2, price_label
from src.models import Event
from src.normalize.urls import is_map_url, pick_event_url
from src.query import Filter, first_match_day
from src.tidy import dedupe_cross_source, is_cancelled, tidy

TODAY = date.today()
SAT = TODAY + timedelta(days=(5 - TODAY.weekday()) % 7 or 7)


def ev(title="X", d=SAT, **kw):
    kw.setdefault("venue", "Blackheath Halls")
    kw.setdefault("url", "https://v.org/e")
    return Event(title=title, date=d, **kw)


# 1 dedupe
def test_untimed_aggregator_loses_to_own_timed():
    agg = ev("Billy The Kid Family Concert", source="Primary Times London", price="£10",
             url="https://www.primarytimes.co.uk/l/billy-1")
    own = ev("Billy the Kid family concert", source="Blackheath Halls", time="16:30",
             url="https://www.blackheathhalls.com/whats-on/billy/")
    out = dedupe_cross_source([agg, own])
    assert len(out) == 1 and out[0].source == "Blackheath Halls"
    assert out[0].time == "16:30" and out[0].price == "£10" and "blackheathhalls" in out[0].url


def test_different_times_same_source_kept():
    a = ev("Sing", source="Blackheath Halls", time="13:30")
    b = ev("Sing", source="Blackheath Halls", time="16:00")
    assert len(dedupe_cross_source([a, b])) == 2


def test_aggregator_run_covered_by_own_instances():
    agg = ev("Festive Family Carols", d=SAT, end_date=SAT + timedelta(days=10), kind="run",
             venue="St Martin In The Fields", source="Primary Times London")
    own = ev("Festive Family Carols", d=SAT + timedelta(days=3), kind="recurring",
             venue="St Martin-in-the-Fields", source="St Martin-in-the-Fields")
    out = dedupe_cross_source([agg, own])
    assert [e.source for e in out] == ["St Martin-in-the-Fields"]


def test_run_outside_own_dates_kept():
    agg = ev("Carols", d=SAT, end_date=SAT + timedelta(days=2), kind="run", source="Primary Times London")
    own = ev("Carols", d=SAT + timedelta(days=30), source="Blackheath Halls")
    assert len(dedupe_cross_source([agg, own])) == 2


# 2 map urls
def test_is_map_url():
    for u in ("https://maps.app.goo.gl/aevtQdBvBUKEeNQQA", "https://goo.gl/maps/xyz", "https://www.google.com/maps/place/x",
              "https://www.google.co.uk/maps?q=1", "https://maps.apple.com/?q=x", "https://what3words.com/a.b.c",
              "https://citymapper.com/directions?x=1"):
        assert is_map_url(u), u
    for u in ("https://www.fulhampalace.org/events/x", "https://www.google.com/search?q=x", "https://goo.gl/abc"):
        assert not is_map_url(u), u


def test_pick_event_url_skips_map_for_next_candidate():
    L = "https://www.fulhampalace.org/whats-on/"
    lines = ["Club -> https://maps.app.goo.gl/abc", "More -> /whats-on/yac/"]
    assert pick_event_url(lines, L) == "https://www.fulhampalace.org/whats-on/yac/"
    assert pick_event_url(["Club -> https://maps.app.goo.gl/abc"], L) is None


def test_stored_map_url_falls_back_to_listing():
    from src.venues.fixes import apply_fixes_stored
    cfg = SimpleNamespace(name="V", disabled=False, resolved_url=lambda y: "https://v.org/whats-on/",
                          fix=SimpleNamespace(drop_title_re=None, url_pattern=None, time_default=None, weekly=None,
                                              byday=None, range_means_weekly=None))
    e = ev("YAC", source="V", url="https://maps.app.goo.gl/abc")
    out = apply_fixes_stored([e], [cfg], TODAY, ["1"], "1")
    assert out[0].url == "https://v.org/whats-on/"


# 3 cancelled
def test_cancelled_en_ru():
    assert is_cancelled(ev("Cancelled - Masterclass"))
    assert is_cancelled(ev("Red Sky July", description="Postponed"))
    assert is_cancelled(ev("Talk", description="This lecture has been postponed."))
    assert is_cancelled(ev("Хор", description="Театральный зал | Мероприятие отменено"))
    assert is_cancelled(ev("Концерт отменен"))
    assert is_cancelled(ev("Томас Кинц", description="Отменён"))
    assert not is_cancelled(ev("Jazz", description="Free cancellation up to 24h before; not postponed"))
    assert not is_cancelled(ev("Cancel Culture: a talk"))


def test_tidy_drops_cancelled():
    good, bad = ev("Jazz", source="V"), ev("Jazz 2", source="V", description="This event has been cancelled")
    assert [e.title for e in tidy([good, bad], TODAY)] == ["Jazz"]


# 4 weekend first day
def test_first_match_day_weekend_run():
    mon = TODAY + timedelta(days=(7 - TODAY.weekday()) % 7 or 7)
    run = ev("Bubble", d=mon, end_date=mon + timedelta(days=20), kind="run")
    f = Filter(when=f"range:{mon.isoformat()}..{(mon + timedelta(days=20)).isoformat()}", days="weekend")
    d = first_match_day(run, f, TODAY)
    assert d is not None and d.weekday() == 5
    f2 = Filter(when=f.when, days="any")
    assert first_match_day(run, f2, TODAY) == mon


def test_digest_pick_shows_first_weekend_day():
    mon = TODAY + timedelta(days=(7 - TODAY.weekday()) % 7 or 7)
    run = ev("Bubble", d=mon, end_date=mon + timedelta(days=20), kind="run")
    f = Filter(when=f"range:{mon.isoformat()}..{(mon + timedelta(days=20)).isoformat()}", days="weekend")
    md = generate_digest_v2([run], f, picks=[run], today=TODAY)
    sat = mon + timedelta(days=5)
    assert f"_{sat.strftime('%a %d %b')}_" in md


# 5 ru price
def test_price_label_ru():
    assert price_label("Free", "ru") == "Бесплатно"
    assert price_label("From ₽500", "ru") == "от ₽500"
    assert price_label("Pay what you can", "ru") == "Цена по желанию"
    assert price_label("Free", "en") == "Free" and price_label("£12", "ru") == "£12"


def test_digest_ru_price():
    e = ev("Концерт", price="Free")
    md = generate_digest_v2([e], Filter(city="moscow", lang="ru"), today=TODAY, lang="ru")
    assert "Бесплатно" in md and "Free" not in md
