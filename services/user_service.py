"""
User-Löschung: vollständige Kaskade über alle Tabellen, die users.id referenzieren.

Genutzt von:
- Admin-API: DELETE /api/admin/users/{user_id}
- User-Settings: DELETE /api/auth/settings/account (Selbstlöschung)

Reihenfolge ist wichtig: NOT-NULL-FKs ohne Relationship-Cascade (z. B.
workspace_runs.submission_id) erfordern, dass Kinder vor Eltern gelöscht
werden. Ein Commit pro gelöschtem eigenen Kurs, ein Commit für den Rest;
Dateien (Avatar, Snapshots, Import-Staging) werden nach dem Commit
best-effort entfernt.
"""

import logging
import shutil

from sqlmodel import Session, select

from config import SUBMISSION_DIR
from database.models import (
    Course,
    CourseImport,
    CourseInvite,
    CourseMaterial,
    CourseMedia,
    CourseReference,
    Feedback,
    ForumChannel,
    ForumChannelReadState,
    ForumMessage,
    HintExchange,
    ImageSpec,
    LLMDebugEntry,
    MediaUsage,
    ScriptQuestion,
    ScriptQuestionResponse,
    ScriptSection,
    Submission,
    Task,
    User,
    UserCourse,
    WorkspaceRun,
)
from services import course_service, import_service, media_service
from services.workspace_service import workspace_service

logger = logging.getLogger(__name__)


