"""Command-line helpers.

Usage:
    python -m app.cli send-test-email [--to someone@example.com]
"""

import argparse
import sys

from app.config import get_settings
from app.tools.resend_client import ResendError, send_email


def send_test_email(to: str | None) -> int:
    recipient = to or get_settings().owner_email
    if not recipient:
        print("No recipient: set OWNER_EMAIL in .env or pass --to.", file=sys.stderr)
        return 2
    try:
        message_id = send_email(
            to=recipient,
            subject="ایمیل آزمایشی دستیار صوتی",
            text="سلام! این یک ایمیل آزمایشی است. کلید Resend درست کار می‌کند.",
            html=(
                '<div dir="rtl" style="font-family:Tahoma,sans-serif">'
                "<p>سلام!</p><p>این یک ایمیل آزمایشی است. کلید Resend درست کار می‌کند.</p>"
                "</div>"
            ),
        )
    except ResendError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    print(f"Sent test email to {recipient} (id {message_id})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    test = sub.add_parser("send-test-email", help="send one test email to the owner")
    test.add_argument("--to", help="recipient (defaults to OWNER_EMAIL)")
    args = parser.parse_args(argv)

    if args.command == "send-test-email":
        return send_test_email(args.to)
    return 1


if __name__ == "__main__":
    sys.exit(main())
