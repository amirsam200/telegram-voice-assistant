"""Aggregation queries for the dashboard (read-only).

Definitions
- Tokens: prompt + completion tokens of llm_calls (failed attempts have none).
- Cost: sum of llm_calls.cost_usd.
- Latency: P50 / P95 of successful llm_calls.latency_ms.
- Decision of a run (from existing data): confirmed (executed, failed or confirmed), cancelled
  (rejected with an action_cancelled audit event), edited (any other rejected run).
- Proposed runs: runs with an action that the user decided on (not running, not still pending).
- Confirmation rate: confirmed / proposed.   Edit rate: edited / proposed.
- Success rate: executed / (executed + failed), i.e. confirmed actions whose tools succeeded.
- JSON-valid rate: runs with no rejected final answer (validation_errors = 0) / runs.
- Thumbs-up share: thumbs up / all feedback.
Rates are fractions 0..1, or None when there is nothing to divide by. Days are Asia/Tehran days.
Demo data (the demo user, llm_calls.demo) is included unless include_demo is False.
"""

import datetime as dt
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.fa import TEHRAN
from app.db.demo import DEMO_TELEGRAM_ID

TZ = "Asia/Tehran"

DECISION = """
    CASE
      WHEN r.status IN ('executed', 'failed', 'confirmed') THEN 'confirmed'
      WHEN r.status = 'rejected' AND EXISTS (
        SELECT 1 FROM audit_events a
        WHERE a.event_type = 'action_cancelled' AND a.payload_json->>'run_id' = r.id::text
      ) THEN 'cancelled'
      WHEN r.status = 'rejected' THEN 'edited'
    END
"""
RUN_COST = "(SELECT coalesce(sum(c.cost_usd), 0) FROM llm_calls c WHERE c.run_id = r.id)"
RUN_TOKENS = "(SELECT coalesce(sum(c.prompt_tokens + c.completion_tokens), 0) FROM llm_calls c WHERE c.run_id = r.id)"
RUNS_DEMO = "(:demo OR u.telegram_user_id IS NULL OR u.telegram_user_id <> :demo_id)"
CALLS_DEMO = "(:demo OR NOT c.demo)"


def _ratio(num: Any, den: Any) -> float | None:
    return float(num) / float(den) if den else None


def _params(include_demo: bool, **extra: Any) -> dict[str, Any]:
    return {"demo": include_demo, "demo_id": DEMO_TELEGRAM_ID, "tz": TZ, **extra}


def day_start(now: dt.datetime) -> dt.datetime:
    local = now.astimezone(TEHRAN)
    return dt.datetime.combine(local.date(), dt.time(0, 0), tzinfo=TEHRAN)


def days(now: dt.datetime, count: int) -> list[dt.date]:
    today = now.astimezone(TEHRAN).date()
    return [today - dt.timedelta(days=i) for i in range(count - 1, -1, -1)]


# --- KPIs -----------------------------------------------------------------------------------------


