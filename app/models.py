"""Calendar-neutral event model."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass
class Event:
    title: str
    # All-day events use `date` for start/end with an *exclusive* end date
    # (the iCalendar / Google convention). Timed events use aware datetimes.
    start: date | datetime
    end: date | datetime
    location: str | None = None
    notes: str | None = None
    id: str | None = None

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "all_day": self.all_day,
            "location": self.location,
            "notes": self.notes,
            "id": self.id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        parse = date.fromisoformat if d["all_day"] else datetime.fromisoformat
        return cls(
            title=d["title"],
            start=parse(d["start"]),
            end=parse(d["end"]),
            location=d.get("location"),
            notes=d.get("notes"),
            id=d.get("id"),
        )

    def start_date(self) -> date:
        return self.start if self.all_day else self.start.date()

    def last_date(self) -> date:
        """Inclusive last day of the event."""
        if self.all_day:
            return self.end - timedelta(days=1)
        return self.end.date()
