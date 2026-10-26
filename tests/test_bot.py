import asyncio
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.assistant import CANCEL_RE, Assistant, apply_changes
from app.calendars.base import CalendarBackend
from app.dates import date_reference, next_weekday, resolve_times, this_weekday
from app.llm import extract_json
from app.models import Event
from app.store import Store

TZ = ZoneInfo("America/Chicago")
SAT = date(2026, 10, 31)  # a Saturday


# ---------------------------------------------------------------- dates
def test_next_weekday_on_same_day_is_seven_days_later():
    assert next_weekday(SAT, 5) == date(2026, 11, 7)
    assert this_weekday(SAT, 5) == SAT


def test_next_weekday_other_day():
    wed = date(2026, 10, 28)
    assert next_weekday(wed, 5) == SAT


def test_date_reference_contains_rows():
    ref = date_reference(datetime(2026, 10, 31, 10, tzinfo=TZ))
    assert "next Saturday = 2026-11-07" in ref
    assert "tomorrow = 2026-11-01" in ref


@pytest.mark.parametrize("hint,hour", [("morning", 9), ("noon", 12), ("afternoon", 14), ("evening", 18), ("night", 20)])
def test_vague_time_defaults(hint, hour):
    s, e, notes = resolve_times(SAT, None, None, None, hint, TZ, 60)
    assert s.time() == time(hour, 0) and e - s == timedelta(hours=1)
    assert notes and hint in notes[0]


def test_no_time_is_all_day():
    s, e, notes = resolve_times(SAT, None, None, None, None, TZ, 60)
    assert s == SAT and e == SAT + timedelta(days=1) and notes == []


def test_range_crossing_midnight():
    s, e, _ = resolve_times(SAT, None, "22:00", "01:00", None, TZ, 60)
    assert e - s == timedelta(hours=3)


def test_cancel_phrases():
    for t in ["/cancel", "cancel", "Never mind", "nevermind!", "forget it", "start over", "Start over."]:
        assert CANCEL_RE.match(t), t
    for t in ["cancel the tennis event", "forget it, tennis is off on Saturday"]:
        assert not CANCEL_RE.match(t), t


def test_extract_json_with_fences():
    assert extract_json('```json\n{"intent": "help"}\n```') == {"intent": "help"}
    assert extract_json('Sure! {"intent": "list"} ok') == {"intent": "list"}


def test_apply_changes_time_keeps_duration():
    ev = Event("Tennis", datetime(2026, 11, 7, 7, 0, tzinfo=TZ), datetime(2026, 11, 7, 9, 30, tzinfo=TZ), id="x")
    new, _ = apply_changes(ev, {"start_time": "08:00"}, TZ, 60)
    assert new.start.hour == 8 and new.end - new.start == timedelta(hours=2, minutes=30)


def test_apply_changes_date_keeps_time():
    ev = Event("Tennis", datetime(2026, 11, 7, 7, 0, tzinfo=TZ), datetime(2026, 11, 7, 9, 30, tzinfo=TZ), id="x")
    new, _ = apply_changes(ev, {"date": "2026-11-08"}, TZ, 60)
    assert new.start == datetime(2026, 11, 8, 7, 0, tzinfo=TZ)


def test_apply_changes_all_day_to_timed():
    ev = Event("Party", SAT, SAT + timedelta(days=1), id="x")
    new, _ = apply_changes(ev, {"start_time": "18:00"}, TZ, 60)
    assert not new.all_day and new.start.hour == 18


# ------------------------------------------------------------ assistant
class FakeCalendar(CalendarBackend):
    def __init__(self):
        self.events: dict[str, Event] = {}
        self.n = 0

    def create(self, ev):
        self.n += 1
        ev = Event(**{**ev.__dict__, "id": f"ev{self.n}"})
        self.events[ev.id] = ev
        return ev

    def update(self, ev):
        self.events[ev.id] = ev
        return ev

    def delete(self, event_id):
        self.events.pop(event_id, None)

    def get(self, event_id):
        return self.events.get(event_id)

    def list_between(self, start, end, limit=50):
        return list(self.events.values())[:limit]


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.contexts = []

    async def interpret(self, context, message):
        self.contexts.append(context)
        return self.responses.pop(0)


