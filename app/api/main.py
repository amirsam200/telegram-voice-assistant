"""Admin API around the agent.

GET /health is public. Every other endpoint needs the ADMIN_API_KEY in the X-API-Key header; with
no key configured they all answer 401. OpenAPI docs are served only when DEBUG is on.
"""

import contextlib
import datetime as dt
import logging
import secrets
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.executor import retry_failed
from app.api import schemas
from app.api.health import DEFAULT_CHECKS, HealthChecker
from app.config import get_settings
from app.logging_setup import configure_logging, log_run
from app.db.models import (
    AgentRun,
    AuditEvent,
    CalendarEvent,
    Contact,
    Email,
    Intent,
    RunStatus,
    ToolCall,
    User,
    VoiceMessage,
)
from app.tools.registry import ToolContext

MAX_LIMIT = 100


# --- dependencies (overridden in tests) ----------------------------------------------------------


def get_db() -> Iterator[Session]:
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


_health = HealthChecker(DEFAULT_CHECKS)


def get_health_checker() -> HealthChecker:
    return _health


def get_tool_context(session: Annotated[Session, Depends(get_db)]) -> ToolContext:
    """Real Google Calendar and email provider; user_id is set from the run being retried."""
    return ToolContext(session=session, user_id=0)


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: Annotated[str | None, Security(_api_key_header)]) -> None:
    expected = get_settings().admin_api_key
    if not expected or not key or not secrets.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


DB = Annotated[Session, Depends(get_db)]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Offset = Annotated[int, Query(ge=0)]


def _page(session: Session, stmt, limit: int, offset: int, out: type) -> dict[str, Any]:
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = session.scalars(stmt.limit(limit).offset(offset)).all()
    return {"items": [out.model_validate(r) for r in rows], "total": total, "limit": limit, "offset": offset}


def _owner_id(session: Session) -> int:
    owner_tg = get_settings().owner_telegram_id
    owner = session.scalar(select(User).where(User.telegram_user_id == owner_tg)) if owner_tg else None
    if owner is None:
        raise HTTPException(status_code=409, detail="no owner user: set ALLOWED_USER_IDS and run the seed")
    return owner.id


# --- app -----------------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    configure_logging(logging.DEBUG if get_settings().debug else logging.INFO)
    yield


