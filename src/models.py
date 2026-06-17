from pydantic import BaseModel, field_validator
from datetime import date
from typing import Optional, Literal
import hashlib

Category = Literal["concert", "exhibition", "kids", "gallery", "theatre", "other"]


class Event(BaseModel):
    title: str
    venue: str
    date: date
    end_date: Optional[date] = None
    time: Optional[str] = None  # "19:30"
    category: Category = "other"
    is_one_off: bool = True
    is_kids: bool = False      # primarily FOR children
    is_family: bool = False    # suitable for families/adults+children together
    url: str
    description: Optional[str] = None
    price: Optional[str] = None
    source: str = ""

    @field_validator("date", mode="before")
    @classmethod
    def parse_date(cls, v):
        if isinstance(v, str):
            from datetime import datetime
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %B %Y", "%d %b %Y"):
                try:
                    return datetime.strptime(v, fmt).date()
                except ValueError:
                    continue
        return v

    @field_validator("time", mode="before")
    @classmethod
    def normalise_time(cls, v):
        if not v:
            return None
        import re
        v = str(v).strip().lower()

        # named times
        if v in ("noon", "midday"):
            return "12:00"
        if v == "midnight":
            return "00:00"

        # extract HH[:.]MM and optional am/pm
        # handles: "7pm", "7:30pm", "7.30pm", "7 pm", "7:30 pm", "19:30", "19.30"
        m = re.match(r"(\d{1,2})(?:[:\.](\d{2}))?\s*(am|pm)?", v)
        if not m:
            return None
        h = int(m.group(1))
        mi = int(m.group(2)) if m.group(2) else 0
        period = m.group(3) or ""

        if period == "pm" and h != 12:
            h += 12
        elif period == "am" and h == 12:
            h = 0
        elif not period and h > 23:
            return None

        if h > 23 or mi > 59:
            return None
        return f"{h:02d}:{mi:02d}"

    @property
    def is_weekday_daytime(self) -> bool:
        """True for Mon–Fri events starting before 17:00."""
        if self.date.weekday() >= 5:
            return False
        if self.time is None:
            return False
        try:
            hour = int(self.time.split(":")[0])
            return hour < 17
        except (ValueError, IndexError):
            return False

    def fingerprint(self) -> str:
        raw = f"{self.title.lower().strip()}|{self.venue.lower().strip()}|{self.date.isoformat()}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

