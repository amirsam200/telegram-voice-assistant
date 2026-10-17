"""One-time Google OAuth for the owner's calendar (Desktop app client, scope calendar.events).

Opens the browser (or prints the URL to open), then prints the GOOGLE_* lines for .env.
With --write-env it writes them into .env instead of printing them.

Run it in Docker (no local Python needed); port 8765 receives Google's redirect:
    docker compose run --rm -p 127.0.0.1:8765:8765 -v ./credentials.json:/app/credentials.json:ro \
        -v ./.env:/app/.env migrate python scripts/google_auth.py --write-env
"""

import argparse
import json
import re
import sys
import webbrowser
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
PORT = 8765


def write_env(env_path: Path, values: dict[str, str]) -> None:
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    newline = "\r\n" if "\r\n" in text else "\n"
    for key, value in values.items():
        line = f"{key}={value}"
        pattern = re.compile(rf"^{key}=.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(lambda _: line, text)
        else:
            if text and not text.endswith(("\n", "\r\n")):
                text += newline
            text += line + newline
    # Write in place (keeps a bind-mounted file intact).
    with env_path.open("r+" if env_path.exists() else "w", encoding="utf-8", newline="") as f:
        f.seek(0)
        f.write(text)
        f.truncate()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--credentials", default="credentials.json", help="OAuth client file from Google Cloud")
    parser.add_argument("--write-env", action="store_true", help="write the values into .env instead of printing")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    creds_path = Path(args.credentials)
    if not creds_path.exists():
        print(f"{creds_path} not found. Download the Desktop app OAuth client JSON from Google Cloud.", file=sys.stderr)
        return 2
    client = json.loads(creds_path.read_text(encoding="utf-8"))
    client = client.get("installed") or client.get("web") or {}

    try:
        webbrowser.get()
        can_open = True
    except webbrowser.Error:  # e.g. inside Docker: just print the URL
        can_open = False

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    credentials = flow.run_local_server(
        host="localhost",
        bind_addr="0.0.0.0",
        port=PORT,
        open_browser=can_open,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="Open this URL in your browser and allow calendar access:\n{url}\n",
        success_message="Done. You can close this tab and return to the terminal.",
    )
    if not credentials.refresh_token:
        print("Google returned no refresh token. Remove the app's access at "
              "https://myaccount.google.com/permissions and run this again.", file=sys.stderr)
        return 1

    values = {
        "GOOGLE_CLIENT_ID": client.get("client_id", ""),
        "GOOGLE_CLIENT_SECRET": client.get("client_secret", ""),
        "GOOGLE_REFRESH_TOKEN": credentials.refresh_token,
    }
    if args.write_env:
        write_env(Path(args.env_file), values)
        print(f"Wrote GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN to {args.env_file}.")
    else:
        print("Add these lines to .env:")
        for key, value in values.items():
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