def kpis(session: Session, now: dt.datetime, include_demo: bool = True) -> dict[str, Any]:
    p = _params(include_demo, today=day_start(now), d7=now - dt.timedelta(days=7), d30=now - dt.timedelta(days=30))
    usage = session.execute(text(f"""
        SELECT
          coalesce(sum(c.prompt_tokens + c.completion_tokens) FILTER (WHERE c.created_at >= :today), 0),
          coalesce(sum(c.prompt_tokens + c.completion_tokens) FILTER (WHERE c.created_at >= :d7), 0),
          coalesce(sum(c.prompt_tokens + c.completion_tokens) FILTER (WHERE c.created_at >= :d30), 0),
          coalesce(sum(c.cost_usd) FILTER (WHERE c.created_at >= :today), 0),
          coalesce(sum(c.cost_usd) FILTER (WHERE c.created_at >= :d30), 0),
          percentile_cont(0.5) WITHIN GROUP (ORDER BY c.latency_ms) FILTER (WHERE c.status = 'ok'),
          percentile_cont(0.95) WITHIN GROUP (ORDER BY c.latency_ms) FILTER (WHERE c.status = 'ok'),
          count(*) FILTER (WHERE c.created_at >= :d30)
        FROM llm_calls c
        WHERE c.created_at >= :d30 AND {CALLS_DEMO}
    """), p).one()
    runs = session.execute(text(f"""
        SELECT
          count(*) FILTER (WHERE r.status = 'executed'),
          count(*) FILTER (WHERE r.status IN ('executed', 'failed')),
          count(*) FILTER (WHERE d.decision = 'confirmed'),
          count(*) FILTER (WHERE d.decision = 'edited'),
          count(*) FILTER (WHERE d.decision IS NOT NULL),
          count(*)
        FROM agent_runs r
        LEFT JOIN users u ON u.id = r.user_id
        CROSS JOIN LATERAL (SELECT {DECISION} AS decision) d
        WHERE r.created_at >= :d30 AND {RUNS_DEMO}
    """), p).one()
    executed, finished, confirmed, edited, proposed, total = runs
    return {
        "tokens_today": int(usage[0]), "tokens_7d": int(usage[1]), "tokens_30d": int(usage[2]),
        "cost_today": float(usage[3]), "cost_30d": float(usage[4]),
        "latency_p50": usage[5], "latency_p95": usage[6], "llm_calls_30d": int(usage[7]),
        "runs_30d": int(total),
        "success_rate": _ratio(executed, finished),
        "confirmation_rate": _ratio(confirmed, proposed),
        "edit_rate": _ratio(edited, proposed),
    }


# --- time series ----------------------------------------------------------------------------------


def usage_by_day(session: Session, now: dt.datetime, group: str = "model", count: int = 30,
                 include_demo: bool = True) -> dict[str, Any]:
    """Tokens and cost per Tehran day, split by model or purpose."""
    if group not in ("model", "purpose"):
        raise ValueError("group must be model or purpose")
    labels = days(now, count)
    rows = session.execute(text(f"""
        SELECT (c.created_at AT TIME ZONE :tz)::date AS day, c.{group} AS key,
               sum(c.prompt_tokens + c.completion_tokens) AS tokens, coalesce(sum(c.cost_usd), 0) AS cost
        FROM llm_calls c
        WHERE c.created_at >= :start AND {CALLS_DEMO}
        GROUP BY 1, 2 ORDER BY 2, 1
    """), _params(include_demo, start=dt.datetime.combine(labels[0], dt.time(0), tzinfo=TEHRAN))).all()
    series: dict[str, dict[str, list]] = {}
    index = {d: i for i, d in enumerate(labels)}
    for day, key, tokens, cost in rows:
        s = series.setdefault(key, {"tokens": [0] * len(labels), "cost": [0.0] * len(labels)})
        s["tokens"][index[day]] = int(tokens)
        s["cost"][index[day]] = round(float(cost), 6)
    return {"days": [d.isoformat() for d in labels], "series": series}


def latency_by_day(session: Session, now: dt.datetime, count: int = 30, include_demo: bool = True) -> dict:
    labels = days(now, count)
    rows = session.execute(text(f"""
        SELECT (c.created_at AT TIME ZONE :tz)::date,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY c.latency_ms),
               percentile_cont(0.95) WITHIN GROUP (ORDER BY c.latency_ms)
        FROM llm_calls c
        WHERE c.status = 'ok' AND c.created_at >= :start AND {CALLS_DEMO}
        GROUP BY 1
    """), _params(include_demo, start=dt.datetime.combine(labels[0], dt.time(0), tzinfo=TEHRAN))).all()
    by_day = {day: (p50, p95) for day, p50, p95 in rows}
    return {
        "days": [d.isoformat() for d in labels],
        "p50": [round(by_day[d][0]) if d in by_day else None for d in labels],
        "p95": [round(by_day[d][1]) if d in by_day else None for d in labels],
    }


