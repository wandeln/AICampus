"""Einmalige Migration: Code-Aufgaben (task_type='code') aus der Datenbank löschen.

Hintergrund: Code-Aufgaben wurden durch Workspace-Aufgaben abgelöst
(s. docs/LLM-plans/remove-code-tasks.md). Der Aufgabentyp „code" existiert
nach diesem Lauf nicht mehr; das Enum-Member TaskType.CODE und die
Schema-Änderungen (DROP COLUMN code_template/test_code, RENAME
code_solution → mc_answers) werden über migrate_schema() in
database/base.py beim nächsten Start erledigt.

Das Skript löscht je Code-Aufgabe auch alle verwandten Zeilen
(submissions + feedback, hints, Workspace-Datei-Metadaten, Workspace-Runs,
Medien-Usages, Content-Versionen) sowie die dazugehörigen
Disk-Verzeichnisse (data/workspaces/{task_id}, data/submissions/{submission_id}).

Idempotent: zweiter Lauf findet nichts mehr.

Aufruf aus dem Projekt-Root:  python3 migrate_remove_code_tasks.py
"""

import shutil
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
DB_FILE = BASE_DIR / "data" / "aicampus.db"


def main() -> int:
    if not DB_FILE.exists():
        print(f"DB nicht gefunden: {DB_FILE}")
        return 1

    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Hinweis: SQLAlchemy persistiert Enum-Member-Namen ("CODE"), nicht
    # .value ("code") — beide Varianten abfangen.
    task_rows = cur.execute(
        "SELECT id FROM tasks WHERE task_type IN ('code', 'CODE')"
    ).fetchall()
    task_ids = [r["id"] for r in task_rows]
    if not task_ids:
        print("Keine Code-Aufgaben gefunden — nichts zu tun.")
        return 0

    q = ",".join("?" * len(task_ids))
    print(f"{len(task_ids)} Code-Aufgabe(n): {task_ids}")

    # Abgaben dieser Aufgaben (für Feedback-Löschung + Snapshot-Verzeichnisse)
    sub_ids = [
        r["id"]
        for r in cur.execute(
            f"SELECT id FROM submissions WHERE task_id IN ({q})", task_ids
        )
    ]

    # 1) Kind-Zeilen zuerst
    if sub_ids:
        q2 = ",".join("?" * len(sub_ids))
        cur.execute(f"DELETE FROM feedback WHERE submission_id IN ({q2})", sub_ids)
    cur.execute(
        f"DELETE FROM workspace_runs WHERE task_id IN ({q})", task_ids
    )
    cur.execute(
        f"DELETE FROM submissions WHERE task_id IN ({q})", task_ids
    )
    cur.execute(
        f"DELETE FROM hint_exchanges WHERE task_id IN ({q})", task_ids
    )
    cur.execute(
        f"DELETE FROM task_workspace_files WHERE task_id IN ({q})", task_ids
    )
    cur.execute(
        f"DELETE FROM task_workspace_folders WHERE task_id IN ({q})", task_ids
    )
    cur.execute(
        f"DELETE FROM task_workspace_orders WHERE task_id IN ({q})", task_ids
    )
    cur.execute(
        f"DELETE FROM media_usages WHERE task_id IN ({q})", task_ids
    )
    cur.execute(
        "DELETE FROM content_versions WHERE entity_type='task' AND entity_id IN "
        f"({q})",
        task_ids,
    )
    # 2) Die Aufgaben selbst
    cur.execute(f"DELETE FROM tasks WHERE id IN ({q})", task_ids)
    con.commit()

    # 3) Disk-Relikte (falls vorhanden)
    for tid in task_ids:
        ws_dir = BASE_DIR / "data" / "workspaces" / str(tid)
        if ws_dir.is_dir():
            shutil.rmtree(ws_dir)
            print(f"  gelöscht: {ws_dir}")
    for sid in sub_ids:
        sub_dir = BASE_DIR / "data" / "submissions" / str(sid)
        if sub_dir.is_dir():
            shutil.rmtree(sub_dir)
            print(f"  gelöscht: {sub_dir}")

    remaining = cur.execute(
        "SELECT COUNT(*) FROM tasks WHERE task_type IN ('code', 'CODE')"
    ).fetchone()[0]
    print(f"Fertig. Verbleibende Code-Aufgaben: {remaining}")
    return 0 if remaining == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
