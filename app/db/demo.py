"""Seed 30 days of realistic demo data so the dashboard is not empty before real use.

    docker compose run --rm migrate python -m app.db.demo           # add (skips if present)
    docker compose run --rm migrate python -m app.db.demo --remove  # delete the demo data only

Everything belongs to a separate demo user (negative Telegram ID, so it can never message the
bot); its llm_calls rows are flagged demo and never count against the token budgets, and the
admin API refuses to retry demo runs, so no demo email is ever sent.
"""

import argparse
import datetime as dt
import math
import random
import sys

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.agent.fa import TEHRAN
from app.agent.llm_log import cost_usd
from app.db.models import (
    AgentRun,
    AuditEvent,
    CalendarEvent,
    Email,
    Intent,
    LlmCall,
    RunFeedback,
    RunStatus,
    ToolCall,
    User,
)

DEMO_TELEGRAM_ID = -1_000_001
DEMO_NAME = "کاربر نمایشی (demo)"
DAYS = 30

MODELS = [("deepseek-chat", 0.8), ("deepseek-reasoner", 0.2)]

SCHEDULES = [
    ("فردا ساعت ده صبح با علی جلسه بودجه بذار", "جلسه بودجه", 60),
    ("پس‌فردا ساعت چهار عصر جلسه تیم فنی", "جلسه تیم فنی", 45),
    ("سه‌شنبه ساعت یازده با سارا قرار بگذار", "قرار با سارا", 30),
    ("هفته بعد دوشنبه ساعت نه جلسه برنامه‌ریزی", "جلسه برنامه‌ریزی", 90),
    ("امروز ساعت شش عصر تماس با مشتری", "تماس با مشتری", 30),
]
EMAILS = [
    ("به سارا ایمیل بزن و بگو گزارش آماده است", "گزارش ماهانه", "سلام سارا، گزارش ماهانه آماده است."),
    ("به علی بگو جلسه فردا لغو شد", "لغو جلسه فردا", "سلام علی، جلسه فردا لغو شد."),
    ("به رضا ایمیل بزن و فایل‌ها را بخواه", "درخواست فایل‌ها", "سلام رضا، لطفاً فایل‌ها را بفرست."),
]
CONTACTS = [("مریم کریمی را با ایمیل maryam@example.com اضافه کن", "مریم کریمی", "maryam@example.com")]
UNCLEAR = ["یه کاری برام بکن", "اون جلسه رو یادت هست؟", "ساعت چنده"]
QUESTIONS = ["لطفاً بگویید جلسه چه ساعتی باشد؟", "نشانی ایمیل این شخص را بگویید.", "چه کاری برایتان انجام دهم؟"]
GUESTS = ["ali.demo@example.com", "sara.demo@example.com", "reza.demo@example.com"]


def _pick(rng: random.Random, weighted: list[tuple]) -> object:
    return rng.choices([w[0] for w in weighted], weights=[w[1] for w in weighted])[0]


def _latency(rng: random.Random, model: str) -> int:
    base = 1400 if model == "deepseek-chat" else 4200
    return int(rng.lognormvariate(math.log(base), 0.45))


def _demo_user(session: Session) -> User:
    user = session.scalar(select(User).where(User.telegram_user_id == DEMO_TELEGRAM_ID))
    if user is None:
        user = User(telegram_user_id=DEMO_TELEGRAM_ID, name=DEMO_NAME)
        session.add(user)
        session.flush()
    return user


def seed(session: Session, days: int = DAYS, now: dt.datetime | None = None, seed_value: int = 42) -> int:
    """Insert the demo data; returns the number of runs (0 if demo data already exists)."""
    rng = random.Random(seed_value)
    now = now or dt.datetime.now(TEHRAN)
    user = _demo_user(session)
    if session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.user_id == user.id)):
        return 0

    runs = 0
    for day_offset in range(days, -1, -1):
        day = (now - dt.timedelta(days=day_offset)).date()
        # Busier on working days (Saturday to Wednesday in Iran), a slow upward trend over the month.
        busy = 1.0 if day.weekday() not in (3, 4) else 0.45
        for _ in range(int(rng.randint(8, 22) * busy * (0.8 + 0.4 * (days - day_offset) / days))):
            start = dt.datetime.combine(day, dt.time(rng.randint(8, 22), rng.randint(0, 59)), tzinfo=TEHRAN)
            if start > now:
                continue
            _demo_run(session, rng, user, start)
            runs += 1
    session.commit()
    return runs