def rates_by_day(session: Session, now: dt.datetime, count: int = 30, include_demo: bool = True) -> dict:
    labels = days(now, count)
    rows = session.execute(text(f"""
        SELECT (r.created_at AT TIME ZONE :tz)::date,
               count(*) FILTER (WHERE r.status = 'executed'),
               count(*) FILTER (WHERE r.status IN ('executed', 'failed')),
               count(*) FILTER (WHERE d.decision = 'edited'),
               count(*) FILTER (WHERE d.decision IS NOT NULL)
        FROM agent_runs r
        LEFT JOIN users u ON u.id = r.user_id
        CROSS JOIN LATERAL (SELECT {DECISION} AS decision) d
        WHERE r.created_at >= :start AND {RUNS_DEMO}
        GROUP BY 1
    """), _params(include_demo, start=dt.datetime.combine(labels[0], dt.time(0), tzinfo=TEHRAN))).all()
    by_day = {row[0]: row[1:] for row in rows}

    def rate(d, num, den):
        values = by_day.get(d)
        return None if values is None or not values[den] else round(values[num] / values[den], 4)

    return {
        "days": [d.isoformat() for d in labels],
        "success": [rate(d, 0, 1) for d in labels],
        "edit": [rate(d, 2, 3) for d in labels],
    }


def expensive_runs(session: Session, now: dt.datetime, limit: int = 10, include_demo: bool = True) -> list[dict]:
    rows = session.execute(text(f"""
        SELECT r.id, r.created_at, r.model, r.intent, r.status, {RUN_TOKENS} AS tokens, {RUN_COST} AS cost,
               left(r.input_text, 80) AS excerpt
        FROM agent_runs r LEFT JOIN users u ON u.id = r.user_id
        WHERE r.created_at >= :d30 AND {RUNS_DEMO}
        ORDER BY cost DESC, r.id DESC LIMIT :limit
    """), _params(include_demo, d30=now - dt.timedelta(days=30), limit=limit)).mappings().all()
    return [dict(r) | {"cost": float(r["cost"])} for r in rows]


# --- quality per model ----------------------------------------------------------------------------


def quality_by_model(session: Session, now: dt.datetime, include_demo: bool = True) -> list[dict]:
    rows = session.execute(text(f"""
        SELECT r.model,
               count(*) AS runs,
               count(*) FILTER (WHERE r.status = 'executed') AS executed,
               count(*) FILTER (WHERE r.status IN ('executed', 'failed')) AS finished,
               count(*) FILTER (WHERE r.validation_errors = 0) AS json_valid,
               count(*) FILTER (WHERE d.decision = 'edited') AS edited,
               count(*) FILTER (WHERE d.decision IS NOT NULL) AS proposed,
               avg(r.steps) AS avg_steps,
               avg({RUN_COST}) AS avg_cost,
               count(f.id) FILTER (WHERE f.rating > 0) AS thumbs_up,
               count(f.id) AS feedback
        FROM agent_runs r
        LEFT JOIN users u ON u.id = r.user_id
        LEFT JOIN run_feedback f ON f.run_id = r.id
        CROSS JOIN LATERAL (SELECT {DECISION} AS decision) d
        WHERE r.created_at >= :d30 AND r.model IS NOT NULL AND {RUNS_DEMO}
        GROUP BY r.model ORDER BY runs DESC
    """), _params(include_demo, d30=now - dt.timedelta(days=30))).mappings().all()
    return [{
        "model": r["model"], "runs": r["runs"],
        "success_rate": _ratio(r["executed"], r["finished"]),
        "json_valid_rate": _ratio(r["json_valid"], r["runs"]),
        "edit_rate": _ratio(r["edited"], r["proposed"]),
        "avg_steps": float(r["avg_steps"]) if r["avg_steps"] is not None else None,
        "avg_cost": float(r["avg_cost"] or 0),
        "thumbs_up_share": _ratio(r["thumbs_up"], r["feedback"]),
        "feedback": r["feedback"],
    } for r in rows]


# --- logs and run detail --------------------------------------------------------------------------

LOG_COLUMNS = ["id", "created_at", "model", "intent", "status", "decision", "tokens", "cost", "latency_ms",
               "steps", "validation_errors", "excerpt", "error"]


