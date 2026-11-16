"""Deterministic price parsing: venue price strings -> short normalised label."""
from __future__ import annotations

import re

__all__ = ["parse_price_text", "find_price", "price_amounts"]

_AMT = re.compile(
    r"(?P<pre>[£€$₽])\s?(?P<a>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<c>\d{1,2}))?(?!\d)"
    r"|(?<![\d.,])(?P<a2>\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,](?P<c2>\d{2}))?\s?"
    r"(?P<post>₽|руб(?:л(?:ей|я|ь))?\.?(?!\w)|р\.?(?!\w))",
    re.I,
)
_FREE = re.compile(r"(?<![\w-])(?:free|бесплатно|вход\s+(?:свободный|бесплатный)|(?:свободный|бесплатный)\s+вход)(?![\w-])", re.I)
_FREE_ADDON = re.compile(
    r"\+\s*(?:a\s+)?free\b|\bfree\s+(?:essential|wi-?fi|cloakroom|parking|drinks?|programme|gift|tote|cancellation|delivery|exchange|access)"
    r"|\bfree\s+(?:for|to)\s+(?:members|friends|patrons|subscribers|supporters)|\bfree\s+with\s+(?:(?:a|your|valid|museum|gallery)\s+)*(?:membership|admission|entry|tickets?)"
    r"|\bstep[\s-]+free|\bfeel\s+free|\bfree\s+(?:of\s+charge\s+)?for\s+(?:carers?|companions?)", re.I)
_PWYC = re.compile(r"\bpay\s+what\s+you\s+(?:can|want|wish|decide|like)\b|\bpwy[cdw]\b", re.I)
_DONATION = re.compile(r"\b(?:donations?|by\s+donation)\b|пожертвован", re.I)
_FROM = re.compile(r"\b(?:from|starting\s+(?:at|from)|prices?\s+from|tickets\s+from)\s*:?\s*$|от\s*$", re.I)
_RANGE_SEP = re.compile(r"^\s*(?:[–—−-]|to|до)\s*$", re.I)
_SEP = r"(?:[–—−-]|to|до)"
# bare number before a range separator inherits the range's currency: "500 – 2 000 ₽"
_BARE_LO = re.compile(r"(?<![\d.,£$€])(?P<a>\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,](?P<c>\d{2}))?\s*" + _SEP + r"\s*$", re.I)
# bare number after a prefixed amount and a separator: "£10–15"
_BARE_HI = re.compile(r"\s*" + _SEP + r"\s*(?P<a>\d+)(?:\.(?P<c>\d{1,2}))?(?![\d.,%]|\s*(?:%|yrs?\b|years?\b|months?\b|[ap]\.?m\b|[a-z]{3}))", re.I)
_FEE = re.compile(r"\+\s*(?:[£€$]\s?\d+(?:\.\d{1,2})?\s*)?(?:a\s+)?(?:booking|transaction|service|admin|handling)\s+fees?\b|\b(?:inc(?:l(?:uding|\.)?)?|plus)\s+(?:booking|transaction)\s+fees?\b", re.I)
_PAREN = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")
_SEGMENT_SPLIT = re.compile(r"\n|\s[|•·/]\s|\s\|\s?|;\s")       # tiers
_CLAUSE_SPLIT = re.compile(r",\s")                                  # clauses of one tier label
# explicit general-admission label: beats an unlabelled tier ("£13.50–£28 full price | £11–£25")
_GENERAL = re.compile(r"\b(?:full[\s-]+price|standard|adults?|general(?:\s+admission)?|non-?\s?members?)\b"
                      r"|полный|взросл", re.I)
_INSTALMENT = re.compile(r"\binstal{1,2}ments?\b|\bper\s+(?:month|week)\b|\bmonthly\b|/\s*month\b", re.I)
# restricted tiers (gold convention: the lowest *general-admission* tier wins)
_TIER = re.compile(
    r"(?<!non-)(?<!non )\b(?:members?|membership|friends?|patrons?|concessions?|concs?|students?|under\s*-?\s*\d+s?"
    r"|u\d+s?|over\s*-?\s*\d+s?|unwaged|seniors?|child(?:ren)?|kids?|carers?|companions?|access|wheelchair|nhs"
    r"|jobseekers?|disabled|young\s+people)\b|член|студент|льгот|пенсион|детск", re.I)
