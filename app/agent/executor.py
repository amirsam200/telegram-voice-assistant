"""Runs a proposed action only after the user confirms it. A run executes at most once."""

import logging
from dataclasses import dataclass

from pydantic import TypeAdapter
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.agent import fa
from app.agent.actions import Action, AddContactAction, DeleteContactAction, EmailAction, ScheduleAction
from app.agent.memory import learn_from_executed
from app.db.models import AgentRun, AuditEvent, CalendarEvent, Email, RunStatus
from app.tools.registry import ToolContext, ToolError, ToolInProgress, execute

logger = logging.getLogger(__name__)

_ACTION = TypeAdapter(Action)


@dataclass
class ExecutionResult:
    ok: bool
    message: str  # Persian, shown to the user


def load_action(run: AgentRun) -> Action:
    return _ACTION.validate_python(run.action_json)


def _claim(session: Session, run_id: int, user_id: int) -> bool:
    """Atomically move pending_confirmation -> confirmed. Only one caller can win."""
    claimed = session.execute(
        update(AgentRun)
        .where(AgentRun.id == run_id, AgentRun.user_id == user_id,
               AgentRun.status == RunStatus.pending_confirmation)
        .values(status=RunStatus.confirmed)
        .returning(AgentRun.id)
    ).scalar_one_or_none()
    session.commit()
    return claimed is not None


ALREADY = {
    RunStatus.confirmed: "این درخواست در حال انجام است.",
    RunStatus.executed: "این درخواست قبلاً انجام شده است.",
    RunStatus.rejected: "این درخواست لغو یا ویرایش شده است.",
    RunStatus.failed: "این درخواست قبلاً با خطا روبه‌رو شد. لطفاً دوباره درخواست بدهید.",
}


def confirm_and_execute(session: Session, run_id: int, user_id: int, ctx: ToolContext) -> ExecutionResult:
    if not _claim(session, run_id, user_id):
        run = session.get(AgentRun, run_id)
        if run is None or run.user_id != user_id:
            return ExecutionResult(False, "این درخواست پیدا نشد.")
        return ExecutionResult(False, ALREADY.get(run.status, "این درخواست قابل انجام نیست."))

    run = session.get(AgentRun, run_id)
    session.refresh(run)
    action = load_action(run)
    ctx.run_id = run.id
    actor = f"user:{user_id}"
    session.add(AuditEvent(event_type="action_confirmed", actor=actor, payload_json={"run_id": run.id}))
    session.commit()

    try:
        if isinstance(action, ScheduleAction):
            message = _create_event(session, run, action, ctx)
        elif isinstance(action, EmailAction):
            message = _send_email(session, run, action, ctx)
        elif isinstance(action, AddContactAction):
            message = _add_contact(run, action, ctx)
        else:
            message = _delete_contact(run, action, ctx)
    except ToolInProgress:
        return ExecutionResult(False, ALREADY[RunStatus.confirmed])
    except ToolError as exc:
        run = session.get(AgentRun, run_id)
        run.status = RunStatus.failed
        run.error = str(exc)
        session.add(AuditEvent(event_type="action_failed", actor=actor,
                               payload_json={"run_id": run.id, "error": str(exc)}))
        session.commit()
        return ExecutionResult(False, _error_message(action, str(exc)))

    run.status = RunStatus.executed
    session.add(AuditEvent(event_type="action_executed", actor=actor, payload_json={"run_id": run.id}))
    session.commit()
    learn_from_executed(session, action, ctx)
    return ExecutionResult(True, message)


def _create_event(session: Session, run: AgentRun, action: ScheduleAction, ctx: ToolContext) -> str:
    args = action.model_dump(mode="json", include={"date", "time", "duration_min", "title", "description", "guests"})
    result = execute(ctx, "create_calendar_event", args, idempotency_key=f"run:{run.id}:create_calendar_event")
    session.add(CalendarEvent(
        run_id=run.id, provider_event_id=result.get("event_id"), date=action.date, time=action.time,
        duration_min=action.duration_min, title=action.title, description=action.description,
        guests=[str(g) for g in action.guests],
    ))
    text = f"✅ جلسه «{action.title}» در تقویم ثبت شد."
    if action.guests:
        text += " دعوت‌نامه برای مهمان‌ها فرستاده شد."
    if result.get("html_link"):
        text += f"\n{result['html_link']}"
    return text


