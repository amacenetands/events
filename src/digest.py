"""Filter-driven markdown digest (generate_digest_v2); EN + RU headings."""
import html
from datetime import date, timedelta
from itertools import groupby as _groupby
from pathlib import Path
from urllib.parse import quote as _quote

from .models import Event

OUTPUT_DIR = Path(__file__).parent.parent / "output"

_RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
_RU_DOW = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
_RU_DOW_S = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

_STR = {
    "en": {"today": "Today", "tomorrow": "Tomorrow", "top": "Top picks", "also": "Also on",
           "one_off": "One-off events", "recurring": "Recurring", "runs": "Exhibitions & runs",
           "tbc": "Exact dates undetermined", "tbc_note": "Couldn't confirm dates — check the venue link.",
           "none": "No events match these filters.", "errors": "Extraction errors", "new": "NEW",
           "until": "until", "from": "from", "closing": "closing soon", "more": "more",
           "n_one": "one-off", "n_rec": "recurring", "n_runs": "runs/exhibitions", "n_tbc": "dates TBC",
           "kids": "kids", "family": "family", "adults": "adults"},
    "ru": {"today": "Сегодня", "tomorrow": "Завтра", "top": "Лучшее", "also": "Ещё",
           "one_off": "Разовые события", "recurring": "Регулярные", "runs": "Выставки",
           "tbc": "Дата уточняется", "tbc_note": "Не удалось подтвердить даты — проверьте по ссылке.",
           "none": "События не найдены.", "errors": "Ошибки извлечения", "new": "НОВОЕ",
           "until": "до", "from": "с", "closing": "скоро закрывается", "more": "ещё",
           "n_one": "разовых", "n_rec": "регулярных", "n_runs": "выставок/показов", "n_tbc": "дата уточняется",
           "kids": "дети", "family": "семья", "adults": "взрослым"},
}


def _t(lang: str, key: str) -> str:
    return _STR.get(lang, _STR["en"])[key]


def _dfmt(d: date, lang: str = "en", fmt: str = "short") -> str:
    """short: 'Sat 14 Nov' | long: 'Saturday 14 Nov' | day: '14 Nov' | full: '08 November 2026'."""
    if lang == "ru":
        mon = _RU_MONTHS[d.month - 1]
        return {"short": f"{_RU_DOW_S[d.weekday()]} {d.day} {mon}", "long": f"{_RU_DOW[d.weekday()]} {d.day} {mon}",
                "day": f"{d.day} {mon}", "full": f"{d.day} {mon} {d.year}"}[fmt]
    return {"short": d.strftime("%a %d %b"), "long": d.strftime("%A %d %b"), "day": d.strftime("%d %b"),
            "full": d.strftime("%d %B %Y")}[fmt]


