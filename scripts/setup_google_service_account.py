#!/usr/bin/env python3
"""Connect the bot to the family calendar through a Google service account.

    .venv/bin/python scripts/setup_google_service_account.py \\
        --key ~/Downloads/<project>-<id>.json --calendar-id <id>@group.calendar.google.com

Before running: create the calendar in the household Google account and share it with the service
account's e-mail ("Make changes to events"). The script then:
  1. copies the key to data/google/service-account.json (readable only by you),
  2. saves the calendar id to data/google/calendar.json,
  3. proves access by creating and deleting a test event.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

ROOT = Path(__file__).resolve().parent.parent
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--key", required=True, type=Path, help="service account JSON key downloaded from Cloud Console")
    p.add_argument("--calendar-id", required=True, help="from Google Calendar → Settings → Integrate calendar")
    p.add_argument("--out", type=Path, default=ROOT / "data" / "google")
    args = p.parse_args()

    cal_id = args.calendar_id.strip()
    if cal_id == "primary" or not cal_id.endswith("@group.calendar.google.com"):
        print(f"✗ {cal_id!r} is not a secondary calendar id (it should end in @group.calendar.google.com).")
        print("  The bot must never use the primary calendar.")
        return 1

    info = json.loads(args.key.expanduser().read_text())
    if info.get("type") != "service_account":
        print("✗ That file is not a service account key (type != service_account).")
        return 1
    sa_email = info["client_email"]

    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    try:
        svc.events().list(calendarId=cal_id, maxResults=1).execute()
        start = datetime.now().astimezone() + timedelta(days=365)
        ev = svc.events().insert(calendarId=cal_id, body={
            "summary": "Family Event Bot connection test (auto-deleted)",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": (start + timedelta(minutes=5)).isoformat()},
        }).execute()
        svc.events().delete(calendarId=cal_id, eventId=ev["id"]).execute()
    except HttpError as e:
        print(f"✗ Google refused access ({e.resp.status}): {e}")
        if e.resp.status in (403, 404):
            print(f"\n  Share the calendar with {sa_email} and choose 'Make changes to events',")
            print("  then wait a minute and run this again.")
        return 1
    print(f"✓ {sa_email} can read and write {cal_id}")

    args.out.mkdir(parents=True, exist_ok=True)
    dest = args.out / "service-account.json"
    shutil.copyfile(args.key.expanduser(), dest)
    dest.chmod(0o600)
    (args.out / "calendar.json").write_text(json.dumps(
        {"calendar_id": cal_id, "service_account": sa_email}, indent=2
    ))
    print(f"✓ Key saved to {dest} (owner-only); calendar id saved to {args.out / 'calendar.json'}")
    print(f"\nYou can now delete the downloaded key: rm '{args.key}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
