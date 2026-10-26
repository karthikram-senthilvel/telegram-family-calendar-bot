"""Google Calendar backend.

Two ways to authenticate, both unable to reach the account's primary calendar:
- Service account (preferred): data/google/service-account.json. It can only see
  calendars explicitly shared with its e-mail address.
- OAuth user token: data/google/token.json with the narrow `calendar.app.created`
  scope, so it can only see calendars the bot created itself.
A second guard refuses 'primary' and anything that is not a secondary calendar id.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..models import Event
from .base import CalendarBackend

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]
SERVICE_ACCOUNT_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
SERVICE_ACCOUNT_FILE = "service-account.json"
SOURCE_TAG = "family-event-bot"


def load_calendar_id(token_path: Path, explicit: str | None) -> str:
    if explicit:
        cal_id = explicit
    else:
        info = token_path.parent / "calendar.json"
        if not info.exists():
            raise RuntimeError(
                f"No Google calendar configured. Run scripts/setup_google_service_account.py "
                f"or scripts/authorize_google.py first (expected {info})."
            )
        cal_id = json.loads(info.read_text())["calendar_id"]
    if cal_id == "primary" or not cal_id.endswith("@group.calendar.google.com"):
        raise RuntimeError(
            f"Refusing to use calendar {cal_id!r}: the bot may only write to its own "
            "secondary calendar, never the primary one."
        )
    return cal_id


class GoogleCalendar(CalendarBackend):
    name = "Google Calendar"

    def __init__(self, token_path: Path, calendar_id: str | None, tz: ZoneInfo):
        self.token_path = token_path
        self.tz = tz
        self.calendar_id = load_calendar_id(token_path, calendar_id)
        sa_path = token_path.parent / SERVICE_ACCOUNT_FILE
        self._is_service_account = sa_path.exists()
        if self._is_service_account:
            self._creds = service_account.Credentials.from_service_account_file(
                str(sa_path), scopes=SERVICE_ACCOUNT_SCOPES
            )
        elif token_path.exists():
            self._creds = Credentials.from_authorized_user_file(str(token_path))
            if SCOPES[0] not in (self._creds.scopes or []):
                raise RuntimeError(f"Google token lacks {SCOPES[0]}; rerun scripts/authorize_google.py")
        else:
            raise RuntimeError(
                f"No Google credentials in {token_path.parent}; run scripts/setup_google_service_account.py"
            )
        self._lock = threading.Lock()
        self._service = None

    def _svc(self):
        with self._lock:
            if not self._creds.valid:
                self._creds.refresh(Request())
                if not self._is_service_account:
                    self.token_path.write_text(self._creds.to_json())
            if self._service is None:
                self._service = build("calendar", "v3", credentials=self._creds, cache_discovery=False)
            return self._service

    # -- conversion -------------------------------------------------------
    def _to_body(self, ev: Event) -> dict:
        body: dict = {
            "summary": ev.title,
            "extendedProperties": {"private": {"source": SOURCE_TAG}},
        }
        if ev.location:
            body["location"] = ev.location
        if ev.notes:
            body["description"] = ev.notes
        if ev.all_day:
            body["start"] = {"date": ev.start.isoformat()}
            body["end"] = {"date": ev.end.isoformat()}
        else:
            tzname = str(self.tz)
            body["start"] = {"dateTime": ev.start.isoformat(), "timeZone": tzname}
            body["end"] = {"dateTime": ev.end.isoformat(), "timeZone": tzname}
        return body

    def _from_body(self, b: dict) -> Event:
        def parse(part: dict) -> date | datetime:
            if "date" in part:
                return date.fromisoformat(part["date"])
            return datetime.fromisoformat(part["dateTime"].replace("Z", "+00:00")).astimezone(self.tz)

        return Event(
            title=b.get("summary") or "(untitled)",
            start=parse(b["start"]),
            end=parse(b["end"]),
            location=b.get("location"),
            notes=b.get("description"),
            id=b["id"],
        )

    # -- operations -------------------------------------------------------
    def create(self, ev: Event) -> Event:
        res = self._svc().events().insert(calendarId=self.calendar_id, body=self._to_body(ev)).execute()
        return self._from_body(res)

    def update(self, ev: Event) -> Event:
        assert ev.id
        body = self._to_body(ev)
        # Clear fields that were removed.
        body.setdefault("location", "")
        body.setdefault("description", "")
        res = self._svc().events().patch(
            calendarId=self.calendar_id, eventId=ev.id, body=body
        ).execute()
        return self._from_body(res)

    def delete(self, event_id: str) -> None:
        try:
            self._svc().events().delete(calendarId=self.calendar_id, eventId=event_id).execute()
        except HttpError as e:
            if e.resp.status not in (404, 410):
                raise

    def get(self, event_id: str) -> Event | None:
        try:
            res = self._svc().events().get(calendarId=self.calendar_id, eventId=event_id).execute()
        except HttpError as e:
            if e.resp.status in (404, 410):
                return None
            raise
        if res.get("status") == "cancelled":
            return None
        return self._from_body(res)

    def list_between(self, start: datetime, end: datetime, limit: int = 50) -> list[Event]:
        res = self._svc().events().list(
            calendarId=self.calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=min(limit, 250),
        ).execute()
        return [self._from_body(b) for b in res.get("items", []) if b.get("status") != "cancelled"]
