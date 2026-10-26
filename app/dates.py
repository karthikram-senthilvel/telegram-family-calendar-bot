"""Deterministic date/time handling.

The language model only extracts fields; every rule about relative dates and
default times lives here so it behaves the same way every time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import Event

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Vague time words -> default start time.
TIME_DEFAULTS = {
    "morning": time(9, 0),
    "noon": time(12, 0),
    "lunch": time(12, 0),
    "afternoon": time(14, 0),
    "evening": time(18, 0),
    "night": time(20, 0),
    "tonight": time(20, 0),
}


def next_weekday(today: date, weekday: int) -> date:
    """'next <weekday>': the next occurrence strictly after today.

    Said on that same weekday it therefore means seven days later.
    """
    delta = (weekday - today.weekday()) % 7
    return today + timedelta(days=delta or 7)


def this_weekday(today: date, weekday: int) -> date:
    """'this <weekday>' / bare '<weekday>': today if it is that day, else the next one."""
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def date_reference(now: datetime) -> str:
    """A lookup table the model copies dates from instead of doing date math."""
    today = now.date()
    lines = [
        f"Now: {WEEKDAYS[today.weekday()]} {today.isoformat()} {now.strftime('%H:%M')} ({now.tzname()})",
        f"today = {today.isoformat()}",
        f"tomorrow = {(today + timedelta(days=1)).isoformat()}",
        f"day after tomorrow = {(today + timedelta(days=2)).isoformat()}",
    ]
    for i, name in enumerate(WEEKDAYS):
        lines.append(
            f"{name} / this {name} = {this_weekday(today, i).isoformat()}; "
            f"next {name} = {next_weekday(today, i).isoformat()}"
        )
    sat = this_weekday(today, 5)
    lines.append(f"this weekend = {sat.isoformat()} to {(sat + timedelta(days=1)).isoformat()}")
    lines.append(f"next weekend = {(sat + timedelta(days=7)).isoformat()} to {(sat + timedelta(days=8)).isoformat()}")
    lines.append("Calendar for the next 6 weeks:")
    start = today - timedelta(days=today.weekday())
    for w in range(6):
        week = [start + timedelta(days=7 * w + d) for d in range(7)]
        lines.append("  " + "  ".join(f"{WEEKDAYS[d.weekday()][:3]} {d.isoformat()}" for d in week))
    return "\n".join(lines)


_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def parse_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    m = _TIME_RE.match(str(value))
    if not m:
        return None
    h, mnt = int(m.group(1)), int(m.group(2))
    if h == 24 and mnt == 0:
        return time(23, 59)
    if 0 <= h < 24 and 0 <= mnt < 60:
        return time(h, mnt)
    return None


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


@dataclass
class Resolved:
    event: Event
    notes: list[str] = field(default_factory=list)  # e.g. "assumed 9:00 for morning"


def resolve_times(
    day: date,
    end_day: date | None,
    start_time: str | None,
    end_time: str | None,
    time_hint: str | None,
    tz: ZoneInfo,
    default_minutes: int,
) -> tuple[date | datetime, date | datetime, list[str]]:
    """Turn extracted date/time fields into (start, end, notes)."""
    notes: list[str] = []
    st = parse_hhmm(start_time)
    et = parse_hhmm(end_time)
    hint = (time_hint or "").strip().lower() or None

    if st is None and hint in TIME_DEFAULTS:
        st = TIME_DEFAULTS[hint]
        notes.append(f"assumed {st.strftime('%-H:%M')} for {hint}")

    if st is None:
        # All-day event (possibly multi-day). End is exclusive.
        last = end_day if end_day and end_day >= day else day
        if et is not None:
            notes.append("ignored the end time because no start time was given")
        return day, last + timedelta(days=1), notes

    start = datetime.combine(day, st, tzinfo=tz)
    if et is not None:
        end = datetime.combine(end_day or day, et, tzinfo=tz)
        if end <= start:
            # "10pm to 1am" crosses midnight.
            end += timedelta(days=1)
    else:
        end = start + timedelta(minutes=default_minutes)
        if end_day and end_day > day:
            end = datetime.combine(end_day, st, tzinfo=tz) + timedelta(minutes=default_minutes)
    return start, end, notes


def fmt_time(t: datetime) -> str:
    return t.strftime("%-I:%M %p")


def fmt_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.strftime('%B')} {d.day}, {d.year}"


def fmt_when(ev: Event) -> str:
    if ev.all_day:
        first, last = ev.start_date(), ev.last_date()
        if first == last:
            return f"{fmt_date(first)} (all day)"
        return f"{fmt_date(first)} – {fmt_date(last)} (all day)"
    s, e = ev.start, ev.end
    if s.date() == e.date():
        return f"{fmt_date(s.date())}, {fmt_time(s)} – {fmt_time(e)}"
    return f"{fmt_date(s.date())} {fmt_time(s)} – {fmt_date(e.date())} {fmt_time(e)}"


def fmt_short(ev: Event) -> str:
    """Compact one-liner used in lists and in the model prompt."""
    d = ev.start_date()
    day = f"{WEEKDAYS[d.weekday()][:3]} {d.strftime('%b')} {d.day}"
    if ev.all_day:
        return f"{day} (all day)"
    return f"{day} {fmt_time(ev.start)}–{fmt_time(ev.end)}"
