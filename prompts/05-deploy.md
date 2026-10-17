Finalize the assistant and prepare its deployment to my Linux server on cloud.ir, which I reach over SSH. Do not run anything on the server yourself: write the files and the exact steps, and I will run them.

## Finalize
- Run the whole test suite and fix the failures.
- An evaluation set of 30 Persian transcripts with the expected JSON; report the accuracy of intent, date and time.
- docker-compose.prod.yml: restart: unless-stopped, no source mounts, healthchecks, log rotation, resource limits.
- Pinned dependency versions, a small image, and a complete README.

## docs/DEPLOY.md, step by step with every command
- Server: Ubuntu, a non-root deploy user, SSH key login, password login off, ufw with SSH, 80 and 443 open only. The bot uses long polling, so it needs no inbound port; the reverse proxy serves the dashboard subdomain over HTTPS (Let's Encrypt) and the API stays on 127.0.0.1.
- Install Docker Engine and the Compose plugin.
- Outbound check: confirm the server reaches api.telegram.org, the DeepSeek API, api.resend.com and Google APIs. If any is blocked, configure an HTTPS proxy or an API gateway through environment variables and document it.
- Clone the repository, create .env on the server from .env.example (secrets never go through git), download the Vosk model.
- DNS: an A record for the dashboard subdomain (for example usage.example.com) pointing to the server. docker compose -f docker-compose.prod.yml up -d --build, run the migrations, check the logs, /health and the dashboard URL.
- Nightly pg_dump backups kept off the server, and a tested restore.
- Updates and rollback: git pull, rebuild, migrate, restart; how to return to the previous commit.
- A deploy.sh that performs the update and is safe to run twice.

## Acceptance criteria
I can deploy using only docs/DEPLOY.md, and on the server a voice message on Telegram creates a calendar event and sends an email after confirmation. Make a final commit and tag v1.0.0, then summarize what is done, what is not, and the known limits.
