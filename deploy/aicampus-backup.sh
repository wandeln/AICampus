#!/usr/bin/env bash
# AICampus tägliches Backup via restic (ausgeführt per cron, s. docs/installation.md §6)
#
# Sichert: data/ (ohne die live SQLite-DB) + .env
# Die DB wird vorher online-konsistent nach data/.backup_staging/ kopiert
# (WAL-Mode → nie die Live-Dateien direkt sichern).
#
# Konfiguration in .env: BACKUP_REPO, BACKUP_PASSWORD
set -euo pipefail

# cron hat einen minimalen PATH — Tools sicher verfügbar machen
export PATH="/home/wandel/anaconda3/bin:/home/wandel/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESTIC_BIN="/home/wandel/bin/restic"
LOCK_FILE="/home/wandel/backups/aicampus/backup.lock"
CONSISTENT_DB="$PROJECT_DIR/data/.backup_staging/aicampus-consistent.db"

cd "$PROJECT_DIR"

# Parallel-Läufe verhindern (z. B. manuel + cron)
exec 200>"$LOCK_FILE"
flock -n 200 || { echo "Backup läuft bereits — Abbruch."; exit 0; }

# Config-Keys aus .env parsen (kein source — robust gegen Sonderzeichen)
BACKUP_REPO="$(grep -E '^BACKUP_REPO=' .env | tail -1 | cut -d= -f2-)"
BACKUP_PASSWORD="$(grep -E '^BACKUP_PASSWORD=' .env | tail -1 | cut -d= -f2-)"
[ -n "$BACKUP_REPO" ] && [ -n "$BACKUP_PASSWORD" ] || { echo "FEHLER: BACKUP_REPO / BACKUP_PASSWORD fehlen in .env"; exit 1; }

export RESTIC_REPOSITORY="$BACKUP_REPO"
export RESTIC_PASSWORD="$BACKUP_PASSWORD"

echo "=== $(date -Is) AICampus-Backup Start ==="

# 1. Online-konsistente DB-Kopie (ohne App-Stopp)
mkdir -p "$PROJECT_DIR/data/.backup_staging"
sqlite3 "$PROJECT_DIR/data/aicampus.db" ".backup '$CONSISTENT_DB'"

# 2. Backup (live DB-Dateien bleiben draußen, konsistente Kopie ist drin)
"$RESTIC_BIN" backup data .env \
  --exclude aicampus.db \
  --exclude aicampus.db-wal \
  --exclude aicampus.db-shm \
  --tag aicampus

# 3. Plausibilitätsprüfung (liest 5 % der Repo-Daten)
"$RESTIC_BIN" check --read-data-subset=5%

# 4. Retention: 14 daily + 4 weekly (~35 Tage)
"$RESTIC_BIN" forget --tag aicampus --keep-daily 14 --keep-weekly 4 --prune

echo "=== $(date -Is) AICampus-Backup fertig ==="
