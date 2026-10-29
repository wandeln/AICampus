"""
Kurs-Löschung in zwei Stufen:

- `soft_delete_course`: setzt nur `deleted_at` (reversibel; Dateien bleiben).
  Genutzt von der Admin-API. Restore per `restore_course`.
- `hard_delete_course`: vollständige Kaskade aller abhängigen Daten inkl.
  Dateien. Wird NICHT aus dem UI aufgerufen — nur vom CLI-Purge
  (scripts/purge_trash.py).
"""

import logging
import shutil
from datetime import datetime

from sqlmodel import Session, select

from config import MEDIA_DIR, SUBMISSION_DIR, VERSION_SNAPSHOT_DIR
from database.models import (
    ContentVersion,
    Course,
    CourseInvite,
    CourseMaterial,
    CourseMedia,
    CourseReference,
    CourseSettings,
    CourseSlidesTheme,
    Feedback,
    ForumChannel,
    ForumChannelReadState,
    ForumMessage,
    HintExchange,
    ImageSpec,
    MediaUsage,
    McpEvent,
    McpOAuthGrantCourse,
    McpTokenCourse,
    Notification,
    PageView,
    ScriptQuestion,
    ScriptQuestionResponse,
    ScriptSection,
    Submission,
    Task,
    TaskWorkspaceFile,
    UserCourse,
)
from services import import_service
from services import upload_service
from services.workspace_service import workspace_service

logger = logging.getLogger(__name__)


def _delete_content_versions(
    session: Session,
    entity_type: str,
    entity_ids: list[int],
    snapshot_dirs: list,
) -> None:
    """Content-Versionen (DB) eines Typs löschen; Task-Dateisnapshot-Verzeichnisse
    für die Disc-Bereinigung nach dem Commit merken."""
    if not entity_ids:
        return
    for version in session.exec(
        select(ContentVersion).where(
            ContentVersion.entity_type == entity_type,
            ContentVersion.entity_id.in_(entity_ids),
        )
    ).all():
        if version.file_snapshot:
            snapshot_dirs.append(VERSION_SNAPSHOT_DIR / str(version.id))
        session.delete(version)


def soft_delete_course(session: Session, course: Course) -> str:
    """Kurs soft löschen (nur `deleted_at` setzen) — reversibel, keine
    Kaskade, keine Dateien. Der Kurs wird dadurch über den globalen
    Soft-Delete-Read-Filter unsichtbar; Restore = `restore_course`."""
    name = course.name
    course.deleted_at = datetime.now()
    session.add(course)
    session.commit()
    return name


def restore_course(session: Session, course: Course) -> None:
    """Soft gelöschten Kurs wiederherstellen (Kinder blieben erhalten)."""
    course.deleted_at = None
    session.add(course)
    session.commit()


