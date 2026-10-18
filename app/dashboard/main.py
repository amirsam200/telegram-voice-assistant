"""Token and quality dashboard (read-only): FastAPI + Jinja2 + Chart.js, owner sign-in only.

Runs as its own service on 127.0.0.1:8080, behind the reverse proxy on DASHBOARD_HOST in production.
It connects with the read-only database role, and every page except /login and /healthz needs the
signed session cookie that the owner gets after entering the dashboard password.
"""

import contextlib
import csv
import datetime as dt
import io
import logging
import secrets
import time
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from app import budget
from app.agent import fa
from app.bot.limits import RateLimiter
from app.config import get_settings
from app.dashboard import queries
from app.dashboard.auth import verify_password
from app.dashboard.formatting import FILTERS
from app.logging_setup import configure_logging

logger = logging.getLogger(__name__)

SESSION_MAX_AGE_S = 12 * 3600
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters.update(FILTERS)

STATUS_FA = {"executed": "انجام شد", "pending_confirmation": "منتظر تأیید", "confirmed": "در حال انجام",
             "rejected": "رد شد", "failed": "ناموفق", "clarification": "پرسش", "running": "در حال اجرا"}
INTENT_FA = {"schedule": "جلسه", "email": "ایمیل", "contact": "مخاطب", "unknown": "نامشخص"}
DECISION_FA = {"confirmed": "تأیید", "edited": "ویرایش", "cancelled": "لغو"}
PURPOSE_FA = {"intent": "تشخیص درخواست", "tool_step": "گام ابزار", "stt_fix": "اصلاح گفتار", "review": "بازبینی"}
templates.env.globals.update(STATUS_FA=STATUS_FA, INTENT_FA=INTENT_FA, DECISION_FA=DECISION_FA,
                             PURPOSE_FA=PURPOSE_FA)


# --- database (read-only) -------------------------------------------------------------------------


@lru_cache
def _sessionmaker() -> sessionmaker:
    s = get_settings()
    url = s.dashboard_database_url
    if not url:
        logger.warning("DASHBOARD_DATABASE_URL is not set; using DATABASE_URL in read-only transactions")
        url = s.database_url
    # Read-only twice over: the role can only SELECT, and every transaction is read-only.
    engine = create_engine(url, pool_pre_ping=True,
                           connect_args={"options": "-c default_transaction_read_only=on"})
    return sessionmaker(bind=engine)


def get_db() -> Iterator[Session]:
    session = _sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def get_now() -> dt.datetime:
    return fa.now_tehran()


DB = Annotated[Session, Depends(get_db)]
Now = Annotated[dt.datetime, Depends(get_now)]


# --- sign-in --------------------------------------------------------------------------------------


class NotSignedIn(Exception):
    pass


def require_owner(request: Request) -> None:
    signed_in_at = request.session.get("owner_since")
    if not isinstance(signed_in_at, (int, float)) or time.time() - signed_in_at > SESSION_MAX_AGE_S:
        raise NotSignedIn


Owner = Depends(require_owner)
_login_limiter = RateLimiter(5, window_s=300)


def _include_demo(demo: str | None) -> bool:
    return demo != "0"


# --- app ------------------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    configure_logging(logging.DEBUG if get_settings().debug else logging.INFO)
    if not get_settings().dashboard_password_hash:
        logger.warning("DASHBOARD_PASSWORD_HASH is not set: nobody can sign in")
    yield


