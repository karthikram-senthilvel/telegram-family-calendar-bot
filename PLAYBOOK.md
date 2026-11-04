# Bot Playbook: lessons and reusable parts from the Family Event Bot

> **Purpose:** everything learned building this family calendar bot, so the next bot (e.g. a planned events-discovery
> agent) reuses the pieces and the setup steps instead of rediscovering them.
> **Project:** `~/family-event-bot` · **Built:** Oct 2026 · **Host:** Mac Mini M4, 16 GB, Docker Desktop

---

## 1. What was built (one paragraph)

A private Telegram bot. Family members send plain-language text **or voice notes**
("tennis next Saturday morning 7 to 9:30"), and it creates, edits or deletes events in a dedicated
Google Calendar (a secondary **Family Events** calendar in the household Google account, never the primary calendar). It replies
with the full date, time and location plus an **Undo** button.

How it is built:
- **Language understanding:** Gemini (paid tier), one JSON-mode call per message.
- **Rules:** all date and time rules are plain Python.
- **Voice:** transcribed locally with NVIDIA Parakeet in its own container.
- **Containers:** the bot, speech-to-text, and a watchdog, all with automatic restart, heartbeats
  and Telegram down-alerts.
- **Size:** about 2,600 lines including 35 tests.

---

## 2. Architecture at a glance

```
Telegram (text / voice)
   │  long polling (no open ports, no public URL)
   ▼
┌ bot container ─────────────────────────────────────────────┐
│ main.py       Telegram handlers, access filter, commands   │
│ assistant.py  conversation brain (Telegram-agnostic)       │──► Gemini API (paid tier)
│ dates.py      deterministic date/time rules                │──► Google Calendar (service acct)
│ store.py      SQLite: pending Q, last event, undo, usage   │
│ stt.py ──────────► stt container (Parakeet ONNX, offline)  │
└───────┬────────────────────────────────────────────────────┘
        │ writes data/heartbeat every 60 s
        ▼
  watchdog container ─► Telegram alert if heartbeat > 5 min old
```

**Core design rule:** *the LLM extracts, the code decides.* Gemini only turns language into
structured JSON (intent and fields). Routing, date math, defaults, validation, calendar writes and
undo are deterministic Python. That makes it reliable, testable, cheap (~$0.002 per message)
and fast (~1–2 s).

---

## 3. Reusable building blocks

Copy these into a new project. Most need little or no change.

| File | What it gives you | Reuse as-is? | What to change |
|---|---|---|---|
| `app/llm.py` | Gemini client and OpenRouter client behind one `interpret(context, message) -> dict`, plus a generic `complete(system, text, attachment)` that also sends an image or PDF. Model fallback list, JSON parsing (`extract_json`), billing-error detection (`LLMBillingError`), token and cost tracking (`on_usage`, `PRICES_PER_M`) | ✅ client code | Replace `SYSTEM_PROMPT` with your task's prompt and schema |
| `app/config.py` | Env-var config with validation: required vars, allowed models (major labs only), provider switch | ✅ pattern | Add or remove fields |
| `app/store.py` | Thread-safe SQLite store: per-chat pending state with expiry, undo tokens (one-time, 7-day expiry), a "proposal awaiting confirmation" slot, LLM usage log, `cleanup()` + `stats()` | ✅ | Add tables as needed (and add them to `cleanup()`) |
| `app/images.py` | Picture → events: extraction prompt with categories, a revise prompt for "skip 3 / add 5", and pure-Python rules (skip categories, prefix by source, past/duplicate/weekday checks) | 🔁 pattern | Categories and rules for your domain |
| `scripts/cleanup.sh` + `install_cleanup_job.sh` | Monthly host-side Docker prune (dangling images, old build cache) via launchd, report shown in `/storage` | ✅ unchanged | Label name |
| `app/main.py` | Telegram application skeleton: `allowed` user filter, command handlers, text and voice handlers, Undo button (`CallbackQueryHandler`), stranger logging, error handler, heartbeat task, startup notification, `set_my_commands` menu | ✅ skeleton | Swap the handlers for your commands |
| `app/watchdog.py` | Standalone stdlib watchdog that alerts on Telegram when the heartbeat is stale and again on recovery | ✅ unchanged | Message text |
| `app/healthcheck.py` | Docker healthcheck based on the heartbeat file age | ✅ unchanged | — |
| `app/stt.py` + `stt/` | Local speech-to-text service (Parakeet TDT 0.6B v2, int8 ONNX, CPU) with an HTTP client | ✅ unchanged | Model, if you need another language (see §7) |
| `app/calendars/` | `CalendarBackend` interface with Google (service account or OAuth) and CalDAV/Radicale backends | ✅ for any calendar project | — |
| `app/dates.py` | `date_reference()` lookup table for prompts, `next_weekday`, vague-time defaults, `resolve_times`, formatting | ✅ for anything date-related | Defaults |
| `app/cli.py` | Terminal REPL to test the brain with the real LLM and an in-memory backend; `--list-models` | ✅ pattern | — |
| `app/assistant.py` | Conversation patterns: cancel regex, clarify-once, fresh-message-resets, per-chat locks, Reply objects with undo tokens | 🔁 patterns | Domain logic is calendar-specific |
| `scripts/setup_google_service_account.py` | Validates a service account key and calendar access with a create-and-delete probe, then stores the key `chmod 600` | ✅ | Probe call for other Google APIs |
| `Dockerfile`, `docker-compose.yml` | Non-root Python image, `restart: unless-stopped`, healthchecks, memory limits, log rotation, a watchdog that reuses the image (`pull_policy: never`), an optional service via `profiles` | ✅ | Names |
| `tests/test_bot.py` | Testing patterns: `FakeLLM` (scripted JSON replies), `FakeCalendar`, `FakeSTT`, `httpx.MockTransport` for API clients, a fixed `now` | ✅ patterns | Your cases |

