# Family Event Bot

Text the bot an event in plain words, for example *"tennis next Saturday morning 7 to 9:30"*, and it lands
in a separate **Family Events** calendar in your household Google account, never the primary one.

```
Telegram ──► bot container ──► Gemini API (paid tier) · or OpenRouter (zero-retention)
                  │
                  └──► Google Calendar "Family Events"    (or Radicale/CalDAV, temporary)
watchdog container ── alerts on Telegram if the bot stops responding
stt container ────── voice notes → text, locally (NVIDIA Parakeet, no audio leaves the Mac)
```

## What it does

| Message | Result |
|---|---|
| `tennis next Saturday morning 7 to 9:30 at Frisco Commons` | Creates the event and replies with the full date, time, location and an **↩️ Undo** button |
| `walk tomorrow morning` | 9:00–10:00, with the note *"assumed 9:00 for morning"* |
| `Diwali party Nov 8` | All-day event |
| `dentist appointment` | Asks **once**: *"What day is Dentist?"* |
| `move it to 8am`, `make it 30 minutes later`, `change the title to Doubles`, `add location Plano` | Edits the last or named event, with Undo |
| `delete the tennis event` | Deletes it, with Undo (restores it) |
| `/upcoming` · `what's on this week?` | Lists the next events |
| `/cancel`, `cancel`, `never mind`, `forget it`, `start over` | Clears any pending question and confirms |
| `/help` | Short help |

Rules:
- *Next &lt;weekday&gt;* said on that weekday means 7 days later.
- Vague times: morning 9:00, noon 12:00, afternoon 14:00, evening 18:00, night 20:00.
- No time means all-day. A missing end time means 1 hour.
- A message that looks like a new event always starts a new conversation.
- A pending question expires after 30 minutes.

The model only extracts fields. Date math and the rules above run in Python (`app/dates.py`), so
they behave the same every time.

## Voice notes
Hold the mic button in Telegram and speak the event the same way you'd type it. The bot:
1. transcribes it locally in the `stt` container (Parakeet TDT 0.6B v2, English, ONNX on CPU,
   about 0.2–0.5 s for a short note),
2. replies starting with what it heard (🎙 *"…"*),
3. handles it exactly like a typed message (create, edit, delete, cancel, Undo).

What happens to the audio:
- The bot downloads it into memory and never writes it to disk.
- Only the transcript goes to Gemini.
- The `stt` container has no host port and never connects to the internet (the model is built into the image).

Gemini is also told the text came from speech, so it fixes mis-heard place names from `LOCAL_PLACES`
(e.g. "Fresco Commons" → "Frisco Commons").

Limits and settings:
- Voice notes are capped at `VOICE_MAX_SECONDS` (default 120).
- Set `STT_URL=` empty to turn voice off.

Resources:
- RAM: about 1.4 GB idle and 2.2 GB peak on a 100-second note (capped at 3 GB).
- Disk: the image is 3.3 GB.

## Setup

### 1. Telegram
1. In @BotFather, **revoke and regenerate** the bot token if it has ever been pasted anywhere.
2. Get the numeric user ID of everyone who should use the bot from @userinfobot.

### 2. Language model: Gemini API (default)
1. At aistudio.google.com/apikey, choose **Create API key** in your Cloud project.
2. **Turn on billing for that project** (AI Studio → *Set up billing*). Only the paid tier keeps prompts
   out of Google's training. The bot's usage costs cents a month.
3. Check the key and model names: `docker compose run --rm bot python -m app.cli --list-models`

OpenRouter still works: set `LLM_PROVIDER=openrouter` and the `OPENROUTER_*` values.

### 3. Configure
```sh
cd ~/family-event-bot
cp .env.example .env      # fill in TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, GEMINI_API_KEY
chmod 600 .env
```