def _send_email(session: Session, run: AgentRun, action: EmailAction, ctx: ToolContext) -> str:
    args = action.model_dump(mode="json", include={"to", "subject", "body"})
    email = Email(run_id=run.id, to=str(action.to), subject=action.subject, body=action.body, status="sending")
    session.add(email)
    session.commit()
    try:
        result = execute(ctx, "send_email", args, idempotency_key=f"run:{run.id}:send_email")
    except ToolError:
        email = session.merge(email)
        email.status = "failed"
        session.commit()
        raise
    email.provider_message_id = result.get("message_id")
    email.status = "sent"
    return f"✅ ایمیل به {action.to} فرستاده شد."


def _add_contact(run: AgentRun, action: AddContactAction, ctx: ToolContext) -> str:
    args = {"name": action.name, "email": str(action.email)}
    result = execute(ctx, "add_contact", args, idempotency_key=f"run:{run.id}:add_contact")
    if result.get("created"):
        return f"✅ مخاطب «{action.name}» با ایمیل {action.email} ذخیره شد."
    return f"✅ ایمیل مخاطب «{action.name}» به {action.email} تغییر کرد."


def _delete_contact(run: AgentRun, action: DeleteContactAction, ctx: ToolContext) -> str:
    execute(ctx, "delete_contact", {"contact_id": action.contact_id},
            idempotency_key=f"run:{run.id}:delete_contact")
    return f"✅ مخاطب «{action.name}» حذف شد."


def _error_message(action: Action, error: str) -> str:
    if isinstance(action, ScheduleAction):
        if "not configured" in error:
            return "❌ تقویم گوگل هنوز تنظیم نشده است، بنابراین جلسه ثبت نشد."
        return "❌ ثبت جلسه در تقویم گوگل ناموفق بود. لطفاً کمی بعد دوباره تلاش کنید."
    if isinstance(action, AddContactAction):
        return "❌ ذخیرهٔ مخاطب ناموفق بود. لطفاً دوباره تلاش کنید."
    if isinstance(action, DeleteContactAction):
        if "not found" in error:
            return "❌ این مخاطب پیدا نشد؛ شاید قبلاً حذف شده باشد."
        return "❌ حذف مخاطب ناموفق بود. لطفاً دوباره تلاش کنید."
    if "not configured" in error or "scope" in error:
        return "❌ ارسال ایمیل از Gmail هنوز تنظیم نشده است. ورود به حساب گوگل را دوباره انجام دهید."
    return "❌ ارسال ایمیل ناموفق بود. لطفاً کمی بعد دوباره تلاش کنید."


def cancel(session: Session, run_id: int, user_id: int) -> bool:
    cancelled = session.execute(
        update(AgentRun)
        .where(AgentRun.id == run_id, AgentRun.user_id == user_id,
               AgentRun.status == RunStatus.pending_confirmation)
        .values(status=RunStatus.rejected)
        .returning(AgentRun.id)
    ).scalar_one_or_none()
    if cancelled:
        session.add(AuditEvent(event_type="action_cancelled", actor=f"user:{user_id}",
                               payload_json={"run_id": run_id}))
    session.commit()
    return cancelled is not None


# --- preview -----------------------------------------------------------------------------------


def preview(action: Action) -> str:
    """A readable Persian summary of the proposed action."""
    if isinstance(action, ScheduleAction):
        start = action.time.strftime("%H:%M")
        end_minutes = action.time.hour * 60 + action.time.minute + action.duration_min
        end = f"{(end_minutes // 60) % 24:02d}:{end_minutes % 60:02d}"
        lines = [
            "📅 ثبت جلسه در تقویم",
            f"عنوان: {action.title}",
            f"تاریخ: {fa.format_date_fa(action.date)}",
            f"ساعت: {fa.to_fa_digits(start)} تا {fa.to_fa_digits(end)} "
            f"({fa.to_fa_digits(action.duration_min)} دقیقه)",
        ]
        if action.guests:
            lines.append("مهمان‌ها: " + "، ".join(str(g) for g in action.guests))
        if action.description:
            lines.append(f"توضیحات: {action.description}")
    elif isinstance(action, AddContactAction):
        if action.previous_email:
            lines = [
                "👤 تغییر ایمیل مخاطب",
                f"نام: {action.name}",
                f"ایمیل فعلی: {action.previous_email}",
                f"ایمیل جدید: {action.email}",
            ]
        else:
            lines = ["👤 افزودن مخاطب", f"نام: {action.name}", f"ایمیل: {action.email}"]
    elif isinstance(action, DeleteContactAction):
        lines = ["🗑️ حذف مخاطب", f"نام: {action.name}", f"ایمیل: {action.email or 'بدون ایمیل'}"]
    else:
        lines = [
            "✉️ ارسال ایمیل",
            f"به: {action.to}",
            f"موضوع: {action.subject}",
            "متن:",
            action.body,
        ]
    lines += ["", "آیا تأیید می‌کنید؟"]
    return "\n".join(lines)
