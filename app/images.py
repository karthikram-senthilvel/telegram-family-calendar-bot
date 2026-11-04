"""Reading events out of pictures (flyers, school calendars, PDFs).

The model only reads the picture; the rules about what goes on the calendar live here, in
code: which categories are skipped, the source prefix ("LA - "), past dates, duplicates, and
a weekday cross-check that catches misread day numbers.
"""
from __future__ import annotations

from datetime import date

from .dates import WEEKDAYS, parse_date

IMAGE_MIME_TYPES = ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf")
MAX_IMAGE_BYTES = 15 * 1024 * 1024  # Gemini accepts up to 20 MB inline per request

# Kept on the calendar. Everything else (public holidays, greetings, themes, other
# organisations' closures) is listed as skipped but can still be added by asking.
KEEP_CATEGORIES = {"event", "closure"}
SKIP_REASONS = {
    "public_holiday": "public holiday",
    "observance": "observance",
    "theme": "theme",
    "other_closure": "not this place",
}

IMAGE_PROMPT = """You read a picture or PDF (a school/daycare calendar, flyer, schedule, invitation)
and list everything on it that has a date. Reply with ONE JSON object only.

Schema:
{
  "source": string | null,       // who published it, from the logo/header, e.g. "Little Acorns Academy"
  "period": "YYYY-MM" | null,     // the month shown, if it is a monthly calendar
  "items": [{
    "title": string,              // short, capitalized; WITHOUT the publisher's name or abbreviation
    "date": "YYYY-MM-DD",
    "end_date": "YYYY-MM-DD" | null,   // only when one item spans several days
    "weekday": "Mon"|"Tue"|"Wed"|"Thu"|"Fri"|"Sat"|"Sun" | null,  // the weekday COLUMN or label printed for that date
    "start_time": "HH:MM" | null,  // 24-hour, only if a time is printed ("@ 3:30" in a school day -> 15:30)
    "end_time": "HH:MM" | null,
    "location": string | null,     // room numbers count, e.g. "Rooms 10-11"
    "notes": string | null,        // what to bring/wear, who provides what
    "audience": string | null,     // if only for some classes/grades, e.g. "Pre-K", "3-year-olds", "Kinder-5th"
    "for_our_class": true | false | null,  // only when OUR CLASS is given and the item has an audience
    "category": "event" | "closure" | "other_closure" | "public_holiday" | "observance" | "theme",
    "needs_check": string | null   // anything unfinished or unclear, e.g. "room says INSERT ROOM #1"
  }],
  "reply": string | null          // only if there is nothing with a date: one short sentence why
}

Categories:
- event: something that happens at/for the publisher: dress-up days, parties, picnics, lunches,
  drives and donation deadlines, conferences, performances, picture day, field trips.
- closure: the publisher itself is closed ("LAA Closed", "No school").
- other_closure: SOMEONE ELSE is closed, e.g. "ISD Schools Closed".
- public_holiday: national/public holidays (Veterans Day, Thanksgiving, Christmas, Memorial Day...),
  including greetings like "Happy Thanksgiving" on a closure day (list the closure separately).
- observance: greetings and awareness days ("Happy Diwali!", "National STEAM Day").
- theme: weekly or monthly themes ("Theme: Wonders of the World").

Rules:
- Use the year/month printed on the picture; otherwise use the DATE REFERENCE.
- Read the day number from the cell and the weekday from its column; report both honestly.
- Repeating notes ("Wear ORANGE every Friday", highlighted Fridays): one item per date it applies to,
  but not on days the publisher is closed.
- One closure spanning consecutive days is ONE item with end_date.
- Never invent times, places or details that are not printed. Leave unfilled template text
  ("INSERT ROOM #1", "TBD") out of the fields and mention it in needs_check instead.
"""

