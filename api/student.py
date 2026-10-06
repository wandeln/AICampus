"""
Student-Endpoints: Aufgaben ansehen, Lösungen einreichen, Tests ausführen.

Rolle: Student (im Kurs)
"""

import asyncio
import json
import logging
import re
import shutil
import tarfile
import zlib
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import websockets
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, WebSocket
from fastapi.responses import JSONResponse
from sqlalchemy import update
from sqlmodel import Session, SQLModel, select
from starlette.websockets import WebSocketDisconnect

from compute_agent.auth import make_token
from config import SUBMISSION_DIR
from database.base import engine, get_session
from database.models import (
    Notification,
    User, Task, Submission, Feedback, HintExchange, ScriptSection,
    TaskType, SubmissionStatus, FeedbackSource,
    Course, UserCourse, CourseRole, WorkspaceRun, GlobalUserRole,
)
from services import notifications
from services.auth_service import decode_access_token, get_current_user
from services.compute_client import (
    ComputeAgentError,
    ComputeClient,
)
from services.grading_service import GradingService
from services.import_service import spawn_job
from services.mc_service import McValidationError, grade_mc, mc_report_summary, parse_mc_data
from services.llm_service import LLMService
from services.media_service import all_media_for_course
from services.sandbox_runner import SandboxedRunner
from services.settings_resolver import get_effective_llm_config
from services.workspace_service import (
    MAX_FILE_BYTES,
    effective_file_access,
    effective_folder_access,
    file_disk_path,
    snapshot_list_files,
    snapshot_read_file,
    workspace_key,
    workspace_service,
)

router = APIRouter(prefix="/api/student", tags=["Student"])
grading_service = GradingService()
sandbox_runner = SandboxedRunner()
llm_service = LLMService()
logger = logging.getLogger(__name__)

# Aktive Grading-Jobs: submission_id → asyncio-Task. Ermöglicht den Abbruch
# eines laufenden LLM-Gradings (POST /submissions/{id}/cancel).
_GRADING_TASKS: dict[int, asyncio.Task] = {}


# Helper: Extrahiere PublicTest-Klasse aus test_code-String
def extract_public_tests(test_code: str) -> str:
    """
    Parse PublicTest class from test_code string.
    Returns just the PublicTest class code.
    """
    if not test_code:
        return ""
    # Find class PublicTest ... up to next class definition or end
    match = re.search(r'class PublicTest\(unittest\.TestCase\):([\s\S]*?)(?=class PrivateTest|$)', test_code)
    if match:
        return 'class PublicTest(unittest.TestCase):' + match.group(1).rstrip()
    return ""


# Helper: Filtere nur Public-Test-Resultate
def filter_public_test_results(test_results: list) -> list:
    """
    Filtert Test-Ergebnisse auf PublicTest-Klasse.
    Ein Test ist public wenn der Name 'PublicTest' enthaelt.
    """
    return [t for t in test_results if 'PublicTest' in t.get('name', '')]


# Helper: Ueberprueft Kurs-Zugriff (Student, Tutor oder PROF)
def _check_course_access(session: Session, user: User, course_id: int) -> bool:
    """Returns True if user is a member of the course with any valid role."""
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()
    if not membership:
        return False
    return membership.role_in_course in (
        CourseRole.STUDENT, CourseRole.TUTOR, CourseRole.PROF
    )


# Helper: Formatiere Test-Ausgabe fuer Studierende
def format_test_error(output: str) -> dict:
    """
    Bereitet die Fehlermeldung eines Tests auf.
    Bewahrt Assert-Messages und den vollen Traceback auf.
    Gibt ein Dict zurueck mit 'summary' (lesbare Kurzform) und 'full' (kompletter Traceback).
    """
    if not output:
        return {"summary": "Test fehlgeschlagen. Details unbekannt.", "full": ""}

    lines = output.strip().split('\n')

    # Suche nach der Exception-Zeile und ggf. Assert-Message
    error_type = ""
    error_msg = ""
    found_exception = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not found_exception:
            # Suche nach Exception-Klasse (z.B. AssertionError, ValueError, ...)
            for exc in ('AssertionError', 'ValueError', 'TypeError', 'KeyError', 'IndexError', 'NameError', 'AttributeError', 'RuntimeError', 'ZeroDivisionError'):
                if exc in stripped and '(' in stripped:
                    found_exception = True
                    error_type = exc
                    # Extrahiere die Message aus der Exception-Zeile
                    paren_idx = stripped.index('(')
                    msg_part = stripped[paren_idx+1:].rstrip(')').strip().strip("'\"")
                    error_msg = msg_part
                    break
        else:
            break

    # Baue eine gut lesbare Zusammenfassung
    readable_parts = []
    if error_type:
        readable_parts.append(f"{error_type}")
        if error_msg:
            readable_parts[-1] += f": {error_msg}"

    # Wenn die Exception-Meldung leer war (z.B. AssertionError ohne Message),
    # verwende die erste signifikante Zeile des Tracebacks
    if error_type and not error_msg:
        for l in lines:
            stripped = l.strip()
            if stripped and stripped != error_type:
                readable_parts[-1] += f": {stripped[:120]}"
                break

    readable = " — ".join(readable_parts) if readable_parts else output.strip()[:300]

    return {"summary": readable, "full": output.strip()}


# =================================================================
# AUFGABEN
# =================================================================