def create_app() -> FastAPI:
    debug = get_settings().debug
    app = FastAPI(
        title="Persian Voice Assistant API",
        lifespan=_lifespan,
        docs_url="/docs" if debug else None,
        redoc_url="/redoc" if debug else None,
        openapi_url="/openapi.json" if debug else None,
    )

    @app.get("/health", response_model=schemas.HealthOut)
    def health(checker: Annotated[HealthChecker, Depends(get_health_checker)]):
        checks = checker.run()
        ok = all(v == "ok" for v in checks.values())
        body = {"status": "ok" if ok else "degraded", "checks": checks}
        return body if ok else JSONResponse(status_code=503, content=body)

    admin = [Depends(require_api_key)]

    @app.get("/runs", response_model=schemas.Page[schemas.RunSummary], dependencies=admin)
    def list_runs(
        session: DB,
        status: RunStatus | None = None,
        intent: Intent | None = None,
        user_id: int | None = None,
        since: dt.datetime | None = Query(None, description="Created at or after (ISO 8601)."),
        until: dt.datetime | None = Query(None, description="Created before (ISO 8601)."),
        limit: Limit = 20,
        offset: Offset = 0,
    ):
        stmt = select(AgentRun).order_by(AgentRun.id.desc())
        if status is not None:
            stmt = stmt.where(AgentRun.status == status)
        if intent is not None:
            stmt = stmt.where(AgentRun.intent == intent)
        if user_id is not None:
            stmt = stmt.where(AgentRun.user_id == user_id)
        if since is not None:
            stmt = stmt.where(AgentRun.created_at >= since)
        if until is not None:
            stmt = stmt.where(AgentRun.created_at < until)
        return _page(session, stmt, limit, offset, schemas.RunSummary)

    @app.get("/runs/{run_id}", response_model=schemas.RunDetail, dependencies=admin)
    def get_run(run_id: int, session: DB):
        run = session.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        detail = schemas.RunDetail.model_validate(run)
        if run.voice_message_id is not None:
            voice = session.get(VoiceMessage, run.voice_message_id)
            detail.voice = schemas.VoiceOut.model_validate(voice) if voice is not None else None
        calls = session.scalars(select(ToolCall).where(ToolCall.run_id == run.id).order_by(ToolCall.id)).all()
        detail.tool_calls = [schemas.ToolCallOut.model_validate(c) for c in calls]
        return detail

    @app.post("/runs/{run_id}/retry", response_model=schemas.RetryOut, dependencies=admin)
    def retry_run(run_id: int, session: DB, ctx: Annotated[ToolContext, Depends(get_tool_context)]):
        run = session.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if run.status != RunStatus.failed or run.action_json is None:
            raise HTTPException(status_code=409, detail=f"only failed actions can be retried (status: {run.status.value})")
        with log_run(run_id):
            result = retry_failed(session, run_id, ctx, actor="api:admin")
        if result is None:  # someone else retried it first
            raise HTTPException(status_code=409, detail="run is no longer failed")
        session.refresh(run)
        return schemas.RetryOut(run_id=run_id, ok=result.ok, status=run.status.value, message=result.message)

    @app.get("/events", response_model=schemas.Page[schemas.EventOut], dependencies=admin)
    def list_events(
        session: DB,
        date_from: dt.date | None = Query(None, alias="from"),
        date_to: dt.date | None = Query(None, alias="to"),
        limit: Limit = 20,
        offset: Offset = 0,
    ):
        stmt = select(CalendarEvent).order_by(CalendarEvent.date.desc(), CalendarEvent.time.desc())
        if date_from is not None:
            stmt = stmt.where(CalendarEvent.date >= date_from)
        if date_to is not None:
            stmt = stmt.where(CalendarEvent.date <= date_to)
        return _page(session, stmt, limit, offset, schemas.EventOut)

    @app.get("/emails", response_model=schemas.Page[schemas.EmailOut], dependencies=admin)
    def list_emails(session: DB, status: str | None = None, to: str | None = None,
                    limit: Limit = 20, offset: Offset = 0):
        stmt = select(Email).order_by(Email.id.desc())
        if status is not None:
            stmt = stmt.where(Email.status == status)
        if to is not None:
            stmt = stmt.where(func.lower(Email.to) == to.lower())
        return _page(session, stmt, limit, offset, schemas.EmailOut)

    @app.get("/contacts", response_model=schemas.Page[schemas.ContactOut], dependencies=admin)
    def list_contacts(session: DB, q: str | None = Query(None, description="Part of a name or email."),
                      user_id: int | None = None, limit: Limit = 50, offset: Offset = 0):
        stmt = select(Contact).order_by(Contact.name)
        if user_id is not None:
            stmt = stmt.where(Contact.user_id == user_id)
        if q:
            like = f"%{q.lower()}%"
            stmt = stmt.where(func.lower(Contact.name).like(like) | func.lower(Contact.email).like(like))
        return _page(session, stmt, limit, offset, schemas.ContactOut)

    @app.post("/contacts", response_model=schemas.ContactOut, status_code=201, dependencies=admin)
    def create_contact(body: schemas.ContactIn, session: DB):
        user_id = body.user_id if body.user_id is not None else _owner_id(session)
        if session.get(User, user_id) is None:
            raise HTTPException(status_code=404, detail="user not found")
        email = str(body.email).lower()
        clash = session.scalar(select(Contact).where(
            Contact.user_id == user_id, (func.lower(Contact.name) == body.name.lower()) | (Contact.email == email)
        ))
        if clash is not None:
            raise HTTPException(status_code=409, detail=f"a contact with this name or email exists (id {clash.id})")
        contact = Contact(user_id=user_id, name=body.name, email=email, aliases=body.aliases)
        session.add(contact)
        session.flush()
        session.add(AuditEvent(event_type="contact_created", actor="api:admin",
                               payload_json={"contact_id": contact.id, "name": contact.name}))
        session.commit()
        return contact

    return app


app = create_app()
