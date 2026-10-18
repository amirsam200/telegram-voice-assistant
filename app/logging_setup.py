"""Structured JSON logs with a run_id correlation ID shared by bot, agent, executor and tools.

The run_id lives in a ContextVar. asyncio.to_thread copies the context into the worker thread, so
set it before handing work to a thread (or inside the thread, for logs written there).
"""

import contextlib
import datetime as dt
import json
import logging
import sys
from collections.abc import Iterator
from contextvars import ContextVar

run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)

# Loggers that log request URLs (the Telegram URL contains the bot token) stay at WARNING.
NOISY = ("httpx", "httpcore", "telegram.ext.ExtBot", "urllib3", "google_auth_httplib2")


def set_run_id(run_id: int | str | None) -> None:
    run_id_var.set(None if run_id is None else str(run_id))


@contextlib.contextmanager
def log_run(run_id: int | str | None) -> Iterator[None]:
    token = run_id_var.set(None if run_id is None else str(run_id))
    try:
        yield
    finally:
        run_id_var.reset(token)


class RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = run_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        run_id = getattr(record, "run_id", None) or run_id_var.get()
        if run_id is not None:
            entry["run_id"] = run_id
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """JSON lines on stderr for the whole process (stdout stays free for the MCP protocol)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RunIdFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    # Let uvicorn's loggers flow through the JSON handler instead of their own.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True