def _save(content: str, prefix: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"{prefix}_{date.today().isoformat()}.md"
    path.write_text(content)
    return path


def _esc(s) -> str:
    """Scraped text -> safe for markdown->HTML (raw <, >, & escaped)."""
    return html.escape(str(s or ""), quote=False)


def _safe_url(u: str) -> str:
    """Percent-encode chars that break markdown link syntax."""
    return u.replace(" ", "%20").replace("(", "%28").replace(")", "%29")




def _href(e: Event) -> str:
    u = (e.url or "").strip()
    if u.startswith(("http://", "https://")):
        return _safe_url(u)
    return _safe_url("https:" + u) if u.startswith("//") else ""   # relative/unknown: no dead link


def _badge(e: Event, lang: str = "en") -> str:
    age = f" {e.min_age}+" if e.min_age is not None else ""
    if e.audience == "kids":
        return f" [{_t(lang, 'kids')}{age}]"
    if e.audience == "family":
        return f" [{_t(lang, 'family')}{age}]"
    if e.audience == "adult":
        return f" [{_t(lang, 'adults')}]"
    return ""


def _link(e: Event) -> str:
    title = _esc(e.title).replace("[", "(").replace("]", ")")
    u = _href(e)
    return f"[{title}]({u})" if u else title


def _desc(e: Event, n: int = 160) -> str:
    if not e.description:
        return ""
    d = " ".join(e.description.split())
    return f"  \n   _{_esc(d[:n])}{'…' if len(d) > n else ''}_"


def _day_label(d: date, today: date, lang: str = "en") -> str:
    if d == today:
        return f"{_t(lang, 'today')} · {_dfmt(d, lang)}"
    if d == today + timedelta(days=1):
        return f"{_t(lang, 'tomorrow')} · {_dfmt(d, lang)}"
    return _dfmt(d, lang, "long")


def _one_off_line(e: Event, new: bool, lang: str = "en") -> str:
    bits = [_esc(e.venue)] + ([e.time] if e.time else []) + ([_esc(e.price)] if e.price else [])
    new_tag = f" **{_t(lang, 'new')}**" if new else ""
    return f"- {_link(e)}{_badge(e, lang)}{new_tag} — {' · '.join(bits)}{_desc(e)}"


def _recurring_line(group: list[Event], new: bool, lang: str = "en") -> str:
    group = sorted(group, key=lambda x: x.date)
    e = group[0]
    by_month: list[str] = []
    shown = group[:6]
    for (_y, _m), g in _groupby(shown, key=lambda x: (x.date.year, x.date.month)):
        g = list(g)
        if lang == "ru":
            by_month.append(", ".join(str(x.date.day) for x in g) + " " + _RU_MONTHS[g[0].date.month - 1])
        else:
            by_month.append(", ".join(x.date.strftime("%a %-d") for x in g) + " " + g[0].date.strftime("%b"))
    more = f" (+{len(group) - 6} {_t(lang, 'more')})" if len(group) > 6 else ""
    times = sorted({x.time for x in group if x.time})
    tail = f" · {', '.join(times[:3])}" if times else ""
    new_tag = f" **{_t(lang, 'new')}**" if new else ""
    return f"- {_link(e)}{_badge(e, lang)}{new_tag} — {_esc(e.venue)} · {'; '.join(by_month)}{more}{tail}"


def _run_line(e: Event, today: date, new: bool, lang: str = "en") -> str:
    when = (f"{_t(lang, 'until')} {_dfmt(e.end_date, lang)}" if e.end_date
            else f"{_t(lang, 'from')} {_dfmt(e.date, lang, 'day')}")
    soon = f" · **{_t(lang, 'closing')}**" if e.end_date and e.date <= today and 0 <= (e.end_date - today).days <= 7 else ""
    extra = f" · {_esc(e.recurrence)}" if e.recurrence else ""
    new_tag = f" **{_t(lang, 'new')}**" if new else ""
    price = f" · {_esc(e.price)}" if e.price else ""
    return f"- {_link(e)}{_badge(e, lang)}{new_tag} — {_esc(e.venue)} · {when}{extra}{price}{soon}{_desc(e)}"


def coverage_summary(counts: dict[str, int], all_sites: list[str]) -> str:
    names = sorted(set(all_sites) | set(counts))
    total = sum(counts.get(n, 0) for n in names)
    zero = sum(1 for n in names if counts.get(n, 0) == 0)
    lines = ["", "---", "## Venue Coverage", "",
             f"_{len(names)} sources · {total} events in date range before other filters · {zero} with 0_", "",
             "| Source | Events |", "|---|---|"]
    for n in sorted(names, key=lambda n: (counts.get(n, 0), n.lower())):
        lines.append(f"| {n} | {counts.get(n, 0)} |")
    return "\n".join(lines)


def generate_digest_v2(
    events: list[Event],
    f,                                   # src.query.Filter
    picks: list[Event] | None = None,
    new_fps: set[str] | None = None,
    today: date | None = None,
    title: str | None = None,
    footer: str = "",
    failed_sites: list[str] | None = None,
    reasons: dict[str, str] | None = None,   # fingerprint -> why picked
    scores: dict[str, float] | None = None,  # fingerprint -> score; orders events within a day
    lang: str | None = None,                 # 'en' | 'ru' (default: f.lang)
) -> str:
    from .query import describe, city_today
    lang = lang or getattr(f, "lang", "en") or "en"
    today = today or city_today(f)
    new_fps = new_fps or set()
    reasons = reasons or {}
    scores = scores or {}
    is_new = lambda e: e.fingerprint() in new_fps  # noqa: E731

    tbc = [e for e in events if e.date_undetermined]
    dated = [e for e in events if not e.date_undetermined]
    one_offs = sorted([e for e in dated if e.kind == "one_off"], key=lambda e: (e.date, e.time or "", e.title.lower()))
    rec_all = [e for e in dated if e.kind == "recurring" and e.end_date is None]
    runs = sorted([e for e in dated if e.kind == "run" or (e.kind == "recurring" and e.end_date)],
                  key=lambda e: (e.end_date or date(9999, 1, 1), e.title.lower()))
    groups: dict[tuple, list[Event]] = {}
    for e in rec_all:
        groups.setdefault((e.title.lower().strip(), e.venue.lower().strip()), []).append(e)
    rec_lines = sorted(groups.values(), key=lambda g: min(x.date for x in g))

    if lang == "ru":
        from .query import resolve_when
        s0, e0 = resolve_when(f.when, today)
        desc = f"{f.city.capitalize() if f.city != 'moscow' else 'Москва'} · " + (
            _dfmt(s0, lang) if s0 == e0 else f"{_dfmt(s0, lang)} – {_dfmt(e0, lang)}")
    else:
        desc = describe(f, today)
    stats = (f"_{len(one_offs)} {_t(lang, 'n_one')} · {len(groups)} {_t(lang, 'n_rec')} · "
             f"{len(runs)} {_t(lang, 'n_runs')}{f' · {len(tbc)} {_t(lang, 'n_tbc')}' if tbc else ''}_")
    lines = [f"# {_esc(title or f.label or ('События' if lang == 'ru' else 'Events'))} — {_dfmt(today, lang, 'full')}", "", f"_{desc}_", "", stats, ""]

    if picks:
        lines += [f"## {_t(lang, 'top')}", ""]
        for e in picks:
            ln = _one_off_line(e, is_new(e), lang) if e.kind == "one_off" else _run_line(e, today, is_new(e), lang)
            when = (_dfmt(e.date, lang) + (f" {e.time}" if e.time else "")) if e.kind == "one_off" else None
            why = reasons.get(e.fingerprint())
            lines.append(f"- ★ {ln[2:]}" + (f" · _{when}_" if when else "") + (f" · _{_esc(why)}_" if why else ""))
        lines.append("")
        pick_ids = {id(e) for e in picks}
        one_offs = [e for e in one_offs if id(e) not in pick_ids]
        runs = [e for e in runs if id(e) not in pick_ids]
        if one_offs:
            lines += [f"## {_t(lang, 'also')}", ""]
    elif one_offs:
        lines += [f"## {_t(lang, 'one_off')}", ""]
    for d, grp in _groupby(one_offs, key=lambda e: e.date):
        lines += [f"### {_day_label(d, today, lang)}", ""]
        if scores:
            grp = sorted(grp, key=lambda e: -scores.get(e.fingerprint(), 0))
        lines += [_one_off_line(e, is_new(e), lang) for e in grp]
        lines.append("")

    if rec_lines:
        lines += [f"## {_t(lang, 'recurring')}", ""]
        lines += [_recurring_line(g, any(is_new(x) for x in g), lang) for g in rec_lines]
        lines.append("")
    if runs:
        lines += [f"## {_t(lang, 'runs')}", ""]
        lines += [_run_line(e, today, is_new(e), lang) for e in runs]
        lines.append("")
    if tbc:
        lines += [f"## {_t(lang, 'tbc')}", "", f"_{_t(lang, 'tbc_note')}_", ""]
        lines += [f"- {_link(e)}{_badge(e, lang)} — {_esc(e.venue)}" for e in sorted(tbc, key=lambda e: (e.venue.lower(), e.title.lower()))]
        lines.append("")
    if not events:
        lines.append(f"_{_t(lang, 'none')}_")
    if failed_sites:
        lines += ["", "---", f"## {_t(lang, 'errors')}", ""] + [f"- {_esc(n)}" for n in failed_sites]
    out = "\n".join(lines)
    return out + footer


def save_digest_v2(content: str, slug: str) -> Path:
    return _save(content, slug)