def hard_delete_course(session: Session, course: Course) -> str:
    """Kurs endgültig löschen, inkl. aller abhängigen Daten (ein Commit, atomar).

    Manuelle Cascading deletes in korrekter Reihenfolge (Kinder zuerst).
    flush() nach jeder Gruppe, damit die Lösch-Reihenfolge garantiert ist.
    Dateien des Kurses (Medien, Submissions-, Versions-Snapshots) werden
    nach dem Commit best-effort entfernt.
    """
    course_id = course.id
    course_name = course.name
    snapshot_dirs: list = []  # VERSION_SNAPSHOT_DIR-Ordner (Task-Versionen)
    submission_dirs: list = []  # SUBMISSION_DIR-Ordner (Workspace-Einreichungen)

    # 1. Skript-Fragen: Antworten → Fragen → Kapitel
    questions = session.exec(
        select(ScriptQuestion).where(ScriptQuestion.course_id == course_id)
    ).all()
    for question in questions:
        responses = session.exec(
            select(ScriptQuestionResponse).where(
                ScriptQuestionResponse.question_id == question.id
            )
        ).all()
        for response in responses:
            session.delete(response)
        session.delete(question)
    session.flush()
    sections = session.exec(
        select(ScriptSection).where(ScriptSection.course_id == course_id)
    ).all()
    for section in sections:
        session.delete(section)
    _delete_content_versions(session, "script_section", [s.id for s in sections], snapshot_dirs)
    session.flush()

    # 2. Forum: Nachrichten → Read-States → Kanäle
    messages = session.exec(
        select(ForumMessage).where(ForumMessage.course_id == course_id)
    ).all()
    for message in messages:
        session.delete(message)
    session.flush()
    channels = session.exec(
        select(ForumChannel).where(ForumChannel.course_id == course_id)
    ).all()
    for channel in channels:
        read_states = session.exec(
            select(ForumChannelReadState).where(
                ForumChannelReadState.channel_id == channel.id
            )
        ).all()
        for read_state in read_states:
            session.delete(read_state)
        session.delete(channel)
    session.flush()

    # 3. Einladungslinks
    invites = session.exec(
        select(CourseInvite).where(CourseInvite.course_id == course_id)
    ).all()
    for invite in invites:
        session.delete(invite)
    session.flush()

    # 4. Medien: Usages → DB-Einträge (Dateien auf Disc nach dem Commit)
    media = session.exec(
        select(CourseMedia).where(CourseMedia.course_id == course_id)
    ).all()
    for medium in media:
        usages = session.exec(
            select(MediaUsage).where(MediaUsage.media_id == medium.id)
        ).all()
        for usage in usages:
            session.delete(usage)
        session.delete(medium)
    _delete_content_versions(session, "applet", [m.id for m in media], snapshot_dirs)
    session.flush()

    # 5. Materialien, Referenzen, Slides-Theme
    materials = session.exec(
        select(CourseMaterial).where(CourseMaterial.course_id == course_id)
    ).all()
    material_ids = [material.id for material in materials]
    for material in materials:
        session.delete(material)
    _delete_content_versions(session, "slide_deck", material_ids, snapshot_dirs)
    references = session.exec(
        select(CourseReference).where(CourseReference.course_id == course_id)
    ).all()
    for reference in references:
        session.delete(reference)
    _delete_content_versions(session, "reference", [r.id for r in references], snapshot_dirs)
    theme = session.exec(
        select(CourseSlidesTheme).where(CourseSlidesTheme.course_id == course_id)
    ).first()
    if theme:
        session.delete(theme)
    session.flush()

    # 6. Tasks: Feedbacks → Submissions → Hint-Dialoge → Tasks
    task_ids: list[int] = []
    tasks = session.exec(select(Task).where(Task.course_id == course_id)).all()
    for task in tasks:
        task_id = task.id
        task_ids.append(task_id)
        # Workspace-Zeilen können auch bei nicht-Workspace-Typen vorhanden
        # sein (z. B. per Typ-Wechsel verwaist) — dann ebenfalls aufräumen.
        task_has_workspace_rows = session.exec(
            select(TaskWorkspaceFile.id).where(TaskWorkspaceFile.task_id == task.id)
        ).first() is not None
        if task.task_type.value == "workspace" or task_has_workspace_rows:
            # Agent-Ressourcen + lokale Dateien + Workspace-DB-Rows
            # (NOT NULL-FKs ohne Relationship-Cascade)
            workspace_service.discard_workspace_content(session, task)
        if task.task_type.value == "upload":
            # Template-/Musterlösungs-Dateien der Aufgabe
            upload_service.cleanup_task(task_id)
        submissions = session.exec(
            select(Submission).where(Submission.task_id == task_id)
        ).all()
        for sub in submissions:
            feedbacks = session.exec(
                select(Feedback).where(Feedback.submission_id == sub.id)
            ).all()
            for fb in feedbacks:
                session.delete(fb)
            if sub.workspace_snapshot or sub.upload_file:
                submission_dirs.append(SUBMISSION_DIR / str(sub.id))
            session.delete(sub)
        hints = session.exec(
            select(HintExchange).where(HintExchange.task_id == task_id)
        ).all()
        for hint in hints:
            session.delete(hint)
        _delete_content_versions(session, "task", [task_id], snapshot_dirs)
        session.delete(task)
    # Media-Usages, die Tasks/Materialien (nicht Medien) referenzieren
    if task_ids:
        for usage in session.exec(
            select(MediaUsage).where(MediaUsage.task_id.in_(task_ids))
        ).all():
            session.delete(usage)
    if material_ids:
        for usage in session.exec(
            select(MediaUsage).where(MediaUsage.material_id.in_(material_ids))
        ).all():
            session.delete(usage)
    session.flush()

    # 7. Kurs-Settings + Mitgliedschaften
    settings = session.exec(
        select(CourseSettings).where(CourseSettings.course_id == course_id)
    ).first()
    if settings:
        session.delete(settings)
    memberships = session.exec(
        select(UserCourse).where(UserCourse.course_id == course_id)
    ).all()
    for mc in memberships:
        session.delete(mc)
    session.flush()

    # 8. Kurs-Import (DB-Zeile + Staging-Dateien, eigenes Session-Handling)
    import_service.delete_import(course_id)

    # 8b. MCP-Allowlisten + Tracking + Benachrichtigungen (Kurs-/Studierenden-Daten)
    for model in (McpEvent, McpTokenCourse, McpOAuthGrantCourse, Notification, PageView):
        for row in session.exec(select(model).where(model.course_id == course_id)).all():
            session.delete(row)
    for spec in session.exec(
        select(ImageSpec).where(ImageSpec.scope == str(course_id))
    ).all():
        session.delete(spec)
    session.flush()

    # 9. Schließlich den Kurs selbst
    session.delete(course)
    session.commit()

    # 10. Dateien des Kurses von Disc entfernen (best effort)
    shutil.rmtree(MEDIA_DIR / f"course_{course_id}", ignore_errors=True)
    for d in snapshot_dirs + submission_dirs:
        shutil.rmtree(d, ignore_errors=True)

    return course_name
