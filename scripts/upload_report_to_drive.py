"""Uploads/updates reports/live_test/live_test_report.xlsx to the user's own
Google Drive, so it can be checked from a phone/laptop without RDP-ing into
the trading server -- built 2026-08-27 at the user's explicit request, after
the report was switched from a once-daily rebuild to a frequent (every
15-30 min) one via a Scheduled Task change.

Uses OAuth (the user's own Google account), not a service account -- the
user's Google Cloud organization has service-account-key creation disabled
by policy, so a service account key was never an option here.

One-time setup before this ever runs successfully:
  1. credentials/client_secret.json -- the OAuth Desktop-app client
     downloaded from Google Cloud Console (Credentials -> OAuth client ID).
     Never commit this (see .gitignore).
  2. First run must happen somewhere with a real browser available (e.g.
     interactively via RDP on the server) -- it opens a browser tab asking
     the user to log into their Google account and approve access, then
     caches a refresh token at credentials/token.json. Every run after that
     is fully headless (no browser, no prompt) -- safe to call from a
     Scheduled Task.
  3. credentials/drive_file_id.txt -- created automatically after the first
     successful upload, so every later run UPDATES that same Drive file
     (keeping one stable shareable link) instead of creating a new file
     each time.

Scope is the narrow drive.file scope (only files this app itself created),
not full Drive access -- this script can never see or touch any other file
in the user's Drive.

    python scripts/upload_report_to_drive.py

Intended to run right after scripts/generate_live_test_report.py, same
Scheduled Task, so the Drive copy stays in sync with the local Excel file.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from bot.config import PROJECT_ROOT

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CREDENTIALS_DIR = PROJECT_ROOT / "credentials"
CLIENT_SECRET_PATH = CREDENTIALS_DIR / "client_secret.json"
TOKEN_PATH = CREDENTIALS_DIR / "token.json"
FILE_ID_PATH = CREDENTIALS_DIR / "drive_file_id.txt"
REPORT_PATH = PROJECT_ROOT / "reports" / "live_test" / "live_test_report.xlsx"
REPORT_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def get_credentials() -> Credentials:
    creds = None
    if TOKEN_PATH.exists():
        # utf-8-sig: PowerShell's `-Encoding utf8` writes a BOM that Python's
        # plain utf-8 JSON parser rejects -- this happened once already when
        # manually reconstructing this file over RDP, see
        # feedback_windows_restart_gotchas memory for the full story.
        with open(TOKEN_PATH, "r", encoding="utf-8-sig") as f:
            creds = Credentials.from_authorized_user_info(json.load(f), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
        return creds

    if not CLIENT_SECRET_PATH.exists():
        raise SystemExit(
            f"{CLIENT_SECRET_PATH} not found. Download the OAuth Desktop-app "
            f"client JSON from Google Cloud Console and save it there first."
        )

    print("No cached login found -- opening a browser window for one-time Google sign-in...")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    CREDENTIALS_DIR.mkdir(exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())
    print(f"Login cached at {TOKEN_PATH} -- future runs won't need the browser.")
    return creds


def main() -> None:
    if not REPORT_PATH.exists():
        raise SystemExit(
            f"{REPORT_PATH} not found -- run scripts/generate_live_test_report.py first."
        )

    creds = get_credentials()
    drive = build("drive", "v3", credentials=creds)
    media = MediaFileUpload(str(REPORT_PATH), mimetype=REPORT_MIME_TYPE, resumable=False)

    existing_id = FILE_ID_PATH.read_text().strip() if FILE_ID_PATH.exists() else None

    if existing_id:
        try:
            drive.files().update(fileId=existing_id, media_body=media).execute()
            print(f"Updated existing Drive file (id={existing_id}).")
            return
        except Exception as exc:
            print(f"Update of existing file failed ({exc}) -- creating a fresh file instead.")

    file_metadata = {"name": "live_test_report.xlsx"}
    result = drive.files().create(body=file_metadata, media_body=media, fields="id, webViewLink").execute()
    FILE_ID_PATH.write_text(result["id"])
    print(f"Created new Drive file (id={result['id']}).")
    print(f"View it at: {result.get('webViewLink', '(link not returned)')}")


if __name__ == "__main__":
    main()
