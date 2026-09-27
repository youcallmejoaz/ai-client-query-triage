FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/app/data/triage.db

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY fixtures ./fixtures
RUN pip install -e . \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /app/data /app/secrets \
 && chown app /app/data

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/api/health')"
CMD ["sh", "-c", "triage serve --host 0.0.0.0 --port ${PORT:-8000}"]
