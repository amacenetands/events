"""Songkick API v3 source — metro area id from cities.yaml (`songkick_metro`)."""
import os
import httpx
from datetime import date, timedelta
from ..models import Event

API = "https://api.songkick.com/api/3.0"


def _parse_event(raw: dict) -> Event | None:
    try:
        start = raw.get("start") or {}
        date_str = start.get("date", "")
        if not date_str:
            return None
        event_date = date.fromisoformat(date_str)

        time_str = start.get("time")
        if time_str:
            time_str = time_str[:5]

        venue_data = raw.get("venue") or {}
        venue_name = venue_data.get("displayName", "London")

        title = raw.get("displayName", "")
        if not title:
            return None

        # Songkick is almost exclusively concerts/gigs
        etype = raw.get("type", "Concert")
        category = "concert" if etype == "Concert" else "other"

        return Event(
            title=title[:120],
            venue=venue_name[:80],
            date=event_date,
            time=time_str,
            category=category,
            is_one_off=True,
            is_kids=False,
            url=raw.get("uri", "https://www.songkick.com"),
            description=None,
            price=None,
            source="Songkick",
        )
    except Exception:
        return None


async def fetch_songkick(metro: int | None = None) -> list[Event]:
    """Upcoming events in a Songkick metro area (default: London's from cities.yaml)."""
    if metro is None:
        from ..config import get_city
        metro = get_city("london").songkick_metro
    if not metro:
        print("  [Songkick] No metro id configured — skipping")
        return []
    key = os.environ.get("SONGKICK_API_KEY", "")
    if not key:
        print("  [Songkick] No key — skipping")
        return []

    params = {
        "apikey": key,
        "per_page": 100,
        "min_date": date.today().isoformat(),
        "max_date": (date.today() + timedelta(days=10)).isoformat(),
    }

    events = []
    async with httpx.AsyncClient(timeout=30) as client:
        page = 1
        while True:
            r = await client.get(
                f"{API}/metro_areas/{metro}/calendar.json",
                params={**params, "page": page},
            )
            if r.status_code != 200:
                print(f"  [Songkick] HTTP {r.status_code}")
                break
            data = r.json().get("resultsPage", {})
            raw_events = data.get("results", {}).get("event") or []
            for raw in raw_events:
                e = _parse_event(raw)
                if e:
                    events.append(e)
            total = data.get("totalEntries", 0)
            per_page = data.get("perPage", 100)
            if page * per_page >= total or page >= 5:
                break
            page += 1

    print(f"  [Songkick] {len(events)} events")
    return events