@router.get("/tasks/{task_id}")
async def get_task_detail(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Einzelne Aufgabe laden (Student-View: keine Musterloesung!)."""
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == task.course_id)
    ).first()

    if not _check_course_access(session, user, task.course_id):
        raise HTTPException(403, "Kein Zugriff auf diese Aufgabe.")

    public_test_code = extract_public_tests(task.test_code or "")

    return {
        "id": task.id,
        "title": task.title,
        "task_type": task.task_type.value,
        "description": task.description,
        "max_points": task.max_points,
        "max_attempts": task.max_attempts,
        "deadline": task.deadline,
        "code_template": task.code_template if task.task_type.value == "code" else None,
        "text_template": task.text_template if task.task_type.value == "text" else None,
        "public_test_code": public_test_code,
        "hints_enabled": task.hints_enabled,
    }


# =================================================================
# EINREICHUNGEN
# =================================================================

@router.post("/tasks/{task_id}/submit")
async def submit_solution(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """
    Loesung einreichen -> sofort speichern, Grading im Hintergrund.

    Request (je nach Typ):
        Text: { "solution": "..." }
        Code: { "code_solution": "..." }

    Gibt sofort "pending" zurueck. Frontend muss per
    GET /submissions/{id}/result das Ergebnis polling-abfragen.
    """
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    # Check course access
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == task.course_id)
    ).first()

    if not _check_course_access(session, user, task.course_id):
        raise HTTPException(403, "Kein Zugriff auf diese Aufgabe.")

    body = await request.json()

    # Deadline check
    if task.deadline:
        dl_str = task.deadline.replace("Z", "+00:00")
        deadline = datetime.fromisoformat(dl_str)
        # Ensure both datetimes are timezone-aware
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > deadline:
            raise HTTPException(400, "Deadline ist verstrichen.")

    # Max attempts check
    existing_subs = session.exec(
        select(Submission)
        .where(Submission.task_id == task.id)
        .where(Submission.student_id == user.id)
    ).all()
    if task.max_attempts and len(existing_subs) >= task.max_attempts:
        raise HTTPException(400, f"Maximal {task.max_attempts} Versuche erlaubt.")

    # Type narrowing: session.get() gibt immer ein Objekt mit ID zurueck
    assert task.id is not None
    assert user.id is not None

    # Multiple-Choice: SYNCHRONES Grading (kein LLM, kein Background-Job,
    # kein Polling) — das Ergebnis kommt direkt in dieser Antwort zurueck.
    if task.task_type.value == "mc":
        try:
            mc = parse_mc_data(task.mc_data)
        except McValidationError:
            raise HTTPException(
                400, "Diese Multiple-Choice-Aufgabe ist noch nicht vollständig "
                     "konfiguriert — bitte die Lehrperson kontaktieren.")
        answers = body.get("mc_answers")
        try:
            mc_result = grade_mc(mc, answers)
        except McValidationError as e:
            raise HTTPException(400, f"Ungültige Antworten: {e}")

        submission = Submission(
            task_id=task.id,
            student_id=user.id,
            solution=mc_result["summary"],
            # strukturierte Auswahl (Tutor-Review markiert die Optionen)
            code_solution="mc:v1:" + json.dumps(answers),
            # Ergebnis persistieren → Historie-Karte bleibt stabil, auch
            # wenn die Aufgabe später bearbeitet wird.
            mc_result=json.dumps(mc_result),
            attempt_number=len(existing_subs) + 1,
            solve_time_seconds=body.get("solve_time_seconds", 0.0),
            status=SubmissionStatus.GRADED,
        )
        session.add(submission)
        session.commit()
        session.refresh(submission)
        assert submission.id is not None

        session.add(Feedback(
            submission_id=submission.id,
            source=FeedbackSource.LLM,
            points_earned=mc_result["total"],
            # Kein Kommentar-Text: Das komplette Feedback steht direkt in
            # der Ergebnis-Karte (mc_result) — ein Text-Zusammenfassung
            # wäre redundant.
            comment="",
        ))
        session.commit()
        session.refresh(submission)

        response = _build_graded_result(submission, task, user, session)
        response["mc_result"] = mc_result
        return response

    # Create submission (status bleibt PENDING)
    submission = Submission(
        task_id=task.id,
        student_id=user.id,
        solution=body.get("solution", ""),
        code_solution=body.get("code_solution", ""),
        attempt_number=len(existing_subs) + 1,
        solve_time_seconds=body.get("solve_time_seconds", 0.0),
        status=SubmissionStatus.PENDING,
    )

    session.add(submission)
    session.commit()
    session.refresh(submission)

    # Type narrowing: Nach refresh() ist die ID gesetzt
    assert submission.id is not None

    # Workspace-Aufgabe: Loesung = ganzes /workspace-Volume → Snapshot aus
    # dem Student-Container sichern und an die Einreichung hängen. Bei
    # Fehlschlag wird die Einreichung verworfen (kein „verlorener“ Versuch).
    if task.task_type.value == "workspace":
        try:
            rel_path = await asyncio.to_thread(
                workspace_service.save_snapshot,
                session, task, user.id, submission.id)
            submission.workspace_snapshot = rel_path
            session.add(submission)
            session.commit()
        except ComputeAgentError as e:
            session.delete(submission)
            session.commit()
            raise HTTPException(
                503, f"Workspace-Abgabe fehlgeschlagen: {e.message}")

    # Grading im Hintergrund starten — spawn_job haelt eine starke Referenz
    # auf den Task, damit er nicht vom GC weggeraeumt wird, waehrend die
    # Antwort generiert wird (asyncio.haelt nur Weak-References auf Tasks).
    grading_task = spawn_job(
        _run_grading_background(
            task_id=task.id,
            submission_id=submission.id,
            attempt_number=submission.attempt_number,
        )
    )
    # Fur den Abbruch registrieren (POST /submissions/{id}/cancel)
    _GRADING_TASKS[submission.id] = grading_task
    grading_task.add_done_callback(
        lambda t, sid=submission.id: _GRADING_TASKS.pop(sid, None)
    )

    # Sofort zurueckgeben — Frontend pollt das Ergebnis
    return {
        "submission_id": submission.id,
        "attempt_number": submission.attempt_number,
        "status": "pending",
        "message": "Loesung wird korrigiert... bitte warten.",
        "max_points": task.max_points,
        "max_attempts": task.max_attempts,
    }


# ──────────────────────────────────────────────────────────────
# BACKGROUND: Asynchrones Grading (Laeuft im Hintergrund)
# ──────────────────────────────────────────────────────────────

async def _run_grading_background(
    task_id: int,
    submission_id: int,
    attempt_number: int,
):
    """
    Fuehrt das Grading in einem eigenen asyncio-Task aus.
    Verwendet eine eigene DB-Session, um Konflikte mit anderen
    Requests zu vermeiden.
    """
    from database.base import engine
    try:
        with Session(engine) as bg_session:
            task = bg_session.get(Task, task_id)
            submission = bg_session.get(Submission, submission_id)
            if not task or not submission:
                return

            result = await grading_service.grade_submission(
                task, submission, bg_session,
            )
            # Glocke: Student ueber das fertige Feedback informieren
            notifications.notify_llm_feedback(
                session=bg_session,
                course_id=task.course_id,
                student_id=submission.student_id,
                task_id=task.id,
                task_title=task.title,
                points=float(result.get("points", 0) or 0),
                max_points=float(result.get("max_points", 0) or 0),
            )
    except Exception as e:
        # Fehler: Status auf PENDING lassen + Fehler-Feedback speichern
        try:
            with Session(engine) as err_session:
                sub = err_session.get(Submission, submission_id)
                if sub and sub.id is not None:
                    sub.status = SubmissionStatus.PENDING
                    err_session.add(Feedback(
                        submission_id=sub.id,
                        source=FeedbackSource.LLM,
                        points_earned=0,
                        comment=f"Grading-Fehler: {str(e)}",
                    ))
                    err_session.commit()
                    task = err_session.get(Task, task_id)
                    if task:
                        # Glocke: Student ueber den Fehler informieren
                        notifications.notify_llm_feedback(
                            session=err_session,
                            course_id=task.course_id,
                            student_id=sub.student_id,
                            task_id=task.id,
                            task_title=task.title,
                            points=0.0,
                            max_points=0.0,
                            success=False,
                            error=str(e)[:300],
                        )
        except Exception:
            pass  # Logging hier waere ideal, aber nicht kritisch


# ──────────────────────────────────────────────────────────────
# CANCEL: Laufendes Grading abbrechen (Einreichung wird verworfen)
# ──────────────────────────────────────────────────────────────

@router.post("/submissions/{submission_id}/cancel")
async def cancel_grading(
    submission_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """
    Bricht ein laufendes LLM-Grading ab und loescht die Einreichung,
    damit sie NICHT als Versuch gezaehlt wird.

    Nur moeglich, waehrend der Status PENDING ist (Grading laeuft noch).
    Danach (graded/overridden) → 409.
    """
    submission = session.get(Submission, submission_id)
    if not submission:
        raise HTTPException(404, "Einreichung nicht gefunden.")
    if submission.student_id != user.id:
        raise HTTPException(403, "Kein Zugriff.")
    if submission.status != SubmissionStatus.PENDING:
        raise HTTPException(409, "Grading ist bereits beendet — die Einreichung zählt.")

    # 1) Grading-Task stoppen und (mit Zeitlimit) auf seine Beendigung
    #    warten, damit er kein Feedback/Status mehr parallel schreibt.
    #    shield(): Beim Timeout wird nur der Wait abgebrochen, der Task
    #    räumt sich im Hintergrund auf (z. B. laufendes Workspace-Testskript
    #    in einem Thread kann nicht sofort gestoppt werden).
    grading_task = _GRADING_TASKS.pop(submission_id, None)
    if grading_task and not grading_task.done():
        grading_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(grading_task), timeout=10.0)
        except asyncio.CancelledError:
            pass  # Task abgebrochen — genau das Ziel
        except Exception:
            pass  # z. B. Timeout

    # 2) Status frisch laden: Eventuell hat das Grading parallel fertig
    #    geschrieben, bevor die Cancellation wirkte.
    session.refresh(submission)
    if submission.status != SubmissionStatus.PENDING:
        raise HTTPException(409, "Grading ist bereits beendet — die Einreichung zählt.")

    # 3) Einreichung loeschen → wird nirgendwo mehr als Versuch gezählt.
    for fb in list(submission.feedback_list):
        session.delete(fb)
    for run in session.exec(
        select(WorkspaceRun).where(WorkspaceRun.submission_id == submission.id)
    ).all():
        run.submission_id = None
    if submission.workspace_snapshot:
        # Snapshot (data/submissions/{id}/) von der Platte räumen
        shutil.rmtree(SUBMISSION_DIR / str(submission.id), ignore_errors=True)
    session.delete(submission)
    session.commit()

    return {
        "message": "Abgebrochen — diese Einreichung zählt nicht als Versuch.",
    }


# ──────────────────────────────────────────────────────────────
# POLLING: Ergebnis abfragen
# ──────────────────────────────────────────────────────────────

def _auto_read_llm_feedback(session: Session, user: User, task: Task) -> None:
    """Glocke: Wer das Ergebnis abruft, hat es gesehen → Auto-Read + SSE.

    Link-Match: notify_llm_feedback speichert die Ergebnis-Seite als Link
    (/courses/{cid}/tasks/{tid}) — dieselbe Formatierung beidseits.
    """
    try:
        result = session.exec(
            update(Notification)
            .where(Notification.user_id == user.id)
            .where(Notification.type == "llm_feedback")
            .where(Notification.link == f"/courses/{task.course_id}/tasks/{task.id}")
            .where(Notification.read_at.is_(None))
            .values(read_at=datetime.now())
        )
        session.commit()
        if result.rowcount:
            notifications.notify_read(user.id)  # type: ignore[arg-type]
    except Exception:
        session.rollback()


@router.get("/submissions/{submission_id}/result")
async def get_submission_result(
    submission_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """
    Polling-Endpoint: Prueft Status und gibt Ergebnis zurueck,
    sobald das Grading abgeschlossen ist.

    Rueckgabe:
        pending:  { "status": "pending", "message": "..." }
        graded:   Vollstaendiges Ergebnis mit Punkten, Feedback, etc.
    """
    submission = session.get(Submission, submission_id)
    if not submission:
        raise HTTPException(404, "Einreichung nicht gefunden.")

    # Zugriffskontrolle
    if submission.student_id != user.id:
        raise HTTPException(403, "Kein Zugriff.")

    task = session.get(Task, submission.task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    if submission.status.value == "pending":
        # Prüfe ob es ein Fehler-Feedback gibt
        has_error_feedback = False
        for fb in submission.feedback_list:
            if fb.comment.startswith("Grading-Fehler:"):
                has_error_feedback = True
                break

        if has_error_feedback:
            # Liefere den Fehler
            for fb in submission.feedback_list:
                if fb.comment.startswith("Grading-Fehler:"):
                    _auto_read_llm_feedback(session, user, task)
                    return {
                        "status": "error",
                        "submission_id": submission.id,
                        "attempt_number": submission.attempt_number,
                        "message": fb.comment,
                        "points": 0,
                        "comment": fb.comment,
                        "max_points": task.max_points,
                    }

        return {
            "status": "pending",
            "submission_id": submission.id,
            "attempt_number": submission.attempt_number,
            "message": "Loesung wird korrigiert... bitte warten.",
        }

    _auto_read_llm_feedback(session, user, task)
    return _build_graded_result(submission, task, user, session)


def _build_graded_result(submission: Submission, task: Task, user: User,
                         session: Session) -> dict:
    """Vollständiges Ergebnis-Payload für eine abgeschlossene Einreichung
    (Punkte, Versuche, Gruppen-Durchschnitt).

    Wird vom Polling-Endpoint (LLM-Grading) UND vom synchronen
    MC-Grading beim Submit verwendet.
    """
    # Find latest points (from most recent submission)
    existing = session.exec(
        select(Submission)
        .where(Submission.task_id == task.id)
        .where(Submission.student_id == user.id)
        .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
    ).all()
    total_attempts = len(existing)

    # Latest points = Punktzahl der neuesten Einreichung
    latest_points = 0.0
    if existing:
        latest_sub = existing[0]  # newest
        human_points = 0.0
        llm_points = 0.0
        override_exists = False
        for fb in latest_sub.feedback_list:
            if fb.source == FeedbackSource.HUMAN:
                human_points = max(human_points, fb.points_earned)
                override_exists = True
            else:
                llm_points = max(llm_points, fb.points_earned)
        latest_points = human_points if override_exists else llm_points

    points = 0.0
    comment = ""
    all_feedback = submission.feedback_list
    if all_feedback:
        # Take the most recent LLM feedback
        latest = max(all_feedback, key=lambda f: f.created_at)
        points = latest.points_earned
        comment = latest.comment

    response = {
        "status": submission.status.value,
        "submission_id": submission.id,
        "attempt_number": submission.attempt_number,
        "total_attempts": total_attempts,
        "submitted_at": submission.submitted_at.isoformat() if submission.submitted_at else "",
        "points": points,
        "latest_points": latest_points,
        "max_points": task.max_points,
        "max_attempts": task.max_attempts,
        "comment": comment,
    }

    # Remaining attempts
    if task.max_attempts:
        response["remaining_attempts"] = task.max_attempts - total_attempts

    # Gruppen-Durchschnitt: Alle Kursstudenten zählen (ohne Abgabe = 0 Punkte)
    course_students = session.exec(
        select(UserCourse)
        .where(UserCourse.course_id == task.course_id)
        .where(UserCourse.role_in_course == CourseRole.STUDENT)
    ).all()
    group_scores = []
    for gm in course_students:
        gm_subs = session.exec(
            select(Submission)
            .where(Submission.task_id == task.id)
            .where(Submission.student_id == gm.user_id)
            .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
        ).all()
        if gm_subs:
            gm_latest = gm_subs[0]
            gm_human = 0.0
            gm_llm = 0.0
            gm_override = False
            for fb in gm_latest.feedback_list:
                if fb.source == FeedbackSource.HUMAN:
                    gm_human = max(gm_human, fb.points_earned)
                    gm_override = True
                else:
                    gm_llm = max(gm_llm, fb.points_earned)
            group_scores.append(gm_human if gm_override else gm_llm)
        else:
            group_scores.append(0.0)
    task_group_avg = round(sum(group_scores) / len(group_scores), 1) if group_scores else 0.0

    response["task_group_avg"] = task_group_avg
    return response


# =================================================================
# WORKSPACE-ABGABEN (read-only Ansicht des Snapshot je Einreichung)
#
# Student (eigene Abgaben) und Tutor/PROF (Review) sehen nachträglich
# den Workspace-Stand beim Einreichen: Dateibaum + Editor, ohne
# Terminals/Ports/Aufgabenstellung.
# =================================================================

def _load_submission_workspace(
    submission_id: int, session: Session, user: User
) -> tuple[Submission, Task, str]:
    """Workspace-Einreichung mit Snapshot laden (404/403/400).

    Zugriff: der Student selbst oder PROF/Tutor/Global-Admin des Kurses
    (Tutor-Review). Liefert (Submission, Task, Snapshot-Pfad) — der Pfad
    ist dabei verifiziert (sonst HTTP-Error)."""
    submission = session.get(Submission, submission_id)
    if not submission:
        raise HTTPException(404, "Einreichung nicht gefunden.")
    task = session.get(Task, submission.task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")
    if submission.student_id != user.id:
        # Review-Zugriff: PROF/Tutor des Kurses oder Global-Admin
        allowed = user.role == GlobalUserRole.ADMIN
        if not allowed:
            membership = session.exec(
                select(UserCourse)
                .where(UserCourse.user_id == user.id)
                .where(UserCourse.course_id == task.course_id)
            ).first()
            allowed = bool(
                membership
                and membership.role_in_course
                in (CourseRole.PROF, CourseRole.TUTOR)
            )
        if not allowed:
            raise HTTPException(403, "Nur Zugriff auf eigene Einreichungen.")
    snapshot = submission.workspace_snapshot
    if task.task_type.value != "workspace" or not snapshot:
        raise HTTPException(400, "Kein Workspace-Snapshot vorhanden.")
    return submission, task, snapshot


def _read_readonly_task_file(session: Session, task: Task,
                             path: str) -> Optional[bytes]:
    """Read-only-Mount-Datei aus dem Server-Task-Dir lesen.

    Liefert None, wenn der Pfad keine read-only Task-Datei ist (oder die
    Datei auf der Platte fehlt) — dann greift kein Fallback."""
    row = workspace_service.get_task_file(session, task, path)
    if not row:
        return None
    fm = workspace_service.folder_map(session, task)
    if effective_file_access(path, row.access, fm) != "readonly":
        return None
    p = file_disk_path(task.id, path)
    if not p.is_file():
        return None
    return p.read_bytes()


@router.get("/submissions/{submission_id}/workspace/files")
async def submission_workspace_files(
    submission_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Dateibaum des Workspace-Snapshots (read-only, Stand bei Einreichung)."""
    _submission, task, snapshot = _load_submission_workspace(submission_id, session, user)
    try:
        raw = await asyncio.to_thread(snapshot_list_files, snapshot)
    except (OSError, tarfile.TarError, EOFError, zlib.error, ValueError):
        raise HTTPException(422, "Snapshot nicht lesbar (Datei beschädigt?).")
    # Zugriffs-Klassen + Sortierung aus der Task-DB (gleiche Baum-Icons
    # wie im Live-Workspace); Dateien, die nicht mehr in der Task gepflegt
    # werden, fallen auf edit zurück.
    fm = workspace_service.folder_map(session, task)
    file_rows = {f.path: f
                 for f in workspace_service.task_files(session, task)}
    # Read-only-Mount-Dateien (Skripte, Datasets …) liegen NICHT im
    # Snapshot (im Container ist es ein separater ro-Mount) — sie werden
    # aus dem Task-Dir auf dem Server ergänzt (aktuelle Fassung, kein
    # zusätzlicher Speicher). Im Snapshot vorhandene Pfade gewinnen.
    snap_paths = {f["path"] for f in raw}
    for row in file_rows.values():
        if row.path in snap_paths:
            continue
        if effective_file_access(row.path, row.access, fm) != "readonly":
            continue
        dp = file_disk_path(task.id, row.path)
        if not dp.is_file():
            continue
        is_binary = False
        try:
            with open(dp, "rb") as fh:
                is_binary = b"\x00" in fh.read(1024)
            raw.append({"path": row.path, "size": dp.stat().st_size,
                        "is_binary": is_binary})
        except OSError:
            continue
    for f in raw:
        p = f["path"]
        row = file_rows.get(p)
        f["access"] = effective_file_access(p, row.access if row else None, fm)
        f["file_access"] = row.access if row else None
        f["sort_order"] = row.sort_order if row else None
    # Ordner: explizite Klassen-Zeilen der Task + alle Teilverzeichnisse
    # aus den Snapshot-Pfaden (leere Ordner bleiben sichtbar).
    folder_paths = set(fm)
    for f in raw:
        parts = f["path"].split("/")
        for i in range(1, len(parts)):
            folder_paths.add("/".join(parts[:i]))
    out_folders = [{
        "path": p,
        "access": effective_folder_access(p, fm),
        "own": fm.get(p),
    } for p in sorted(folder_paths)
        if effective_folder_access(p, fm) != "hidden"]
    return JSONResponse(
        content={
            "files": raw,
            "folders": out_folders,
            "folder_order": workspace_service.order_map(session, task),
            "total": sum(f["size"] for f in raw),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/submissions/{submission_id}/workspace/files/{path:path}")
async def submission_workspace_file(
    submission_id: int,
    path: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Datei-Inhalt aus dem Snapshot (für den Editor). Binarys: octet-stream."""
    _submission, task, snapshot = _load_submission_workspace(submission_id, session, user)
    p = _safe_ws_path(path)
    try:
        data = await asyncio.to_thread(snapshot_read_file, snapshot, p)
    except (OSError, tarfile.TarError, EOFError, zlib.error, ValueError):
        raise HTTPException(422, "Snapshot nicht lesbar (Datei beschädigt?).")
    if data is None:
        # Fallback: Read-only-Mount-Dateien fehlen im Snapshot → aus dem
        # Server-Task-Dir (aktuelle Fassung) laden.
        data = await asyncio.to_thread(_read_readonly_task_file, session, task, p)
    if data is None:
        raise HTTPException(404, "Datei nicht gefunden.")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß für den Editor (max. 50 MB).")
    is_binary = b"\x00" in data[:1024]
    headers = {"Cache-Control": "no-store"}
    if is_binary:
        headers["Content-Disposition"] = f'attachment; filename="{p.split("/")[-1]}"'
    return Response(
        content=data,
        media_type="application/octet-stream" if is_binary else "text/plain; charset=utf-8",
        headers=headers,
    )


# =================================================================
# CODE-AUSFUEHRUNG (ohne Tests, nur stdout)
# =================================================================

@router.post("/tasks/{task_id}/run-code")
async def run_code(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """
    Code ausfuehren ohne Unit-Tests — zeigt nur die Konsolen-Ausgabe.
    Laeuft mit Timeout (sandbox config) — blockiert den Server NICHT
    fueher als SANDBOX_TIMEOUT Sekunden.
    """
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    if task.task_type.value != "code":
        raise HTTPException(400, "Nur bei Code-Aufgaben verfuegbar.")

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == task.course_id)
    ).first()

    if not membership:
        raise HTTPException(403, "Kein Zugriff.")

    body = await request.json()
    code = body.get("code_solution", "")

    if not code.strip():
        raise HTTPException(400, "Kein Code eingereicht.")

    # Fuehre Code in Sandbox aus (async, mit Timeout)
    result = await sandbox_runner.run_code_only(code=code)

    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")

    # Behalte den vollen Python-Traceback anstelle ihn zu ersetzen
    if stderr:
        if "IndentationError" in stderr:
            match = re.search(r'line\s+(\d+)', stderr)
            line = match.group(1) if match else "unbekannt"
            stderr = f"Eindeckungsfehler (Indentation) in Zeile {line}. Ueberpruefe deine Einrueckung (Tab vs. Leerzeichen).\n\nOriginal:\n{stderr}"

    images = result.get("images", [])

    return {
        "stdout": stdout,
        "stderr": stderr,
        "error": stderr or None,
        "timeout": bool(stderr and "Zeitlimit" in stderr),
        "images": images,
    }


# =================================================================
# PUBLIC TESTS (schnelles Feedback, kein Grading)
# =================================================================

@router.post("/tasks/{task_id}/run-tests")
async def run_public_tests(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """
    Public Tests ausfuehren (server-seitig, sandbox).

    Kein LLM-Grading — nur Test-Ergebnisse fuer schnelles Feedback.
    Laeuft mit Timeout — blockiert den Server NICHT fueher als SANDBOX_TIMEOUT.
    """
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    if task.task_type.value != "code":
        raise HTTPException(400, "Nur bei Code-Aufgaben verfuegbar.")

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == task.course_id)
    ).first()

    if not membership:
        raise HTTPException(403, "Kein Zugriff.")

    public_test_code = extract_public_tests(task.test_code or "")
    if not public_test_code:
        return {"message": "Keine Public Tests fuer diese Aufgabe.", "test_results": []}

    body = await request.json()
    code = body.get("code_solution", "")

    if not code.strip():
        raise HTTPException(400, "Kein Code eingereicht.")

    result = await sandbox_runner.run(code=code, tests_code=public_test_code)

    # Formatte Fehlermeldungen fuer bessere Lesbarkeit
    formatted_results = []
    for t in result.get("test_results", []):
        formatted = {
            "name": t.get("name", "Unbekannt"),
            "passed": t.get("passed", False),
        }
        if not formatted["passed"]:
            raw_output = t.get("output", "")
            formatted_error = format_test_error(raw_output)
            formatted["error"] = formatted_error
        formatted_results.append(formatted)

    has_timeout = result.get("timeout", False)
    stderr_val = result.get("stderr", "")
    if "Zeitlimit" in stderr_val:
        has_timeout = True

    return {
        "passed": result.get("passed", False),
        "test_results": formatted_results,
        "tests_passed": sum(1 for t in formatted_results if t.get("passed")),
        "tests_total": len(formatted_results),
        "stdout": result.get("stdout", ""),
        "stderr": stderr_val,
        "timeout": has_timeout,
    }


# =================================================================
# FEEDBACK & PUNKTE
# =================================================================

@router.get("/courses/{course_id}/my-submissions")
async def get_my_submissions(
    course_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Alle eigenen Einreichungen im Kurs."""
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()

    if not membership:
        raise HTTPException(403, "Kein Kurs-Mitglied.")

    tasks = session.exec(
        select(Task).where(Task.course_id == course_id)
    ).all()

    my_submissions = []
    for task in tasks:
        subs = session.exec(
            select(Submission)
            .where(Submission.task_id == task.id)
            .where(Submission.student_id == user.id)
            .order_by(Submission.submitted_at.desc())
        ).all()

        if not subs:
            continue

        latest_sub = subs[0]  # newest (already ordered desc)
        human_points = 0.0
        llm_points = 0.0
        override_exists = False
        for fb in latest_sub.feedback_list:
            if fb.source == FeedbackSource.HUMAN:
                human_points = max(human_points, fb.points_earned)
                override_exists = True
            else:
                llm_points = max(llm_points, fb.points_earned)
        latest_points = human_points if override_exists else llm_points


        my_submissions.append({
            "task_id": task.id,
            "task_title": task.title,
            "max_points": task.max_points,
            "latest_points": latest_points,
            "attempt_count": len(subs),
        })

    return {
        "course_id": course_id,
        "submissions": my_submissions,
    }


@router.get("/courses/{course_id}/my-points")
async def get_my_points(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Punkteübersicht und Aufgabenliste für alle (gefilterten) Aufgaben im Kurs."""
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()

    if not membership:
        raise HTTPException(403, "Kein Zugriff.")

    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    # Filter aus Query-Parametern
    filter_text = request.query_params.get("filter_text", "").strip()
    type_filter = request.query_params.get("type_filter", "").strip()

    tasks = session.exec(
        select(Task)
        .where(Task.course_id == course_id)
        .where(Task.is_visible == True)
        .order_by(Task.display_order.asc())  # type: ignore[attr-defined]
    ).all()

    if filter_text:
        tasks = [t for t in tasks if filter_text.lower() in t.title.lower()]
    if type_filter:
        tasks = [t for t in tasks if t.task_type.value == type_filter]

    task_list = []
    total_points = 0.0
    max_points = 0

    for task in tasks:
        max_points += task.max_points
        subs = session.exec(
            select(Submission)
            .where(Submission.task_id == task.id)
            .where(Submission.student_id == user.id)
            .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
        ).all()

        human_points = 0.0
        llm_points = 0.0
        override_exists = False
        has_feedback = False
        if subs:
            latest_sub = subs[0]  # newest
            for fb in latest_sub.feedback_list:
                has_feedback = True
                if fb.source == FeedbackSource.HUMAN:
                    human_points = max(human_points, fb.points_earned)
                    override_exists = True
                else:
                    llm_points = max(llm_points, fb.points_earned)
        my_points = human_points if override_exists else llm_points

        total_points += my_points

        task_list.append({
            "id": task.id,
            "title": task.title,
            "task_type": task.task_type.value,
            "max_points": task.max_points,
            "max_attempts": task.max_attempts,
            "attempts_used": len(subs),
            "deadline": task.deadline,
            "has_tests": bool(task.test_code),
            "my_points": my_points,
            "has_feedback": has_feedback,
        })

    # Berechne Perzentil im Kurs: Gesamtpunkte der gefilterten Aufgaben vergleichen
    student_members = session.exec(
        select(UserCourse)
        .where(UserCourse.course_id == course_id)
        .where(UserCourse.role_in_course == CourseRole.STUDENT)
    ).all()

    other_total_scores = []
    for member in student_members:
        if member.user_id == user.id:
            continue
        member_total = 0.0
        for task in tasks:  # Gefilterte Aufgaben (konsistent mit total_points)
            m_subs = session.exec(
                select(Submission)
                .where(Submission.task_id == task.id)
                .where(Submission.student_id == member.user_id)
                .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
            ).all()
            if m_subs:
                m_latest = m_subs[0]
                m_human = 0.0
                m_llm = 0.0
                m_override = False
                for fb in m_latest.feedback_list:
                    if fb.source == FeedbackSource.HUMAN:
                        m_human = max(m_human, fb.points_earned)
                        m_override = True
                    else:
                        m_llm = max(m_llm, fb.points_earned)
                member_total += m_human if m_override else m_llm
        other_total_scores.append(member_total)

    below = sum(1 for s in other_total_scores if s < total_points)
    equal = sum(1 for s in other_total_scores if s == total_points)
    if other_total_scores:
        course_percentile = round((below + 0.5 * equal) / len(other_total_scores) * 100)
    else:
        course_percentile = 100

    # Gruppen-Durchschnitt
    all_scores = other_total_scores + [total_points]
    group_avg = round(sum(all_scores) / len(all_scores), 1)

    return {
        "total_points": round(total_points, 1),
        "max_points": max_points,
        "percentage": round(total_points / max_points * 100, 1) if max_points else 0,
        "course_percentile": course_percentile,
        "group_avg": group_avg,
        "tasks": task_list,
    }


# =================================================================
# FOTO -> LATEX KONVERTIERUNG
# =================================================================

@router.post("/latex-from-image")
async def latex_from_image(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """
    Konvertiert ein Foto (Base64) einer handgeschriebenen Notiz mit Formeln
    in Markdown mit LaTeX-Code via LLM.
    """
    body = await request.json()
    image_base64 = body.get("image_base64", "")
    mime_type = body.get("mime_type", "image/png")

    if not image_base64:
        raise HTTPException(400, "Kein Bild uebergeben.")

    # Resolve effektive LLM-Config (wird vom LLM-Service genutzt)
    llm_cfg = get_effective_llm_config(session)

    result = await llm_service.convert_image_to_latex(
        image_base64=image_base64,
        mime_type=mime_type,
        config=llm_cfg,
    )

    if not result.get("success"):
        raise HTTPException(500, f"LLM-Fehler: {result.get('error', 'Unbekannter Fehler')}")

    return {
        "latex": result.get("data", {}).get("latex", ""),
        "latency_ms": result.get("latency_ms", 0),
    }


# =================================================================
# SOKRATISCHE HINWEISE (Hints)
# =================================================================

class HintRequest(SQLModel):
    question: str
    current_solution: str = ""


@router.post("/tasks/{task_id}/hints")
async def request_hint(
    task_id: int,
    hint_request: HintRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Student fragt einen sokratischen Hinweis bei.

    Das LLM bekommt Kontext (Aufgabenstellung, Musterloesung, aktuelle Loesung,
    vorherige Submissions mit Feedback, Hinweisverlauf) und antwortet in
    sokratischer Weise, ohne die Loesung zu verraten.
    """
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    if not task.hints_enabled:
        raise HTTPException(403, "Hinweise sind fuer diese Aufgabe deaktiviert.")

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == task.course_id)
    ).first()

    if not membership:
        raise HTTPException(403, "Kein Zugriff auf diese Aufgabe.")

    # Sammle Hinweise-Verlauf (letzte 5) fuer Kontext
    hint_history = session.exec(
        select(HintExchange)
        .where(HintExchange.task_id == task.id)
        .where(HintExchange.student_id == user.id)
        .order_by(HintExchange.created_at.desc())  # type: ignore[attr-defined]
    ).all()

    hint_history_text = ""
    if hint_history:
        lines = []
        for i, he in enumerate(hint_history[:5]):
            lines.append(f"Frage {i+1}: {he.question}")
            lines.append(f"Antwort {i+1}: {he.llm_response}")
            lines.append("")
        hint_history_text = "\n".join(lines)

    # Sammle vorherige Submissions mit Feedback
    prev_submissions = session.exec(
        select(Submission)
        .where(Submission.task_id == task.id)
        .where(Submission.student_id == user.id)
        .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
    ).all()

    prev_submissions_text = ""
    if prev_submissions:
        lines = []
        for sub in prev_submissions[:5]:
            solution_text = sub.solution if task.task_type.value == "text" else sub.code_solution
            lines.append(f"Abgabe #{sub.attempt_number}: {solution_text[:500]}")
            for fb in sub.feedback_list:
                lines.append(f"  Feedback: {fb.comment}")
                lines.append(f"  Punkte: {fb.points_earned}/{task.max_points}")
            lines.append("")
        prev_submissions_text = "\n".join(lines)

    # Erstelle HintExchange-Eintrag
    hint_exchange = HintExchange(
        task_id=task.id,
        student_id=user.id,
        question=hint_request.question,
        current_solution=hint_request.current_solution,
    )
    session.add(hint_exchange)
    session.commit()
    session.refresh(hint_exchange)

    # Resolve effektive LLM-Config
    llm_cfg = get_effective_llm_config(session)

    # Skript-Kontext für konsistente Notation & Querverweise im Hinweis.
    # NUR sichtbare Kapitel: der Student sieht genau diese — Labels aus
    # versteckten Kapiteln wären kaputte Referenzen (❓) im Hinweis.
    script_chapters = [
        (s.title, (s.summary or "").strip())
        for s in session.exec(
            select(ScriptSection)
            .where(ScriptSection.course_id == task.course_id)
            .where(ScriptSection.is_visible)
            .order_by(ScriptSection.display_order.asc())  # type: ignore[union-attr]
        ).all()
        if (s.summary or "").strip()
    ][:20]
    script_context = ""
    if script_chapters:
        lines = [f"- {t} — {s}" for t, s in script_chapters]
        script_context = (
            "SKRIPT-KAPITEL DES KURSES (mit ihren internen Zusammenfassungen):\n"
            + "\n".join(lines)
            + "\nHalte die Notation, Schreibweisen und Begriffswahl konsistent mit dem Skript, wo dies sinnvoll ist."
            + "\nQuerverweise: Auf Abbildungen/Gleichungen aus dem Skript verweist du im Hinweis per @fig:label bzw. @eq:label — verwende NUR Labels, die in den obigen Zusammenfassungen vorkommen (sonst ist die Referenz kaputt). Lege KEINE neuen fig/eq-Labels an."
            + "\nWICHTIG: @fig:label / @eq:label sind KEIN Code — schreibe sie IMMER als normalen Fließtext, NIEMALS in Backticks (`...`), Code-Blöcke (``` ... ```) oder Anführungszeichen. Nur so werden sie zu klickbaren Referenzen aufgelöst. Richtig: „wie in @eq:shannon gezeigt“ — Falsch: „wie in `@eq:shannon` gezeigt“."
        )

    course_media = all_media_for_course(session, task.course_id)
    media_context = ""
    if course_media:
        lines = [
            f"- {m['title']}{' — ' + m['description'] if m['description'] else ''} | ![{m['title']}]({m['url']})"
            for m in course_media
        ]
        media_context = (
            "MEDIEN DES KURSES (Titel — Beschreibung | Einbindung-Snippet):\n"
            + "\n".join(lines)
            + "\nEin inhaltlich passendes Medium aus dieser Liste DARFST du im Hinweis einbinden (max. 1) — verwende dafür exakt den angegebenen /media/-Pfad. Erfinde KEINE anderen Medien-Pfade."
        )

    # Rufe LLM fuer sokratischen Hinweis auf
    result = await llm_service.generate_socratic_hint(
        task_description=task.description,
        model_solution=task.model_solution or "(Keine Musterloesung hintergelegt)",
        # Vorlagen-Kontext: Code-Aufgabe → Code-Template, Text-Aufgabe → Text-Vorlage
        code_template=(task.code_template if task.task_type.value == "code" else task.text_template) or "",
        current_solution=hint_request.current_solution,
        previous_submissions=prev_submissions_text,
        hint_history=hint_history_text,
        student_question=hint_request.question,
        script_context=script_context,
        media_context=media_context,
        config=llm_cfg,
    )

    if not result.get("success"):
        error_msg = result.get("error", "Unbekannter Fehler")
        hint_exchange.llm_response = f"Fehler beim Generieren des Hinweises: {error_msg}"
        session.commit()
        raise HTTPException(500, f"LLM-Fehler: {error_msg}")

    # Speichere LLM-Antwort
    llm_data = result.get("data", {})
    hint_text = llm_data.get("hint", "Konnte keinen Hinweis generieren.")
    hint_exchange.llm_response = hint_text
    hint_exchange.response_at = datetime.now()
    session.commit()
    session.refresh(hint_exchange)

    return {
        "id": hint_exchange.id,
        "question": hint_exchange.question,
        "llm_response": hint_text,
        "suggestion_type": llm_data.get("suggestion_type", "hint"),
        "created_at": hint_exchange.created_at.isoformat(),
        "response_at": hint_exchange.response_at.isoformat() if hint_exchange.response_at else None,
        "latency_ms": result.get("latency_ms", 0),
    }


@router.get("/tasks/{task_id}/hints")
async def get_hints(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Lädt den Hinweis-Verlauf fuer eine Aufgabe."""
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == task.course_id)
    ).first()

    if not membership:
        raise HTTPException(403, "Kein Zugriff auf diese Aufgabe.")

    hints = session.exec(
        select(HintExchange)
        .where(HintExchange.task_id == task.id)
        .where(HintExchange.student_id == user.id)
        .order_by(HintExchange.created_at.asc())  # type: ignore[attr-defined]
    ).all()

    return [
        {
            "id": h.id,
            "question": h.question,
            "llm_response": h.llm_response,
            "current_solution": h.current_solution,
            "created_at": h.created_at.isoformat(),
            "response_at": h.response_at.isoformat() if h.response_at else None,
        }
        for h in hints
    ]


@router.post("/courses/{course_id}/generate-report")
async def generate_student_report(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Generiert einen persönlichen Performance-Report für den Studenten via LLM.

    Sammelt alle Daten der gefilterten Aufgaben (Aufgaben, eigene Submissions,
    Feedback, Hinweise, Bearbeitungsdauer) und lässt das LLM einen strukturierten
    Markdown-Report erstellen, der dem Studenten hilft, seine Schwächen zu erkennen
    und Tipps zur Verbesserung liefert.
    """
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()

    if not membership:
        raise HTTPException(403, "Du bist kein Mitglied dieses Kurses.")

    # Filter aus Query-Parametern (identisch zu Tutor-Overview)
    filter_text = request.query_params.get("filter_text", "").strip()
    type_filter = request.query_params.get("type_filter", "").strip()

    # Alle sichtbaren Aufgaben des Kurses
    tasks = list(session.exec(
        select(Task)
        .where(Task.course_id == course_id)
        .where(Task.is_visible == True)
        .order_by(Task.display_order.asc())  # type: ignore[attr-defined]
    ).all())

    if filter_text:
        tasks = [t for t in tasks if filter_text.lower() in t.title.lower()]
    if type_filter:
        tasks = [t for t in tasks if t.task_type.value == type_filter]

    if not tasks:
        raise HTTPException(400, "Keine Aufgaben gefunden. Passe den Filter an.")

    # ── Daten für das LLM sammeln ──────────────────────────────

    # Aufgaben-Informationen formatieren
    tasks_lines = []
    for t in tasks:
        tasks_lines.append(f"- **{t.title}** ({t.task_type.value}) — {t.max_points} Punkte, Max. Versuche: {t.max_attempts or 'unlimitiert'}")
        tasks_lines.append(f"  Aufgabenstellung: {t.description[:500]}")
        if t.model_solution:
            tasks_lines.append(f"  Musterlösung: {t.model_solution[:500]}")
        tasks_lines.append("")
    tasks_data = "\n".join(tasks_lines)

    # Persönliche Studentendaten formatieren (Submissions + Feedback + Hinweise)
    student_lines = []
    student_lines.append(f"Student: {user.name} ({user.username})")
    student_lines.append("")

    has_submissions = False
    for task in tasks:
        # Alle Submissions des Studenten für diese Aufgabe
        subs = session.exec(
            select(Submission)
            .where(Submission.task_id == task.id)
            .where(Submission.student_id == user.id)
            .order_by(Submission.submitted_at.asc())  # type: ignore[attr-defined]
        ).all()

        if not subs:
            student_lines.append(f"- **{task.title}**: Keine Einreichung")
            student_lines.append("")
            continue

        has_submissions = True
        best_points = 0.0
        total_attempts = len(subs)
        latest_sub = subs[-1]

        # Punkte aus Feedback sammeln
        for fb in latest_sub.feedback_list:
            if fb.source == FeedbackSource.HUMAN:
                best_points = max(best_points, fb.points_earned)
            else:
                best_points = max(best_points, fb.points_earned)

        # Solve-Zeiten
        solve_times = [s.solve_time_seconds for s in subs if s.solve_time_seconds > 0]
        avg_time = sum(solve_times) / len(solve_times) if solve_times else 0
        max_time = max(solve_times) if solve_times else 0

        student_lines.append(f"- **{task.title}**:")
        student_lines.append(f"  Punkte: {best_points}/{task.max_points}, Versuche: {total_attempts}")
        if solve_times:
            student_lines.append(f"  Bearbeitungsdauer: Ø {avg_time/60:.1f}min, Max {max_time/60:.1f}min")

        # Feedback-Kommentare der letzten Submission (leere Kommentare
        # überspringen — bei MC ist der Kommentar per Design leer)
        for fb in latest_sub.feedback_list:
            if not fb.comment:
                continue
            source_label = "LLM" if fb.source == FeedbackSource.LLM else "Tutor"
            student_lines.append(f"  [{source_label}]: {fb.comment[:300]}")

        # MC: Feedback-Texte liegen in mc_result → kompakte Zusammenfassung
        # für das LLM (wird NICHT in der Feedback-UI angezeigt)
        if task.task_type.value == "mc" and latest_sub.mc_result:
            try:
                mc_lines = mc_report_summary(json.loads(latest_sub.mc_result))
                student_lines.extend("  " + ln for ln in mc_lines)
            except (json.JSONDecodeError, TypeError):
                pass

        # Hinweis-Anfragen für diese Aufgabe
        hints = session.exec(
            select(HintExchange)
            .where(HintExchange.task_id == task.id)
            .where(HintExchange.student_id == user.id)
        ).all()
        if hints:
            student_lines.append(f"  Hinweise angefragt: {len(hints)} mal")
            for h in hints[:3]:
                student_lines.append(f"    Frage: {h.question[:200]}")
                student_lines.append(f"    Antwort: {h.llm_response[:200]}")

        student_lines.append("")

    if not has_submissions:
        student_lines.append("Keine Einreichungen bei den gefilterten Aufgaben.")

    student_data = "\n".join(student_lines)

    # LLM-Config ermitteln
    llm_config = get_effective_llm_config(session, course_id)

    # Report generieren
    logger.info(f"Generate student report for {course.name}: {len(tasks)} tasks")
    result = await llm_service.generate_student_report(
        course_name=course.name,
        tasks_data=tasks_data,
        student_data=student_data,
        config=llm_config,
    )

    if not result["success"]:
        raise HTTPException(
            500,
            f"Report-Generierung fehlgeschlagen: {result.get('error', 'Unbekannter Fehler')}",
        )

    return {
        "success": True,
        "report": result["data"]["report"],
        "latency_ms": result["latency_ms"],
    }


# =================================================================
# WORKSPACE (Student-IDE: Dateien + Ausführung im eigenen Container)
#
# Alle Endpunkte greifen auf den EIGENEN Container des Nutzers zu
# (Key = ws-{course}-{task}-{user.id}; Tutoren-Preview via as_student
# läuft damit auf dem Container des Tutoren-Accounts).
# Im Container sichtbar: /workspace (editierbares Volumen) + je 🔒-
# Top-Level-Pfad ein ro-Bind-Mount (geteilte Datasets/Pakete). 👤-Pfade
# sind nicht gemountet und fließen nie in die Datei-API.
# =================================================================

def _safe_ws_path(path: str) -> str:
    """Pfad validieren (relativ, kein ..)."""
    p = str(path).replace("\\", "/").lstrip("/")
    if not p or any(x == ".." for x in p.split("/")):
        raise HTTPException(400, "Ungültiger Dateipfad")
    return p


async def _load_ws_task(task_id: int, session: Session, user: User) -> Task:
    """Task laden + Kurs-Zugriff + Workspace-Typ prüfen."""
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")
    if not _check_course_access(session, user, task.course_id):
        raise HTTPException(403, "Kein Zugriff auf diese Aufgabe.")
    if task.task_type.value != "workspace":
        raise HTTPException(400, "Keine Workspace-Aufgabe.")
    return task


def _ws_client_or_error(session: Session, task: Task) -> ComputeClient:
    """Gesunden Agent wählen oder 503 (degradierter Modus).
    Routing über den Engine-Pool der Aufgabe.
    Zusätzlich: Aufgabe muss lauffähig sein (Image + Engines hinterlegt),
    sonst graceful 503 — deckt alle Workspace-Endpoints ab."""
    if not workspace_service.is_enabled(session, task.course_id):
        raise HTTPException(503, "Workspace-Aufgaben sind derzeit deaktiviert.")
    not_ready = workspace_service.validate_task_ready(session, task)
    if not_ready:
        raise HTTPException(503, not_ready)
    agent = workspace_service.pick_task_agent(session, task)
    if agent is None:
        raise HTTPException(
            503, workspace_service.pick_agent_error(
                session, task.course_id, workspace_service.task_engine_names(task)))
    return workspace_service.client_for(agent)


def _agent_http(e: ComputeAgentError) -> HTTPException:
    """Agent-Fehler → HTTP-Fehler (Agent-5xx → 502)."""
    status = e.status if e.status < 500 else 502
    return HTTPException(status, e.message)


def _ws_key(task: Task, user: User) -> str:
    return workspace_key(task.course_id, task.id, user.id)


async def _ws_error_close(ws: WebSocket, message: str) -> None:
    """WS-Fehlermeldung (JSON) + Close — für die Terminal-Route."""
    try:
        await ws.send_text(json.dumps({"type": "error", "message": message}))
    except Exception:
        pass
    try:
        await ws.close(code=1011)
    except Exception:
        pass


def _ws_effective_access(session: Session, task: Task, path: str) -> str:
    """Effektive Zugriffs-Klasse eines Pfads (eigene + Ordner-Vorfahren)."""
    fm = workspace_service.folder_map(session, task)
    file_acc = {f.path: f.access
                for f in workspace_service.task_files(session, task)}
    return effective_file_access(path, file_acc.get(path), fm)


def _ws_require_writable(session: Session, task: Task, path: str) -> None:
    """Studenten schreiben nur Dateien der effektiven Klasse ✏️ edit:
    👤-Dateien existieren für sie nicht (404), 🔒-Dateien sind read-only (403)."""
    p = str(path).replace("\\", "/").lstrip("/")
    acc = _ws_effective_access(session, task, p)
    if acc == "hidden":
        raise HTTPException(404, "Datei nicht gefunden.")
    if acc == "readonly":
        raise HTTPException(403, "Diese Datei ist read-only (vom Tutor verwaltet).")


@router.get("/tasks/{task_id}/workspace/status")
async def workspace_status(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Workspace-Status für die Lösungs-UI (Container, Preset, degraded).

    Nebeneffekt: Container wird idempotent gesichert (gestartet/erstellt).
    """
    task = await _load_ws_task(task_id, session, user)
    out = {
        "enabled": workspace_service.is_enabled(session, task.course_id),
        "degraded": False,
        "agent": None,
        "container": None,
        "error": None,
        "main_file": task.workspace_main_file,
        "timeout": task.workspace_timeout,
        "memory": task.workspace_memory,
        "assets": [],
        "disk": None,
        "ports": [],
    }
    if not out["enabled"]:
        out["degraded"] = True
        out["error"] = "Workspace-Aufgaben sind derzeit deaktiviert."
        return out

    not_ready = workspace_service.validate_task_ready(session, task)
    if not_ready:
        out["degraded"] = True
        out["error"] = not_ready
        return out

    try:
        client = _ws_client_or_error(session, task)
    except HTTPException as e:
        out["degraded"] = True
        out["error"] = e.detail
        return out

    try:
        ws = await asyncio.to_thread(
            workspace_service.ensure_workspace, session, task, user.id)
        out["agent"] = ws.get("agent")
        out["container"] = {"state": ws.get("state"),
                            "fresh": bool(ws.get("fresh")),
                            "held": ws.get("held")}
        assets = await asyncio.to_thread(client.list_assets, task.course_id, task.id)
        out["assets"] = [{"path": a.get("path"), "size": a.get("size")} for a in assets]
        # Disk-Quota-Status (Usage/Quota/over) — isoliert, damit ein
        # gemessenes Fehlverhalten den Rest des Status nicht down macht.
        try:
            out["disk"] = await asyncio.to_thread(client.disk, _ws_key(task, user))
        except ComputeAgentError:
            out["disk"] = None
        # Lauschende Ports (Preview-UI) — isoliert wie disk.
        try:
            out["ports"] = await asyncio.to_thread(
                client.ports, _ws_key(task, user))
        except ComputeAgentError:
            out["ports"] = []
    except ComputeAgentError as e:
        out["degraded"] = True
        out["error"] = e.message
    return out


@router.post("/tasks/{task_id}/workspace/ports/{port}/kill")
async def workspace_port_kill(
    task_id: int,
    port: int,
    force: bool = False,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Prozess eines lauschenden Ports beenden (Preview-UI: × beim Port).

    Ohne force: SIGTERM + 5 s Wartezeit; danach noch belegt → 409
    (UI bietet erzwungene Beendigung als zweiten Schritt an).
    force=true: SIGKILL über den ganzen Prozessbaum.
    """
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    try:
        await asyncio.to_thread(
            client.kill_port, _ws_key(task, user), port, force)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True}


@router.websocket("/tasks/{task_id}/workspace/terminal")
async def workspace_terminal(ws: WebSocket, task_id: int) -> None:
    """Terminal (PTY) im eigenen Workspace-Container.

    1:1-WS-Weiterleitung zum Agenten (Token per Query-Param). Optional
    ?cmd=<relativer Pfad>: PTY startet das Skript direkt (Play-Button
    im Dateibaum); 👤-Dateien werden abgewiesen. Protokoll:
    Binary = rohes Terminal-Input/Output · Text = Kontrolle
    ({"cols","rows"} init/resize · {"type":"exit","code"} · {"type":"error"}).
    """
    # Cookie-Auth manuell (FastAPI-WS unterstützt keine Depends)
    token = ws.cookies.get("access_token", "")
    payload = decode_access_token(token) if token else None
    user_id = payload.get("sub") if payload else None
    if not user_id:
        await ws.close(code=4401)
        return
    await ws.accept()

    agent_url = agent_key = key = cmd_path = None
    student_id = None
    try:
        with Session(engine) as session:
            user = session.get(User, user_id)
            if user is None:
                await _ws_error_close(ws, "Nicht authentifiziert.")
                return
            task = await _load_ws_task(task_id, session, user)
            try:
                if not workspace_service.is_enabled(session, task.course_id):
                    raise HTTPException(
                        503, "Workspace-Aufgaben sind derzeit deaktiviert.")
                not_ready = workspace_service.validate_task_ready(session, task)
                if not_ready:
                    raise HTTPException(503, not_ready)
                agent = workspace_service.pick_task_agent(session, task)
                if agent is None:
                    raise HTTPException(
                        503, workspace_service.pick_agent_error(
                            session, task.course_id,
                            workspace_service.task_engine_names(task)))
                agent_url = agent["url"]
                agent_key = agent.get("key") or ""
            except HTTPException as e:
                await _ws_error_close(ws, str(e.detail))
                return
            key = _ws_key(task, user)
            student_id = user.id
            raw_cmd = ws.query_params.get("cmd")
            if raw_cmd:
                cmd_path = _safe_ws_path(raw_cmd)
                # 👤-Dateien existieren für den Studenten nicht;
                # readonly/edit dürfen ausgeführt werden
                # (ausführen ≠ schreiben).
                if _ws_effective_access(session, task, cmd_path) == "hidden":
                    raise HTTPException(404, "Datei nicht gefunden.")
    except Exception:
        logger.warning("Workspace-Terminal: Setup fehlgeschlagen", exc_info=True)
        await _ws_error_close(ws, "Terminal derzeit nicht verfügbar.")
        return

    agent_token = make_token(agent_key, f"ws:{key}",
                             task_id=task_id, student_id=student_id)
    uri = (agent_url.replace("https://", "wss://")
                  .replace("http://", "ws://").rstrip("/")
           + f"/workspaces/{key}/terminal"
           + f"?token={quote(agent_token, safe='')}"
           + (f"&cmd={quote(cmd_path, safe='')}" if cmd_path else ""))
    try:
        async with websockets.connect(uri, max_size=None, open_timeout=10) as up:
            async def _browser_to_agent() -> None:
                try:
                    while True:
                        msg = await ws.receive()
                        mtype = msg.get("type")
                        if mtype == "websocket.disconnect":
                            return
                        if mtype != "websocket.receive":
                            continue
                        if msg.get("bytes") is not None:
                            await up.send(msg["bytes"])
                        elif msg.get("text") is not None:
                            await up.send(msg["text"])
                except WebSocketDisconnect:
                    pass
                except Exception:
                    pass

            async def _agent_to_browser() -> None:
                try:
                    async for msg in up:
                        if isinstance(msg, (bytes, bytearray)):
                            await ws.send_bytes(bytes(msg))
                        else:
                            await ws.send_text(msg)
                except Exception:
                    pass

            t1 = asyncio.create_task(_browser_to_agent())
            t2 = asyncio.create_task(_agent_to_browser())
            try:
                await asyncio.wait(
                    {t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (t1, t2):
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
    except Exception:
        logger.warning("Workspace-Terminal: Agent-WS fehlgeschlagen",
                       exc_info=True)
        await _ws_error_close(ws, "Terminal-Verbindung fehlgeschlagen.")
        return
    try:
        await ws.close()
    except Exception:
        pass


@router.get("/tasks/{task_id}/workspace/files")
async def workspace_list_files(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Dateiliste des eigenen Containers (/workspace)."""
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    key = _ws_key(task, user)
    try:
        files = await asyncio.to_thread(client.list_files, key)
    except ComputeAgentError as e:
        raise _agent_http(e)
    # Effektive Zugriffs-Klasse je Datei (effektiv inkl. Ordner-Erbung;
    # 👤-Dateien fehlen im Container automatisch) + gepflegte
    # (Tutor-)Reihenfolge sort_order aus der DB.
    fm = workspace_service.folder_map(session, task)
    file_rows = {f.path: f
                 for f in workspace_service.task_files(session, task)}
    for f in files:
        p = str(f.get("path") or "")
        row = file_rows.get(p)
        f["access"] = effective_file_access(p, row.access if row else None, fm)
        f["sort_order"] = row.sort_order if row else None
    # Ordner: explizite Klassen-Zeilen + reale Verzeichnisse im Volume
    # (leere Ordner — z. B. per Terminal/mkdir — wären sonst unsichtbar;
    # own: None markiert implizite Einträge ohne eigene Zeile).
    try:
        vol_dirs = await asyncio.to_thread(client.list_dirs, key)
    except ComputeAgentError:
        vol_dirs = []
    folder_paths = set(fm)
    for d in vol_dirs:
        if d and d not in folder_paths:
            folder_paths.add(d)
    out_folders = [{
        "path": p,
        "access": effective_folder_access(p, fm),
        "own": fm.get(p),
    } for p in sorted(folder_paths)
        if effective_folder_access(p, fm) != "hidden"]
    # no-store: nach Moves/Deletes gecachte (alte) Liste vermeiden.
    return JSONResponse(
        content={
            "files": files,
            "folders": out_folders,
            "folder_order": workspace_service.order_map(session, task),
            "total": sum(f.get("size", 0) for f in files),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/tasks/{task_id}/workspace/files/{path:path}")
async def workspace_read_file(
    task_id: int,
    path: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Datei-Inhalt laden (für den Editor). Binarys: als octet-stream."""
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    key = _ws_key(task, user)
    p = _safe_ws_path(path)
    try:
        data = await asyncio.to_thread(client.read_file, key, p)
    except ComputeAgentError as e:
        raise _agent_http(e)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß für den Editor (max. 50 MB).")
    is_binary = b"\x00" in data[:1024]
    headers = {"Cache-Control": "no-store"}
    if is_binary:
        headers["Content-Disposition"] = f'attachment; filename="{p.split("/")[-1]}"'
    return Response(
        content=data,
        media_type="application/octet-stream" if is_binary else "text/plain; charset=utf-8",
        headers=headers,
    )


@router.put("/tasks/{task_id}/workspace/files/{path:path}")
async def workspace_write_file(
    task_id: int,
    path: str,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Textdatei speichern (Editor-Save)."""
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    key = _ws_key(task, user)
    p = _safe_ws_path(path)
    _ws_require_writable(session, task, p)
    body = await request.json()
    content = body.get("content")
    if not isinstance(content, str):
        raise HTTPException(422, "Feld 'content' (String) fehlt.")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß (max. 50 MB).")
    try:
        result = await asyncio.to_thread(client.write_file, key, p, data)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True, "path": p, "size": result.get("size")}


@router.post("/tasks/{task_id}/workspace/files")
async def workspace_create_file(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Neue Textdatei anlegen ({path, content})."""
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    key = _ws_key(task, user)
    body = await request.json()
    if not body.get("path"):
        raise HTTPException(422, "Feld 'path' fehlt.")
    p = _safe_ws_path(str(body["path"]))
    _ws_require_writable(session, task, p)
    content = body.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise HTTPException(422, "Feld 'content' muss ein String sein.")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß (max. 50 MB).")
    try:
        result = await asyncio.to_thread(client.write_file, key, p, data)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True, "path": p, "size": result.get("size")}


@router.post("/tasks/{task_id}/workspace/upload")
async def workspace_upload_file(
    task_id: int,
    file: UploadFile = File(...),
    path: str = Form(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Binary-Upload (Datasete etc.) in den eigenen Container.

    Mehrfach-Upload (Dateien/Ordner) läuft client-seitig als mehrere
    Einzel-Requests; der Zielpfad trägt die Ordner-Struktur und wird
    anlegen inkl. Eltern-Verzeichnissen (docker-exec mkdir -p).
    """
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    key = _ws_key(task, user)
    p = _safe_ws_path(path)
    _ws_require_writable(session, task, p)
    data = await file.read()
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß (max. 50 MB).")
    try:
        result = await asyncio.to_thread(client.write_file, key, p, data)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True, "path": p, "size": result.get("size")}


@router.post("/tasks/{task_id}/workspace/files/move")
async def workspace_move_file(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Datei verschieben/umbenennen ({src, dst}) — nur zwischen Dateien
    der effektiven Klasse ✏️ edit (🔒/👤-Bereiche bleiben außen vor)."""
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    key = _ws_key(task, user)
    body = await request.json()
    src = _safe_ws_path(str(body.get("src") or ""))
    dst = _safe_ws_path(str(body.get("dst") or ""))
    if _ws_effective_access(session, task, src) == "hidden":
        raise HTTPException(404, "Datei nicht gefunden.")
    if _ws_effective_access(session, task, src) != "edit" \
            or _ws_effective_access(session, task, dst) != "edit":
        raise HTTPException(403, "Verschieben ist nur innerhalb der editierbaren Bereiche möglich.")
    try:
        await asyncio.to_thread(client.move_file, key, src, dst)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True, "path": dst}


@router.delete("/tasks/{task_id}/workspace/files/{path:path}")
async def workspace_delete_file(
    task_id: int,
    path: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Datei aus dem eigenen Container löschen."""
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    key = _ws_key(task, user)
    p = _safe_ws_path(path)
    _ws_require_writable(session, task, p)
    try:
        await asyncio.to_thread(client.delete_file, key, p)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True}


@router.post("/tasks/{task_id}/workspace/folders")
async def workspace_create_folder(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """(Möglicherweise leeren) Verzeichnis anlegen ({path}) — persistiert
    im Volume, auch wenn der Ordner bleibt (z. B. für Terminal-Arbeit)."""
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    key = _ws_key(task, user)
    body = await request.json()
    p = _safe_ws_path(str(body.get("path") or ""))
    _ws_require_writable(session, task, p)
    try:
        await asyncio.to_thread(client.create_dir, key, p)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True, "path": p}


@router.post("/tasks/{task_id}/workspace/folders/move")
async def workspace_move_folder(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Verzeichnis verschieben/umbenennen ({src, dst}) — auch leere
    Ordner, nur innerhalb der editierbaren Bereiche."""
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    key = _ws_key(task, user)
    body = await request.json()
    src = _safe_ws_path(str(body.get("src") or ""))
    dst = _safe_ws_path(str(body.get("dst") or ""))
    if src == dst:
        raise HTTPException(400, "Quelle und Ziel sind identisch.")
    if dst.startswith(src + "/"):
        raise HTTPException(400, "Ein Ordner kann nicht in sich selbst verschoben werden.")
    fm = workspace_service.folder_map(session, task)
    if effective_folder_access(src, fm) == "hidden":
        raise HTTPException(404, "Ordner nicht gefunden.")
    if effective_folder_access(src, fm) != "edit" \
            or effective_folder_access(dst, fm) != "edit":
        raise HTTPException(403, "Verschieben ist nur innerhalb der editierbaren Bereiche möglich.")
    try:
        await asyncio.to_thread(client.move_file, key, src, dst)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True, "path": dst}


@router.delete("/tasks/{task_id}/workspace/folders/{path:path}")
async def workspace_delete_folder(
    task_id: int,
    path: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Verzeichnis inkl. Inhalt aus dem eigenen Container löschen."""
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    key = _ws_key(task, user)
    p = _safe_ws_path(path)
    fm = workspace_service.folder_map(session, task)
    acc = effective_folder_access(p, fm)
    if acc == "hidden":
        raise HTTPException(404, "Ordner nicht gefunden.")
    if acc != "edit":
        raise HTTPException(403, "Dieser Ordner ist read-only (vom Tutor verwaltet).")
    try:
        await asyncio.to_thread(client.delete_file, key, p)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True}


@router.post("/tasks/{task_id}/workspace/reset")
async def workspace_reset(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Workspace auf die Vorlage zurücksetzen.

    Löscht Container + Volume; beim nächsten Status-Aufruf wird der
    Workspace frisch angelegt und die Starter-Dateien neu geschrieben.
    """
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    key = _ws_key(task, user)
    try:
        await asyncio.to_thread(client.delete_workspace, key)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True}


@router.post("/tasks/{task_id}/workspace/stop")
async def workspace_stop(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Container manuell stoppen (Dateien/Volume bleiben erhalten)."""
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    key = _ws_key(task, user)
    try:
        await asyncio.to_thread(client.stop_workspace, key)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True}


@router.post("/tasks/{task_id}/workspace/start")
async def workspace_start(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Container explizit starten (hebt die manuelle Stop-Sperre)."""
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    key = _ws_key(task, user)
    try:
        await asyncio.to_thread(client.start_workspace, key)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True}


@router.get("/tasks/{task_id}/package")
async def workspace_download_package(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Student-Download-Paket (Aufgaben-Vorlage): Workspace-Dateien + data/
    + docker-compose + images/ (öffentliches Basis-Image + dünne Schicht)
    + README. Enthält bewusst KEINE Musterlösung/private Tests/verify."""
    from fastapi.responses import Response
    from services.compose_gen import build_package
    task = await _load_ws_task(task_id, session, user)
    try:
        fname, data = await asyncio.to_thread(build_package, task, "student", None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Response(
        content=data,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/tasks/{task_id}/workspace/package")
async def workspace_download_solution_package(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Student-Download-Paket der EIGENEN Lösung: gleiche Struktur wie die
    Aufgaben-Vorlage (Dockerfile + .init.sh + compose, gleiches Setup), aber
    workspace/ enthält die eigenen + vom Code generierten Dateien des
    aktuellen Workspaces statt der Starter-Dateien."""
    from fastapi.responses import Response
    from services.compose_gen import build_package
    task = await _load_ws_task(task_id, session, user)
    client = _ws_client_or_error(session, task)
    key = _ws_key(task, user)
    try:
        # Container (re-)anlegen, falls Idle-Kill — Volume/Dateien bleiben.
        await asyncio.to_thread(
            workspace_service.ensure_workspace, session, task, user.id)
        snap = await asyncio.to_thread(client.snapshot, key)
        fname, data = await asyncio.to_thread(
            build_package, task, "student", None, snap)
    except ComputeAgentError as e:
        raise _agent_http(e)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Response(
        content=data,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
