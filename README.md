# Persian Voice Assistant

A personal Telegram assistant: send a Persian voice message, and it schedules a
calendar event or sends an email on your behalf after you confirm.
See [docs/SPEC.md](docs/SPEC.md) for the full spec.

Status: **01 Foundation** (git, Docker, Telegram bot, Resend, database).
The agent (speech to text, DeepSeek, tools) arrives in step 02.

## Setup

1. Install Docker Desktop and make sure the engine is running.
2. Copy `.env.example` to `.env` and fill in the values. `.env` is git-ignored; never commit it.
   - `TELEGRAM_BOT_TOKEN` from @BotFather, `ALLOWED_USER_IDS` from @userinfobot
     (comma-separated; the first ID is the owner).
   - `RESEND_API_KEY`, `EMAIL_FROM` (use `onboarding@resend.dev` until you verify a domain;
     it can then only send to your Resend account's email).
   - `OWNER_NAME`, `OWNER_EMAIL` for the seed script and the test email.
   - `DATABASE_URL` must use the same user, password and database as `POSTGRES_*`, with host `db`.

## Run

```bash
docker compose up --build
```

This starts `db` (PostgreSQL 16), runs `migrate` once (`alembic upgrade head` plus the seed
script), then starts `bot` (Telegram long polling) and `api` (FastAPI on
http://127.0.0.1:8000, health check at `/health`).

## Useful commands

Send one test email to `OWNER_EMAIL`:

```bash
docker compose run --rm migrate python -m app.cli send-test-email
```

Check stored voice messages:

```bash
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select id, user_id, duration_s, created_at from voice_messages"'
```

Re-run the seed (idempotent):

```bash
docker compose run --rm migrate python -m app.db.seed
```

Create a new migration after changing `app/db/models.py` (rebuild first so the image has the
new models; the bind mount writes the file back into `migrations/`):

```bash
docker compose run --rm --build -v ./migrations:/app/migrations migrate alembic revision --autogenerate -m "describe change"
```

## Layout

```
app/bot     Telegram bot          app/db      models, session, seed
app/tools   Resend email client   app/api     FastAPI
app/stt, app/agent, app/mcp       placeholders for later steps
migrations/ Alembic               docs/SPEC.md  spec
```
