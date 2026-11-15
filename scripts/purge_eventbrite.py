"""One-off: delete cached events whose url host is eventbrite.* from cache/memory.db."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import events_db

print(f"deleted {events_db.purge_banned()} eventbrite rows")
