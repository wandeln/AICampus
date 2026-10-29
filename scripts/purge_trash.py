#!/usr/bin/env python3
"""Endgültige Löschung (Purge) von Papierkorb-Inhalten.

Der EINZIGE Weg, soft gelöschte Daten aus DB + Disc zu entfernen —
bewusst kein UI-Button und kein Auto-Purge (Production: wichtige Daten
dürfen nie versehentlich weg sein).

Reihenfolge ist wichtig (FK-Verweise):
  1. individuell soft gelöschte Items (Task/Material/Kapitel/Medium)
  2. dann soft gelöschte Kurse

Aufruf (im Container `aicampus`):
    python scripts/purge_trash.py --dry-run           # Items + ca. Volumen zeigen
    python scripts/purge_trash.py                     # ganzen Papierkorb leeren
    python scripts/purge_trash.py --course 3          # nur Kurs 3 (Items + Kurs)
    python scripts/purge_trash.py --task 12           # nur Task 12
    python scripts/purge_trash.py --older-than 30     # nur > 30 Tage gelöschtes
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select  # noqa: E402

from config import (  # noqa: E402
    MEDIA_DIR,
    SUBMISSION_DIR,
    UPLOAD_TASK_DIR,
    VERSION_SNAPSHOT_DIR,
    WORKSPACE_DIR,
)
from database.base import engine  # noqa: E402  (registriert den Soft-Delete-Filter)
from database.models import (  # noqa: E402
    ContentVersion,
    Course,
    CourseMaterial,
    CourseMedia,
    ScriptSection,
    Submission,
    Task,
)
from database.soft_delete import SOFT_DELETE_BYPASS  # noqa: E402
from services import course_service, trash_service  # noqa: E402

# (kind, Modell, Hard-Delete-Funktion) — Reihenfolge irrelevant, FKs decken wir ab
ITEM_SPECS = [
    ("task", Task, trash_service.hard_delete_task),
    ("material", CourseMaterial, trash_service.hard_delete_material),
    ("section", ScriptSection, trash_service.hard_delete_section),
    ("media", CourseMedia, trash_service.hard_delete_media),
]


def _dir_size(d: Path) -> int:
    if not d.is_dir():
        return 0
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def _human(n: float) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _version_bytes(session: Session, entity_type: str, entity_ids: list[int]) -> int:
    """Größe der File-Snapshots der Content-Versionen."""
    if not entity_ids:
        return 0
    total = 0
    for v in session.exec(
        select(ContentVersion).where(
            ContentVersion.entity_type == entity_type,
            ContentVersion.entity_id.in_(entity_ids),
        )
    ).all():
        if v.file_snapshot:
            total += _dir_size(VERSION_SNAPSHOT_DIR / str(v.id))
    return total


def _task_bytes(session: Session, task_id: int) -> int:
    total = _dir_size(WORKSPACE_DIR / str(task_id)) + _dir_size(UPLOAD_TASK_DIR / str(task_id))
    for sub in session.exec(select(Submission).where(Submission.task_id == task_id)).all():
        total += _dir_size(SUBMISSION_DIR / str(sub.id))
    return total + _version_bytes(session, "task", [task_id])


def _course_bytes(session: Session, course_id: int) -> int:
    """Kurs-Medien-Verzeichnis + (aktive) Tasks + Versionen der restlichen Inhalte."""
    total = _dir_size(MEDIA_DIR / f"course_{course_id}")
    tasks = session.exec(select(Task).where(Task.course_id == course_id)).all()
    for t in tasks:
        total += _task_bytes(session, t.id)
    mats = session.exec(select(CourseMaterial).where(CourseMaterial.course_id == course_id)).all()
    secs = session.exec(select(ScriptSection).where(ScriptSection.course_id == course_id)).all()
    medias = session.exec(select(CourseMedia).where(CourseMedia.course_id == course_id)).all()
    total += _version_bytes(session, "slide_deck", [m.id for m in mats])
    total += _version_bytes(session, "script_section", [s.id for s in secs])
    total += _version_bytes(session, "applet", [m.id for m in medias])
    return total


def _item_bytes(session: Session, kind: str, row) -> int:
    if kind == "task":
        return _task_bytes(session, row.id)
    if kind == "material":
        return _version_bytes(session, "slide_deck", [row.id])
    if kind == "section":
        return _version_bytes(session, "script_section", [row.id])
    if kind == "media":
        p = MEDIA_DIR / row.file_path
        return p.stat().st_size if p.is_file() else 0
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Papierkorb endgültig leeren (einziger Purge-Weg, s. Docstring)."
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="nur anzeigen, was gelöscht würde (inkl. ca. Volumen)")
    ap.add_argument("--course", type=int, metavar="ID",
                    help="nur Items/Kurs dieses Kurses")
    ap.add_argument("--task", type=int, metavar="ID",
                    help="nur diese (soft gelöschte) Aufgabe")
    ap.add_argument("--older-than", type=int, metavar="TAGE",
                    help="nur Items, die länger als N Tage gelöscht sind")
    ap.add_argument("--yes", action="store_true",
                    help="Bestätigungs-Frage überspringen")
    args = ap.parse_args()

    older_than = (datetime.now() - timedelta(days=args.older_than)) if args.older_than else None

    with Session(engine) as session:
        # ── 1. Individuell soft gelöschte Items ────────────────────
        print("== Individuell soft gelöschte Items ==")
        plan = []  # (kind, id, label, bytes)
        for kind, model, _fn in ITEM_SPECS:
            if args.task is not None and kind != "task":
                continue
            stmt = (
                select(model)
                .where(model.deleted_at.is_not(None))
                .execution_options(**SOFT_DELETE_BYPASS)
            )
            if args.course is not None:
                stmt = stmt.where(model.course_id == args.course)
            if older_than is not None:
                stmt = stmt.where(model.deleted_at < older_than)
            if args.task is not None:
                stmt = stmt.where(model.id == args.task)
            for row in session.exec(stmt).all():
                # Label-Felder JETZT sichern (nach Commit wären sie expired/detached)
                label = (f"[{kind:8}] id={row.id:4}  {row.title!r}  "
                         f"(gelöscht {row.deleted_at:%d.%m.%Y})  Kurs {row.course_id}")
                plan.append((kind, row.id, label, _item_bytes(session, kind, row)))

        for kind, _id, label, size in plan:
            print(f"  {label}   ~{_human(size)}")

        # ── 2. Soft gelöschte Kurse ────────────────────────────────
        course_plan = []
        if args.task is None:  # --task-Filter betrifft nur Items
            stmt = (
                select(Course)
                .where(Course.deleted_at.is_not(None))
                .execution_options(**SOFT_DELETE_BYPASS)
            )
            if args.course is not None:
                stmt = stmt.where(Course.id == args.course)
            if older_than is not None:
                stmt = stmt.where(Course.deleted_at < older_than)
            for c in session.exec(stmt).all():
                label = (f"[course  ] id={c.id:4}  {c.name!r} ({c.semester})  "
                         f"(gelöscht {c.deleted_at:%d.%m.%Y})")
                course_plan.append((c.id, label, _course_bytes(session, c.id)))

        print("== Soft gelöschte Kurse ==")
        for _id, label, size in course_plan:
            print(f"  {label}   ~{_human(size)}")

        total = sum(s for *_x, s in plan) + sum(s for *_x, s in course_plan)
        print(f"\nGesamt: {len(plan)} Items + {len(course_plan)} Kurse, ~{_human(total)}")

        if not plan and not course_plan:
            print("Papierkorb ist leer.")
            return
        if args.dry_run:
            print("\n[DRY-RUN] Es wurde nichts gelöscht.")
            return
        if not args.yes:
            ans = input("Wirklich endgültig löschen? [ja/NEIN] ")
            if ans.strip().lower() not in ("ja", "yes", "j"):
                print("Abgebrochen — nichts gelöscht.")
                return

    # ── Löschen (pro Item try/except: Fehler loggen, weitermachen) ──
    models = {k: m for k, m, _f in ITEM_SPECS}
    hard_fns = {k: f for k, _m, f in ITEM_SPECS}
    ok, failed = 0, []

    for kind, item_id, label, _size in plan:
        try:
            with Session(engine) as session:
                fresh = session.get(models[kind], item_id, execution_options=SOFT_DELETE_BYPASS)
                if fresh is None:
                    continue
                hard_fns[kind](session, fresh)
            print(f"OK   {label}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            failed.append(label)
            print(f"FEHLER {label}: {e}")

    for course_id, label, _size in course_plan:
        try:
            with Session(engine) as session:
                fresh = session.get(Course, course_id, execution_options=SOFT_DELETE_BYPASS)
                if fresh is None:
                    continue
                course_service.hard_delete_course(session, fresh)
            print(f"OK   {label}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            failed.append(label)
            print(f"FEHLER {label}: {e}")

    print(f"\nFertig: {ok} gelöscht" + (f", {len(failed)} FEHLER" if failed else ""))
    if failed:
        print("Fehlgeschlagen:")
        for f in failed:
            print(f"  - {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
