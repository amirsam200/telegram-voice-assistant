# syntax=docker/dockerfile:1
# Stage 1: fetch the Vosk Persian model once; this layer stays cached between builds.
FROM python:3.12-slim AS vosk-model
ARG VOSK_MODEL=vosk-model-small-fa-0.5
RUN python -c "import urllib.request; urllib.request.urlretrieve('https://alphacephei.com/vosk/models/${VOSK_MODEL}.zip', '/tmp/model.zip')" \
    && python -m zipfile -e /tmp/model.zip /tmp/models \
    && mv /tmp/models/${VOSK_MODEL} /opt/vosk-model \
    && rm -rf /tmp/model.zip /tmp/models

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system app && useradd --system --gid app --create-home --home-dir /home/app app

COPY --from=vosk-model /opt/vosk-model /opt/vosk-model

WORKDIR /app

COPY requirements.txt .
# The cache mount keeps downloaded wheels between builds on slow networks.
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY . .
# Voice files live here; the named volume inherits this ownership.
RUN mkdir -p /app/data/voice && chown -R app:app /app/data

USER app

CMD ["python", "-m", "app.bot"]
