# Persian Voice Assistant

A personal Telegram assistant: send a Persian voice message, and it schedules a
calendar event or sends an email on your behalf after you confirm.
See [docs/SPEC.md](docs/SPEC.md) for the full spec.

Status: **02 Agent** (speech to text, DeepSeek agent, tools, confirmation, memory) on top of
01 Foundation (git, Docker, Telegram bot, Resend, database).

## How a message is handled

1. **Speech to text**: the voice note is converted with ffmpeg to 16 kHz mono WAV and transcribed by
   Vosk (`vosk-model-small-fa-0.5`, bundled in the image). The transcript and confidence are stored in
   `voice_messages` and the audio is deleted. Empty or low-confidence transcripts
   (below `STT_MIN_CONFIDENCE`) get a Persian request to repeat. Text messages skip this step.
2. **Agent**: DeepSeek (any OpenAI-compatible API) with a versioned system prompt
   (`app/agent/prompts/system_v1.md`), at most 6 steps. The context holds the Tehran date and time with a
   Jalali day table, preferences from `memory_facts`, contacts named in the transcript and the last
   5 messages. While planning the model may only call read-only tools (`get_current_datetime`,
   `find_contact`, `list_calendar_events`). It must end with exactly one `ScheduleAction`,
   `EmailAction`, or a clarifying question in Persian. Proposals are sent back to the model when they
   use an email address that is not a contact or in the user's words, are in the past, or are not in Persian.
3. **Confirmation**: the bot shows a Persian preview with ✅ تأیید / ✏️ ویرایش / ❌ لغو. Only Confirm runs
   `create_calendar_event` or `send_email`. Each run executes at most once (atomic status change and one
   idempotency key per run), so a double tap never acts twice.
4. **Memory**: after an executed action, new contacts and the usual meeting length are saved through the
   `save_contact` and `remember` tools. Every run and tool call is logged in `agent_runs` and
   `tool_calls` with tokens and latency.

## Setup

1. Install Docker Desktop and make sure the engine is running.
2. Copy `.env.example` to `.env` and fill in the values. `.env` is git-ignored; never commit it.
   - `TELEGRAM_BOT_TOKEN` from @BotFather, `ALLOWED_USER_IDS` from @userinfobot
     (comma-separated; the first ID is the owner).
   - `RESEND_API_KEY`, `EMAIL_FROM` (use `onboarding@resend.dev` until you verify a domain;
     it can then only send to your Resend account's email).
   - `OWNER_NAME`, `OWNER_EMAIL` for the seed script and the test email.
   - `DATABASE_URL` must use the same user, password and database as `POSTGRES_*`, with host `db`.
   - `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`: any OpenAI-compatible endpoint, e.g.
     `https://api.deepseek.com` with a DeepSeek key, or `https://api.avalai.ir/v1` with an AvalAI key.

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

   On Windows PowerShell:

   ```powershell
   .\scripts\google_auth.ps1
   ```

   Elsewhere:

   ```bash
   docker compose run --rm -p 127.0.0.1:8765:8765 -v ./credentials.json:/app/credentials.json:ro -v ./.env:/app/.env -v ./scripts:/app/scripts migrate python scripts/google_auth.py --write-env
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

## Tests

Tests run in the container against a separate `<db>_test` database. Offline tests use a scripted
LLM; tests marked `llm` (10 Persian sample transcripts plus a memory check) call the real model.

```bash
docker compose run --rm migrate pytest -m "not llm"
```

```bash
docker compose run --rm migrate pytest -m llm
```

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
app/bot     Telegram bot, confirmation buttons
app/stt     ffmpeg + Vosk speech to text
app/agent   provider, system prompt, agent loop, context, executor, memory
app/tools   tool registry, Google Calendar, Resend
app/db      models, session, seed       app/api   FastAPI     app/mcp  (step 03)
migrations/ Alembic                     scripts/  google_auth.py / .ps1
tests/      pytest                      docs/SPEC.md  spec
```
