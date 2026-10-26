"""Try the bot from the terminal, without Telegram.

    docker compose run --rm bot python -m app.cli          # in-memory calendar (dry run)
    docker compose run --rm bot python -m app.cli --live   # the configured real calendar

    docker compose run --rm bot python -m app.cli --list-models  # check the Gemini key / model names

Uses the LLM_PROVIDER / GEMINI_* settings from .env. Type 'undo' to undo the last action, Ctrl-D to quit.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from .assistant import Assistant
from .calendars.base import CalendarBackend
from .config import llm_settings
from .llm import GeminiLLM, make_llm
from .models import Event
from .store import Store


class MemoryCalendar(CalendarBackend):
    name = "memory"

    def __init__(self):
        self.events: dict[str, Event] = {}

    def create(self, ev):
        ev = Event(**{**ev.__dict__, "id": f"mem{len(self.events) + 1}"})
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
        return sorted(self.events.values(), key=lambda e: e.start_date())[:limit]


async def repl(live: bool, list_models: bool = False) -> None:
    tz = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))
    provider, api_key, models = llm_settings()
    llm = make_llm(provider, api_key, models,
                   zdr=os.environ.get("OPENROUTER_ZDR", "true").lower() != "false")
    if list_models:
        if not isinstance(llm, GeminiLLM):
            raise SystemExit("--list-models only works with LLM_PROVIDER=gemini")
        names = await llm.list_models()
        print("Gemini models your key can use:\n  " + "\n  ".join(sorted(names)))
        print(f"\nConfigured GEMINI_MODELS: {', '.join(models)}")
        missing = [m for m in models if m not in names]
        if missing:
            print(f"!! Not available: {', '.join(missing)} - pick from the list above.")
        await llm.aclose()
        return
    if live:
        from .calendars import make_backend
        from .config import Config

        cal = make_backend(Config.from_env())
    else:
        cal = MemoryCalendar()
    store = Store(Path(tempfile.mkdtemp()) / "cli.sqlite3")
    bot = Assistant(llm, cal, store, tz)
    last_token = None
    print(f"Family Event Bot CLI — calendar: {cal.name}, model: {models[0]}. Ctrl-D to quit.")
    while True:
        try:
            text = await asyncio.to_thread(input, "\nyou> ")
        except EOFError:
            break
        if text.strip().lower() == "undo" and last_token:
            print("bot>", strip(await bot.undo(0, last_token)))
            last_token = None
            continue
        if text.strip() == "/upcoming":
            replies = [await bot.upcoming()]
        else:
            replies = await bot.handle(0, text)
        for r in replies:
            print("bot>", strip(r.text) + ("   [Undo]" if r.undo_token else ""))
            last_token = r.undo_token or last_token
    await llm.aclose()


def strip(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).replace("&amp;", "&").replace("&#x27;", "'").replace("\n", "\n     ")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="use the configured real calendar")
    p.add_argument("--list-models", action="store_true", help="show Gemini models this key can use")
    args = p.parse_args()
    asyncio.run(repl(args.live, args.list_models))