_PWYC_FLOOR = re.compile(r"\b(?:from|min(?:imum)?)\b|\+", re.I)
_LINK = re.compile(r"\s*->\s*\S+\s*$")
_STRICT_FREE = re.compile(
    r"^\s*(?:price|tickets?|entry|admission|cost)?\s*:?\s*free\b[^a-z]{0,3}(?:entry|admission|event|to\s+attend|for\s+all|,|!|\.|–|-|$)"
    r"|\b(?:free\s+(?:entry|admission|event|to\s+attend|tickets?|but\s+book)|admission\s+(?:is\s+)?free|entry\s+(?:is\s+)?free|tickets?\s*:?\s*free)\b"
    r"|^\s*free\s*$|бесплатно|вход\s+свободный|свободный\s+вход", re.I)


def _num(a: str, c: str | None) -> tuple[float, str]:
    """(value, display number) from integer and cents parts; display drops '.00'."""
    a = re.sub(r"[,  ]", "", a)
    c = c.ljust(2, "0") if c else ""
    return float(f"{a}.{c}" if c else a), (a if not c or c == "00" else f"{a}.{c}")


def _fmt(m: re.Match) -> tuple[float, str, str]:
    """(value, display number, currency) for one amount match."""
    if m["pre"]:
        return (*_num(m["a"], m["c"]), m["pre"])
    return (*_num(m["a2"], m["c2"]), "₽")


def _show(num: str, cur: str) -> str:
    """'£12' / '500 ₽'."""
    return f"{num} ₽" if cur == "₽" else f"{cur}{num}"


def _range(lo: tuple[float, str, str], hi: tuple[float, str, str]) -> str:
    """'£8–£30' / '500–2000 ₽' / '£10'; zero bounds stay numeric ('£0–£10')."""
    if lo[0] == hi[0]:
        return "Free" if lo[0] == 0 else _show(lo[1], lo[2])
    if lo[2] == hi[2] == "₽":
        return f"{lo[1]}–{hi[1]} ₽"
    return f"{_show(lo[1], lo[2])}–{_show(hi[1], hi[2])}"


def _aside(m: re.Match) -> str:
    """A bracketed aside naming a restricted tier without an amount ('(under 16s)') labels its
    segment: keep a tier word; any other aside goes."""
    return " concession " if _TIER.search(m.group()) and not _AMT.search(m.group()) else " "


def _clean(text: str) -> str:
    """Drop bracketed asides, fee/'full price' wording, link markers; squash spaces."""
    t = _LINK.sub("", str(text))
    for _ in range(2):
        t = _PAREN.sub(_aside, t)
    t = _FEE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _free_match(seg: str) -> re.Match | None:
    """First 'free' that is not an add-on/accessibility/members-only phrase."""
    for fm in _FREE.finditer(seg):
        if not _FREE_ADDON.search(seg[max(0, fm.start() - 6):fm.end() + 30]):
            return fm
    return None


def _segment_price(seg: str) -> str | None:
    """Price label for one cleaned segment."""
    amts = list(_AMT.finditer(seg))
    fm = _free_match(seg)
    first_amt = amts[0].start() if amts else None
    pw = _PWYC.search(seg)
    if pw and (first_amt is None or pw.start() < first_amt):
        floor = [v for v in (_fmt(a) for a in amts) if v[0] > 0]
        if floor and _PWYC_FLOOR.search(seg[pw.end():]):        # "PWYC from £1", "PWYC £3+"
            lo = min(floor, key=lambda v: v[0])
            return f"Pay what you can from {_show(lo[1], lo[2])}"
        return "Pay what you can"
    vals = [_fmt(a) for a in amts]
    if fm and (first_amt is None or fm.start() < first_amt or all(v[0] == 0 for v in vals)):
        return "Free"
    if not amts:
        dm = _DONATION.search(seg)
        return "Donation" if dm else None
    a1, v1 = amts[0], vals[0]
    bl = _BARE_LO.search(seg[:a1.start()])
    if bl and not a1["pre"]:
        return _range((*_num(bl["a"], bl["c"]), v1[2]), v1)
    if len(amts) >= 2 and _RANGE_SEP.match(seg[a1.end():amts[1].start()]):
        return _range(v1, vals[1])
    bh = _BARE_HI.match(seg, a1.end()) if a1["pre"] else None
    if bh:
        hi = (*_num(bh["a"], bh["c"]), v1[2])
        if hi[0] > v1[0]:
            return _range(v1, hi)
    if _FROM.search(seg[:a1.start()]) or seg[a1.end():a1.end() + 1] == "+":
        if v1[0] == 0:
            return "Free" if len(amts) == 1 else _range(v1, max(vals[1:]))
        return f"From {_show(v1[1], v1[2])}"
    if v1[0] == 0:
        if len(amts) == 1:
            return "Free"
        return _range(v1, max(vals[1:]))     # "£ 0 £25 £50": tiers, not free
    return _show(v1[1], v1[2])