def make(tmp_path, *responses):
    cal = FakeCalendar()
    llm = FakeLLM(*responses)
    a = Assistant(llm, cal, Store(tmp_path / "s.db"), TZ, now=lambda: datetime(2026, 10, 31, 10, tzinfo=TZ))
    return a, cal, llm


def run(coro):
    return asyncio.run(coro)


def test_create_with_undo(tmp_path):
    a, cal, _ = make(tmp_path, {"intent": "create", "events": [
        {"title": "Tennis", "date": "2026-11-07", "start_time": "07:00", "end_time": "09:30", "location": "Frisco Commons"}
    ]})
    [r] = run(a.handle(1, "tennis next Saturday morning 7 to 9:30 at Frisco Commons"))
    assert "Saturday, November 7, 2026, 7:00 AM – 9:30 AM" in r.text and "Frisco Commons" in r.text
    assert r.undo_token and len(cal.events) == 1
    msg = run(a.undo(1, r.undo_token))
    assert "Undone" in msg and not cal.events
    assert "can't be undone" in run(a.undo(1, r.undo_token))


def test_default_note_shown(tmp_path):
    a, _, _ = make(tmp_path, {"intent": "create", "events": [
        {"title": "Walk", "date": "2026-11-01", "time_hint": "morning"}]})
    [r] = run(a.handle(1, "walk tomorrow morning"))
    assert "assumed 9:00 for morning" in r.text


def test_clarify_once_then_create(tmp_path):
    a, cal, llm = make(
        tmp_path,
        {"intent": "create", "events": [{"title": "Dentist", "date": None}]},
        {"intent": "create", "continues_pending": True, "events": [{"title": "Dentist", "date": "2026-11-12", "start_time": "15:15"}]},
    )
    [q] = run(a.handle(1, "dentist appointment"))
    assert "What day is Dentist?" in q.text and not cal.events
    [r] = run(a.handle(1, "nov 12 at 3:15"))
    assert "Added" in r.text and len(cal.events) == 1
    assert "PENDING: the bot asked" in llm.contexts[1]


def test_never_asks_twice(tmp_path):
    a, cal, _ = make(
        tmp_path,
        {"intent": "create", "events": [{"title": "Dentist", "date": None}]},
        {"intent": "create", "continues_pending": True, "events": [{"title": "Dentist", "date": None}]},
    )
    run(a.handle(1, "dentist"))
    [r] = run(a.handle(1, "not sure yet"))
    assert "didn't add it" in r.text and not cal.events
    assert a.store.get_pending(1) is None


def test_cancel_clears_pending(tmp_path):
    a, _, _ = make(tmp_path, {"intent": "create", "events": [{"title": None, "date": "2026-11-02"}]})
    run(a.handle(1, "something on monday"))
    [r] = run(a.handle(1, "never mind"))
    assert "cancelled" in r.text and a.store.get_pending(1) is None


def test_fresh_event_replaces_pending(tmp_path):
    a, cal, _ = make(
        tmp_path,
        {"intent": "create", "events": [{"title": "Dentist", "date": None}]},
        {"intent": "create", "continues_pending": False, "events": [{"title": "Swim class", "date": "2026-11-03", "start_time": "17:00"}]},
    )
    run(a.handle(1, "dentist"))
    [r] = run(a.handle(1, "swim class tuesday 5pm"))
    assert "Swim class" in r.text and a.store.get_pending(1) is None


