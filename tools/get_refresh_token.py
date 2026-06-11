"""One-time OAuth flow to capture a refresh token for centralagentai@gmail.com.

Finds a client_secret*.json in the project root, runs a local-server consent
flow (opens the browser), and prints the refresh token + client id/secret so we
can store them in .env.

Scopes (a SUPERSET — re-minting always grants every scope the app needs so we
never lose one): read-write Calendar events (auto-RSVP), Gmail send (insight
email) AND Gmail readonly (the invite scanner reads the inbox for Meet invites
created from meet.google.com, which produce no Calendar event).

Usage:
    cd /Users/rohitbind/Desktop/centralagent
    .venv/bin/python tools/get_refresh_token.py              # prints the token
    .venv/bin/python tools/get_refresh_token.py --write-env  # also updates .env
"""
from __future__ import annotations

import glob
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    # read-write events so the bot can auto-RSVP "yes" to invitations
    "https://www.googleapis.com/auth/calendar.events",
    # send the insight email as the bot
    "https://www.googleapis.com/auth/gmail.send",
    # read the inbox for Meet invites that create no Calendar event
    # (powers the Gmail invite scanner)
    "https://www.googleapis.com/auth/gmail.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(PROJECT_ROOT, ".env")


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


def _upsert_env_var(path: str, key: str, value: str) -> None:
    """Replace (or append) a single KEY=value line in .env, leaving the rest intact."""
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()

    new_line = f"{key}={value}"
    replaced = False
    for i, line in enumerate(lines):
        # Match the assignment even if it has leading spaces; ignore comments.
        if line.lstrip().startswith(f"{key}=") and not line.lstrip().startswith("#"):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> None:
    write_env = "--write-env" in sys.argv[1:]

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
            "You will see a NEW permission this time: 'Read your email messages\n"
            "and settings' — that is the gmail.readonly scope the invite scanner needs.\n"
        ),
    )
    if not creds.refresh_token:
        print("WARNING: no refresh_token returned. Re-run; ensure prompt=consent.")
        sys.exit(1)

    print("\n==================  COPY THESE  ==================")
    print("GOOGLE_OAUTH_CLIENT_ID=", creds.client_id, sep="")
    print("GOOGLE_OAUTH_CLIENT_SECRET=", creds.client_secret, sep="")
    print("GOOGLE_OAUTH_REFRESH_TOKEN=", creds.refresh_token, sep="")
    print("=================================================")

    if write_env:
        _upsert_env_var(ENV_FILE, "GOOGLE_OAUTH_REFRESH_TOKEN", creds.refresh_token)
        print(f"\nUpdated GOOGLE_OAUTH_REFRESH_TOKEN in {ENV_FILE}")
    else:
        print("\n(Run again with --write-env to drop the token straight into .env.)")

    # Reminder for the deploy side — the token also has to reach Railway.
    print(
        "\nNEXT: push the new token to Railway, then enable the scanner:\n"
        "  cd backend\n"
        '  railway variables --set "GOOGLE_OAUTH_REFRESH_TOKEN=<the token above>"\n'
        '  railway variables --set "GMAIL_SCAN_ENABLED=true"\n'
        "  railway up\n"
    )


if __name__ == "__main__":
    main()