def _low(label: str) -> float | None:
    """Lowest amount in a normalised label ('£8–£30' -> 8); None for Free / PWYC / Donation."""
    if label.startswith(("Free", "Pay what", "Donation")):
        return None
    m = re.search(r"\d+(?:\.\d+)?", label)
    return float(m.group()) if m else None


def _from(label: str) -> str:
    """'From <lowest amount>' of a normalised label ('£11–£25' -> 'From £11')."""
    if label.startswith("From "):
        return label
    num = re.search(r"\d+(?:\.\d+)?", label).group()
    cur = "₽" if "₽" in label else label[0] if label[0] in "£€$" else ""
    return f"From {_show(num, cur)}"


def _tiers(text: str) -> list[tuple[str, str | None]]:
    """(price label, 'tier' | 'gen' | None) per priced clause. A label (restricted tier or
    explicit general admission) carries to the following comma clauses of its tier until the
    next label; instalment / per-month clauses are skipped."""
    out: list[tuple[str, str | None]] = []
    for seg in _SEGMENT_SPLIT.split(_clean(text)):
        state: str | None = None
        for cl in _CLAUSE_SPLIT.split(seg):
            if _INSTALMENT.search(cl):
                continue
            if _GENERAL.search(cl):
                state = "gen"
            elif _TIER.search(cl):
                state = "tier"
            if p := _segment_price(cl):
                out.append((p, state))
    return out


def parse_price_text(text: str) -> str | None:
    """Normalised price ('Free', '£12', '£8–£30', 'From £15', 'Pay what you can') or None.
    Several tiers: restricted ones (member / concession / student / child / ...) are dropped;
    an explicitly labelled general tier (full price / standard / adult) wins; two or more
    distinct general tiers give 'From <lowest>'."""
    try:
        if not text or not isinstance(text, str):
            return None
        found = _tiers(text)
        if not found:
            return None
        general = [x for x in found if x[1] != "tier"]
        if not general:
            # every tier restricted ("Concession £5 per workshop, £70 for 14 week"): nothing
            # general to pick, so fall back to the cheapest amount stated
            if _low(found[0][0]) is None:
                return found[0][0]
            return min((x for x in found if _low(x[0]) is not None), key=lambda x: _low(x[0]))[0]
        if _low(general[0][0]) is None:          # Free / PWYC / Donation first: as stated
            return general[0][0]
        priced = [x for x in general if _low(x[0]) is not None]
        pool = [x for x in priced if x[1] == "gen"] or priced
        labels = list(dict.fromkeys(x[0] for x in pool))
        if len(labels) == 1:
            return labels[0]
        return _from(min(labels, key=_low))      # "£15 | £12 Advance | £8 MEMBERS" -> From £12
    except Exception:
        return None


def price_amounts(text: str) -> frozenset[float]:
    """Every currency amount in text as a number ('£10.00' and '£10' -> 10.0; 'от 750 руб.' and
    'From 750 ₽' -> 750.0); bare range partners ('£10–15') included."""
    out: set[float] = set()
    try:
        t = str(text or "")
        for m in _AMT.finditer(t):
            out.add(_fmt(m)[0])
            bh = _BARE_HI.match(t, m.end()) if m["pre"] else None
            if bh:
                out.add(_num(bh["a"], bh["c"])[0])
            bl = _BARE_LO.search(t[:m.start()]) if not m["pre"] else None
            if bl:
                out.add(_num(bl["a"], bl["c"])[0])
    except Exception:
        pass
    return frozenset(out)


def _price_like(seg: str) -> bool:
    """Strict check that a block segment is about price (not a title like 'Free Your Mind')."""
    s = _clean(seg)
    if _AMT.search(s) or _PWYC.search(s) or _STRICT_FREE.search(s):
        return True
    return bool(re.search(r"\b(?:by|suggested|free\s+with)\s+donation\b|\bdonation\s+(?:only|welcome|requested)\b", s, re.I))


def find_price(block_text: str) -> str | None:
    """First price-like segment in a multi-line block, normalised; None if none."""
    try:
        if not block_text or not isinstance(block_text, str):
            return None
        for line in block_text.splitlines():
            for seg in re.split(r"\s\|\s|\s[•·]\s", _LINK.sub("", line)):
                if _price_like(seg):
                    p = parse_price_text(seg)
                    if p:
                        return p
        return None
    except Exception:
        return None
