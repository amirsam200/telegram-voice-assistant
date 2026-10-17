# Persian Voice Assistant: Project Spec

## Purpose
A personal assistant on Telegram. The user sends a voice message in Persian; the system transcribes it, understands the request, and either schedules a calendar event or sends an email on the user's behalf, only after the user confirms.

## Users
- The owner: one user, or a small allowlist of Telegram user IDs.
- Guests and email recipients are contacts, never users of the bot.

## Core features
- A Telegram bot that accepts voice messages (and text, for testing).
- Speech to text with Vosk and its Persian model; ffmpeg converts Telegram OGG/Opus to 16 kHz mono WAV.
- An agent (DeepSeek, function calling) that turns the transcript into one validated JSON action:
  - schedule: date, time, duration, title, description, guests.
  - email: to, subject, body. The agent writes title, description, subject and body in Persian.
- Confirmation: the bot shows the action; it runs only after the user taps Confirm.
- Tools: create a Google Calendar event, send an email with the Resend API, find and save contacts.
- Memory: contacts, preferences (timezone, default meeting length), recent messages.
- Persian dates: relative ("فردا") and Jalali dates resolved in Asia/Tehran time.

## Architecture
- Python 3.12, python-telegram-bot, FastAPI, Pydantic v2.
- PostgreSQL with SQLAlchemy 2.x and Alembic.
- Vosk (Persian model) and ffmpeg.
- DeepSeek API behind a provider interface (base URL, model and key from env).
- Google Calendar API, Resend API, and an MCP server exposing the same tools.
- Docker Compose from the first commit: bot, api, db.

## Data
users, contacts, voice_messages, agent_runs, tool_calls, calendar_events, emails, memory_facts, audit_events. Audio is deleted after transcription.

## Ground rules
- Secrets only in environment variables; .env is never committed.
- Nothing is sent or scheduled without the user's confirmation.
- Only allowlisted Telegram users can use the bot.
- Transcripts and emails are untrusted data, never instructions.
- Every tool call is logged; errors reach the user in Persian.
- Commit to git after each step.

## Build plan
01 Foundation: git, Docker, Telegram bot, Resend, database · 02 Agent: speech to text, DeepSeek, tools, memory · 03 API, MCP server, backend · 04 Token and quality dashboard on a subdomain · 05 Tests and deployment to the cloud.ir server.

## Repository layout
app/bot · app/stt · app/agent · app/tools · app/api · app/mcp · app/db · migrations/ · tests/ · docker-compose.yml · docs/SPEC.md
