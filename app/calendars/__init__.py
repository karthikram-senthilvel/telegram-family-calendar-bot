from __future__ import annotations

from ..config import Config
from .base import CalendarBackend


def make_backend(cfg: Config) -> CalendarBackend:
    if cfg.calendar_backend == "google":
        from .google_cal import GoogleCalendar

        return GoogleCalendar(cfg.google_token_path, cfg.google_calendar_id, cfg.tz)
    from .caldav_cal import CalDAVCalendar

    return CalDAVCalendar(
        cfg.caldav_url, cfg.caldav_username, cfg.caldav_password, cfg.caldav_calendar_name, cfg.tz
    )
