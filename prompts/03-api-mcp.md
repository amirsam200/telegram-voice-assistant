Add the backend surface around the agent from Batch 02, as described in docs/SPEC.md.

## FastAPI
- GET /health: database, Telegram and the LLM provider reachable.
- Admin endpoints behind an ADMIN_API_KEY header: GET /runs with filters, GET /runs/{id} (transcript, action JSON, tool calls), GET /events, GET /emails, GET and POST /contacts, POST /runs/{id}/retry for failed runs.
- Pydantic response models and pagination; no secrets or audio in responses; OpenAPI docs only when DEBUG is on.

## MCP server
- An MCP server with the official Python MCP SDK exposing find_contact, list_calendar_events, create_calendar_event and send_email, plus a resource with the upcoming events.
- It reuses the agent's tool functions: one implementation, two interfaces.
- Write tools require confirm=true and are recorded in audit_events.
- Document in the README how to connect it to an MCP client such as Claude Desktop or an IDE.

## Backend hardening
- Structured JSON logs with one run_id correlation ID through bot, agent and tools.
- Rate limit per Telegram user; voice messages up to 60 seconds.
- Errors reach the user as short Persian messages; full details only in the logs.
- A periodic job that deletes leftover audio files older than one hour.

## Acceptance criteria
pytest covers the API (auth, filters, errors) and the MCP tools with fake calendar and email clients; docker compose up serves the API on 127.0.0.1:8000 and the MCP server works from a client; commit after each part.
