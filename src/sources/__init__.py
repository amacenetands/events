from .eventbrite import fetch_eventbrite
from .skiddle import fetch_skiddle
from .songkick import fetch_songkick

# All API sources — add new sources here; html_sites handled separately
API_SOURCES = [fetch_eventbrite, fetch_skiddle, fetch_songkick]
