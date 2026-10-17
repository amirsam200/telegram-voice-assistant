"""Learning after an executed action. Facts are saved through the tools only, so they are logged."""

import logging
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.actions import Action, ScheduleAction
from app.db.models import AgentRun, CalendarEvent, MemoryFact
from app.tools.registry import ToolContext, ToolError, execute

logger = logging.getLogger(__name__)

USUAL_LENGTH_KEY = "usual_meeting_length_min"
RECENT_MEETINGS = 10


def _usual_meeting_length(session: Session, user_id: int) -> int | None:
    durations = session.scalars(
        select(CalendarEvent.duration_min)
        .join(AgentRun, AgentRun.id == CalendarEvent.run_id)
        .where(AgentRun.user_id == user_id)
        .order_by(CalendarEvent.id.desc())
        .limit(RECENT_MEETINGS)
    ).all()
    if not durations:
        return None
    # Most common recent length; ties go to the most recent.
    counts = Counter(durations)
    best = max(counts.values())
    return next(d for d in durations if counts[d] == best)


def learn_from_executed(session: Session, action: Action, ctx: ToolContext) -> None:
    """Save a new contact and the usual meeting length. Never fails the action itself."""
    try:
        for contact in action.new_contacts:
            execute(ctx, "save_contact", {"name": contact.name, "email": str(contact.email)})

        if isinstance(action, ScheduleAction):
            usual = _usual_meeting_length(session, ctx.user_id)
            current = session.scalar(
                select(MemoryFact.value).where(MemoryFact.user_id == ctx.user_id, MemoryFact.key == USUAL_LENGTH_KEY)
            )
            if usual is not None and current != str(usual):
                execute(ctx, "remember", {"key": USUAL_LENGTH_KEY, "value": str(usual)})
    except ToolError:
        logger.exception("Could not save memory after run %s", ctx.run_id)
