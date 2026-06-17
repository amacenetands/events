"""Eventbrite API v3 source."""
import os
import httpx
from datetime import date, timedelta, datetime
from ..models import Event

API = "https://www.eventbriteapi.com/v3"


def _map_category(eb_category: str) -> str:
    mapping = {
        "music": "concert",
        "arts": "gallery",
        "film & media": "other",
        "performing & visual arts": "theatre",
        "family & education": "kids",
        "science & technology": "other",
        "food & drink": "other",
        "sports & fitness": "other",
    }
    return mapping.get(eb_category.lower(), "other")


def _parse_event(raw: dict) -> Event | None:
    try:
        start_local = raw.get("start", {}).get("local", "")
        if not start_local:
            return None
        dt = datetime.fromisoformat(start_local)
        event_date = dt.date()
        event_time = dt.strftime("%H:%M")

        venue_data = raw.get("venue") or {}
        venue_name = venue_data.get("name") or raw.get("organizer", {}).get("name", "London")

        cat_name = raw.get("category", {}).get("name", "") if raw.get("category") else ""
        category = _map_category(cat_name)

        is_free = raw.get("is_free", False)
        price_str = "Free" if is_free else None
        if not price_str:
            ticket = raw.get("ticket_availability") or {}
            min_price = ticket.get("minimum_ticket_price") or {}
            if min_price.get("major_value"):
                price_str = f"£{min_price['major_value']}"

        name_text = (raw.get("name") or {}).get("text", "")
        if not name_text:
            return None

        return Event(
            title=name_text[:120],
            venue=venue_name[:80],
            date=event_date,
            time=event_time,
            category=category,
            is_one_off=True,
            is_kids=(category == "kids"),
            url=raw.get("url", ""),
            description=((raw.get("description") or {}).get("text") or "")[:200] or None,
            price=price_str,
            source="Eventbrite",
        )
    except Exception:
        return None


async def fetch_eventbrite() -> list[Event]:
    # Eventbrite public search API deprecated
    return []
