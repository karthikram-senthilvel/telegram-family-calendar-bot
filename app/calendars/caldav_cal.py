"""CalDAV backend (used with the local Radicale server)."""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import caldav
from caldav.lib.error import NotFoundError
from icalendar import Calendar, Event as ICalEvent

from ..models import Event
from .base import CalendarBackend

log = logging.getLogger(__name__)


class CalDAVCalendar(CalendarBackend):
    name = "CalDAV"

    def __init__(self, url: str, username: str, password: str, calendar_name: str, tz: ZoneInfo):
        self.tz = tz
        self._lock = threading.Lock()
        self._client = caldav.DAVClient(url=url, username=username, password=password)
        principal = self._client.principal()
        cal = next((c for c in principal.calendars() if c.name == calendar_name), None)
        if cal is None:
            log.info("Creating CalDAV calendar %r", calendar_name)
            cal = principal.make_calendar(name=calendar_name)
        self._cal = cal

    # -- conversion -------------------------------------------------------
    def _to_ics(self, ev: Event, uid: str) -> str:
        cal = Calendar()
        cal.add("prodid", "-//family-event-bot//EN")
        cal.add("version", "2.0")
        ie = ICalEvent()
        ie.add("uid", uid)
        ie.add("dtstamp", datetime.now(tz=ZoneInfo("UTC")))
        ie.add("summary", ev.title)
        # Timed events are stored in UTC: a bare TZID without a VTIMEZONE block is
        # misread by servers when expanding, while UTC is unambiguous everywhere.
        utc = ZoneInfo("UTC")
        ie.add("dtstart", ev.start if ev.all_day else ev.start.astimezone(utc))
        ie.add("dtend", ev.end if ev.all_day else ev.end.astimezone(utc))
        if ev.location:
            ie.add("location", ev.location)
        if ev.notes:
            ie.add("description", ev.notes)
        cal.add_component(ie)
        return cal.to_ical().decode()

    def _from_obj(self, obj) -> Event:
        comp = obj.icalendar_component
        start = comp.decoded("dtstart")
        if "dtend" in comp:
            end = comp.decoded("dtend")
        elif "duration" in comp:
            end = start + comp.decoded("duration")
        else:
            end = start + (timedelta(days=1) if not isinstance(start, datetime) else timedelta(hours=1))
        if isinstance(start, datetime):
            start = start.astimezone(self.tz) if start.tzinfo else start.replace(tzinfo=self.tz)
            end = end.astimezone(self.tz) if end.tzinfo else end.replace(tzinfo=self.tz)
        return Event(
            title=str(comp.get("summary", "(untitled)")),
            start=start,
            end=end,
            location=str(comp["location"]) if comp.get("location") else None,
            notes=str(comp["description"]) if comp.get("description") else None,
            id=str(comp["uid"]),
        )

    # -- operations -------------------------------------------------------
    def create(self, ev: Event) -> Event:
        uid = str(uuid.uuid4())
        with self._lock:
            self._cal.save_event(self._to_ics(ev, uid))
        return Event(**{**ev.__dict__, "id": uid})

    def update(self, ev: Event) -> Event:
        assert ev.id
        with self._lock:
            obj = self._cal.event_by_uid(ev.id)
            obj.data = self._to_ics(ev, ev.id)
            obj.save()
        return ev

    def delete(self, event_id: str) -> None:
        with self._lock:
            try:
                self._cal.event_by_uid(event_id).delete()
            except NotFoundError:
                pass

    def get(self, event_id: str) -> Event | None:
        with self._lock:
            try:
                return self._from_obj(self._cal.event_by_uid(event_id))
            except NotFoundError:
                return None

    def list_between(self, start: datetime, end: datetime, limit: int = 50) -> list[Event]:
        with self._lock:
            objs = self._cal.search(start=start, end=end, event=True, expand=True)
        events = [self._from_obj(o) for o in objs]

        def key(e: Event):
            s = e.start if not e.all_day else datetime.combine(e.start, datetime.min.time(), tzinfo=self.tz)
            return s

        events.sort(key=key)
        return events[:limit]
