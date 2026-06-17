"""Generate markdown digests — weekly and family-focused."""
from datetime import date, timedelta
from pathlib import Path
from typing import Callable
from .models import Event

_RU = {
    "london_events_title": "Лондонские события",
    "london_family_title": "Лондонские семейные события",
    "london_daytime_title": "Лондонские дневные концерты",
    "week_of": "неделя",
    "one_off_stat": "разовых событий",
    "exhibition_stat": "выставок",
    "new_stat": "новых",
    "running_exh": "n/a",  # running_stat новых
    "weekend": "Выходные",
    "weekday_evenings": "Будни вечером",
    "also": "Ещё",
    "running_exhibitions": "Текущие выставки и шоу",
    "running_preamble": "Многонедельные показы — посещайте в любое время или проверяйте конкретные события.",
    "this_weekend": "Эти выходные",
    "weekday_this_week": "Вечера будних дней этой недели, до {date}",
    "next_weekend": "Следующие выходные",
    "future_weekends": "Впереди — выходные, до {date}",
    "future_weekdays": "Впереди — будни вечером, до {date}",
    "new_exhibitions": "Новые выставки",
    "new_exh_preamble": "Добавлено с последнего запуска — посещайте в любое время в указанные даты.",
    "continuing_exhibitions": "Продолжающиеся выставки",
    "continuing_preamble": "Идут с предыдущего дайджеста.",
    "extraction_errors": "⚠️ Ошибки извлечения",
    "extraction_errors_preamble": "Следующие сайты не смогли обработать данные и могут иметь пропущенные события:",
    "week_label": "Неделя",
    "no_events": "События не найдены.",
    "events_stat": "событий",
    "weekday_daytime": "будни · дневное время",
    "next_days": "следующие {n} дн.",
}

OUTPUT_DIR = Path(__file__).parent.parent / "output"


def _fmt(e: Event, i: int) -> str:
    date_str = e.date.strftime("%a %d %b")
    if e.end_date and not e.is_one_off:
        date_str += f" – {e.end_date.strftime('%d %b')}"
    time_str = f" · {e.time}" if e.time else ""
    price_str = f" · {e.price}" if e.price else ""
    desc = f"\n   _{e.description}_" if e.description else ""
    return f"{i}. **{e.title}**  \n   {e.venue} · {date_str}{time_str}{price_str} · [link]({e.url}){desc}"


def _fmt_compact(e: Event) -> str:
    date_str = e.date.strftime("%a %d %b")
    time_str = f" · {e.time}" if e.time else ""
    return f"- **{e.title}** | {e.venue} | {date_str}{time_str} · [link]({e.url})"


def _section(
    lines: list[str],
    title: str,
    events: list[Event],
    preamble: str | None = None,
    fmt_fn: Callable[[Event, int], str] | None = None,
) -> None:
    if not events:
        return
    lines += [f"## {title}", ""]
    if preamble:
        lines += [preamble, ""]
    for i, e in enumerate(events, 1):
        lines.append((fmt_fn or _fmt)(e, i))
    lines.append("")


def _one_off_section(
    lines: list[str],
    title: str,
    ranked: list[Event],
    rest: list[Event],
    also_label: str = "Also",
) -> None:
    if not ranked and not rest:
        return
    lines += [f"## {title}", ""]
    for i, e in enumerate(ranked, 1):
        lines.append(_fmt(e, i))
    if ranked:
        lines.append("")
    if rest:
        lines += [f"### {also_label} {title}", ""]
        for e in rest:
            lines.append(_fmt_compact(e))
        lines.append("")


