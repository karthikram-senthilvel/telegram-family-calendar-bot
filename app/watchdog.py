"""Independent watchdog: alerts admins on Telegram if the bot's heartbeat goes stale.

Runs in its own container (stdlib only) so it still works when the bot is hung or crashed.
"""
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s watchdog: %(message)s")
log = logging.getLogger()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMINS = [
    int(x) for x in (os.environ.get("ADMIN_USER_IDS") or os.environ["ALLOWED_USER_IDS"]).split(",") if x.strip()
]
HEARTBEAT = Path(os.environ.get("DATA_DIR", "/data")) / "heartbeat"
STALE_AFTER = int(os.environ.get("WATCHDOG_STALE_SECONDS", "300"))
CHECK_EVERY = 60
STARTUP_GRACE = 180


def send(text: str) -> None:
    for uid in ADMINS:
        data = urllib.parse.urlencode({"chat_id": uid, "text": text}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data, timeout=20).read()
        except Exception as e:
            log.warning("could not alert %s: %s", uid, e)


def age() -> float | None:
    try:
        return time.time() - int(HEARTBEAT.read_text().strip())
    except (OSError, ValueError):
        return None


def main() -> None:
    time.sleep(STARTUP_GRACE)
    alerted = False
    while True:
        a = age()
        stale = a is None or a > STALE_AFTER
        if stale and not alerted:
            mins = "unknown" if a is None else f"{int(a // 60)} min"
            log.info("heartbeat stale (%s), alerting", mins)
            send(f"🚨 Family Event Bot has not responded for {mins}. Check the Mac Mini (docker compose ps / logs).")
            alerted = True
        elif not stale and alerted:
            send("✅ Family Event Bot is responding again.")
            alerted = False
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
