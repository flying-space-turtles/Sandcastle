FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY bot /app/bot
COPY scripts/lib /app/scripts/lib
COPY services/example-vuln /app/services/example-vuln
RUN chmod +x /app/bot/deploy.sh

ENV ARENA_CONFIG_FILE=/app/config/arena.env \
    BOT_CONTROLLER_DB=/data/controller.db \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python", "-u", "/app/bot/bot_api.py", "--host", "0.0.0.0"]
