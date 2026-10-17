Add a simple BI-style dashboard to the same project, to monitor token consumption and model quality. It is a new dashboard service in docker-compose, served on its own subdomain (for example usage.example.com, configured with DASHBOARD_HOST) behind the reverse proxy; locally it runs on 127.0.0.1:8080.

## Data
- Log every LLM call to a new table llm_calls: run_id, model, purpose (stt_fix, intent, tool_step, review), prompt_tokens, completion_tokens, cached_tokens, cost_usd (from per-model prices in config), latency_ms, status, error, created_at. Add an Alembic migration and write the rows from the provider interface, so nothing else changes.
- Quality signals per run, from existing data: user decision (confirmed, edited, cancelled), tool success, JSON validation failures, retries, steps used, clarifying questions asked. Add a run_feedback table for the optional thumbs up/down under each bot reply.
- Seed 30 days of realistic demo data with a script, so the charts are not empty before real use.

## Dashboard (read-only, FastAPI + Jinja2 + Chart.js, no frontend build)
- KPI cards: tokens today / 7 days / 30 days, cost, average latency (P50 and P95), success rate, confirmation rate.
- Charts: tokens and cost per day, split by model and purpose; latency over time; success and edit rates over time; most expensive runs.
- Quality table per model: success rate, JSON-valid rate, edit rate, average steps, average cost per run, thumbs up share.
- A logs table with filters (date, model, status, intent) and a run detail page: transcript, action JSON, tool calls, tokens, latency, error.
- Controls: a daily and a monthly token budget with a visible progress bar; an alert (Telegram message to the owner) at 80 and 100 percent; an optional hard stop that makes the bot refuse new requests after 100 percent.
- Clean light theme, responsive, Persian labels with the English metric names, numbers formatted for Persian locale; CSV export of the logs.

## Access and security
- Sign-in required: only the owner. Use a signed session cookie after a password from env (DASHBOARD_PASSWORD_HASH) or a Telegram login link sent by the bot; no public access.
- The dashboard has read access to the database through a separate read-only role; it never shows API keys, audio or full email bodies unless the owner opens a run detail.
- Add the dashboard service and the reverse-proxy config (Caddy or nginx) for the subdomain with automatic HTTPS, and document the DNS record needed.

## Acceptance criteria
docker compose up serves the dashboard on 127.0.0.1:8080 with the demo data; every new LLM call appears in llm_calls within seconds; crossing the budget sends the Telegram alert; unauthenticated requests are refused; pytest covers the aggregation queries and the budget logic; commit after each part.
