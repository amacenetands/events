from .skiddle import fetch_skiddle
from .songkick import fetch_songkick

# All API sources — add new sources here; html_sites handled separately
API_SOURCES = [fetch_skiddle, fetch_songkick]
