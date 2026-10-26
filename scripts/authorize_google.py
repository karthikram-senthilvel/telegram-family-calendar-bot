#!/usr/bin/env python3
"""One-time Google setup for the Family Event Bot. Run on the Mac Mini (needs a browser).

    .venv/bin/python scripts/authorize_google.py --client-secret ~/Downloads/client_secret.json

What it does:
  1. Opens a browser so you can sign in as the household Google account and approve access.
     The bot asks only for `calendar.app.created`: it can manage calendars it creates,
     and cannot see or change the primary calendar or any other calendar.
  2. Creates the secondary calendar ("Family Events" by default), or reuses it.
  3. Optionally shares it with other Google accounts (--share partner@example.com).
  4. Saves data/google/token.json and data/google/calendar.json for the container.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2 import id_token
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/calendar.app.created",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]
ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--client-secret", required=True, type=Path, help="OAuth client JSON (Desktop app) from Google Cloud")
    p.add_argument("--account", required=True, help="Google account that must own the calendar, e.g. household@gmail.com")
    p.add_argument("--calendar-name", default="Family Events")
    p.add_argument("--timezone", default="America/Chicago")
    p.add_argument("--share", action="append", default=[], metavar="EMAIL",
                   help="Give this Google account edit access to the calendar (repeatable)")
    p.add_argument("--share-role", default="writer", choices=["reader", "writer"])
    p.add_argument("--out", type=Path, default=ROOT / "data" / "google")
    args = p.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    token_path = args.out / "token.json"
    cal_path = args.out / "calendar.json"

    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secret), SCOPES)
    print(f"\nA browser window will open. Sign in as {args.account} and click Allow.\n")
    creds = flow.run_local_server(port=0, login_hint=args.account, prompt="consent",
                                  access_type="offline", open_browser=True)

    info = id_token.verify_oauth2_token(creds.id_token, Request(), flow.client_config["client_id"])
    email = info.get("email", "").lower()
    if email != args.account.lower():
        print(f"\n✗ You signed in as {email}, but the calendar must live in {args.account}. Nothing was saved.")
        return 1
    if not creds.refresh_token:
        print("\n✗ Google did not return a refresh token. Remove the app at "
              "https://myaccount.google.com/permissions and run this again.")
        return 1
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)
    print(f"✓ Signed in as {email}; token saved to {token_path}")

    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)

    cal_id = None
    if cal_path.exists():
        old = json.loads(cal_path.read_text()).get("calendar_id")
        try:
            svc.calendars().get(calendarId=old).execute()
            cal_id = old
            print(f"✓ Reusing existing calendar {old}")
        except HttpError:
            print("  (previous calendar not reachable with this login; creating a new one)")
    if cal_id is None:
        cal = svc.calendars().insert(body={
            "summary": args.calendar_name,
            "description": "Events added by the Family Event Bot (Telegram).",
            "timeZone": args.timezone,
        }).execute()
        cal_id = cal["id"]
        print(f"✓ Created calendar '{args.calendar_name}' ({cal_id})")
    cal_path.write_text(json.dumps({"calendar_id": cal_id, "name": args.calendar_name, "account": email}, indent=2))

    for addr in args.share:
        try:
            svc.acl().insert(calendarId=cal_id, sendNotifications=True, body={
                "role": args.share_role, "scope": {"type": "user", "value": addr},
            }).execute()
            print(f"✓ Shared with {addr} ({args.share_role})")
        except HttpError as e:
            print(f"✗ Could not share with {addr}: {e}. Share it from Google Calendar settings instead.")

    print("\nDone. Restart the bot:  docker compose up -d --build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
