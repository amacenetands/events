"""Coverage report input: last fetch status per venue from the scrapes table."""
from __future__ import annotations

from typing import Iterable


def fetch_status_report(city_tag_or_venue_names: str | Iterable) -> list[dict]:
    """Rows {name, url, status, status_at, note, fetched_at, html_len, ...}, one per venue.
    Arg: a city name ('london'), a tag ('moscow-theatre'), or an iterable of venue
    names / VenueConfig. Venues never fetched come back with status None."""
    from .. import store
    from ..config import city_for_tag, city_names, load_venues
    arg = city_tag_or_venue_names
    if isinstance(arg, str):
        if arg in city_names():
            names = [v.name for v in load_venues(arg)]
        else:
            names = [v.name for v in load_venues(city_for_tag(arg), tags=[arg])]
    else:
        names = [getattr(v, "name", v) for v in arg]
    rows = {r["site_name"]: r for r in store.scrape_status_rows(names)}
    out = []
    for n in names:
        r = rows.get(n)
        if r is None:
            out.append({"name": n, "status": None})
        else:
            out.append({"name": n, **{k: v for k, v in r.items() if k != "site_name"}})
    return out