def _demo_run(session: Session, rng: random.Random, user: User, start: dt.datetime) -> None:
    model = _pick(rng, MODELS)
    kind = _pick(rng, [("schedule", 0.5), ("email", 0.3), ("contact", 0.05), ("unclear", 0.15)])
    outcome = _pick(rng, [("executed", 0.74), ("cancelled", 0.07), ("edited", 0.09), ("failed", 0.04),
                          ("pending", 0.06)])
    validation_errors = 1 if rng.random() < (0.05 if model == "deepseek-chat" else 0.09) else 0
    steps = rng.choice([1, 2, 2, 2, 3]) + validation_errors

    action: dict | None = None
    if kind == "schedule":
        text, title, minutes = rng.choice(SCHEDULES)
        when = (start + dt.timedelta(days=rng.randint(0, 6), hours=1)).replace(minute=0, second=0, microsecond=0)
        action = {"type": "schedule", "date": when.date().isoformat(), "time": when.strftime("%H:%M:00"),
                  "duration_min": minutes, "title": title, "description": "",
                  "guests": rng.sample(GUESTS, rng.randint(0, 2)), "new_contacts": []}
        intent, tool = Intent.schedule, "create_calendar_event"
    elif kind == "email":
        text, subject, body = rng.choice(EMAILS)
        action = {"type": "email", "to": rng.choice(GUESTS), "subject": subject, "body": body, "new_contacts": []}
        intent, tool = Intent.email, "send_email"
    elif kind == "contact":
        text, name, email = rng.choice(CONTACTS)
        action = {"type": "add_contact", "name": name, "email": email, "previous_email": None}
        intent, tool = Intent.contact, "add_contact"
    else:
        text, intent, tool = rng.choice(UNCLEAR), Intent.unknown, None

    if action is None:
        status = RunStatus.clarification
    else:
        status = {"executed": RunStatus.executed, "cancelled": RunStatus.rejected, "edited": RunStatus.rejected,
                  "failed": RunStatus.failed, "pending": RunStatus.pending_confirmation}[outcome]

    # LLM calls: one per step, sometimes a retried attempt first.
    calls, t = [], start
    for step in range(steps):
        if rng.random() < 0.03:
            calls.append(LlmCall(model=model, purpose="intent" if step == 0 else "tool_step", status="retry",
                                 error="APITimeoutError: Request timed out.", latency_ms=60000, demo=True,
                                 created_at=t))
            t += dt.timedelta(seconds=rng.randint(2, 5))
        prompt = rng.randint(2600, 4200) + step * rng.randint(250, 600)
        completion = rng.randint(60, 320)
        cached = int(prompt * rng.uniform(0.2, 0.7)) if step == 0 else int(prompt * rng.uniform(0.6, 0.9))
        latency = _latency(rng, model)
        calls.append(LlmCall(model=model, purpose="intent" if step == 0 else "tool_step", status="ok",
                             prompt_tokens=prompt, completion_tokens=completion, cached_tokens=cached,
                             cost_usd=cost_usd(model, prompt, completion, cached), latency_ms=latency, demo=True,
                             created_at=t))
        t += dt.timedelta(milliseconds=latency)

    ok_calls = [c for c in calls if c.status == "ok"]
    run = AgentRun(
        user_id=user.id, input_text=text, intent=intent, status=status, action_json=action, model=model,
        prompt_version="v2", steps=steps, validation_errors=validation_errors,
        prompt_tokens=sum(c.prompt_tokens for c in ok_calls),
        completion_tokens=sum(c.completion_tokens for c in ok_calls),
        tokens=sum(c.prompt_tokens + c.completion_tokens for c in ok_calls),
        latency_ms=int((t - start).total_seconds() * 1000),
        reply_text=rng.choice(QUESTIONS) if status == RunStatus.clarification else None,
        error="Gmail returned 503: backend error" if status == RunStatus.failed else None,
        created_at=start, updated_at=t,
    )
    session.add(run)
    session.flush()
    for call in calls:
        call.run_id = run.id
    session.add_all(calls)

    if steps > 1 and kind in ("schedule", "email"):
        session.add(ToolCall(run_id=run.id, tool_name="find_contact", arguments_json={"name": text.split()[1]},
                             result_json={"contacts": []}, status="success", latency_ms=rng.randint(3, 25),
                             created_at=start))
    done = t + dt.timedelta(seconds=rng.randint(5, 90))
    if outcome in ("executed", "failed") and tool is not None:
        ok = outcome == "executed"
        session.add(ToolCall(
            run_id=run.id, tool_name=tool, idempotency_key=f"demo:{run.id}:{tool}",
            arguments_json={k: v for k, v in action.items() if k != "type"},
            result_json={"ok": True} if ok else {"error": run.error},
            status="success" if ok else "error", latency_ms=rng.randint(300, 1800), created_at=done,
        ))
        session.add(AuditEvent(event_type="action_confirmed", actor=f"user:{user.id}",
                               payload_json={"run_id": run.id}, created_at=done))
        if kind == "schedule" and ok:
            session.add(CalendarEvent(run_id=run.id, provider_event_id=f"demo{run.id}",
                                      date=dt.date.fromisoformat(action["date"]),
                                      time=dt.time.fromisoformat(action["time"]), duration_min=action["duration_min"],
                                      title=action["title"], guests=action["guests"], created_at=done))
        if kind == "email":
            session.add(Email(run_id=run.id, to=action["to"], subject=action["subject"], body=action["body"],
                              status="sent" if ok else "failed", provider_message_id=f"demo-{run.id}" if ok else None,
                              created_at=done))
    elif outcome == "cancelled" and action is not None:
        session.add(AuditEvent(event_type="action_cancelled", actor=f"user:{user.id}",
                               payload_json={"run_id": run.id}, created_at=done))
    elif outcome == "edited" and action is not None:
        session.add(AuditEvent(event_type="action_edit_requested", actor=f"user:{user.id}",
                               payload_json={"run_id": run.id}, created_at=done))

    if rng.random() < 0.35 and status in (RunStatus.executed, RunStatus.clarification, RunStatus.failed):
        good = 0.9 if status == RunStatus.executed else 0.45
        session.add(RunFeedback(run_id=run.id, user_id=user.id, rating=1 if rng.random() < good else -1,
                                created_at=done))


def remove(session: Session) -> int:
    """Delete the demo user and everything it owns (runs cascade to their rows)."""
    user = session.scalar(select(User).where(User.telegram_user_id == DEMO_TELEGRAM_ID))
    if user is None:
        return 0
    run_ids = select(AgentRun.id).where(AgentRun.user_id == user.id)
    session.execute(delete(LlmCall).where(LlmCall.run_id.in_(run_ids)))
    session.execute(delete(LlmCall).where(LlmCall.demo.is_(True)))
    count = session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.user_id == user.id))
    session.execute(delete(AgentRun).where(AgentRun.user_id == user.id))
    session.delete(user)
    session.commit()
    return count


def main(argv: list[str] | None = None) -> int:
    from app.db.session import session_scope

    parser = argparse.ArgumentParser(prog="python -m app.db.demo", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--remove", action="store_true", help="delete the demo data instead of adding it")
    args = parser.parse_args(argv)
    with session_scope() as session:
        if args.remove:
            print(f"demo: removed {remove(session)} demo run(s)")
        else:
            runs = seed(session)
            print(f"demo: added {runs} run(s) over {DAYS} days" if runs else "demo: already present, nothing added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
