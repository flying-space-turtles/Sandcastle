# syntax=docker/dockerfile:1.6
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data \
    SERVICE_PORT=8080

# iputils-ping is required for the diagnostics endpoint to work as
# documented (and to give the command-injection vulnerability a realistic
# shape).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        iputils-ping \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app

RUN useradd --create-home --uid 10001 turtle \
    && mkdir -p /app/data \
    && chown -R turtle:turtle /app

USER turtle

EXPOSE 8080

# We bind to 0.0.0.0 inside the ctf-network bridge. Workers stay at 1 so the
# in-memory rate limit (if added later) and the SQLite handle stay coherent.
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 4 --timeout 30 --bind 0.0.0.0:${SERVICE_PORT} --chdir /app/app app:app"]