def test_edit_it_and_delete(tmp_path):
    a, cal, llm = make(
        tmp_path,
        {"intent": "create", "events": [{"title": "Tennis", "date": "2026-11-07", "start_time": "07:00", "end_time": "09:30"}]},
        {"intent": "edit", "target_event_id": "ev1", "changes": {"start_time": "08:00"}},
        {"intent": "delete", "target_event_id": "ev1"},
    )
    run(a.handle(1, "tennis next saturday 7-9:30am"))
    [r] = run(a.handle(1, "move it to 8am"))
    assert "8:00 AM – 10:30 AM" in r.text
    assert "LAST EVENT: ev1 | Tennis" in llm.contexts[1]
    [d] = run(a.handle(1, "delete the tennis event"))
    assert "Deleted" in d.text and not cal.events
    back = run(a.undo(1, d.undo_token))
    assert "back on the calendar" in back and len(cal.events) == 1


def test_edit_unknown_target(tmp_path):
    a, _, _ = make(tmp_path, {"intent": "edit", "target_event_id": "nope", "changes": {"title": "X"}})
    [r] = run(a.handle(1, "rename the gala"))
    assert "couldn't tell which event" in r.text


def test_undo_other_chat_rejected(tmp_path):
    a, cal, _ = make(tmp_path, {"intent": "create", "events": [{"title": "A", "date": "2026-11-07"}]})
    [r] = run(a.handle(1, "A next saturday"))
    assert "can't be undone" in run(a.undo(2, r.undo_token)) and cal.events


# ------------------------------------------------------- google / llm shape
def test_google_roundtrip_and_primary_guard(tmp_path):
    from app.calendars.google_cal import GoogleCalendar, load_calendar_id

    with pytest.raises(RuntimeError):
        load_calendar_id(tmp_path / "token.json", "primary")
    with pytest.raises(RuntimeError):
        load_calendar_id(tmp_path / "token.json", "someone@gmail.com")
    g = GoogleCalendar.__new__(GoogleCalendar)
    g.tz = TZ
    ev = Event("Tennis", datetime(2026, 11, 7, 7, tzinfo=TZ), datetime(2026, 11, 7, 9, 30, tzinfo=TZ), "Park")
    body = g._to_body(ev)
    assert body["start"] == {"dateTime": "2026-11-07T07:00:00-06:00", "timeZone": "America/Chicago"}
    back = g._from_body({**body, "id": "abc", "start": {"dateTime": "2026-11-07T13:00:00Z"},
                         "end": {"dateTime": "2026-11-07T15:30:00Z"}})
    assert back.start == ev.start and back.location == "Park"
    allday = g._to_body(Event("Diwali", SAT, SAT + timedelta(days=1)))
    assert allday["start"] == {"date": "2026-10-31"} and allday["end"] == {"date": "2026-11-01"}


def test_openrouter_payload_privacy():
    from app.llm import OpenRouterLLM

    p = OpenRouterLLM("k", ["anthropic/claude-haiku-5.5", "anthropic/claude-sonnet-5.5"])._payload("ctx", "msg")
    assert p["provider"]["data_collection"] == "deny" and p["provider"]["zdr"] is True
    assert p["models"][1] == "anthropic/claude-sonnet-5.5"


def test_config_rejects_non_major_lab(monkeypatch):
    from app.config import Config, ConfigError

    for k, v in {"TELEGRAM_BOT_TOKEN": "t", "ALLOWED_USER_IDS": "1,2", "OPENROUTER_API_KEY": "k",
                 "LLM_PROVIDER": "openrouter", "OPENROUTER_MODELS": "meta-llama/llama-4"}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ConfigError):
        Config.from_env()