def _log_filters(date_from: dt.date | None, date_to: dt.date | None, model: str | None, status: str | None,
                 intent: str | None) -> tuple[str, dict[str, Any]]:
    clauses, params = [], {}
    if date_from:
        clauses.append("r.created_at >= :date_from")
        params["date_from"] = dt.datetime.combine(date_from, dt.time(0), tzinfo=TEHRAN)
    if date_to:
        clauses.append("r.created_at < :date_to")
        params["date_to"] = dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time(0), tzinfo=TEHRAN)
    if model:
        clauses.append("r.model = :model")
        params["model"] = model
    if status:
        clauses.append("r.status::text = :status")
        params["status"] = status
    if intent:
        clauses.append("r.intent::text = :intent")
        params["intent"] = intent
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def logs(session: Session, *, date_from: dt.date | None = None, date_to: dt.date | None = None,
         model: str | None = None, status: str | None = None, intent: str | None = None,
         include_demo: bool = True, limit: int | None = 50, offset: int = 0) -> tuple[list[dict], int]:
    """Runs newest first with tokens, cost and decision; transcripts cut to 80 characters."""
    where, extra = _log_filters(date_from, date_to, model, status, intent)
    params = _params(include_demo, **extra)
    base = f"FROM agent_runs r LEFT JOIN users u ON u.id = r.user_id WHERE {RUNS_DEMO}{where}"
    total = session.scalar(text(f"SELECT count(*) {base}"), params)
    page = "" if limit is None else " LIMIT :limit OFFSET :offset"
    rows = session.execute(text(f"""
        SELECT r.id, r.created_at, r.model, r.intent::text AS intent, r.status::text AS status,
               {DECISION} AS decision, {RUN_TOKENS} AS tokens, {RUN_COST} AS cost, r.latency_ms, r.steps,
               r.validation_errors, left(r.input_text, 80) AS excerpt, left(r.error, 200) AS error
        {base} ORDER BY r.id DESC{page}
    """), params | {"limit": limit, "offset": offset}).mappings().all()
    return [dict(r) | {"cost": float(r["cost"])} for r in rows], int(total)


def filter_options(session: Session) -> dict[str, list[str]]:
    models = session.scalars(text("SELECT DISTINCT model FROM agent_runs WHERE model IS NOT NULL ORDER BY 1")).all()
    return {
        "models": list(models),
        "statuses": ["executed", "pending_confirmation", "confirmed", "rejected", "failed", "clarification",
                     "running"],
        "intents": ["schedule", "email", "contact", "unknown"],
    }


def run_detail(session: Session, run_id: int) -> dict | None:
    """Everything about one run, including the full action (email body) — shown only on request."""
    run = session.execute(text(f"""
        SELECT r.*, r.intent::text AS intent, r.status::text AS status, {DECISION} AS decision,
               {RUN_TOKENS} AS total_tokens, {RUN_COST} AS cost,
               v.duration_s AS voice_duration_s, v.stt_confidence, f.rating AS feedback
        FROM agent_runs r
        LEFT JOIN voice_messages v ON v.id = r.voice_message_id
        LEFT JOIN run_feedback f ON f.run_id = r.id
        WHERE r.id = :id
    """), {"id": run_id}).mappings().one_or_none()
    if run is None:
        return None
    tool_calls = session.execute(text("""
        SELECT id, tool_name, status, latency_ms, arguments_json, result_json, created_at
        FROM tool_calls WHERE run_id = :id ORDER BY id
    """), {"id": run_id}).mappings().all()
    llm_calls = session.execute(text("""
        SELECT id, model, purpose, status, prompt_tokens, completion_tokens, cached_tokens, cost_usd,
               latency_ms, error, created_at
        FROM llm_calls WHERE run_id = :id ORDER BY id
    """), {"id": run_id}).mappings().all()
    return {"run": dict(run) | {"cost": float(run["cost"])},
            "tool_calls": [dict(t) for t in tool_calls], "llm_calls": [dict(c) for c in llm_calls]}
