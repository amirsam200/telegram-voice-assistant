"""Dashboard sign-in: one owner password, stored only as a PBKDF2 hash in DASHBOARD_PASSWORD_HASH.

Create the hash (the password is read without echo and never stored):
    docker compose run --rm -it --no-deps dashboard python -m app.dashboard.auth

The format avoids "$" so docker compose does not try to expand it in .env:
    pbkdf2_sha256:<iterations>:<salt>:<hash>   (salt and hash base64url)
"""

import base64
import getpass
import hashlib
import hmac
import secrets
import sys

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, iterations: int = ITERATIONS, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{ALGORITHM}:{iterations}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, digest = stored.split(":")
        if algorithm != ALGORITHM:
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, _unb64(digest))


def main() -> int:
    password = getpass.getpass("Dashboard password: ")
    if len(password) < 10:
        print("Use at least 10 characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat: ") != password:
        print("The passwords differ.", file=sys.stderr)
        return 1
    print(f"DASHBOARD_PASSWORD_HASH={hash_password(password)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