def delete_user_with_data(session: Session, user: User) -> str:
    """User löschen, inkl. aller referenzierenden Daten.

    - Eigene Kurse: vollständige Kaskade (je eigener Commit)
    - In anderen Kursen erstellter Inhalt (Tasks, Materialien, Kapitel,
      Medien, Referenzen, Einladungen, Forum-Kanäle): wird gelöscht
    - Foren-Posts, Read-States, Skript-Fragen, Hint-Dialoge,
      Workspace-Runs, Einreichungen (inkl. Feedback): werden gelöscht
    - Image-Specs + LLM-Debug-Log: Zeilen bleiben, User-Referenz wird entfernt
    - Dateien (Avatar, Workspace-Snapshots, Import-Staging): best effort

    Returns: der Username des gelöschten Users (für Meldungen).
    """
    user_id = user.id
    username = user.username

    # 1. Skript-Fragen: eigene Antworten + eigene Fragen (inkl. deren Antworten)
    for resp in session.exec(
        select(ScriptQuestionResponse).where(ScriptQuestionResponse.user_id == user_id)
    ).all():
        session.delete(resp)
    for question in session.exec(
        select(ScriptQuestion).where(ScriptQuestion.student_id == user_id)
    ).all():
        for resp in session.exec(
            select(ScriptQuestionResponse).where(ScriptQuestionResponse.question_id == question.id)
        ).all():
            session.delete(resp)
        session.delete(question)
    session.flush()

    # 2. Eigene Kurse (vollständige Kaskade, commit pro Kurs)
    for course in session.exec(select(Course).where(Course.created_by == user_id)).all():
        course_service.delete_course(session, course)

    # 3. In anderen Kursen erstellte Tasks
    #    (Workspace-Aufräumen + Einreichungen/Feedback/Hints anderer Studenten)
    for task in session.exec(select(Task).where(Task.created_by == user_id)).all():
        if task.task_type.value == "workspace":
            try:
                workspace_service.on_task_deleted(session, task)
            except Exception as e:  # noqa: BLE001
                logger.warning("Workspace-Aufräumen (task %s) fehlgeschlagen: %s", task.id, e)
            workspace_service.delete_task_db_rows(session, task)
        # Media-Usages der Aufgabe vorher entfernen (NOT-NULL-FK task_id)
        for usage in session.exec(
            select(MediaUsage).where(MediaUsage.task_id == task.id)
        ).all():
            session.delete(usage)
        for sub in session.exec(select(Submission).where(Submission.task_id == task.id)).all():
            for fb in session.exec(
                select(Feedback).where(Feedback.submission_id == sub.id)
            ).all():
                session.delete(fb)
            session.delete(sub)
        for hint in session.exec(select(HintExchange).where(HintExchange.task_id == task.id)).all():
            session.delete(hint)
        session.delete(task)
    session.flush()

    # 4. In anderen Kursen erstellte Materialien, Kapitel, Medien, Referenzen
    for material in session.exec(
        select(CourseMaterial).where(CourseMaterial.created_by == user_id)
    ).all():
        session.delete(material)
    for section in session.exec(
        select(ScriptSection).where(ScriptSection.created_by == user_id)
    ).all():
        # Fragen anderer Studenten zu diesem Kapitel: section_id → NULL
        # (Design: NULL = "Kapitel gelöscht", Frage bleibt allgemein)
        for question in session.exec(
            select(ScriptQuestion).where(ScriptQuestion.section_id == section.id)
        ).all():
            question.section_id = None
        session.delete(section)
    for medium in session.exec(
        select(CourseMedia).where(CourseMedia.created_by == user_id)
    ).all():
        for usage in session.exec(
            select(MediaUsage).where(MediaUsage.media_id == medium.id)
        ).all():
            session.delete(usage)
        session.delete(medium)
    for reference in session.exec(
        select(CourseReference).where(CourseReference.created_by == user_id)
    ).all():
        session.delete(reference)
    session.flush()

    # 5. Eigene Einladungslinks
    for invite in session.exec(
        select(CourseInvite).where(CourseInvite.created_by == user_id)
    ).all():
        session.delete(invite)
    session.flush()

    # 6. Forum: eigene Nachrichten → eigene Read-States → erstellte Kanäle
    for message in session.exec(
        select(ForumMessage).where(ForumMessage.user_id == user_id)
    ).all():
        session.delete(message)
    session.flush()
    for read_state in session.exec(
        select(ForumChannelReadState).where(ForumChannelReadState.user_id == user_id)
    ).all():
        session.delete(read_state)
    session.flush()
    for channel in session.exec(
        select(ForumChannel).where(ForumChannel.created_by == user_id)
    ).all():
        for message in session.exec(
            select(ForumMessage).where(ForumMessage.channel_id == channel.id)
        ).all():
            session.delete(message)
        for read_state in session.exec(
            select(ForumChannelReadState).where(ForumChannelReadState.channel_id == channel.id)
        ).all():
            session.delete(read_state)
        session.delete(channel)
    session.flush()

    # 7. Eigene Hint-Dialoge + Workspace-Runs
    #    (Runs vor den eigenen Einreichungen: workspace_runs.submission_id-FK)
    for hint in session.exec(
        select(HintExchange).where(HintExchange.student_id == user_id)
    ).all():
        session.delete(hint)
    for run in session.exec(
        select(WorkspaceRun).where(WorkspaceRun.student_id == user_id)
    ).all():
        session.delete(run)
    session.flush()

    # 8. Eigene Einreichungen (inkl. Feedback + Snapshot-Ordner auf Disc)
    snapshot_dirs = []
    for sub in session.exec(select(Submission).where(Submission.student_id == user_id)).all():
        for fb in session.exec(
            select(Feedback).where(Feedback.submission_id == sub.id)
        ).all():
            session.delete(fb)
        if sub.workspace_snapshot:
            snapshot_dirs.append(SUBMISSION_DIR / str(sub.id))
        session.delete(sub)
    session.flush()

    # 9. Vom User gegebene Feedbacks (auf Einreichungen anderer)
    for fb in session.exec(select(Feedback).where(Feedback.giver_id == user_id)).all():
        session.delete(fb)

    # 10. Kurs-Imports (DB-Zeilen; Staging-Ordner nach dem Commit)
    staging_dirs = []
    for imp in session.exec(
        select(CourseImport).where(CourseImport.created_by == user_id)
    ).all():
        if imp.course_id is not None:  # laut Schema NOT NULL — defensiv
            staging_dirs.append(import_service.staging_dir(imp.course_id, imp.job_id))
        session.delete(imp)

    # 11. User-Referenzen, die nicht gelöscht werden: → NULL
    for spec in session.exec(select(ImageSpec).where(ImageSpec.created_by == user_id)).all():
        spec.created_by = None
    for entry in session.exec(
        select(LLMDebugEntry).where(LLMDebugEntry.user_id == user_id)
    ).all():
        entry.user_id = None

    # 12. Kurs-Mitgliedschaften
    for uc in session.exec(select(UserCourse).where(UserCourse.user_id == user_id)).all():
        session.delete(uc)

    # 13. Den User selbst
    avatar_rel = user.avatar
    session.delete(user)
    session.commit()

    # 14. Dateien (best effort, nach dem Commit)
    if avatar_rel:
        media_service.delete_avatar_file(avatar_rel)
    for d in snapshot_dirs:
        shutil.rmtree(d, ignore_errors=True)
    for d in staging_dirs:
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)

    return username