---

## 4. Patterns worth repeating

### 4.1 LLM usage
- **Structured output only.** Ask for one JSON object with an explicit schema in the system prompt.
  Use `responseMimeType: application/json` for Gemini, or `response_format: json_object` for
  OpenRouter. Parse defensively with `extract_json`, which strips code fences and finds the `{...}`.
- **Give the model lookup tables, not math.** `date_reference()` lists "next Saturday = 2026-10-31",
  so the model copies values and never computes them.
- **Put the world state in the context:** known items with IDs, the "last item" (so "it" resolves),
  any pending question and draft. Then **validate whatever the model returns.** For example, an
  event ID must exist in the known set, or the bot replies "couldn't tell which".
- **`temperature: 0`**, a fallback model list, and two attempts per model.
  - A 402 billing error is final: fail fast, with a clear user message.
  - A 404 (model not found) moves on to the next model.
- **Track usage on every call** (`on_usage` callback → SQLite → `/usage` command).
- **Allowlist model vendors in config** (privacy requirement: major labs only).

### 4.2 Conversation handling
- **Cancel shortcut before the LLM.** A regex that matches only when the *whole* message is a cancel
  phrase, so "cancel the tennis event" still becomes a delete.
- **Ask at most once.** Clear the pending state on *every* LLM-handled message, and re-arm it only
  for a brand-new incomplete request. (Bug found by tests: the old code forgot to clear pending
  after the follow-up answer.)
- **Fresh message = fresh conversation.** The LLM returns `continues_pending: true/false`.
- **Pending state expires** (30 min).
- **Per-chat `asyncio.Lock`**, so rapid messages in one chat don't interleave.
- **Every change returns an undo token.** Store the inverse action (`delete` / `revert` to the
  old copy / `restore` the deleted copy). One-time use, 7-day expiry, checked against the chat.
