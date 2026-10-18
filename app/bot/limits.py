"""Bot safeguards: per-user rate limit, retry with exponential backoff, leftover audio cleanup."""

import asyncio
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RateLimiter:
    """Sliding window: at most `limit` events per `window_s` seconds per key."""

    def __init__(self, limit: int, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._events: dict[int, deque[float]] = defaultdict(deque)

    def hit(self, key: int) -> float | None:
        """Record one event. Returns None if allowed, else the seconds to wait."""
        now = self._clock()
        events = self._events[key]
        while events and now - events[0] >= self.window_s:
            events.popleft()
        if len(events) >= self.limit:
            return self.window_s - (now - events[0])
        events.append(now)
        return None


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    retry_on: tuple[type[BaseException], ...],
    give_up_on: tuple[type[BaseException], ...] = (),
    attempts: int = 3,
    base_delay_s: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    what: str = "operation",
) -> T:
    """Call fn up to `attempts` times, waiting base_delay_s * 2**n between tries (1 s, 2 s, ...)."""
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except give_up_on:
            raise
        except retry_on as exc:
            if attempt == attempts:
                logger.error("%s failed after %d attempts: %s", what, attempts, exc.__class__.__name__)
                raise
            delay = base_delay_s * 2 ** (attempt - 1)
            logger.warning("%s failed (%s); retry %d/%d in %.0f s",
                           what, exc.__class__.__name__, attempt, attempts - 1, delay)
            await sleep(delay)
    raise AssertionError("unreachable")


def delete_old_audio(directory: str | Path, max_age_s: float, now: float | None = None) -> int:
    """Delete audio files older than max_age_s under directory (normally already deleted after STT)."""
    now = time.time() if now is None else now
    deleted = 0
    root = Path(directory)
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".ogg", ".oga", ".opus", ".wav"}:
            try:
                if now - path.stat().st_mtime > max_age_s:
                    path.unlink()
                    deleted += 1
            except FileNotFoundError:
                continue
    return deleted