REVISE_PROMPT = """The bot read a picture and is waiting for the family to confirm a list of
calendar items before adding them. Decide what the new MESSAGE asks for. Reply with ONE JSON object only.

{
  "action": "confirm" | "cancel" | "revise" | "unrelated",
  "items": [Item, ...]   // action=revise only: the COMPLETE updated list, same order and fields
}
Item = {"n", "title", "date", "end_date", "start_time", "end_time", "location", "notes",
        "include": true|false, "reason": string|null}

- confirm: "yes", "add them", "looks good", "go ahead".
- cancel: "no", "don't add any", "forget it".
- revise: skip/include items by number or name ("skip 1 and 3", "add the ISD closures too"),
  or change them ("make 2 at 4pm", "rename 5 to Food drive"). Keep every other item unchanged.
  Including a skipped item means include=true and reason=null.
- unrelated: the message is about something else (a new event, a question, an edit to an existing event).
Times are 24-hour "HH:MM". Never change items the message does not mention.
"""


def parse_sources(raw: str) -> list[tuple[str, str]]:
    """'Little Acorns=LA; Lakeside Soccer=LS' -> [('little acorns', 'LA'), ...]"""
    out = []
    for part in raw.replace(",", ";").split(";"):
        name, sep, prefix = part.partition("=")
        if sep and name.strip() and prefix.strip():
            out.append((name.strip().lower(), prefix.strip()))
    return out


def prefix_for(source: str | None, sources: list[tuple[str, str]]) -> str | None:
    s = (source or "").lower()
    for name, prefix in sources:
        if name in s:
            return prefix
    return None


def with_prefix(title: str, prefix: str | None) -> str:
    title = title.strip()
    if not prefix:
        return title
    # Don't double up, and drop "LAA Closed" style abbreviations the model left in.
    for lead in (f"{prefix} - ", f"{prefix}-", f"{prefix}: ", f"{prefix} "):
        if title.lower().startswith(lead.lower()):
            title = title[len(lead):].strip()
            break
    first, _, rest = title.partition(" ")
    if rest and first.isupper() and first.startswith(prefix.upper()) and len(first) <= len(prefix) + 2:
        title = rest.strip()
    return f"{prefix} - {title}"


def number_rows(rows: list[dict]) -> list[dict]:
    """Items to add first (1, 2, 3...), then skipped ones, each in date order."""
    rows.sort(key=lambda r: (not r["include"], r["date"], r.get("start_time") or ""))
    for n, r in enumerate(rows, 1):
        r["n"] = n
    return rows


def _clean(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def review_items(
    raw_items: list,
    today: date,
    prefix: str | None,
    existing: set[tuple[str, date]],
) -> list[dict]:
    """Turn the model's items into proposal rows: each has include=True/False and a reason."""
    rows: list[dict] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        title = _clean(it.get("title"))
        day = parse_date(it.get("date"))
        if not title or not day:
            continue
        end_day = parse_date(it.get("end_date"))
        if end_day and end_day <= day:
            end_day = None
        row = {
            "title": with_prefix(title, prefix),
            "date": day.isoformat(),
            "end_date": end_day.isoformat() if end_day else None,
            "start_time": _clean(it.get("start_time")),
            "end_time": _clean(it.get("end_time")),
            "location": _clean(it.get("location")),
            "notes": _clean(it.get("notes")),
            "include": True,
            "reason": None,
            "warning": _clean(it.get("needs_check")),
        }
        category = str(it.get("category") or "event").lower()
        printed = str(it.get("weekday") or "")[:3].title()
        actual = WEEKDAYS[day.weekday()][:3]
        if category not in KEEP_CATEGORIES:
            row["include"], row["reason"] = False, SKIP_REASONS.get(category, category)
        elif it.get("for_our_class") is False:
            row["include"], row["reason"] = False, f"for {_clean(it.get('audience')) or 'another class'}"
        elif (end_day or day) < today:
            row["include"], row["reason"] = False, "already passed"
        elif (row["title"].lower(), day) in existing:
            row["include"], row["reason"] = False, "already on the calendar"
        elif printed and printed != actual:
            # The day number and its column disagree: probably misread. Ask before adding.
            row["include"] = False
            row["reason"] = f"check the date: picture shows {printed}, but {day.strftime('%b')} {day.day} is a {actual}"
        rows.append(row)
    return number_rows(rows)
