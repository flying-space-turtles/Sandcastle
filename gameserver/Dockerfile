FROM python:3.12-slim

WORKDIR /app

# No third-party requirements since we use python standard libraries.
COPY . /app/

# We'll run main.py as the entry point
CMD ["python", "-u", "main.py"]
