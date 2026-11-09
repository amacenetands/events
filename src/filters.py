"""Content filters applied to scraped events before ranking/digest.

Religious-service filter: churches in the source list (St Bride's, St Paul's,
Southwark Cathedral, …) list both *concerts/recitals* (which we want) and
*acts of worship* — Choral Evensong, Sung Eucharist, Mass, etc. (which we do
not). We keep anything that looks like a concert/recital and drop the rest by
word match on the title.
"""
import re
from .models import Event

# Worship / liturgical service indicators. Word-boundary, case-insensitive.
# Calibrated against real extracted church titles (St Bride's, St Paul's).
_RELIGIOUS_RE = re.compile(
    r"\b("
    r"evensong|eucharist|communion|vespers|matins|compline|evensongs|"
    r"sung\s+mass|choral\s+mass|holy\s+mass|requiem\s+mass|solemn\s+mass|parish\s+mass|"
    r"holy\s+communion|morning\s+prayer|evening\s+prayer|"
    r"liturgy|litany|patronal|benediction|"
    r"a\s+service\s+of|service\s+of\s+(celebration|thanksgiving|remembrance|commemoration)|"
    r"sermon"
    r")\b",
    re.IGNORECASE,
)

# Concert / performance indicators that override a religious match: a title like
# "Choral Mass in B minor — concert performance" is a concert, not a service.
# Deliberately excludes bare "music"/"choral" (they appear in service titles).
_CONCERT_RE = re.compile(
    r"\b("
    r"concert|recital|symphony|orchestra|quartet|quintet|sonata|cantata|"
    r"jazz|gig|ensemble|philharmon|sinfoni"
    r")\b",
    re.IGNORECASE,
)


def is_religious_service(event: Event) -> bool:
    """True when the title looks like an act of worship rather than a concert."""
    title = event.title or ""
    if _CONCERT_RE.search(title):
        return False
    return bool(_RELIGIOUS_RE.search(title))


def drop_religious_services(events: list[Event]) -> list[Event]:
    """Remove worship services, keeping concerts/recitals held in churches."""
    return [e for e in events if not is_religious_service(e)]
