FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        poppler-utils \
        tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
        tesseract-ocr-vie tesseract-ocr-jpn tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "${APP_GID}" dongjiang \
    && useradd --uid "${APP_UID}" --gid dongjiang --home-dir /app --no-create-home dongjiang

COPY pyproject.toml README.md LICENSE ./
COPY dongjiang_agent ./dongjiang_agent
RUN python -m pip install --upgrade pip \
    && python -m pip install ".[documents]"

RUN mkdir -p /app/data /app/output \
    && chown -R dongjiang:dongjiang /app

USER dongjiang

EXPOSE 8765

STOPSIGNAL SIGINT

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)); assert data.get('ok') is True"

CMD ["python", "-m", "dongjiang_agent.cli", "serve", "--host", "0.0.0.0", "--port", "8765"]
