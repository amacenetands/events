"""Two-stage ranking: score → top 50 → heuristic picks."""
from .models import Event


def score(event: Event) -> float:
    s = 0.0
    if event.is_one_off:
        s += 10.0
    if event.is_kids:
        s += 4.0
    if event.is_family:
        s += 3.0
    if event.category == "concert":
        s += 3.0
    if event.category in ("gallery", "theatre"):
        s += 2.0
    if event.time and int(event.time.split(":")[0]) >= 18:
        s += 1.0
    return s

def pre_filter(events: list[Event]) -> list[Event]:
    """Score and return top 50 candidates for Claude ranking."""
    sorted_events = sorted(events, key=score, reverse=True)
    return sorted_events[:50]


def rank_with_heuristic(events: list[Event], n: int = 10) -> list[Event]:
    """Score-based ranking with venue + category diversity."""
    scored = sorted(events, key=score, reverse=True)
    selected: list[Event] = []
    venue_counts: dict[str, int] = {}
    cat_counts: dict[str, int] = {}

    for e in scored:
        if len(selected) >= n:
            break
        if venue_counts.get(e.venue, 0) >= 2:
            continue
        if cat_counts.get(e.category, 0) >= 3:
            continue
        selected.append(e)
        venue_counts[e.venue] = venue_counts.get(e.venue, 0) + 1
        cat_counts[e.category] = cat_counts.get(e.category, 0) + 1

    # fill remaining with relaxed constraints (venue cap only)
    if len(selected) < n:
        selected_ids = {id(e) for e in selected}
        for e in scored:
            if len(selected) >= n:
                break
            if id(e) not in selected_ids and venue_counts.get(e.venue, 0) < 2:
                selected.append(e)
                venue_counts[e.venue] = venue_counts.get(e.venue, 0) + 1
    # final fill with no constraints
    if len(selected) < n:
        selected_ids = {id(e) for e in selected}
        for e in scored:
            if len(selected) >= n:
                break
            if id(e) not in selected_ids:
                selected.append(e)

    return selected


async def rank(events: list[Event]) -> list[Event]:
    """Heuristic ranking with venue + category diversity."""
    return rank_with_heuristic(events)
