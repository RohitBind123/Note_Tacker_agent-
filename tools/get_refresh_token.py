"""One-time OAuth flow to capture a refresh token for centralagentai@gmail.com.

Finds a client_secret*.json in the project root, runs a local-server consent
flow (opens the browser), and prints the refresh token + client id/secret so we
can store them in .env. Scopes: read calendar events + send Gmail.

Usage:
    cd /Users/rohitbind/Desktop/centralagent
    .venv/bin/python tools/get_refresh_token.py
"""
from __future__ import annotations

import glob
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    # read-write events so the bot can auto-RSVP "yes" to invitations
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.send",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_client_secret() -> str:
    matches = sorted(glob.glob(os.path.join(PROJECT_ROOT, "client_secret*.json")))
    if not matches:
        print(
            "ERROR: no client_secret*.json found in",
            PROJECT_ROOT,
            "\n  Download the Desktop OAuth client JSON from the Console and save it there.",
        )
        sys.exit(1)
    return matches[0]


def main() -> None:
    cs = find_client_secret()
    print(f"Using client secret: {os.path.basename(cs)}")
    flow = InstalledAppFlow.from_client_secrets_file(cs, scopes=SCOPES)
    # access_type=offline + prompt=consent guarantees a refresh_token is returned.
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        login_hint="centralagentai@gmail.com",
        authorization_prompt_message=(
            "\nOpening your browser. Sign in as centralagentai@gmail.com.\n"
            "If you see 'Google hasn't verified this app': Advanced -> "
            "Go to CentralAgent (unsafe) -> Allow.\n"
        ),
    )
    print("\n==================  COPY THESE  ==================")
    print("GOOGLE_OAUTH_CLIENT_ID=", creds.client_id, sep="")
    print("GOOGLE_OAUTH_CLIENT_SECRET=", creds.client_secret, sep="")
    print("GOOGLE_OAUTH_REFRESH_TOKEN=", creds.refresh_token, sep="")
    print("=================================================")
    if not creds.refresh_token:
        print("WARNING: no refresh_token returned. Re-run; ensure prompt=consent.")


if __name__ == "__main__":
    main()
