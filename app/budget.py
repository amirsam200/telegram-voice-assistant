"""Daily and monthly token budgets: usage, alerts at 80 and 100 percent, and the optional hard stop.

Usage is prompt + completion tokens of real LLM calls (demo rows excluded) since the start of the
current day or month in Asia/Tehran. Each threshold alerts once per period: the alert row's unique
key (period, threshold) makes that true across the bot, API and MCP processes.
"""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.agent import fa
from app.config import get_settings
from app.db.models import BudgetAlert, LlmCall

logger = logging.getLogger(__name__)

THRESHOLDS = (80, 100)
PERIOD_FA = {"day": "روزانه", "month": "ماهانه"}


@dataclass(frozen=True)
class BudgetStatus:
    period: str  # "day" or "month"
    key: str  # "day:2026-10-18" or "month:2026-10"
    used: int
    budget: int

    @property
    def percent(self) -> float:
        return 100.0 * self.used / self.budget if self.budget else 0.0

    @property
    def exhausted(self) -> bool:
        return self.budget > 0 and self.used >= self.budget


def period_starts(now: dt.datetime) -> dict[str, dt.datetime]:
    local = now.astimezone(fa.TEHRAN)
    day = dt.datetime.combine(local.date(), dt.time(0, 0), tzinfo=fa.TEHRAN)
    return {"day": day, "month": day.replace(day=1)}


def tokens_since(session: Session, start: dt.datetime) -> int:
    return int(session.scalar(
        select(func.coalesce(func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens), 0))
        .where(LlmCall.created_at >= start, LlmCall.demo.is_(False))
    ))


def statuses(session: Session, now: dt.datetime | None = None) -> list[BudgetStatus]:
    """The configured budgets (a budget of 0 is off) with their usage so far."""
    s = get_settings()
    now = now or fa.now_tehran()
    starts = period_starts(now)
    local = now.astimezone(fa.TEHRAN)
    keys = {"day": f"day:{local.date().isoformat()}", "month": f"month:{local:%Y-%m}"}
    budgets = {"day": s.daily_token_budget, "month": s.monthly_token_budget}
    return [BudgetStatus(period, keys[period], tokens_since(session, starts[period]), budget)
            for period, budget in budgets.items() if budget > 0]


def is_blocked(session: Session, now: dt.datetime | None = None) -> bool:
    """True when the hard stop is on and a budget is used up."""
    return get_settings().budget_hard_stop and any(b.exhausted for b in statuses(session, now))


def alert_text(status: BudgetStatus, threshold: int) -> str:
    period = PERIOD_FA[status.period]
    used = fa.to_fa_digits(f"{status.used:,}").replace(",", "٬")
    budget = fa.to_fa_digits(f"{status.budget:,}").replace(",", "٬")
    text = (f"⚠️ هشدار بودجهٔ توکن {period}: {fa.to_fa_digits(threshold)}٪ مصرف شد "
            f"({used} از {budget} توکن).")
    if threshold >= 100:
        text += ("\nربات درخواست تازه نمی‌پذیرد تا بودجه تمدید شود." if get_settings().budget_hard_stop
                 else "\nتوقف خودکار خاموش است؛ ربات به کار ادامه می‌دهد.")
    return text


def send_telegram(text: str) -> None:
    """Send a message to the owner through the Bot API (sync, used from worker threads)."""
    s = get_settings()
    if not (s.telegram_bot_token and s.owner_telegram_id):
        raise RuntimeError("TELEGRAM_BOT_TOKEN or ALLOWED_USER_IDS is not set")
    response = httpx.post(f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
                          json={"chat_id": s.owner_telegram_id, "text": text}, timeout=10)
    if response.status_code != 200:
        # Never include the URL: it contains the bot token.
        raise RuntimeError(f"Telegram sendMessage returned {response.status_code}")


def check_and_alert(session: Session, now: dt.datetime | None = None,
                    send: Callable[[str], None] = send_telegram) -> list[tuple[str, int]]:
    """Send each crossed threshold once per period. Returns the (period key, threshold) alerts sent."""
    sent = []
    for status in statuses(session, now):
        for threshold in THRESHOLDS:
            if status.percent < threshold:
                continue
            claimed = session.execute(
                insert(BudgetAlert)
                .values(period=status.key, threshold=threshold, used_tokens=status.used, budget_tokens=status.budget)
                .on_conflict_do_nothing(constraint="uq_budget_alerts_period_threshold")
                .returning(BudgetAlert.id)
            ).scalar_one_or_none()
            session.commit()
            if claimed is None:
                continue  # already alerted in this period
            try:
                send(alert_text(status, threshold))
            except Exception as exc:
                # Release the claim so the next LLM call tries again.
                session.execute(BudgetAlert.__table__.delete().where(BudgetAlert.id == claimed))
                session.commit()
                logger.error("Budget alert %s %d%% could not be sent: %s", status.key, threshold, exc)
                continue
            logger.warning("Budget alert sent: %s at %d%% (%d of %d tokens)",
                           status.key, threshold, status.used, status.budget)
            sent.append((status.key, threshold))
    return sent


def after_llm_call(record) -> None:
    """Hook for the LLM call recorder: check the budgets after each successful call."""
    if record.status != "ok":
        return
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        check_and_alert(session)