def create_app() -> FastAPI:
    s = get_settings()
    secret = s.dashboard_secret_key
    if not secret:
        secret = secrets.token_urlsafe(32)
        logger.warning("DASHBOARD_SECRET_KEY is not set; sessions end when the dashboard restarts")
    app = FastAPI(title="Voice assistant usage", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="dashboard_session",
                       max_age=SESSION_MAX_AGE_S, same_site="strict", https_only=s.dashboard_cookie_secure)

    @app.exception_handler(NotSignedIn)
    async def _to_login(request: Request, exc: NotSignedIn):
        if request.url.path.endswith(".csv") or request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"detail": "sign in required"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        return templates.TemplateResponse(request, "login.html", {"next": next, "error": None})

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, password: Annotated[str, Form()], next: Annotated[str, Form()] = "/"):
        client = request.client.host if request.client else "unknown"
        wait_s = _login_limiter.hit(client)
        stored = get_settings().dashboard_password_hash
        if wait_s is not None:
            error = "تلاش‌های ورود زیاد بود. چند دقیقه دیگر دوباره امتحان کنید."
        elif not stored:
            error = "رمز داشبورد هنوز تنظیم نشده است (DASHBOARD_PASSWORD_HASH)."
        elif verify_password(password, stored):
            request.session.clear()  # a fresh session after sign-in
            request.session["owner_since"] = time.time()
            logger.info("Dashboard sign-in from %s", client)
            target = next if next.startswith("/") and not next.startswith("//") else "/"
            return RedirectResponse(target, status_code=303)
        else:
            error = "رمز نادرست است."
        logger.warning("Dashboard sign-in refused from %s", client)
        return templates.TemplateResponse(request, "login.html", {"next": next, "error": error}, status_code=401)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse, dependencies=[Owner])
    def overview(request: Request, session: DB, now: Now, demo: str | None = None):
        include = _include_demo(demo)
        context = {
            "now": now, "demo": include,
            "kpi": queries.kpis(session, now, include),
            "budgets": budget.statuses(session, now),
            "hard_stop": get_settings().budget_hard_stop,
            "by_model": queries.usage_by_day(session, now, "model", include_demo=include),
            "by_purpose": queries.usage_by_day(session, now, "purpose", include_demo=include),
            "latency": queries.latency_by_day(session, now, include_demo=include),
            "rates": queries.rates_by_day(session, now, include_demo=include),
            "expensive": queries.expensive_runs(session, now, include_demo=include),
            "quality": queries.quality_by_model(session, now, include),
        }
        return templates.TemplateResponse(request, "index.html", context)

    def _log_params(date_from, date_to, model, status, intent):
        return {"date_from": date_from, "date_to": date_to, "model": model or None, "status": status or None,
                "intent": intent or None}

    @app.get("/logs", response_class=HTMLResponse, dependencies=[Owner])
    def logs(request: Request, session: DB, date_from: dt.date | None = Query(None, alias="from"),
             date_to: dt.date | None = Query(None, alias="to"), model: str | None = None,
             status: str | None = None, intent: str | None = None, demo: str | None = None,
             page: Annotated[int, Query(ge=1)] = 1):
        filters = _log_params(date_from, date_to, model, status, intent)
        per_page = 50
        rows, total = queries.logs(session, **filters, include_demo=_include_demo(demo),
                                   limit=per_page, offset=(page - 1) * per_page)
        query = request.url.query
        return templates.TemplateResponse(request, "logs.html", {
            "rows": rows, "total": total, "page": page, "pages": max(1, -(-total // per_page)),
            "filters": filters, "demo": _include_demo(demo), "options": queries.filter_options(session),
            "csv_url": f"/logs.csv?{query}" if query else "/logs.csv",
            "page_url": lambda p: "/logs?" + "&".join(
                [kv for kv in query.split("&") if kv and not kv.startswith("page=")] + [f"page={p}"]),
        })

    @app.get("/logs.csv", dependencies=[Owner])
    def logs_csv(session: DB, date_from: dt.date | None = Query(None, alias="from"),
                 date_to: dt.date | None = Query(None, alias="to"), model: str | None = None,
                 status: str | None = None, intent: str | None = None, demo: str | None = None):
        rows, _ = queries.logs(session, **_log_params(date_from, date_to, model, status, intent),
                               include_demo=_include_demo(demo), limit=None)
        out = io.StringIO()
        out.write("﻿")  # BOM so Excel reads the Persian text as UTF-8
        writer = csv.DictWriter(out, fieldnames=queries.LOG_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row | {"created_at": row["created_at"].isoformat()})
        return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="llm-runs.csv"'})

    @app.get("/runs/{run_id}", response_class=HTMLResponse, dependencies=[Owner])
    def run(request: Request, run_id: int, session: DB):
        detail = queries.run_detail(session, run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="run not found")
        return templates.TemplateResponse(request, "run.html", detail)

    return app


app = create_app()