def generate_digest(
    ranked: list[Event],
    rest: list[Event] | None = None,
    exhibitions: list[Event] | None = None,
    lang: str = "en",
) -> str:
    today = date.today()
    rest = list(rest or [])
    ranked = list(ranked)
    exhibitions = exhibitions or []
    ru = lang == "ru"

    total_one_offs = len(ranked) + len(rest)
    if ru:
        title = f"# {_RU['london_events_title']} — {_RU['week_of']} {today.strftime('%d %B %Y')}"
        stats = f"_{total_one_offs} {_RU['one_off_stat']} · {len(exhibitions)} {_RU['exhibition_stat']}_"
        s_weekend = _RU["weekend"]
        s_weekday = _RU["weekday_evenings"]
        s_also = _RU["also"]
        s_exh = _RU["running_exhibitions"]
        s_exh_preamble = f"_{_RU['running_preamble']}_"
    else:
        title = f"# London Events — week of {today.strftime('%d %B %Y')}"
        stats = f"_{total_one_offs} one-off events · {len(exhibitions)} exhibitions_"
        s_weekend = "Weekend"
        s_weekday = "Weekday Evenings"
        s_also = "Also"
        s_exh = "Running Exhibitions & Shows"
        s_exh_preamble = "_Multi-week runs — visit any time or check for specific programming events._"

    lines = [title, "", stats, ""]

    ranked_weekend = sorted([e for e in ranked if e.date.weekday() >= 5], key=lambda e: (e.date, e.time or ""))
    ranked_weekday = sorted([e for e in ranked if e.date.weekday() < 5], key=lambda e: (e.date, e.time or ""))
    rest_weekend = sorted([e for e in rest if e.date.weekday() >= 5], key=lambda e: (e.date, e.time or ""))
    rest_weekday = sorted([e for e in rest if e.date.weekday() < 5], key=lambda e: (e.date, e.time or ""))

    _one_off_section(lines, s_weekend, ranked_weekend, rest_weekend, also_label=s_also)
    _one_off_section(lines, s_weekday, ranked_weekday, rest_weekday, also_label=s_also)

    if exhibitions:
        sorted_exh = sorted(exhibitions, key=lambda e: e.end_date or date(9999, 1, 1))
        _section(lines, s_exh, sorted_exh, preamble=s_exh_preamble)

    return "\n".join(lines)


def _next_weekend(today: date) -> tuple[date, date]:
    days_to_sat = (5 - today.weekday()) % 7
    sat = today + timedelta(days=days_to_sat)
    return sat, sat + timedelta(days=1)


def generate_family_digest(
    events: list[Event],
    new_exhibition_fps: set[str] | None = None,
    failed_sites: list[str] | None = None,
    lang: str = "en",
) -> str:
    today = date.today()
    month_end = today + timedelta(days=30)

    one_offs = sorted([e for e in events if e.is_one_off], key=lambda e: (e.date, e.time or ""))
    all_exhibitions = sorted(
        [e for e in events if not e.is_one_off],
        key=lambda e: e.end_date or date(9999, 1, 1),
    )

    if new_exhibition_fps:
        new_exhibitions = [e for e in all_exhibitions if e.fingerprint() in new_exhibition_fps]
        continuing = [e for e in all_exhibitions if e.fingerprint() not in new_exhibition_fps]
    else:
        new_exhibitions, continuing = [], all_exhibitions

    this_sat, this_sun = _next_weekend(today)
    next_sat = this_sat + timedelta(days=7)
    next_sun = next_sat + timedelta(days=1)
    ru = lang == "ru"

    def is_weekend(e: Event) -> bool:
        return e.date.weekday() >= 5

    this_weekend = [e for e in one_offs if this_sat <= e.date <= this_sun]
    weekday_this_week = [e for e in one_offs if today <= e.date < this_sat and not is_weekend(e)]
    next_weekend = [e for e in one_offs if next_sat <= e.date <= next_sun]
    future_weekends = [e for e in one_offs if e.date > next_sun and is_weekend(e) and e.date <= month_end]
    future_weekdays = [e for e in one_offs if e.date > next_sun and not is_weekend(e) and e.date <= month_end]

    if ru:
        title = f"# {_RU['london_family_title']} — {today.strftime('%d %B %Y')}"
        stats = f"_{len(one_offs)} {_RU['one_off_stat']} · {len(all_exhibitions)} {_RU['exhibition_stat']} ({len(new_exhibitions)} {_RU['new_stat']})_"
        s_this_wk = f"Эти выходные  ({this_sat.strftime('%a %d %b')} – {this_sun.strftime('%a %d %b')})"
        s_wday_wk = f"Вечера будних дней  (до {(this_sat - timedelta(days=1)).strftime('%d %b')})"
        s_next_wk = f"Следующие выходные  ({next_sat.strftime('%a %d %b')} – {next_sun.strftime('%a %d %b')})"
        s_fut_wk = f"Впереди — выходные  (до {month_end.strftime('%d %b')})"
        s_fut_wd = f"Впереди — будни вечером  (до {month_end.strftime('%d %b')})"
        s_new_exh = _RU["new_exhibitions"]
        s_new_preamble = f"_{_RU['new_exh_preamble']}_"
        s_cont_exh = _RU["continuing_exhibitions"]
        s_cont_preamble = f"_{_RU['continuing_preamble']}_"
        s_err_hdr = f"## {_RU['extraction_errors']}"
        s_err_body = f"_{_RU['extraction_errors_preamble']}_"
    else:
        title = f"# London Family Events — {today.strftime('%d %B %Y')}"
        stats = f"_{len(one_offs)} one-off events · {len(all_exhibitions)} running exhibitions ({len(new_exhibitions)} new)_"
        s_this_wk = f"This Weekend  ({this_sat.strftime('%a %d %b')} – {this_sun.strftime('%a %d %b')})"
        s_wday_wk = f"Weekday Evenings  (this week, to {(this_sat - timedelta(days=1)).strftime('%d %b')})"
        s_next_wk = f"Next Weekend  ({next_sat.strftime('%a %d %b')} – {next_sun.strftime('%a %d %b')})"
        s_fut_wk = f"Coming Up — Weekends  (to {month_end.strftime('%d %b')})"
        s_fut_wd = f"Coming Up — Weekday Evenings  (to {month_end.strftime('%d %b')})"
        s_new_exh = "New Exhibitions"
        s_new_preamble = "_Added since last run — visit any time within the dates shown._"
        s_cont_exh = "Continuing Exhibitions"
        s_cont_preamble = "_Ongoing from previous digest._"
        s_err_hdr = "## ⚠️ Extraction Errors"
        s_err_body = "_The following sites failed LLM extraction and may have missing events:_"

    lines = [title, "", stats, ""]

    _section(lines, s_this_wk, this_weekend)
    _section(lines, s_wday_wk, weekday_this_week)
    _section(lines, s_next_wk, next_weekend)
    _section(lines, s_fut_wk, future_weekends)
    _section(lines, s_fut_wd, future_weekdays)
    _section(lines, s_new_exh, new_exhibitions, preamble=s_new_preamble)
    _section(lines, s_cont_exh, continuing, preamble=s_cont_preamble)

    if failed_sites:
        lines += ["", "---", s_err_hdr, "", s_err_body, ""]
        for name in failed_sites:
            lines.append(f"- {name}")

    return "\n".join(lines)