- **Confirm before bulk or uncertain writes.** Pictures produce a numbered proposal stored in SQLite
  (24 h expiry, a token in the button data so old buttons can't fire). Buttons confirm or cancel;
  a free-text or voice reply goes to a small "revise" prompt that returns the full updated list.
  One Undo covers the whole batch (`delete_many`).
- **Let the model classify, let code decide.** The picture prompt labels each item
  (`public_holiday`, `theme`…); Python owns the skip list, the title prefix and the sanity checks
  (e.g. the printed weekday must match the date), so wording changes in the prompt can't break the rules.
- **Reply objects** (`Reply(text_html, undo_token, buttons)`) keep the brain independent of Telegram, so the
  same logic runs in Telegram, the CLI and tests.

### 4.3 Telegram
- **Long polling** (`run_polling`): no inbound ports, works behind home NAT.
- **Access control:** a `filters.User(user_id=...)` filter on every handler, plus a last-resort
  `~allowed` handler that logs strangers' IDs and stays silent. Button taps (callback queries) need
  their own user-ID check.
- **Handlers and the command menu are separate things.** `CommandHandler` makes a command work;
  `bot.set_my_commands([...])` in `post_init` makes it appear in the ☰ menu and `/` autocomplete.
- **HTML parse mode, and `html.escape()` all user and model text.**
- **Voice:** check `voice.duration` *before* downloading; `get_file()` → `download_as_bytearray()`
  (in memory only).
- **`concurrent_updates(True)`** plus per-chat locks.

### 4.4 Reliability
- `restart: unless-stopped` on every container.
- **Heartbeat:** every 60 s, call Telegram `get_me()` and write `data/heartbeat`. The Docker
  healthcheck fails when it's older than 3 minutes. After 10 failures in a row the bot calls
  `os._exit(1)` so Docker restarts it.
- **Separate watchdog container** (stdlib only). A hung bot can't alert about itself.
- **Startup message to admins** (`NOTIFY_ON_START`) works as a free crash and restart detector.
- **Fail fast at startup:** `post_init` checks the calendar is reachable.
- **Degrade gracefully:** if the speech service is down, voice notes get a polite error and text
  still works.

### 4.5 Secrets and privacy
- **Secrets only in `.env` (chmod 600) and `data/`.** Both are git- and docker-ignored. Never paste
  tokens in chat; edit with `nano .env`.
- **Check whether a secret is set without printing it:** `echo "${#VAR} chars"`.
- **Narrowest access possible.** A service account sees only calendars explicitly shared with it,
  plus a code guard that refuses `primary` and any ID that isn't `@group.calendar.google.com`.
- **The Gemini *paid* tier doesn't train on prompts; the free tier may.** Enable billing.
- **Keep audio local** (the `stt` container has no host port and no internet at runtime). Never log
  transcripts, message text or model output; log durations, intents and counts (a test asserts this).

### 4.6 Retention (don't let the Mac fill up)
- **Don't store inputs at all:** audio, pictures and text live in memory only.
- **Every table has an expiry** and the bot runs `store.cleanup()` at startup and every 24 h
  (delete expired rows, `VACUUM`).
- **Docker logs:** `json-file` with `max-size: 5m`, `max-file: 3` on every service.
- **Docker itself is the real disk hog** (rebuilds leave GBs of layers and build cache): a monthly
  launchd job runs `scripts/cleanup.sh` on the host. Never mount the Docker socket into a container
  for this.
- **`/storage`** shows the database size and the last cleanup report, so you can check from Telegram.

---

## 5. Setup recipes that worked (accounts and consoles)

### 5.1 Telegram bot
1. @BotFather → `/newbot` (or `/revoke` to rotate the token if it was ever pasted anywhere).
2. Your numeric user ID: message **@userinfobot**.
3. Put them in `.env`: `TELEGRAM_BOT_TOKEN=…`, `ALLOWED_USER_IDS=id1,id2`.

Use **one bot per project**, so an outage or access change in one can't affect another.

### 5.2 Gemini API
1. aistudio.google.com/apikey → **Create API key** in your Cloud project → `GEMINI_API_KEY=` in `.env`.
2. **Add prepaid credits** in AI Studio → Billing. The $300 Google Cloud free-trial credit does
   **not** cover the Gemini API. Without credits you get `HTTP 402 "prepayment credits are depleted"`.
3. Check the model names: `docker compose run --rm bot python -m app.cli --list-models`.

As of Oct 2026, the bot uses `gemini-3.8-flash` with `gemini-3.5-flash` as fallback, at about
$0.002 per message.

### 5.3 Google Calendar: use a service account (not OAuth)
Why not OAuth: an External app in "Testing" has its tokens expire after **7 days**, and "In
production" requires homepage and privacy-policy URLs on a domain you own.

1. Cloud Console → enable **Google Calendar API**.
2. **IAM & Admin → Service Accounts → Create** (no roles) → **Keys → Add key → JSON**.
   - ⚠️ If you get *"Service account key creation is disabled"* (`iam.disableServiceAccountKeyCreation`,
     enforced by default on new organizations), there are two ways around it:
     - **Option A:** at the organization level, grant yourself **Organization Policy Administrator**.
       Then, on this project, go to **Organization Policies** → that constraint → **Override
       parent's policy → Enforcement Off**. Create the key, then set it back to **Inherit**;
       existing keys keep working.
     - **Option B:** a new project with **Location: No organization**.
3. calendar.google.com (as the calendar owner) → **Create new calendar**. Under **Share with
   specific people**, add the service account e-mail with **Make changes to events**. Copy the
   **Calendar ID** from *Integrate calendar*.
4. Run `.venv/bin/python scripts/setup_google_service_account.py --key <json> --calendar-id <id>`.
   It probes by creating and deleting a test event, then stores the key `chmod 600`. Delete the
   downloaded key afterwards.

The same approach works for other Google APIs (Sheets, Drive): share the specific file or folder
with the service account e-mail.

### 5.4 Local speech-to-text (Parakeet)
- `onnx-asr[cpu,hub]==0.12.0`, `onnxruntime==1.30.0`, model `nemo-parakeet-tdt-0.6b-v2`, `int8`.
- The model is downloaded **at image build time**, then `HF_HUB_OFFLINE=1`.
- ffmpeg decodes Telegram OGG/Opus from stdin to 16 kHz mono s16le on stdout, with no temp files.
- **Measured on the M4 (CPU inside Docker):**
  - load: 1.6 s
  - short note: 0.2–0.5 s
  - 100 s note: 6 s
  - RAM: 1.4 GB idle, 2.2 GB peak (capped at 3 GB)
  - image: 3.3 GB
- **English only.** For Tamil or mixed languages, use faster-whisper (large-v3-turbo) or send the
  audio straight to Gemini (it accepts `audio/ogg`).

---

## 6. Gotchas already solved (don't rediscover these)

| Symptom | Cause | Fix |
|---|---|---|
| CalDAV events listed 6 h off (7:00 AM shown as 1:00 AM) | `TZID=America/Chicago` written without a `VTIMEZONE` block; Radicale's expand step treats the time as UTC | Store timed events in **UTC** (`caldav_cal.py`) |
| Clarifying question asked again | Pending state not cleared after the follow-up answer | Always clear pending after an LLM call; re-arm only for new requests |
| Gemini `HTTP 402` retried 4×, log flooded, "try again in a minute" | Treated as a transient error | `LLMBillingError`: fail fast, tell the user to top up |
| Thinking models return empty or cut-off JSON | Thinking tokens count against `maxOutputTokens`; thought parts mixed into the output | `maxOutputTokens: 4096`, skip parts with `"thought": true` |
| Voice: a local town name mis-heard (e.g. "Frisco" → "Fresco") | Proper nouns are hard for speech recognition | Tell the LLM the text is a transcript and pass `LOCAL_PLACES`; it corrects the name |
| Watchdog container tries to pull `family-event-bot:latest` from a registry | It uses the bot's image but has no `build:` | `pull_policy: never` + `depends_on: [bot]` |
| `docker compose run … python /clips/x.py` → `No module named 'app'` | Script outside `/srv` | `-e PYTHONPATH=/srv` |
| `docker compose config` fails | `env_file: .env` missing | `cp .env.example .env` first |
| OAuth "Make external → In production" blocked | Needs homepage and privacy URLs | Use a service account (§5.3) |
| Picture read takes ~30 s; Telegram's "typing…" disappears after 5 s | Thinking model on a dense image | Send "📷 Reading the picture…" first, then the result |
| Picture titles like "LA - LAA Closed" | Model leaves the publisher's own abbreviation in | `with_prefix()` strips a leading all-caps word starting with the prefix |
| Watchdog kept running an old untagged image after a rebuild | Compose didn't recreate it | `docker compose up -d --build --force-recreate bot watchdog` |
| Old local Ollama 7B looped asking pointless questions | Weak model plus free-form chat | Stronger hosted model + JSON schema + ask-once rule in code |

---

## 7. Recipe: start a new bot in about an hour

1. **Copy the skeleton:**
   ```sh
   cp -R ~/family-event-bot ~/new-bot && cd ~/new-bot
   rm -rf data .env radicale .pytest_cache && mkdir data
   /opt/homebrew/bin/python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
   ```
2. **Create a new Telegram bot** in @BotFather. Don't reuse a token. Fill in `.env` from
   `.env.example`.
3. **Keep unchanged:** `llm.py` (client part), `config.py` pattern, `store.py`, `watchdog.py`,
   `healthcheck.py`, `stt/`, the Dockerfile, the compose services.
4. **Rewrite:**
   - `SYSTEM_PROMPT` and its JSON schema in `llm.py`
   - `assistant.py` domain logic: keep `handle`/`handle_voice`/`Reply`/cancel/locks/undo, replace
     the intent handlers
   - commands in `main.py` (handler **and** `set_my_commands` entry)
5. **Rename containers** in `docker-compose.yml` (`container_name`, `image`) so two projects never
   collide.
   - Share **one** `stt` container between bots by pointing `STT_URL` at it over a shared Docker
     network, instead of running two 1.4 GB copies.
6. **Write tests first** with `FakeLLM` scripted replies, then
   `.venv/bin/python -m pytest -q tests`.
7. **Check in the terminal:** `docker compose run --rm bot python -m app.cli`.
8. **Deploy and confirm:**
   - `docker compose up -d --build`
   - `docker compose ps` shows healthy
   - you receive the "(re)started" message
   - `/usage` works
   - stop the bot and confirm the watchdog alert arrives

### Checklist before calling a bot "done"
- [ ] Only approved Telegram IDs get responses; strangers are logged
- [ ] Secrets only in `.env` / `data/`, `chmod 600`, not in git
- [ ] LLM returns JSON; all of its output is validated before acting
- [ ] Every change has Undo (or a confirmation step for destructive actions)
- [ ] `/help`, `/cancel` and the command menu are set
- [ ] Healthcheck, watchdog alert and restart message have all been tested
- [ ] `/usage` shows the model and cost; a vendor credit limit or prepaid cap is set
- [ ] Tests pass; a CLI dry run was done with the real LLM

---

## 8. Head start for the Tamil Events Agent (phase 2)

This one is genuinely **agentic** (autonomous, scheduled, tool-using). The Family bot is a routed
workflow. Reuse:

| Need | Reuse from here |
|---|---|
| Telegram alerts and commands | `main.py` skeleton, a new, separate bot token, own `.env` |
| LLM relevance judging and extraction | `llm.py` client + JSON schema pattern + `on_usage` cost tracking |
| Calendar or ICS output | `app/models.Event`, `icalendar` usage in `caldav_cal.py` (UTC times) |
| Date parsing of event pages | `dates.py` (`parse_date`, `resolve_times`, formatting) |
| State, dedupe, source health | `store.py` pattern (add `sources`, `events`, `runs` tables) |
| Reliability | heartbeat + watchdog + healthcheck; also alert if no run has completed in 24 h |
| Scheduled runs | `Application.job_queue` (needs `python-telegram-bot[job-queue]`) or a cron-style asyncio loop |
| Tests | `FakeLLM` / `MockTransport` patterns for search and fetch clients |

New pieces it will need:
- SearXNG container (search)
- fetch-and-clean (e.g. `trafilatura`)
- source discovery and scoring loop
- email sending (separate address)
- ICS feed endpoint

---

## 9. Operations cheat sheet

```sh
cd ~/family-event-bot
docker compose ps                                  # health of all containers
docker compose logs -f bot                         # live logs
docker compose logs bot | grep "LLM model="        # which model, tokens, est. cost per message
docker compose logs stt                            # voice timings (never the text)
docker compose up -d --build                       # deploy after code or .env changes
docker compose restart bot                         # restart only the bot
docker compose run --rm bot python -m app.cli      # chat in terminal (dry run)
docker compose run --rm bot python -m app.cli --list-models
.venv/bin/python -m pytest -q tests                # run tests
docker stats --no-stream                           # memory per container
```
In Telegram: `/help` · `/upcoming` · `/usage` · `/storage` · `/cancel` · ↩️ Undo on any change · 📷 send a picture.

Cleanup: `scripts/cleanup.sh` (manual) · `scripts/install_cleanup_job.sh` (monthly launchd job).

Key `.env` switches:

| Setting | Values / effect |
|---|---|
| `LLM_PROVIDER` | `gemini` \| `openrouter` |
| `CALENDAR_BACKEND` | `google` \| `caldav` |
| `STT_URL` | empty = voice off |
| `ALLOWED_USER_IDS` | approved Telegram IDs |
| `LOCAL_PLACES` | place names for fixing voice transcripts |
| `IMAGE_SOURCES` | `Name=PREFIX; …` title prefix for pictures by publisher |
| `CHILD_CLASS` | empty = keep every class's events from pictures |
| `VOICE_MAX_SECONDS` | longest voice note accepted |
| `NOTIFY_ON_START` | restart message on or off |
