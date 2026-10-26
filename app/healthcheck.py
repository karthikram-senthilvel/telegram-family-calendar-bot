"""Docker healthcheck: healthy if the bot reached Telegram within the last 3 minutes."""
import os
import sys
import time
from pathlib import Path

hb = Path(os.environ.get("DATA_DIR", "/data")) / "heartbeat"
try:
    age = time.time() - int(hb.read_text().strip())
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if age < 180 else 1)
