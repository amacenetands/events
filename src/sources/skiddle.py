"""Skiddle API v1 source."""
import os
import httpx
from datetime import date, timedelta
from ..models import Event

API = "https://www.skiddle.com/api/v1"

_CATEGORY_MAP = {
    "LIVE": "concert",
    "CLUB": "concert",
    "GIGS": "concert",
    "GRDN": "other",
    "FEST": "other",
    "COMEDY": "theatre",
    "THEATRE": "theatre",
    "EXHIBITION": "exhibition",
    "KIDS": "kids",
    "FAMILY": "kids",
}


def _parse_event(raw: dict) -> Event | None:
    try:
        event_date_str = raw.get("date", "")
        if not event_date_str:
            return None
        event_date = date.fromisoformat(event_date_str)

        venue = raw.get("venue") or {}
        venue_name = venue.get("name") or "London"

        etype = raw.get("EventCode", "LIVE")
        category = _CATEGORY_MAP.get(etype, "other")

        price_val = raw.get("entryprice", "")
        price_str = "Free" if price_val in ("0", "0.00", "") else f"£{price_val}" if price_val else None

        title = raw.get("eventname", "")
        if not title:
            return None

        start_time = raw.get("starttime", "")
        time_str = start_time[:5] if start_time else None

        return Event(
            title=title[:120],
            venue=venue_name[:80],
            date=event_date,
            time=time_str,
            category=category,
            is_one_off=True,
            is_kids=(category == "kids"),
            url=raw.get("link", "https://www.skiddle.com"),
            description=(raw.get("description") or "")[:200] or None,
            price=price_str,
            source="Skiddle",
        )
    except Exception:
        return None


async def fetch_skiddle() -> list[Event]:
    key = os.environ.get("SKIDDLE_API_KEY", "")
    if not key:
        print("  [Skiddle] No key — skipping")
        return []

    start = date.today()
    end = start + timedelta(days=10)
    params = {
        "api_key": key,
        "latitude": 51.5074,
        "longitude": -0.1278,
        "radius": 10,
        "startdate": start.isoformat(),
        "enddate": end.isoformat(),
        "limit": 100,
        "order": "date",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{API}/events/search/", params=params)
        if r.status_code != 200:
            print(f"  [Skiddle] HTTP {r.status_code}")
            return []
        data = r.json()

    events = []
    for raw in data.get("results", []):
        e = _parse_event(raw)
        if e:
            events.append(e)

    print(f"  [Skiddle] {len(events)} events")
    return events
