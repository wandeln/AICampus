"""
User-Löschung (Soft Delete + Anonymisierung, DSGVO-konform):

- `soft_delete_user`: User-Zeile bleibt als anonymisierter FK-Anker
  (Kurse/Inhalt des Users überleben dem Institut — das war das größte
  Risiko der alten Vollkaskade), persönliche Daten werden HART gelöscht
  (Art. 17): Forum-Posts, Einreichungen (Lösungen!), Hints, Runs,
  Feedback, Tracking, Tokens.
- `restore_user`: setzt nur `deleted_at=NULL` (anonymisiert bleibt;
  Username/Name/Email trägt der Admin per Edit-Dialog wieder ein).

Echte Vollkaskade (inkl. eigener Kurse) gibt es bewusst NICHT mehr im App-
Code — nur via CLI/Support bei Bedarf.

Reihenfolge ist wichtig: NOT-NULL-FKs ohne Relationship-Cascade (z. B.
workspace_runs.submission_id) erfordern, dass Kinder vor Eltern gelöscht
werden. Dateien (Avatar, Snapshots, Import-Staging) werden nach dem Commit
best-effort entfernt.
"""

import logging
import shutil
from datetime import datetime

from sqlmodel import Session, select

from config import SUBMISSION_DIR
from database.models import (
    ContentVersion,
    CourseImport,
    Feedback,
    ForumChannelReadState,
    ForumMessage,
    HintExchange,
    ImageSpec,
    LLMDebugEntry,
    McpClientContext,
    McpEvent,
    McpOAuthCode,
    McpOAuthGrantCourse,
    McpOAuthToken,
    McpToken,
    McpTokenCourse,
    Notification,
    PageView,
    ScriptQuestion,
    ScriptQuestionResponse,
    Submission,
    User,
    UserCourse,
    WorkspaceRun,
)
from services import import_service, media_service

logger = logging.getLogger(__name__)


def soft_delete_user(session: Session, user: User) -> str:
    """User soft löschen: anonymisieren + persönliche Daten hart löschen.

    - User-Zeile bleibt als FK-Anker (Kurse, die er erstellt hat, und sein
      erstellter Kurs-Inhalt überleben — Institutsdaten)
    - Persönliche Daten werden HART gelöscht (Art. 17): Skript-Fragen,
      Foren-Posts, Read-States, Hint-Dialoge, Workspace-Runs, eigene
      Einreichungen (Lösungen!), gegebenes Feedback, Kurs-Mitgliedschaften,
      Imports, MCP-Tokens/-Events, OAuth, Benachrichtigungen, Tracking,
      erstellte Content-Versionen
    - Login wird unmöglich (Password + LDAP-DN → NULL); bei LDAP-Usern
      provisioniert ein erneuter Login einen frischen Account (der alte
      Username ist durch die Anonymisierung frei) — bewusst so

    Returns: der alte Username des gelöschten Users (für Meldungen).
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

    # 2. In anderen Kursen erstellte Tasks/Materialien/Kapitel/Medien/
    #    Referenzen bleiben (Instituts-Inhalt; `created_by` zeigt auf den
    #    anonymisierten Anker) — das schützt Einreichungen anderer Studenten.

    # 3. Eigene Einladungslinks bleiben (Zugangslinks des Instituts).

    # 4. Forum: eigene Nachrichten + eigene Read-States (persönliche Daten).
    #    Erstellte Kanäle bleiben (Kurs-Inhalt) — nur deren eigene Posts weg.
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

    # 5. Eigene Hint-Dialoge + Workspace-Runs
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

    # 6. Eigene Einreichungen (inkl. Feedback + Snapshot-Ordner auf Disc)
    snapshot_dirs = []
    for sub in session.exec(select(Submission).where(Submission.student_id == user_id)).all():
        for fb in session.exec(
            select(Feedback).where(Feedback.submission_id == sub.id)
        ).all():
            session.delete(fb)
        if sub.workspace_snapshot or sub.upload_file:
            snapshot_dirs.append(SUBMISSION_DIR / str(sub.id))
        session.delete(sub)
    session.flush()

    # 7. Vom User gegebene Feedbacks (auf Einreichungen anderer)
    for fb in session.exec(select(Feedback).where(Feedback.giver_id == user_id)).all():
        session.delete(fb)

    # 8. Kurs-Imports (DB-Zeilen; Staging-Ordner nach dem Commit)
    staging_dirs = []
    for imp in session.exec(
        select(CourseImport).where(CourseImport.created_by == user_id)
    ).all():
        if imp.course_id is not None:  # laut Schema NOT NULL — defensiv
            staging_dirs.append(import_service.staging_dir(imp.course_id, imp.job_id))
        session.delete(imp)

    # 9. MCP-Tokens (Allowlist-Zeilen vor den Tokens)
    mcp_token_ids = [
        t.id for t in session.exec(select(McpToken).where(McpToken.user_id == user_id)).all()
    ]
    for tc in session.exec(
        select(McpTokenCourse).where(McpTokenCourse.token_id.in_(mcp_token_ids))
    ).all():
        session.delete(tc)
    session.flush()
    for token in session.exec(select(McpToken).where(McpToken.user_id == user_id)).all():
        session.delete(token)
    session.flush()

    # 10. User-Referenzen, die nicht gelöscht werden: → NULL
    for spec in session.exec(select(ImageSpec).where(ImageSpec.created_by == user_id)).all():
        spec.created_by = None
    for entry in session.exec(
        select(LLMDebugEntry).where(LLMDebugEntry.user_id == user_id)
    ).all():
        entry.user_id = None

    # 11. Kurs-Mitgliedschaften (Zugang wird entzogen)
    for uc in session.exec(select(UserCourse).where(UserCourse.user_id == user_id)).all():
        session.delete(uc)

    # 12. Benachrichtigungen + Tracking + MCP-OAuth + Content-Versionen
    for note in session.exec(
        select(Notification).where(
            (Notification.user_id == user_id) | (Notification.actor_id == user_id)
        )
    ).all():
        session.delete(note)
    for pv in session.exec(select(PageView).where(PageView.user_id == user_id)).all():
        session.delete(pv)
    for ev in session.exec(select(McpEvent).where(McpEvent.user_id == user_id)).all():
        session.delete(ev)
    for gc in session.exec(
        select(McpOAuthGrantCourse).where(McpOAuthGrantCourse.user_id == user_id)
    ).all():
        session.delete(gc)
    for code in session.exec(select(McpOAuthCode).where(McpOAuthCode.user_id == user_id)).all():
        session.delete(code)
    for tok in session.exec(select(McpOAuthToken).where(McpOAuthToken.user_id == user_id)).all():
        session.delete(tok)
    for ctx in session.exec(
        select(McpClientContext).where(McpClientContext.user_id == user_id)
    ).all():
        session.delete(ctx)
    for ver in session.exec(
        select(ContentVersion).where(ContentVersion.created_by == user_id)
    ).all():
        session.delete(ver)
    session.flush()

    # 13. Den User anonymisieren (Zeile bleibt als FK-Anker, Login unmöglich)
    avatar_rel = user.avatar
    user.deleted_at = datetime.now()
    user.username = f"geloescht_{user_id}"
    user.name = "Gelöschter Account"
    user.email = None
    user.password_hash = None
    user.ldap_dn = None
    user.avatar = None
    session.add(user)
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


def restore_user(session: Session, user: User) -> None:
    """Soft gelöschten User wiederherstellen (anonymisiert bleibt; Username/
    Name/Email trägt der Admin anschließend per Edit-Dialog wieder ein)."""
    user.deleted_at = None
    session.add(user)
    session.commit()
