"""Backend hardening: JSON logs with run_id, rate limit, voice length, download retry, audio cleanup."""

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import ApplicationHandlerStop

from app.agent.agent import run_agent
from app.bot import main as bot
from app.bot.limits import RateLimiter, delete_old_audio, retry_async
from app.config import get_settings
from app.logging_setup import JsonFormatter, RunIdFilter, log_run, run_id_var, set_run_id
from tests.test_agent import ScriptedProvider


# --- JSON logs and run_id -------------------------------------------------------------------------


@pytest.fixture
def json_logs():
    """Capture formatted JSON log lines from the root logger."""
    lines: list[dict] = []

    class Collect(logging.Handler):
        def emit(self, record):
            lines.append(json.loads(self.format(record)))

    handler = Collect()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RunIdFilter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    token = run_id_var.set(None)
    yield lines
    run_id_var.reset(token)
    root.removeHandler(handler)
    root.setLevel(old_level)


def test_json_log_line_has_run_id_and_exception(json_logs) -> None:
    log = logging.getLogger("test.hardening")
    with log_run(42):
        log.info("سلام %s", "دنیا")
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("failed")
    log.info("outside")

    first, second, third = json_logs
    assert first["msg"] == "سلام دنیا" and first["run_id"] == "42" and first["level"] == "INFO"
    assert "ValueError: boom" in second["exc"]
    assert "run_id" not in third


def test_one_run_id_through_agent_and_tools(session, owner, ctx, json_logs) -> None:
    provider = ScriptedProvider([("find_contact", {"name": "سارا"})], [("propose_email", {
        "to": "sara.mohammadi@example.com", "subject": "گزارش", "body": "سلام سارا"})])
    result = run_agent(session, owner, "به سارا ایمیل بزن", provider, ctx)

    mine = [line for line in json_logs if line["logger"].startswith("app.")]
    assert {line["run_id"] for line in mine} == {str(result.run_id)}
    messages = " | ".join(line["msg"] for line in mine)
    assert "Agent run started" in messages and "Tool find_contact succeeded" in messages
    assert "Agent run finished" in messages


# --- rate limit -----------------------------------------------------------------------------------


def test_rate_limiter_sliding_window() -> None:
    now = [0.0]
    limiter = RateLimiter(3, window_s=60, clock=lambda: now[0])
    assert [limiter.hit(1) for _ in range(3)] == [None, None, None]
    assert limiter.hit(1) == pytest.approx(60)
    assert limiter.hit(2) is None  # per user
    now[0] = 30
    assert limiter.hit(1) == pytest.approx(30)
    now[0] = 60.5
    assert limiter.hit(1) is None


def _update(user_id: int, voice_duration: int | None = None, callback: bool = False):
    message = SimpleNamespace(reply_text=AsyncMock(), message_id=7, chat_id=1,
                              voice=SimpleNamespace(duration=voice_duration, file_unique_id="u", file_id="f",
                                                    get_file=AsyncMock()))
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, full_name="مالک"),
        effective_message=message,
        callback_query=SimpleNamespace(answer=AsyncMock()) if callback else None,
    )


@pytest.fixture
def bot_settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "allowed_user_ids", "1001")
    monkeypatch.setattr(s, "max_voice_seconds", 60)
    bot._limiter.cache_clear()
    monkeypatch.setattr(bot, "_limiter", lambda: limiter)
    limiter = RateLimiter(2, window_s=60)
    yield s


def test_guard_rate_limits_messages_in_persian(bot_settings) -> None:
    async def scenario():
        for _ in range(2):
            await bot.guard(_update(1001), None)
        blocked = _update(1001)
        with pytest.raises(ApplicationHandlerStop):
            await bot.guard(blocked, None)
        return blocked

    blocked = anyio.run(scenario)
    text = blocked.effective_message.reply_text.call_args.args[0]
    assert "ثانیه دیگر" in text


