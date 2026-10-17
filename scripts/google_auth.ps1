# One-time Google Calendar authorization (Windows). Run from the project root:
#   .\scripts\google_auth.ps1
# Writes GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN into .env.
docker compose run --rm -p 127.0.0.1:8765:8765 `
    -v "${PWD}/credentials.json:/app/credentials.json:ro" `
    -v "${PWD}/.env:/app/.env" `
    -v "${PWD}/scripts:/app/scripts" `
    migrate python scripts/google_auth.py --write-env
