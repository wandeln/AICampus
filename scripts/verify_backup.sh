#!/usr/bin/env bash
# Restore-Drill: stellt das letzte Backup in ein Temp-Verzeichnis wieder her und
# prüft, ob die DB eine Integrity-Check besteht. Führt KEINE echte Daten-Übernahme durch.
#
# Aufruf: scripts/verify_backup.sh
set -euo pipefail

# cron hat einen minimalen PATH — Tools sicher verfügbar machen
export PATH="/home/wandel/anaconda3/bin:/home/wandel/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESTIC_BIN="/home/wandel/bin/restic"
JQ="/home/wandel/anaconda3/bin/jq"

cd "$PROJECT_DIR"

# Config-Keys aus .env parsen (kein source — robust gegen Sonderzeichen)
BACKUP_REPO="$(grep -E '^BACKUP_REPO=' .env | tail -1 | cut -d= -f2-)"
BACKUP_PASSWORD="$(grep -E '^BACKUP_PASSWORD=' .env | tail -1 | cut -d= -f2-)"
[ -n "$BACKUP_REPO" ] && [ -n "$BACKUP_PASSWORD" ] || { echo "FEHLER: BACKUP_REPO / BACKUP_PASSWORD fehlen in .env"; exit 1; }

export RESTIC_REPOSITORY="$BACKUP_REPO"
export RESTIC_PASSWORD="$BACKUP_PASSWORD"

SNAP="$("$RESTIC_BIN" snapshots --json | "$JQ" -r '.[-1].short_id')"
echo "Neuester Snapshot: $SNAP"

TARGET="$(mktemp -d)"
trap 'rm -rf "$TARGET"' EXIT

echo "Restore nach $TARGET ..."
"$RESTIC_BIN" restore "$SNAP" --target "$TARGET"

# --- DB-Prüfung -------------------------------------------------------------
DB_FILE="$(find "$TARGET" -name aicampus-consistent.db | head -1)"
[ -n "$DB_FILE" ] || { echo "FEHLER: aicampus-consistent.db nicht im Snapshot gefunden"; exit 1; }

INTEGRITY="$(sqlite3 "$DB_FILE" "PRAGMA integrity_check;")"
[ "$INTEGRITY" = "ok" ] || { echo "FEHLER: PRAGMA integrity_check = $INTEGRITY"; exit 1; }
echo "PRAGMA integrity_check: ok"

echo "Zeilenanzahlen im Backup:"
for t in users courses tasks course_materials course_script_sections; do
  printf "  %-24s %s\n" "$t" "$(sqlite3 "$DB_FILE" "SELECT count(*) FROM $t;")"
done

# --- Datei-Zählvergleich gegen live data/ ------------------------------------
RESTORED="$(find "$TARGET/data" -type f | wc -l)"
LIVE="$(find "$PROJECT_DIR/data" -type f | wc -l)"
LIVE_DB_FILES=0
for f in aicampus.db aicampus.db-wal aicampus.db-shm; do
  [ -f "$PROJECT_DIR/data/$f" ] && LIVE_DB_FILES=$((LIVE_DB_FILES + 1))
done
EXPECTED=$((LIVE - LIVE_DB_FILES))
[ "$RESTORED" -eq "$EXPECTED" ] || {
  echo "FEHLER: Dateianzahl im Snapshot ($RESTORED) != erwartet ($EXPECTED = live $LIVE - DB-Dateien $LIVE_DB_FILES)"
  exit 1
}
[ -f "$TARGET/.env" ] || { echo "FEHLER: .env nicht im Snapshot"; exit 1; }

echo "Dateianzahl: Snapshot=$RESTORED live data/=$LIVE (exkl. $LIVE_DB_FILES DB-Dateien) → OK"
echo ".env im Snapshot: OK"
echo ""
echo "✔ Restore-Drill erfolgreich"