def test_gemini_payload_and_parsing(monkeypatch):
    import httpx
    from app.llm import GeminiLLM

    calls = []

    def handler(request):
        calls.append(request)
        if "gemini-missing" in str(request.url):
            return httpx.Response(404, json={"error": {"message": "not found"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "thinking...", "thought": True}, {"text": '{"intent": "list"}'}]}}]})

    llm = GeminiLLM("key", ["gemini-missing", "gemini-ok"])
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"x-goog-api-key": "key"})
    assert asyncio.run(llm.interpret("ctx", "what's coming up")) == {"intent": "list"}
    assert len(calls) == 2 and calls[-1].headers["x-goog-api-key"] == "key"
    body = llm._payload("ctx", "m")
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_config_gemini_default(monkeypatch):
    from app.config import Config

    for k, v in {"TELEGRAM_BOT_TOKEN": "t", "ALLOWED_USER_IDS": "1", "GEMINI_API_KEY": "g",
                 "CALENDAR_BACKEND": "google"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    cfg = Config.from_env()
    assert cfg.llm_provider == "gemini" and cfg.llm_api_key == "g"


def test_gemini_billing_error_stops_immediately(tmp_path):
    import httpx
    from app.llm import GeminiLLM

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(402, json={"error": {"message": "Your prepayment credits are depleted."}})

    llm = GeminiLLM("key", ["gemini-a", "gemini-b"])
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    a = Assistant(llm, FakeCalendar(), Store(tmp_path / "s.db"), TZ)
    [r] = asyncio.run(a.handle(1, "tennis saturday"))
    assert "out of credits" in r.text and len(calls) == 1


def test_usage_recorded_and_reported(tmp_path):
    import httpx
    from app.llm import GeminiLLM

    def handler(request):
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": '{"intent": "list"}'}]}}],
            "usageMetadata": {"promptTokenCount": 2000, "candidatesTokenCount": 50, "thoughtsTokenCount": 150},
        })

    store = Store(tmp_path / "s.db")
    llm = GeminiLLM("key", ["gemini-3.8-flash"])
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm.on_usage = store.add_usage
    a = Assistant(llm, FakeCalendar(), store, TZ)
    asyncio.run(a.handle(1, "what's coming up"))
    text = a.usage().text
    assert "gemini-3.8-flash: 1 msgs, 2,000 in / 200 out tokens" in text and "$0.0025" in text


# ------------------------------------------------------------------ voice
class FakeSTT:
    def __init__(self, text=None, error=False):
        self.text, self.error, self.calls = text, error, 0

    async def transcribe(self, audio):
        from app.stt import STTError

        self.calls += 1
        if self.error:
            raise STTError("down")
        return self.text


def make_voice(tmp_path, stt, *responses):
    a, cal, llm = make(tmp_path, *responses)
    a.stt, a.local_places = stt, "Frisco, Prosper"
    return a, cal, llm


def test_voice_creates_event_with_transcript(tmp_path):
    stt = FakeSTT("Tennis next Saturday morning 7-9.30 at Fresco Commons.")
    a, cal, llm = make_voice(tmp_path, stt, {"intent": "create", "events": [
        {"title": "Tennis", "date": "2026-11-07", "start_time": "07:00", "end_time": "09:30", "location": "Frisco Commons"}]})
    [r] = run(a.handle_voice(1, b"ogg", 4))
    assert r.text.startswith("🎙 <i>“Tennis next Saturday morning 7-9.30 at Fresco Commons.”</i>")
    assert "Added" in r.text and r.undo_token and len(cal.events) == 1
    assert "speech-to-text transcript" in llm.contexts[0] and "Frisco, Prosper" in llm.contexts[0]


def test_voice_errors_never_touch_calendar(tmp_path):
    for stt, seconds, expected in [
        (FakeSTT(error=True), 4, "couldn't transcribe"),
        (FakeSTT(""), 4, "couldn't make that out"),
        (FakeSTT("x"), 500, "keep voice notes short"),
    ]:
        a, cal, _ = make_voice(tmp_path, stt)
        [r] = run(a.handle_voice(1, b"ogg", seconds))
        assert expected in r.text and not cal.events


def test_voice_never_mind_clears_pending(tmp_path):
    a, _, _ = make_voice(tmp_path, FakeSTT("Never mind."),
                         {"intent": "create", "events": [{"title": "Dentist", "date": None}]})
    run(a.handle(1, "dentist"))
    [r] = run(a.handle_voice(1, b"ogg", 1))
    assert "cancelled" in r.text and a.store.get_pending(1) is None


def test_typed_message_has_no_voice_note(tmp_path):
    a, _, llm = make(tmp_path, {"intent": "list"})
    run(a.handle(1, "what's coming up"))
    assert "speech-to-text" not in llm.contexts[0]
