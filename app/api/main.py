from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from app.db.session import engine

app = FastAPI(title="Persian Voice Assistant API")


@app.get("/health")
def health() -> dict:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok"}
