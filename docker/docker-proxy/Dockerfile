# syntax=docker/dockerfile:1.6
FROM python:3.12-slim

WORKDIR /app
COPY docker/docker-proxy/proxy.py /app/proxy.py

# No third-party dependencies — stdlib asyncio only.

CMD ["python", "-u", "/app/proxy.py"]
