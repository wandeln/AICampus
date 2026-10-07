"""
Kurs-Löschung: vollständige Kaskade aller abhängigen Daten.

Genutzt von:
- Admin-API: DELETE /api/admin/courses/{course_id}
- User-Löschung: eigene Kurse eines gelöschten Users (services/user_service.py)
"""

import logging
import shutil

from sqlmodel import Session, select

from config import MEDIA_DIR
from database.models import (
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
    MediaUsage,
    ScriptQuestion,
    ScriptQuestionResponse,
    ScriptSection,
    Submission,
    Task,
    TaskWorkspaceFile,
    UserCourse,
)
from services import import_service
from services.workspace_service import workspace_service

logger = logging.getLogger(__name__)


def delete_course(session: Session, course: Course) -> str:
    """Kurs löschen, inkl. aller abhängigen Daten (ein Commit, atomar).

    Manuelle Cascading deletes in korrekter Reihenfolge (Kinder zuerst).
    flush() nach jeder Gruppe, damit die Lösch-Reihenfolge garantiert ist.
    Medien-Dateien des Kurses werden nach dem Commit best-effort entfernt.
    """
    course_id = course.id
    course_name = course.name

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
    session.flush()

    # 5. Materialien, Referenzen, Slides-Theme
    materials = session.exec(
        select(CourseMaterial).where(CourseMaterial.course_id == course_id)
    ).all()
    for material in materials:
        session.delete(material)
    references = session.exec(
        select(CourseReference).where(CourseReference.course_id == course_id)
    ).all()
    for reference in references:
        session.delete(reference)
    theme = session.exec(
        select(CourseSlidesTheme).where(CourseSlidesTheme.course_id == course_id)
    ).first()
    if theme:
        session.delete(theme)
    session.flush()

    # 6. Tasks: Feedbacks → Submissions → Hint-Dialoge → Tasks
    tasks = session.exec(select(Task).where(Task.course_id == course_id)).all()
    for task in tasks:
        task_id = task.id
        # Workspace-Zeilen können auch bei nicht-Workspace-Typen vorhanden
        # sein (z. B. per Typ-Wechsel verwaist) — dann ebenfalls aufräumen.
        task_has_workspace_rows = session.exec(
            select(TaskWorkspaceFile.id).where(TaskWorkspaceFile.task_id == task.id)
        ).first() is not None
        if task.task_type.value == "workspace" or task_has_workspace_rows:
            # Agent-Ressourcen + lokale Dateien + Workspace-DB-Rows
            # (NOT NULL-FKs ohne Relationship-Cascade)
            workspace_service.discard_workspace_content(session, task)
        submissions = session.exec(
            select(Submission).where(Submission.task_id == task_id)
        ).all()
        for sub in submissions:
            feedbacks = session.exec(
                select(Feedback).where(Feedback.submission_id == sub.id)
            ).all()
            for fb in feedbacks:
                session.delete(fb)
            session.delete(sub)
        hints = session.exec(
            select(HintExchange).where(HintExchange.task_id == task_id)
        ).all()
        for hint in hints:
            session.delete(hint)
        session.delete(task)
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

    # 9. Schließlich den Kurs selbst
    session.delete(course)
    session.commit()

    # 10. Medien-Dateien des Kurses von Disc entfernen (best effort)
    shutil.rmtree(MEDIA_DIR / f"course_{course_id}", ignore_errors=True)

    return course_name