### 4a. Google Calendar via service account (recommended)
This method needs no consent screen, no publishing step and no 7-day expiry. The bot can reach **only** calendars
shared with the service account.
1. In Cloud Console, go to **IAM & Admin → Service Accounts → Create service account** (`family-event-bot`). Skip the roles.
2. Open it, go to **Keys → Add key → Create new key → JSON**, and download the file.
3. Enable the **Google Calendar API** in the same project.
4. In Google Calendar, signed in as the household account, create the calendar **Family Events** (Other calendars → + → Create new calendar).
   Then, in its settings:
   - Under **Share with specific people**, add the service account e-mail with **Make changes to events**.
   - Copy the **Calendar ID** from *Integrate calendar*.
5. Run:
   `.venv/bin/python scripts/setup_google_service_account.py --key ~/Downloads/<key>.json --calendar-id <id>@group.calendar.google.com`

### 4a′. Google Calendar via OAuth (alternative)
Do this once, in a browser, signed in as the **household Google account**:
1. Go to console.cloud.google.com, create a project (e.g. "family-event-bot"), and enable **Google Calendar API**.
2. Set up the OAuth consent screen:
   - User type: External; add the household account as a test user.
   - Then click **Publish app** ("In production"). Apps left in "Testing" have their tokens expire after 7 days.
   - Unverified is fine for personal use: you'll click *Advanced → Go to app* once.
3. Under Credentials, choose **Create OAuth client ID → Desktop app**, and download the JSON.
4. On the Mac Mini, run:
   ```sh
   .venv/bin/python scripts/authorize_google.py --client-secret ~/Downloads/client_secret_XXXX.json \
       --account household@gmail.com --share partner@gmail.com   # --share is optional
   ```
   The script:
   - checks that you signed in as the `--account` you named
   - creates **Family Events**
   - writes `data/google/token.json` and `data/google/calendar.json`

   The only calendar scope requested is `calendar.app.created`. The bot can manage just the calendars
   it created, so it cannot read or change the primary calendar. The code also refuses `primary`.
5. In `.env`, set `CALENDAR_BACKEND=google`.

On phones, the calendar shows up as its own layer that you can hide:
- **iPhone:** Settings → Calendar → Accounts → add the Google account, then enable "Family Events".
- **Google Calendar app:** it appears under the account automatically. If it was shared, accept the invite.

### 4b. Radicale (temporary / backup)
```sh
echo "family:$(openssl rand -hex 12)" > radicale/users && chmod 600 radicale/users
```
Then, in `.env`, set:
- `COMPOSE_PROFILES=radicale`
- `CALENDAR_BACKEND=caldav`
- `CALDAV_PASSWORD=` the password from `radicale/users`

Radicale listens on `127.0.0.1:5232` only.

### 5. Run
```sh
docker compose up -d --build
docker compose ps              # bot should become "healthy"
docker compose logs -f bot
```
The bot messages the admins "✅ Family Event Bot (re)started" on every start.

### Try it without Telegram
```sh
docker compose run --rm bot python -m app.cli          # real LLM, in-memory calendar
docker compose run --rm bot python -m app.cli --live   # real LLM, real calendar
```

## Reliability
- **Restarts:** `restart: unless-stopped` on every container.
- **Heartbeat:** the bot calls Telegram every 60 s and writes `data/heartbeat`. Docker's healthcheck fails
  when the heartbeat is older than 3 minutes. If Telegram has been unreachable for about 10 minutes, the
  bot exits so Docker restarts it.
- **Watchdog:** the `watchdog` container is separate and stdlib-only. It messages the admins
  if the heartbeat is older than 5 minutes, and again when the bot recovers.
- **Access:** messages from unapproved Telegram users are ignored and logged with their ID.

## Files
- `app/assistant.py` – conversation logic (create, clarify once, edit, delete, undo, cancel)
- `app/dates.py` – relative dates, default times, formatting
- `app/llm.py` – Gemini / OpenRouter clients and prompt
- `app/calendars/` – Google and CalDAV backends
- `app/store.py` – SQLite state (pending question, last event, undo log)
- `app/main.py` – Telegram handlers
- `app/watchdog.py` – down alerts
- `scripts/authorize_google.py` – one-time Google setup

Tests: `.venv/bin/python -m pytest -q tests`