def test_guard_does_not_count_button_taps(bot_settings) -> None:
    async def scenario():
        for _ in range(5):
            await bot.guard(_update(1001, callback=True), None)
        await bot.guard(_update(1001), None)

    anyio.run(scenario)  # no ApplicationHandlerStop


def test_guard_refuses_strangers(bot_settings) -> None:
    update = _update(999)
    with pytest.raises(ApplicationHandlerStop):
        anyio.run(bot.guard, update, None)
    assert "اجازه" in update.effective_message.reply_text.call_args.args[0]


# --- voice length ---------------------------------------------------------------------------------


def test_voice_longer_than_60_seconds_is_refused_before_download(bot_settings, monkeypatch) -> None:
    ensure = MagicMock()
    monkeypatch.setattr(bot, "_ensure_user", ensure)
    update = _update(1001, voice_duration=61)
    anyio.run(bot.voice, update, SimpleNamespace(bot=AsyncMock()))

    assert "۶۰ ثانیه" in update.effective_message.reply_text.call_args.args[0]
    update.effective_message.voice.get_file.assert_not_called()
    ensure.assert_not_called()


# --- download retry with exponential backoff ------------------------------------------------------


class FlakyVoice:
    """A Telegram voice whose download fails `failures` times first."""

    def __init__(self, failures: int, error: Exception) -> None:
        self.failures = failures
        self.error = error
        self.calls = 0

    async def get_file(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return SimpleNamespace(download_to_drive=self._download)

    async def _download(self, custom_path):
        Path(custom_path).write_bytes(b"ogg")


def _download(voice, target):
    delays: list[float] = []

    async def fake_sleep(s):
        delays.append(s)

    ok = anyio.run(bot._download_voice, voice, target, fake_sleep)
    return ok, delays


def test_download_retries_with_exponential_backoff(tmp_path) -> None:
    voice = FlakyVoice(failures=2, error=TimedOut())
    ok, delays = _download(voice, tmp_path / "v.ogg")
    assert ok and voice.calls == 3
    assert delays == [1.0, 2.0]
    assert (tmp_path / "v.ogg").exists()


def test_download_gives_up_after_three_attempts(tmp_path) -> None:
    voice = FlakyVoice(failures=5, error=NetworkError("connection reset"))
    ok, delays = _download(voice, tmp_path / "v.ogg")
    assert not ok and voice.calls == 3 and delays == [1.0, 2.0]
    assert not (tmp_path / "v.ogg").exists()


def test_download_does_not_retry_bad_request(tmp_path) -> None:
    voice = FlakyVoice(failures=5, error=BadRequest("file is too big"))
    ok, delays = _download(voice, tmp_path / "v.ogg")
    assert not ok and voice.calls == 1 and delays == []


def test_retry_async_returns_first_success() -> None:
    calls = []

    async def fn():
        calls.append(1)
        return "ok"

    assert anyio.run(lambda: retry_async(fn, retry_on=(OSError,))) == "ok"
    assert len(calls) == 1


# --- leftover audio cleanup -----------------------------------------------------------------------


def test_delete_old_audio_only_removes_old_audio(tmp_path) -> None:
    now = 10_000.0
    user_dir = tmp_path / "8924828466"
    user_dir.mkdir()
    files = {
        "old.ogg": now - 3601, "old.wav": now - 7200, "fresh.ogg": now - 60, "old.txt": now - 9999,
    }
    for name, mtime in files.items():
        path = user_dir / name
        path.write_bytes(b"x")
        os.utime(path, (mtime, mtime))

    assert delete_old_audio(tmp_path, max_age_s=3600, now=now) == 2
    assert sorted(p.name for p in user_dir.iterdir()) == ["fresh.ogg", "old.txt"]
    assert delete_old_audio(tmp_path / "missing", max_age_s=3600, now=now) == 0


def test_set_run_id_accepts_none() -> None:
    token = run_id_var.set("x")
    set_run_id(None)
    assert run_id_var.get() is None
    run_id_var.reset(token)
