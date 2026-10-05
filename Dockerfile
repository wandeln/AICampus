# AICampus Web-App (FastAPI)
#
# Build:  docker build -t aicampus-app .
# Lauf:   siehe deploy/compose.local.yml (lokal) bzw. deploy/README.md
#
# Die App erwartet ihre Laufzeit-Daten (DB, Uploads, Workspaces,
# Submissions) im Verzeichnis /app/data — bitte als Volume mounten,
# sonst gehen die Daten mit dem Container verloren.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies zuerst (Bilde-Cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code
COPY . .

# Laufzeit-Daten (DB, Uploads, …) — per Volume überlagern
RUN mkdir -p /app/data

EXPOSE 8000

# timeout-graceful-shutdown: bei Reload/Neustart dürfen offene SSE-Streams
# (Glocke-Livestream) den Shutdown nicht endlos blockieren — nach 5 s
# werden verbliebene Verbindungen forciert geschlossen, Clients reconnecten.
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "5"]
