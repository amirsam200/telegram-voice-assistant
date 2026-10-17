Set up the foundation of the Persian voice assistant described in docs/SPEC.md. Do not build the agent yet; this batch is the skeleton every later batch builds on.

## Repository and git
- Initialize a git repository with a Python .gitignore, .env.example, README.md and docs/SPEC.md.
- Commit after each step below with a clear message, for example chore: docker compose with postgres.

## Docker
- A Dockerfile for the app: python:3.12-slim, ffmpeg installed, a non-root user.
- docker-compose.yml with db (postgres:16, named volume, healthcheck), bot (Telegram worker) and api (FastAPI on 127.0.0.1:8000 only). All settings come from .env.
- One command starts everything: docker compose up --build.

## Telegram bot
- python-telegram-bot v21 with long polling.
- Only user IDs in ALLOWED_USER_IDS may use it; anyone else gets a short, polite refusal in Persian.
- /start and /help answer in Persian. Voice messages are downloaded, saved, and acknowledged; text messages are echoed for now.

## Resend
- A small email client module for the Resend API (RESEND_API_KEY, EMAIL_FROM).
- A CLI command that sends one test email to the owner, to prove the key works.

## Database: PostgreSQL, SQLAlchemy 2.x, Alembic
- users: telegram_user_id, name, email, timezone (default Asia/Tehran).
- contacts: user_id, name, aliases (Persian and English spellings), email.
- voice_messages: user_id, telegram_file_id, duration_s, transcript, stt_confidence.
- agent_runs: voice_message_id, intent (schedule, email, unknown), action_json, status (pending_confirmation, confirmed, executed, rejected, failed), model, tokens, latency_ms, error.
- tool_calls: run_id, tool_name, arguments_json, result_json, status, idempotency_key.
- calendar_events: run_id, provider_event_id, date, time, duration_min, title, description, guests.
- emails: run_id, to, subject, body, provider_message_id, status.
- memory_facts: user_id, key, value, source. audit_events: event_type, actor, payload_json.
- Timestamps on every table, the first Alembic migration, and a seed script for the owner and two sample contacts.

## Acceptance criteria
docker compose up starts db, bot and api and runs alembic upgrade head; /start answers in Persian; a voice message is stored in voice_messages; the test email arrives; no secret is committed and .env.example lists every variable. Report what you built, the commands to run, and the commits you made.
