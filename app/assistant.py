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
    WEEKDAYS,
    date_reference,
    fmt_date,
    fmt_short,
    fmt_time,
    fmt_when,
    parse_date,
    parse_hhmm,
    resolve_times,
)
from .images import (
    IMAGE_MIME_TYPES,
    IMAGE_PROMPT,
    MAX_IMAGE_BYTES,
    REVISE_PROMPT,
    number_rows,
    prefix_for,
    review_items,
    with_prefix,
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
    "🎙 You can also send a voice note instead of typing.\n"
    "📷 Send a photo or PDF of a school calendar or flyer: I'll list the events and ask before adding them.\n\n"
    "Times: morning 9:00, noon 12:00, afternoon 14:00, evening 18:00, night 20:00. "
    "No time means all-day."
)


CREDITS_MSG = (
    "⚠️ The language model account is out of credits, so I can't read messages right now. "
    "Top up the Gemini API credits in AI Studio (ai.studio → Billing) and try again."
)
LLM_DOWN_MSG = "⚠️ I couldn't reach the language model just now. Please try again in a minute."
EXPIRED_MSG = "That list has expired or was already handled — send the picture again if needed."


@dataclass
class Reply:
    text: str  # Telegram HTML
    undo_token: str | None = None
    buttons: list[tuple[str, str]] | None = None  # (label, callback data)


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
        image_sources: list[tuple[str, str]] | None = None,
        child_class: str = "",
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
        self.image_sources = image_sources or []
        self.child_class = child_class

    def now(self) -> datetime:
        return self._now().astimezone(self.tz)

    async def _cal(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    # ------------------------------------------------------------------ entry
    def _lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(chat_id, asyncio.Lock())

    async def handle(self, chat_id: int, text: str, voice: bool = False) -> list[Reply]:
        async with self._lock(chat_id):
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

        proposal = self.store.get_proposal(chat_id)
        if proposal:
            replies = await self._revise(chat_id, text, *proposal)
            if replies is not None:
                return replies

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
            return [Reply(CREDITS_MSG)]
        except LLMError as e:
            log.error("LLM failure: %s", e)
            return [Reply(LLM_DOWN_MSG)]

        intent = str(result.get("intent") or "chat").lower()
        # Never log what was said: only its shape.
        log.info("interpretation: intent=%s events=%d", intent, len(result.get("events") or []))
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
        if self.store.clear_proposal(chat_id):
            self.store.clear_pending(chat_id)
            return Reply("Okay, nothing added from that picture. Starting fresh.")
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

    def _event_from_draft(self, d: dict) -> tuple[Event, list[str]]:
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
        return ev, notes

    async def _create_one(self, chat_id: int, d: dict) -> Reply:
        ev, notes = self._event_from_draft(d)
        day = ev.start_date()
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
            if op == "delete_many":
                for event_id in action["event_ids"]:
                    await self._cal(self.cal.delete, event_id)
                if self.store.get_last_event(chat_id) in action["event_ids"]:
                    self.store.set_last_event(chat_id, None)
                n = len(action["event_ids"])
                return f"↩️ Undone — removed the {n} event{'s' if n != 1 else ''} from the picture."
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

    # -------------------------------------------------------------- pictures
    async def handle_image(self, chat_id: int, data: bytes, mime: str, caption: str = "") -> list[Reply]:
        """Read events from a picture or PDF and propose them; nothing is added until confirmed."""
        if mime not in IMAGE_MIME_TYPES:
            return [Reply("I can read photos (JPG, PNG, HEIC) and PDFs. Please send it as one of those.")]
        if len(data) > MAX_IMAGE_BYTES:
            return [Reply("That file is too big for me (over 15 MB). Try a screenshot of the calendar instead.")]
        async with self._lock(chat_id):
            parts = ["DATE REFERENCE:", date_reference(self.now())]
            if self.child_class:
                parts.append(f"\nOUR CLASS: {self.child_class}")
            if caption.strip():
                parts.append(f"\nSENDER'S NOTE: {caption.strip()}")
            try:
                result = await self.llm.complete(IMAGE_PROMPT, "\n".join(parts), (data, mime), max_tokens=8192)
            except LLMBillingError:
                return [Reply(CREDITS_MSG)]
            except LLMError as e:
                log.error("LLM failure on picture: %s", e)
                return [Reply(LLM_DOWN_MSG)]
            items = [i for i in (result.get("items") or []) if isinstance(i, dict)]
            if not items:
                why = _clean(result.get("reply"))
                return [Reply(escape(why) if why else "I couldn't find any dated events in that picture.")]

            source = _clean(result.get("source"))
            prefix = prefix_for(source, self.image_sources)
            rows = review_items(items, self.now().date(), prefix, await self._existing(items))
            proposal = {"source": source, "period": _clean(result.get("period")), "prefix": prefix, "rows": rows}
            token = self.store.set_proposal(chat_id, proposal)
            log.info("picture: %d items read, %d proposed", len(rows), sum(r["include"] for r in rows))
            return [self._proposal_reply(token, proposal)]

    async def _existing(self, items: list[dict]) -> set[tuple[str, date]]:
        """(lower-case title, start date) of events already on the calendar around these dates."""
        days = [d for d in (parse_date(i.get("date")) for i in items) if d]
        if not days:
            return set()
        start = datetime.combine(min(days), datetime.min.time(), tzinfo=self.tz)
        end = datetime.combine(max(days) + timedelta(days=2), datetime.min.time(), tzinfo=self.tz)
        try:
            events = await self._cal(self.cal.list_between, start, end, 250)
        except Exception:
            log.exception("could not list events")
            return set()
        return {(e.title.lower(), e.start_date()) for e in events}

    def _row_line(self, r: dict) -> str:
        day = parse_date(r["date"])
        when = f"{WEEKDAYS[day.weekday()][:3]} {day.strftime('%b')} {day.day}"
        end_day = parse_date(r.get("end_date"))
        if end_day:
            when += f" – {WEEKDAYS[end_day.weekday()][:3]} {end_day.strftime('%b')} {end_day.day}"
        st, et = parse_hhmm(r.get("start_time")), parse_hhmm(r.get("end_time"))
        if st:
            when += ", " + fmt_time(st) + (f"–{fmt_time(et)}" if et else "")
        line = f"{r['n']}. {escape(when)} · <b>{escape(r['title'])}</b>"
        if r.get("location"):
            line += f" · {escape(r['location'])}"
        return line

    def _proposal_reply(self, token: str, proposal: dict) -> Reply:
        rows = proposal["rows"]
        add = [r for r in rows if r["include"]]
        skip = [r for r in rows if not r["include"]]
        head = escape(proposal.get("source") or "The picture")
        period = parse_date((proposal.get("period") or "") + "-01")
        if period:
            head += f" — {period.strftime('%B %Y')}"
        lines = [f"📷 <b>{head}</b>"]
        if add:
            lines.append("\nI'll add these:")
            for r in add:
                lines.append(self._row_line(r))
                if r.get("notes"):
                    lines.append(f"    📝 {escape(r['notes'])}")
                if r.get("warning"):
                    lines.append(f"    ⚠️ <i>{escape(r['warning'])}</i>")
        else:
            lines.append("\nNothing new to add from this one.")
        if skip:
            # Grouped by reason to keep it short; numbers still work for "add 12 too".
            lines.append("\n<i>Not adding:</i>")
            groups: dict[str, list[str]] = {}
            prefix = proposal.get("prefix")
            for r in skip:
                title = r["title"].removeprefix(f"{prefix} - ") if prefix else r["title"]
                groups.setdefault(r["reason"] or "skipped", []).append(f"{r['n']}. {title}")
            for reason, names in groups.items():
                lines.append(f"<i>• {escape(reason)}: {escape(', '.join(names))}</i>")
        lines.append(
            "\nReply to change it, e.g. <i>skip 2</i>, <i>add 9 too</i>, <i>make 3 at 4pm</i>."
            if add else "\nReply e.g. <i>add 2</i> to include something anyway."
        )
        buttons = [(f"✅ Add {len(add)}", f"img:add:{token}")] if add else []
        buttons.append(("❌ Cancel", f"img:no:{token}"))
        return Reply("\n".join(lines), buttons=buttons)

    async def proposal_button(self, chat_id: int, token: str, action: str) -> Reply:
        async with self._lock(chat_id):
            current = self.store.get_proposal(chat_id)
            if not current or current[0] != token:
                return Reply(EXPIRED_MSG)
            if action == "add":
                return await self._confirm(chat_id, current[1])
            self.store.clear_proposal(chat_id)
            return Reply("Okay, nothing added from that picture.")

    async def _confirm(self, chat_id: int, proposal: dict) -> Reply:
        self.store.clear_proposal(chat_id)
        rows = [r for r in proposal["rows"] if r["include"]]
        created: list[Event] = []
        failed: list[str] = []
        for r in rows:
            ev, _ = self._event_from_draft(r)
            try:
                created.append(await self._cal(self.cal.create, ev))
            except Exception:
                log.exception("create failed")
                failed.append(ev.title)
        if not created:
            return Reply("⚠️ I couldn't save those to the calendar. Please try again." if failed
                         else "Nothing to add.")
        self.store.set_last_event(chat_id, created[-1].id)
        token = self.store.add_undo(chat_id, {"op": "delete_many", "event_ids": [e.id for e in created]})
        lines = [f"✅ Added {len(created)} event{'s' if len(created) != 1 else ''}:"]
        lines += [f"• <b>{escape(e.title)}</b> — {escape(fmt_short(e))}" for e in created]
        if failed:
            lines.append("⚠️ Couldn't save: " + ", ".join(escape(t) for t in failed))
        return Reply("\n".join(lines), token)

    async def _revise(self, chat_id: int, text: str, token: str, proposal: dict) -> list[Reply] | None:
        """Apply a typed/spoken reply to the waiting picture list. None = not about the list."""
        fields = ("n", "title", "date", "end_date", "start_time", "end_time", "location", "notes",
                  "include", "reason")
        listing = [{k: r.get(k) for k in fields} for r in proposal["rows"]]
        context = "DATE REFERENCE:\n" + date_reference(self.now()) + "\n\nLIST:\n" + json.dumps(listing)
        try:
            result = await self.llm.complete(REVISE_PROMPT, f"{context}\n\nMESSAGE:\n{text}", max_tokens=8192)
        except LLMBillingError:
            return [Reply(CREDITS_MSG)]
        except LLMError as e:
            log.error("LLM failure on picture reply: %s", e)
            return [Reply(LLM_DOWN_MSG)]
        action = str(result.get("action") or "unrelated").lower()
        log.info("picture reply: action=%s", action)
        if action == "confirm":
            return [await self._confirm(chat_id, proposal)]
        if action == "cancel":
            self.store.clear_proposal(chat_id)
            return [Reply("Okay, nothing added from that picture.")]
        if action != "revise":
            return None

        warnings = {r["n"]: r.get("warning") for r in proposal["rows"]}
        rows = []
        for it in result.get("items") or []:
            if not isinstance(it, dict) or not _clean(it.get("title")) or not parse_date(it.get("date")):
                continue
            row = {k: _clean(it.get(k)) for k in fields if k not in ("n", "include")}
            row["title"] = with_prefix(row["title"], proposal.get("prefix"))
            row["include"] = bool(it.get("include"))
            row["reason"] = None if row["include"] else (row["reason"] or "skipped")
            row["warning"] = warnings.get(it.get("n"))
            rows.append(row)
        if not rows:
            return [Reply("I didn't follow that change. Try e.g. <i>skip 2</i> or <i>add 5 too</i>, "
                          "or tap ✅ / ❌ on the list.")]
        proposal = {**proposal, "rows": number_rows(rows)}
        return [self._proposal_reply(self.store.set_proposal(chat_id, proposal), proposal)]

    # --------------------------------------------------------------- storage
    def storage(self, docker_report: str | None = None) -> Reply:
        st = self.store.stats()
        lines = [
            "<b>Storage</b>",
            f"Bot database: {st['bytes'] / 1024:.0f} KB "
            f"({st['undo']} undo, {st['proposal']} picture drafts, {st['llm_usage']} usage rows)",
            "Voice notes, pictures and message text are never saved.",
        ]
        if docker_report:
            lines.append(f"\n<b>Docker (last cleanup)</b>\n<pre>{escape(docker_report.strip()[:1500])}</pre>")
        else:
            lines.append("\n<i>No Docker report yet — it appears after scripts/cleanup.sh first runs.</i>")
        return Reply("\n".join(lines))

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
