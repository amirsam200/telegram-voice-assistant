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

## Google Calendar setup (one time)

Events are created in the owner's Google Calendar with OAuth 2.0 (scope `calendar.events`).

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project and enable the
   **Google Calendar API**.
2. **OAuth consent screen**: user type *External*, fill in the app name, and add your own Google
   account under **Test users**.
3. **Credentials → Create credentials → OAuth client ID → Desktop app**. Download the JSON and save it
   as `credentials.json` in the project root (it is git-ignored; on Windows check it is not
   `credentials.json.json`).
4. Run the one-time auth script. It prints a Google URL (and opens it when a browser is available);
   sign in, allow access, and the script writes `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and
   `GOOGLE_REFRESH_TOKEN` into `.env`. Leave out `--write-env` to print the lines instead.

   ```bash
   docker compose run --rm -p 127.0.0.1:8765:8765 -v ./credentials.json:/app/credentials.json:ro -v ./.env:/app/.env migrate python scripts/google_auth.py --write-env
   ```
5. Set `GOOGLE_CALENDAR_ID` (`primary` for your main calendar) and restart: `docker compose up -d --force-recreate`.

Events use the Asia/Tehran time zone, include attendees, and are created with `sendUpdates=all`, so
guests get Google's invitation email.

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
