"""Conversation logic, independent of Telegram so it can be tested directly."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from html import escape
from typing import Callable
from zoneinfo import ZoneInfo

from .calendars.base import CalendarBackend
from .dates import (
    TIME_DEFAULTS,
    date_reference,
    fmt_date,
    fmt_short,
    fmt_when,
    parse_date,
    parse_hhmm,
    resolve_times,
)
from .llm import LLM, LLMBillingError, LLMError, estimate_cost
from .models import Event
from .stt import STT, STTError
from .store import Store

log = logging.getLogger(__name__)

CANCEL_RE = re.compile(
    r"^\s*/?(cancel|never\s*mind|nvm|forget\s+it|forget\s+that|start\s+over|reset|stop|scratch\s+that)\W*$",
    re.IGNORECASE,
)

HELP_TEXT = (
    "<b>Family Event Bot</b>\n"
    "Just tell me about an event in plain words, for example:\n"
    "• <i>tennis next Saturday morning 7 to 9:30</i>\n"
    "• <i>kids' dentist Nov 12 at 3:15pm in Plano</i>\n"
    "• <i>Diwali party Saturday evening at Priya's</i>\n\n"
    "Change things the same way:\n"
    "• <i>move it to 8am</i> · <i>change the title to Doubles</i> · <i>delete the tennis event</i>\n\n"
    "/upcoming – next events\n"
    "/usage – which AI model answered and what it cost\n"
    "/cancel – drop whatever I was asking and start over\n"
    "/help – this message\n\n"
    "🎙 You can also send a voice note instead of typing.\n\n"
    "Times: morning 9:00, noon 12:00, afternoon 14:00, evening 18:00, night 20:00. "
    "No time means all-day."
)


@dataclass
class Reply:
    text: str  # Telegram HTML
    undo_token: str | None = None


def _clean(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


class Assistant:
    def __init__(
        self,
        llm: LLM,
        calendar: CalendarBackend,
        store: Store,
        tz: ZoneInfo,
        default_minutes: int = 60,
        now: Callable[[], datetime] | None = None,
        stt: STT | None = None,
        voice_max_seconds: int = 120,
        local_places: str = "",
    ):
        self.llm = llm
        self.cal = calendar
        self.store = store
        self.tz = tz
        self.default_minutes = default_minutes
        self._now = now or (lambda: datetime.now(tz))
        self._locks: dict[int, asyncio.Lock] = {}
        self.stt = stt
        self.voice_max_seconds = voice_max_seconds
        self.local_places = local_places

    def now(self) -> datetime:
        return self._now().astimezone(self.tz)

    async def _cal(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    # ------------------------------------------------------------------ entry
    async def handle(self, chat_id: int, text: str, voice: bool = False) -> list[Reply]:
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            return await self._handle(chat_id, text.strip(), voice)

    async def handle_voice(self, chat_id: int, audio: bytes, seconds: int) -> list[Reply]:
        """Transcribe a voice note locally, then treat it exactly like a typed message."""
        if self.stt is None:
            return [Reply("Voice notes aren't enabled — please type it.")]
        if seconds > self.voice_max_seconds:
            mins = self.voice_max_seconds // 60
            return [Reply(f"That's over {mins} minute{'s' if mins != 1 else ''} — please keep voice notes short.")]
        try:
            text = await self.stt.transcribe(audio)
        except STTError as e:
            log.error("voice transcription failed: %s", e)
            return [Reply("⚠️ I couldn't transcribe that just now — please try again or type it.")]
        if not text:
            return [Reply("I couldn't make that out — try again or type it.")]
        replies = await self.handle(chat_id, text, voice=True)
        heard = f"🎙 <i>“{escape(text)}”</i>"
        if not replies:
            return [Reply(heard)]
        replies[0] = replace(replies[0], text=f"{heard}\n\n{replies[0].text}")
        return replies

    async def _handle(self, chat_id: int, text: str, voice: bool = False) -> list[Reply]:
        if not text:
            return []
        if CANCEL_RE.match(text):
            return [self.cancel(chat_id)]

        pending = self.store.get_pending(chat_id)
        known = await self._known_events(chat_id)
        context = self._build_context(chat_id, pending, known)
        if voice:
            context += (
                "\n\nNOTE: MESSAGE is a speech-to-text transcript and may contain mis-heard words. "
                "Correct obvious mis-hearings of names using KNOWN EVENTS"
                + (f" and these local places: {self.local_places}" if self.local_places else "")
                + ". Times like '7-9.30' mean 07:00-09:30, '3.15pm' means 15:15."
            )
        try:
            result = await self.llm.interpret(context, text)
        except LLMBillingError:
            return [Reply(
                "⚠️ The language model account is out of credits, so I can't read messages right now. "
                "Top up the Gemini API credits in AI Studio (ai.studio → Billing) and try again."
            )]
        except LLMError as e:
            log.error("LLM failure: %s", e)
            return [Reply("⚠️ I couldn't reach the language model just now. Please try again in a minute.")]
        log.info("interpretation: %s", json.dumps(result)[:1000])

        intent = str(result.get("intent") or "chat").lower()
        continues = bool(result.get("continues_pending")) and pending is not None and intent == "create"
        if pending is not None:
            # Either answered now or replaced by a fresh request; _create re-arms it only
            # for a brand-new event (we ask at most once).
            self.store.clear_pending(chat_id)

        if intent == "create":
            return await self._create(chat_id, result.get("events") or [], asked_already=continues)
        if intent == "edit":
            return [await self._edit(chat_id, result, known)]
        if intent == "delete":
            return [await self._delete(chat_id, result, known)]
        if intent == "list":
            return [await self.upcoming()]
        if intent == "cancel":
            return [Reply("Okay, cancelled. Starting fresh — send me a new event any time.")]
        if intent == "help":
            return [Reply(HELP_TEXT)]
        reply = _clean(result.get("reply"))
        if reply:
            return [Reply(escape(reply))]
        return [Reply(
            "I didn't spot an event in that. Try something like <i>tennis next Saturday 7–9:30am</i>, "
            "or /help."
        )]

    def cancel(self, chat_id: int) -> Reply:
        if self.store.clear_pending(chat_id):
            return Reply("Okay, cancelled that. Starting fresh — send me a new event any time.")
        return Reply("Nothing was pending — we're starting fresh. Send me a new event any time.")

    # --------------------------------------------------------------- context
    async def _known_events(self, chat_id: int) -> dict[str, Event]:
        now = self.now()
        start = (now - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            events = await self._cal(self.cal.list_between, start, now + timedelta(days=180), 60)
        except Exception:
            log.exception("could not list events")
            events = []
        known = {e.id: e for e in events if e.id}
        last_id = self.store.get_last_event(chat_id)
        if last_id and last_id not in known:
            try:
                ev = await self._cal(self.cal.get, last_id)
            except Exception:
                ev = None
            if ev:
                known[ev.id] = ev
        return known

    def _build_context(self, chat_id: int, pending: dict | None, known: dict[str, Event]) -> str:
        parts = ["DATE REFERENCE:", date_reference(self.now())]
        parts.append("\nKNOWN EVENTS (id | title | when | location):")
        if known:
            for e in known.values():
                parts.append(f"- {e.id} | {e.title} | {fmt_short(e)} | {e.location or ''}")
        else:
            parts.append("(none)")
        last_id = self.store.get_last_event(chat_id)
        last = known.get(last_id) if last_id else None
        parts.append(f"\nLAST EVENT: {last.id + ' | ' + last.title if last else '(none)'}")
        if pending:
            parts.append(
                "\nPENDING: the bot asked: " + json.dumps(pending["question"])
                + "\nPending draft events: " + json.dumps(pending["drafts"])
            )
        else:
            parts.append("\nPENDING: (none)")
        return "\n".join(parts)

    # ---------------------------------------------------------------- create
    async def _create(self, chat_id: int, drafts: list, asked_already: bool) -> list[Reply]:
        drafts = [d for d in drafts if isinstance(d, dict)]
        if not drafts:
            return [Reply(
                "I couldn't find an event in that. Try something like "
                "<i>dinner with friends Friday 7pm</i>."
            )]

        ready: list[dict] = []
        incomplete: list[dict] = []
        skipped: list[str] = []
        for d in drafts:
            title = _clean(d.get("title"))
            day = parse_date(d.get("date"))
            if title and day:
                ready.append(d)
            elif not asked_already:
                incomplete.append(d)
            elif day:  # asked once already: never ask again
                ready.append({**d, "title": "Event"})
            else:
                skipped.append(title or "that event")

        replies: list[Reply] = []
        for d in ready:
            replies.append(await self._create_one(chat_id, d))
        for name in skipped:
            replies.append(Reply(
                f"I still couldn't tell the date for <b>{escape(name)}</b>, so I didn't add it. "
                "Send it again with a date and I'll put it on the calendar."
            ))
        if incomplete:
            question = self._question(incomplete)
            self.store.set_pending(chat_id, {"question": question, "drafts": incomplete})
            replies.append(Reply(escape(question) + "\n<i>(or /cancel)</i>"))
        return replies

    def _question(self, drafts: list[dict]) -> str:
        if len(drafts) == 1:
            d = drafts[0]
            title, day = _clean(d.get("title")), parse_date(d.get("date"))
            if not title and not day:
                return "What's the event, and what day is it?"
            if not day:
                return f"What day is {title}?"
            return f"What should I call the event on {fmt_date(day)}?"
        names = [(_clean(d.get("title")) or "the untitled one") for d in drafts]
        return "I need a bit more for " + ", ".join(names) + ": what are the titles and dates?"

    async def _create_one(self, chat_id: int, d: dict) -> Reply:
        day = parse_date(d["date"])
        start, end, notes = resolve_times(
            day, parse_date(d.get("end_date")), d.get("start_time"), d.get("end_time"),
            d.get("time_hint"), self.tz, self.default_minutes,
        )
        ev = Event(
            title=_clean(d.get("title")) or "Event",
            start=start, end=end,
            location=_clean(d.get("location")),
            notes=_clean(d.get("notes")),
        )
        if day < self.now().date():
            notes.append("heads up: that date is in the past")
        try:
            created = await self._cal(self.cal.create, ev)
        except Exception:
            log.exception("create failed")
            return Reply(f"⚠️ I couldn't save <b>{escape(ev.title)}</b> to the calendar. Please try again.")
        self.store.set_last_event(chat_id, created.id)
        token = self.store.add_undo(chat_id, {"op": "delete", "event_id": created.id, "title": created.title})
        return Reply(self._describe("✅ Added", created, notes), token)

    def _describe(self, verb: str, ev: Event, notes: list[str] | None = None) -> str:
        lines = [f"{verb}: <b>{escape(ev.title)}</b>", f"📅 {escape(fmt_when(ev))}"]
        if ev.location:
            lines.append(f"📍 {escape(ev.location)}")
        if ev.notes:
            lines.append(f"📝 {escape(ev.notes)}")
        for n in notes or []:
            lines.append(f"<i>ℹ️ {escape(n)}</i>")
        return "\n".join(lines)

    # ------------------------------------------------------------ edit/delete
    def _target(self, result: dict, known: dict[str, Event]) -> Event | None:
        tid = _clean(result.get("target_event_id"))
        return known.get(tid) if tid else None

    def _not_found(self, known: dict[str, Event]) -> Reply:
        today = self.now().date()
        upcoming = [e for e in known.values() if e.last_date() >= today][:6]
        text = "I couldn't tell which event you mean."
        if upcoming:
            text += " Coming up:\n" + "\n".join(
                f"• <b>{escape(e.title)}</b> — {escape(fmt_short(e))}" for e in upcoming
            )
            text += "\nTry naming it, e.g. <i>move the tennis to 8am</i>."
        return Reply(text)

    async def _edit(self, chat_id: int, result: dict, known: dict[str, Event]) -> Reply:
        ev = self._target(result, known)
        if not ev:
            return self._not_found(known)
        changes = result.get("changes") or {}
        updated, notes = apply_changes(ev, changes, self.tz, self.default_minutes)
        if updated == ev:
            return Reply(
                f"I didn't see what to change on <b>{escape(ev.title)}</b>. "
                "Try e.g. <i>move it to 8am</i> or <i>change the title to …</i>."
            )
        try:
            saved = await self._cal(self.cal.update, updated)
        except Exception:
            log.exception("update failed")
            return Reply(f"⚠️ I couldn't update <b>{escape(ev.title)}</b>. Please try again.")
        self.store.set_last_event(chat_id, saved.id)
        token = self.store.add_undo(chat_id, {"op": "revert", "event": ev.to_dict()})
        return Reply(self._describe("✏️ Updated", saved, notes), token)

    async def _delete(self, chat_id: int, result: dict, known: dict[str, Event]) -> Reply:
        ev = self._target(result, known)
        if not ev:
            return self._not_found(known)
        try:
            await self._cal(self.cal.delete, ev.id)
        except Exception:
            log.exception("delete failed")
            return Reply(f"⚠️ I couldn't delete <b>{escape(ev.title)}</b>. Please try again.")
        if self.store.get_last_event(chat_id) == ev.id:
            self.store.set_last_event(chat_id, None)
        token = self.store.add_undo(chat_id, {"op": "restore", "event": ev.to_dict()})
        return Reply(self._describe("🗑 Deleted", ev), token)

    # ------------------------------------------------------------------ undo
    async def undo(self, chat_id: int, token: str) -> str:
        taken = self.store.take_undo(token)
        if not taken or taken[0] != chat_id:
            return "That can't be undone any more."
        _, action = taken
        op = action["op"]
        try:
            if op == "delete":
                await self._cal(self.cal.delete, action["event_id"])
                if self.store.get_last_event(chat_id) == action["event_id"]:
                    self.store.set_last_event(chat_id, None)
                return f"↩️ Undone — removed <b>{escape(action.get('title', 'the event'))}</b>."
            if op == "revert":
                ev = Event.from_dict(action["event"])
                await self._cal(self.cal.update, ev)
                return self._describe("↩️ Undone — restored", ev)
            if op == "restore":
                ev = Event.from_dict(action["event"])
                created = await self._cal(self.cal.create, replace(ev, id=None))
                self.store.set_last_event(chat_id, created.id)
                return self._describe("↩️ Undone — back on the calendar", created)
        except Exception:
            log.exception("undo failed")
            return "⚠️ Undo failed. Please check the calendar."
        return "That can't be undone."

    # ----------------------------------------------------------------- usage
    def usage(self) -> Reply:
        now = self.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        periods = [
            ("Today", now.replace(hour=0, minute=0, second=0, microsecond=0)),
            (now.strftime("%B"), month_start),
            ("Last 30 days", now - timedelta(days=30)),
        ]
        lines = ["<b>AI usage</b> (estimated cost)"]
        for label, since in periods:
            rows = self.store.usage_since(since.timestamp())
            if not rows:
                lines.append(f"\n<b>{label}</b>: no calls")
                continue
            total = 0.0
            lines.append(f"\n<b>{label}</b>")
            for model, calls, tin, tout in rows:
                cost = estimate_cost(model, tin or 0, tout or 0)
                total += cost or 0
                price = f"${cost:.4f}" if cost is not None else "price n/a"
                lines.append(f"• {escape(model)}: {calls} msgs, {tin:,} in / {tout:,} out tokens, {price}")
            lines.append(f"  Total ≈ ${total:.4f}")
        lines.append("\n<i>Exact charges and remaining credits: ai.studio → Billing.</i>")
        return Reply("\n".join(lines))

    # -------------------------------------------------------------- upcoming
    async def upcoming(self, days: int = 60, limit: int = 10) -> Reply:
        now = self.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            events = await self._cal(self.cal.list_between, start, now + timedelta(days=days), limit)
        except Exception:
            log.exception("list failed")
            return Reply("⚠️ I couldn't read the calendar just now.")
        if not events:
            return Reply(f"Nothing on the calendar for the next {days} days.")
        lines = ["<b>Coming up</b>"]
        for e in events:
            line = f"• <b>{escape(e.title)}</b> — {escape(fmt_short(e))}"
            if e.location:
                line += f" · {escape(e.location)}"
            lines.append(line)
        return Reply("\n".join(lines))


def apply_changes(ev: Event, changes: dict, tz: ZoneInfo, default_minutes: int) -> tuple[Event, list[str]]:
    """Apply an edit from the model to an existing event, keeping what wasn't mentioned."""
    notes: list[str] = []
    new = replace(ev)
    if "title" in changes and _clean(changes.get("title")):
        new.title = _clean(changes["title"])
    for key, attr in (("location", "location"), ("notes", "notes")):
        if key in changes and changes[key] is not None:
            setattr(new, attr, _clean(changes[key]))  # "" clears the field

    new_day = parse_date(changes.get("date"))
    new_end_day = parse_date(changes.get("end_date"))
    st = parse_hhmm(changes.get("start_time"))
    et = parse_hhmm(changes.get("end_time"))
    hint = (_clean(changes.get("time_hint")) or "").lower() or None
    if st is None and hint in TIME_DEFAULTS:
        st = TIME_DEFAULTS[hint]
        notes.append(f"assumed {st.strftime('%-H:%M')} for {hint}")

    if not any([new_day, new_end_day, st, et]):
        return new, notes

    day = new_day or ev.start_date()
    if ev.all_day:
        if st is None and et is None:
            length = ev.end - ev.start
            new.start = day
            new.end = (new_end_day + timedelta(days=1)) if new_end_day else day + length
        else:
            new.start, new.end, more = resolve_times(
                day, None, st.strftime("%H:%M") if st else None,
                et.strftime("%H:%M") if et else None, None, tz, default_minutes,
            )
            notes += more
        return new, notes

    duration = ev.end - ev.start
    start_t = st or ev.start.timetz().replace(tzinfo=None)
    new.start = datetime.combine(day, start_t, tzinfo=tz)
    if et is not None:
        new.end = datetime.combine(new_end_day or day, et, tzinfo=tz)
        if new.end <= new.start:
            new.end += timedelta(days=1)
    else:
        new.end = new.start + duration
    return new, notes
