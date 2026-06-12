FROM python:3.12-slim

WORKDIR /app

# No third-party requirements since we use python standard libraries.
COPY gameserver /app/gameserver
COPY services/example-vuln/checker.py /app/services/example-vuln/checker.py

# We'll run main.py as the entry point
WORKDIR /app/gameserver
CMD ["python", "-u", "main.py"]
