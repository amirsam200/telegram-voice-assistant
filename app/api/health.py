"""Reachability checks for /health: database, Telegram and the LLM provider.

Results are cached briefly so a busy health probe does not hammer Telegram or the LLM API.
Failures report only the error class: URLs (the Telegram one contains the bot token) and
response bodies never leave this module.
"""

import logging
import threading
import time
from collections.abc import Callable

import httpx
from sqlalchemy import text

from app.config import get_settings

logger = logging.getLogger(__name__)

CACHE_S = 30.0
TIMEOUT_S = 5.0

Check = Callable[[], None]  # raises on failure


def check_database() -> None:
    from app.db.session import engine

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def check_telegram() -> None:
    token = get_settings().telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    response = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=TIMEOUT_S)
    if response.status_code != 200 or not response.json().get("ok"):
        raise RuntimeError(f"Telegram returned {response.status_code}")


def check_llm() -> None:
    s = get_settings()
    if not s.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    response = httpx.get(
        f"{s.deepseek_base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {s.deepseek_api_key}"},
        timeout=TIMEOUT_S,
    )
    if response.status_code != 200:
        raise RuntimeError(f"LLM provider returned {response.status_code}")


DEFAULT_CHECKS: dict[str, Check] = {"database": check_database, "telegram": check_telegram, "llm": check_llm}


class HealthChecker:
    def __init__(self, checks: dict[str, Check], cache_s: float = CACHE_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._checks = checks
        self._cache_s = cache_s
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, str]] | None = None

    def run(self) -> dict[str, str]:
        with self._lock:
            now = self._clock()
            if self._cached is not None and now - self._cached[0] < self._cache_s:
                return self._cached[1]
            results = {}
            for name, check in self._checks.items():
                try:
                    check()
                    results[name] = "ok"
                except Exception as exc:  # any failure means "unreachable"
                    logger.warning("Health check %s failed: %s", name, exc.__class__.__name__)
                    results[name] = f"error: {exc.__class__.__name__}"
            self._cached = (now, results)
            return results
