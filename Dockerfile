FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system app && useradd --system --gid app --create-home --home-dir /home/app app

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Voice files live here; the named volume inherits this ownership.
RUN mkdir -p /app/data/voice && chown -R app:app /app/data

USER app

CMD ["python", "-m", "app.bot"]