def generate_daytime_digest(events: list[Event], date_window: int = 3, lang: str = "en") -> str:
    today = date.today()
    cutoff = today + timedelta(days=date_window)

    daytime = sorted(
        [e for e in events if e.date.weekday() < 5 and e.date <= cutoff],
        key=lambda e: (e.date, e.time or ""),
    )

    ru = lang == "ru"
    if ru:
        title = f"# {_RU['london_daytime_title']} — {today.strftime('%d %B %Y')}"
        stats = f"_{len(daytime)} {_RU['events_stat']} · {_RU['weekday_daytime']} · {_RU['next_days'].format(n=date_window)}_"
        s_week_of = _RU["week_label"]
        s_no_events = f"_{_RU['no_events']}_"
    else:
        title = f"# London Daytime Concerts — {today.strftime('%d %B %Y')}"
        stats = f"_{len(daytime)} events · weekday daytime · next {date_window} days_"
        s_week_of = "Week of"
        s_no_events = "_No daytime events found._"

    lines = [title, "", stats, ""]

    from itertools import groupby

    def _week_key(e: Event) -> date:
        return e.date - timedelta(days=e.date.weekday())

    for week_start, week_events in groupby(daytime, key=_week_key):
        week_list = list(week_events)
        week_end = week_start + timedelta(days=4)
        label = f"{s_week_of} {week_start.strftime('%d %b')} – {week_end.strftime('%d %b')}"
        lines += [f"## {label}", ""]
        for i, e in enumerate(week_list, 1):
            lines.append(_fmt(e, i))
        lines.append("")

    if not daytime:
        lines.append(s_no_events)

    return "\n".join(lines)


def _save(content: str, prefix: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"{prefix}_{date.today().isoformat()}.md"
    path.write_text(content)
    return path


def save_digest(content: str) -> Path:
    return _save(content, "events")


def save_family_digest(content: str) -> Path:
    return _save(content, "family")


def save_daytime_digest(content: str) -> Path:
    return _save(content, "daytime")
