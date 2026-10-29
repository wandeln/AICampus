"""
Soft Delete (Papierkorb) für Kurs-Inhalte: Task, Material, Kapitel, Medium.

- `soft_delete` / `restore` setzen nur `deleted_at` (reversibel, Dateien
  bleiben auf der Platte, Kinder-Zeilen unangetastet).
- `hard_delete_*` = endgültige Kaskade inkl. Dateien, extrahiert aus den
  alten Lösch-Endpoints. Wird NICHT aus dem UI aufgerufen — nur vom
  CLI-Purge (scripts/purge_trash.py).
"""

import logging
from datetime import datetime

from sqlmodel import Session, delete, select

from database.models import (
    ContentVersion,
    CourseMaterial,
    CourseMedia,
    MediaUsage,
    MaterialType,
    ScriptQuestion,
    Task,
    TaskWorkspaceFile,
)
from services import media_service, notifications, upload_service
from services.media_service import sync_media_usages
from services.workspace_service import workspace_service

logger = logging.getLogger(__name__)


# ─── Soft Delete / Restore ─────────────────────────────────────────────


def soft_delete(session: Session, entity) -> None:
    """Entity in den Papierkorb legen (nur `deleted_at` setzen)."""
    entity.deleted_at = datetime.now()
    session.add(entity)
    session.commit()


def restore(session: Session, entity) -> None:
    """Entity aus dem Papierkorb wiederherstellen."""
    entity.deleted_at = None
    session.add(entity)
    session.commit()


# ─── Endgültige Löschung (nur CLI-Purge) ──────────────────────────────


def hard_delete_task(session: Session, task: Task) -> None:
    """Aufgabe endgültig löschen (Workspace-Ressourcen, Uploads, alle
    Einreichungen inkl. Feedback, Hints, Versionen, DB-Row)."""
    task_course_id = task.course_id
    # Workspace-Zeilen können auch bei nicht-Workspace-Typen vorhanden sein
    # (z. B. per Typ-Wechsel verwaist) — dann ebenfalls aufräumen, sonst
    # bricht session.delete(task) mit IntegrityError ab (500).
    task_has_workspace_rows = session.exec(
        select(TaskWorkspaceFile.id).where(TaskWorkspaceFile.task_id == task.id)
    ).first() is not None
    if task.task_type.value == "workspace" or task_has_workspace_rows:
        # Agent-Ressourcen + lokale Dateien + Workspace-DB-Rows, BEVOR der
        # Task aus der DB gelöscht wird (Instanz ist danach expired/detached)
        workspace_service.discard_workspace_content(session, task)

    # Upload-Dateien (Task-Dateien + Einreichungs-Dateien) aufräumen
    upload_service.cleanup_task(task.id)

    # Media-Usages der Aufgabe vorher entfernen (FK nach tasks.id)
    session.exec(delete(MediaUsage).where(MediaUsage.task_id == task.id))

    for sub in task.submissions:
        upload_service.cleanup_submission(sub.id, sub.upload_file)
        for fb in sub.feedback_list:
            session.delete(fb)
        session.delete(sub)
    for hint in task.hint_exchanges:
        session.delete(hint)

    # Versions-History der Aufgabe mit aufräumen
    session.exec(delete(ContentVersion)
                 .where(ContentVersion.entity_type == "task")
                 .where(ContentVersion.entity_id == task.id))

    session.delete(task)
    session.commit()
    sync_media_usages(session, task_course_id)


def hard_delete_material(session: Session, material: CourseMaterial) -> None:
    """Material (Slide-Deck) endgültig löschen (Versionen + Usages + DB-Row)."""
    course_id = material.course_id

    # Slide-Decks: Versions-History mit löschen (Skripte nutzen Kapitel, nicht Material)
    if material.material_type == MaterialType.SLIDES:
        session.exec(
            delete(ContentVersion)
            .where(ContentVersion.entity_type == "slide_deck")
            .where(ContentVersion.entity_id == material.id)
        )

    # Media-Usages des Materials vorher entfernen (FK nach course_materials.id)
    session.exec(delete(MediaUsage).where(MediaUsage.material_id == material.id))

    session.delete(material)
    session.commit()
    sync_media_usages(session, course_id)


def hard_delete_section(session: Session, section) -> None:
    """Skriptkapitel endgültig löschen (Fragen → „Allgemein“, Versionen, DB-Row)."""
    course_id = section.course_id

    # Fragen auf dieses Kapitel bleiben als „Allgemein“ (sonst FK-Verstoß)
    for q in session.exec(
        select(ScriptQuestion).where(ScriptQuestion.section_id == section.id)
    ).all():
        q.section_id = None
        session.add(q)

    # Versions-History des Kapitels mit löschen
    session.exec(
        delete(ContentVersion)
        .where(ContentVersion.entity_type == "script_section")
        .where(ContentVersion.entity_id == section.id)
    )

    session.delete(section)
    session.commit()
    sync_media_usages(session, course_id)


def hard_delete_media(session: Session, media: CourseMedia) -> None:
    """Medium endgültig löschen (Versionen + Usages + DB-Row + Datei)."""
    course_id = media.course_id

    if media.media_type == "applet":
        session.exec(
            delete(ContentVersion)
            .where(ContentVersion.entity_type == "applet")
            .where(ContentVersion.entity_id == media.id)
        )
    for u in session.exec(select(MediaUsage).where(MediaUsage.media_id == media.id)).all():
        session.delete(u)
    session.delete(media)
    session.commit()

    media_service.delete_file(media.file_path)
    # Live-Update: offene Medien-Listen/Studio-Tabs entfernen die Kachel.
    notifications.push_content_updated(
        session, course_id, "applet" if media.media_type == "applet" else "media", media.id)
