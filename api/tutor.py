"""
Tutor-Endpoints: Aufgaben-Management + Korrektur + Übersicht.

Rollen: Tutor und PROF (im Kurs), Admin (global)
"""

import asyncio
import io
import json
import logging
import mimetypes
import re
import shutil
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlmodel import Session, SQLModel, delete, select

from database.base import engine, get_session
from database.models import (
    ContentVersion,
    Course,
    CourseRole,
    CourseSettings,
    Feedback,
    FeedbackSource,
    GlobalUserRole,
    HintExchange,
    ScriptSection,
    Submission,
    SubmissionStatus,
    Task,
    TaskType,
    TaskWorkspaceFile,
    TaskWorkspaceFolder,
    TaskWorkspaceOrder,
    User,
    UserCourse,
    WorkspaceRun,
    WorkspaceRunStatus,
)
from services.auth_service import get_current_user, require_course_access
from services import file_edits
from services.export_service import ExportService
from services.grading_service import GradingService
from services.llm_service import LLMService
from services.mc_service import (
    McValidationError,
    mc_report_summary,
    mc_total_points,
    parse_mc_data,
    student_view as _mc_student_view,
    tutor_view as _mc_tutor_view,
    validate_mc_data as _mc_validate,
)
from services.media_service import all_media_for_course, sync_media_usages
from services.references_service import build_references_text
from services.settings_resolver import get_effective_compute_config, get_effective_llm_config
from services.workspace_presets import (
    IMAGE_SELECTION_FIELDS,
    validate_workspace_generation,
)
from services.workspace_service import (
    MAX_FILE_BYTES,
    MAX_WORKSPACE_TOTAL_BYTES,
    TEST_SOLUTION_SCRIPT,
    apply_workspace_env_fields as _apply_workspace_env_fields,
    check_image_spec as _check_image_spec,
    effective_file_access,
    effective_folder_access,
    ensure_fresh_workspace,
    file_disk_path,
    parse_engine_list as _parse_engine_list,
    schedule_task_sync as _schedule_workspace_sync,
    task_has_init,
    task_init_hash,
    task_workspace_dir,
    test_run_key,
    validate_workspace_provided as _validate_workspace_provided,
    workspace_service,
)

router = APIRouter(prefix="/api", tags=["Tutor"])
grading_service = GradingService()
llm_service = LLMService()
export_service = ExportService()
logger = logging.getLogger(__name__)


def _check_course_role(user: User, course_id: int, session: Session):
    """
    Prüft, ob der User im Kurs PROF oder TUTOR ist.
    Global-Admin hat immer Zugriff.
    Hebt HTTPException 403, falls keine Berechtigung.
    """
    if user.role == GlobalUserRole.ADMIN:
        return
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()
    if not membership or membership.role_in_course not in (CourseRole.PROF, CourseRole.TUTOR):
        raise HTTPException(403, "Nur PROF/Tutor dürfen auf diese Daten zugreifen.")


# ═══════════════════════════════════════════════════════════════════
# WORKSPACE-AUFGABEN (Helfer)
# ═══════════════════════════════════════════════════════════════════
# Umgebungsfelder/Engine-Liste/Image-Spec/Validierung + Sync-Scheduling
# leben in services/workspace_service.py (alias-importiert oben), damit
# auch version_service.py (Versions-Wiederherstellung) sie nutzen kann.


def _task_pool_agents(session: Session, task: Task) -> list[dict]:
    """Agenten des Engine-Pools der Aufgabe (None → alle registrierten)."""
    agents = workspace_service.get_agents(session, task.course_id)
    engine_names = workspace_service.task_engine_names(task)
    if engine_names:
        by_name = {a["name"]: a for a in agents}
        agents = [by_name[n] for n in engine_names if n in by_name]
    return agents


async def _load_workspace_task(
    task_id: int,
    session: Session,
    user: User,
) -> Task:
    """Task laden + PROF/Tutor-Zugriff prüfen (course_id nicht im Path)."""
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")
    _check_course_role(user, task.course_id, session)
    return task


# ═══════════════════════════════════════════════════════════════════
# AUFGABEN
# ═══════════════════════════════════════════════════════════════════

@router.get("/courses/{course_id}/tasks")
async def list_tasks(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Alle Aufgaben eines Kurses auflisten."""
    tasks = list(session.exec(
        select(Task)
        .where(Task.course_id == course_id)
        .order_by(Task.display_order.asc())  # type: ignore[attr-defined]
    ).all())
    
    return [
        {
            "id": t.id,
            "title": t.title,
            "task_type": t.task_type.value,
            "max_points": t.max_points,
            "max_attempts": t.max_attempts,
            "deadline": t.deadline,
            "has_tests": bool(t.test_code),
            "submission_count": len(t.submissions),
            "is_visible": t.is_visible,
            "hints_enabled": t.hints_enabled,
            "display_order": t.display_order,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tasks
    ]


@router.post("/courses/{course_id}/tasks")
async def create_task(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Neue Aufgabe erstellen."""
    user, _ = user_and_course
    body = await request.json()

    # Bestimme naechsten display_order-Wert (am Ende der Liste)
    existing_tasks = session.exec(
        select(Task)
        .where(Task.course_id == course_id)
        .order_by(Task.display_order.desc())  # type: ignore[attr-defined]
    ).all()
    next_order = existing_tasks[0].display_order + 1 if existing_tasks else 0

    model_solution = body.get("model_solution")
    # model_solution kann None sein (optional)
    if model_solution == "":
        model_solution = None

    try:
        task_type = TaskType(body.get("task_type", "text"))
    except ValueError:
        raise HTTPException(400, f"Ungültiger Aufgabentyp: {body.get('task_type')!r}")

    max_points = body.get("max_points", 10)
    try:
        max_points = int(max_points)
    except (TypeError, ValueError):
        raise HTTPException(400, "Max. Punkte muss eine Zahl sein.")
    if max_points < 0:
        raise HTTPException(400, "Max. Punkte muss mindestens 0 sein.")

    # Multiple-Choice: mc_data validieren; bei Fragen wird max_points aus
    # der Punkte-Summe berechnet (Tutor gibt nur Fragen-Punkte ein).
    # Leere mc_data erlaubt (Aufgabe noch nicht konfiguriert — ist
    # standardmäßig versteckt, Submit lehnt ab).
    mc_data_value: Optional[str] = None
    if task_type == TaskType.MC:
        raw_mc = body.get("mc_data")
        if isinstance(raw_mc, str) and raw_mc.strip():
            try:
                mc_parsed = parse_mc_data(raw_mc)
            except McValidationError as e:
                raise HTTPException(400, f"Multiple-Choice-Fehler: {e}")
            mc_data_value = json.dumps(mc_parsed, ensure_ascii=False)
            max_points = mc_total_points(mc_parsed)
        elif raw_mc not in (None, ""):
            raise HTTPException(400, "mc_data muss ein JSON-String sein.")

    task = Task(
        course_id=course_id,
        created_by=user.id,  # type: ignore[arg-type]
        title=body.get("title", ""),
        task_type=task_type,
        description=body.get("description", ""),
        model_solution=model_solution,
        max_points=max_points,
        max_attempts=body.get("max_attempts"),
        deadline=body.get("deadline"),
        code_template=body.get("code_template"),
        test_code=body.get("test_code"),
        mc_data=mc_data_value,
        is_visible=body.get("is_visible", False),  # Default: für Studenten versteckt
        display_order=next_order,
    )
    if task_type == TaskType.WORKSPACE:
        _apply_workspace_env_fields(task, body)
        task.workspace_engines = _parse_engine_list(body.get("workspace_engines"))
        task.workspace_image = _check_image_spec(session, course_id,
                                                 body.get("workspace_image"))
        _validate_workspace_provided(session, task)

    session.add(task)
    session.commit()
    session.refresh(task)
    sync_media_usages(session, course_id)
    if task_type == TaskType.WORKSPACE:
        # Stub-Skripte sofort (leerer Datei-Baum) — ohne Warten auf den
        # lauffähigen Zustand (Image/Engine); on_task_saved heilt nach.
        try:
            workspace_service.ensure_system_stubs(session, task)
        except Exception as e:  # noqa: BLE001 — Stubs dürfen die Anlage nicht blockieren
            logger.warning("System-Stubs (task %s): %s", task.id, e)
        _schedule_workspace_sync(task.id)

    return {
        "message": f"Aufgabe '{task.title}' erstellt.",
        "task": {
            "id": task.id,
            "title": task.title,
            "task_type": task.task_type.value,
        },
    }


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Einzelne Aufgabe laden."""
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")
    
    # Check course access
    statement = (
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == task.course_id)
    )
    if not session.exec(statement).first():
        raise HTTPException(403, "Kein Zugriff auf diese Aufgabe.")
    
    is_tutor = (
        user.role == GlobalUserRole.ADMIN
        or (
            session.exec(
                select(UserCourse.role_in_course)
                .where(UserCourse.user_id == user.id)
                .where(UserCourse.course_id == task.course_id)
            ).first()
            in (CourseRole.PROF, CourseRole.TUTOR)
        )
    )
    
    result = {
        "id": task.id,
        "title": task.title,
        "task_type": task.task_type.value,
        "description": task.description,
        "max_points": task.max_points,
        "max_attempts": task.max_attempts,
        "deadline": task.deadline,
        "code_template": task.code_template if task.task_type.value == "code" else None,
        "test_code": task.test_code if is_tutor else None,
        "hints_enabled": task.hints_enabled,
    }

    # Multiple-Choice: Studenten sehen NUR Options-Texte (kein Correct-Flag,
    # kein Feedback) — sonst wäre die Lösung im DevTools erkennbar.
    if task.task_type.value == "mc" and task.mc_data:
        try:
            mc_parsed = parse_mc_data(task.mc_data)
        except McValidationError:
            mc_parsed = None
        if mc_parsed is not None:
            result["mc_data"] = (
                _mc_tutor_view(mc_parsed) if is_tutor
                else _mc_student_view(
                    mc_parsed, task_id=task.id, user_id=user.id))

    # Workspace-Aufgabe: Preset/GPU für alle (UI-Badges, Editor-Modus),
    # Spec/Dateien nur für Tutoren
    if task.task_type.value == "workspace":
        result["workspace_main_file"] = task.workspace_main_file
        try:
            result["workspace_engines"] = (
                json.loads(task.workspace_engines)
                if task.workspace_engines else None)
        except (json.JSONDecodeError, TypeError):
            result["workspace_engines"] = None
        result["workspace_image"] = task.workspace_image
        if is_tutor:
            result["workspace_timeout"] = task.workspace_timeout
            result["workspace_cpu"] = task.workspace_cpu
            result["workspace_memory"] = task.workspace_memory
            result["workspace_disk_quota"] = task.workspace_disk_quota
            result["workspace_internet"] = task.workspace_internet
            result["workspace_assets_status"] = task.workspace_assets_status
    
    if is_tutor:
        result["model_solution"] = task.model_solution
    else:
        # Student: mask private tests (replace PrivateTest class body with *** )
        if task.test_code:
            result["test_code"] = re.sub(
                r'(class PrivateTest.*?)(class PublicTest|$)',
                r'\1***\2',
                task.test_code,
                flags=re.DOTALL
            )
    
    return result


@router.put("/tasks/{task_id}")
async def update_task(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Aufgabe bearbeiten."""
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")
    
    # Manueler Kurs-Zugriffs-Check (course_id nicht im Path)
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == task.course_id)
    ).first()
    if not membership or membership.role_in_course not in (CourseRole.PROF, CourseRole.TUTOR):
        raise HTTPException(403, "Keine Berechtigung, diese Aufgabe zu bearbeiten.")
    
    body = await request.json()
    
    # Update allowed fields
    if "title" in body: task.title = body["title"]
    if "task_type" in body: task.task_type = TaskType(body["task_type"])
    if "description" in body: task.description = body["description"]
    
    # model_solution: leerer String → None (optional)
    if "model_solution" in body:
        val = body["model_solution"]
        task.model_solution = None if val == "" else val
    
    if "max_points" in body:
        mp = body["max_points"]
        try:
            mp = int(mp)
        except (TypeError, ValueError):
            raise HTTPException(400, "Max. Punkte muss eine Zahl sein.")
        if mp < 0:
            raise HTTPException(400, "Max. Punkte muss mindestens 0 sein.")
        task.max_points = mp
    if "max_attempts" in body:
        task.max_attempts = None if body["max_attempts"] in (None, "") else int(body["max_attempts"])
    if "deadline" in body:
        task.deadline = None if body["deadline"] in (None, "") else body["deadline"]
    if "code_template" in body:
        val = body["code_template"]
        task.code_template = None if val in (None, "") else val
    if "test_code" in body: task.test_code = body["test_code"]
    if "is_visible" in body: task.is_visible = body["is_visible"]
    if "hints_enabled" in body: task.hints_enabled = body["hints_enabled"]

    # Multiple-Choice: mc_data validieren; bei Fragen wird max_points aus
    # der Punkte-Summe der Fragen berechnet (setzt eingegebene Punkte außer Kraft).
    if "mc_data" in body:
        val = body["mc_data"]
        if val is None or (isinstance(val, str) and not val.strip()):
            task.mc_data = None
        else:
            try:
                mc_parsed = parse_mc_data(val)
            except McValidationError as e:
                raise HTTPException(400, f"Multiple-Choice-Fehler: {e}")
            task.mc_data = json.dumps(mc_parsed, ensure_ascii=False)
            if task.task_type == TaskType.MC:
                task.max_points = mc_total_points(mc_parsed)
    # Typ-Wechsel weg von MC: altes mc_data entsorgen
    if "task_type" in body and task.task_type != TaskType.MC and task.mc_data:
        task.mc_data = None

    # Workspace-Felder (Umgebung + Dateien; nur explizit übergebene Keys)
    if task.task_type == TaskType.WORKSPACE:
        _apply_workspace_env_fields(task, body)
    if "workspace_engines" in body:
        task.workspace_engines = _parse_engine_list(body["workspace_engines"])
    if "workspace_image" in body:
        task.workspace_image = _check_image_spec(session, task.course_id,
                                                 body["workspace_image"])

    task.updated_at = datetime.now(timezone.utc)
    if task.task_type == TaskType.WORKSPACE:
        _validate_workspace_provided(session, task)
    session.add(task)
    session.commit()
    session.refresh(task)
    sync_media_usages(session, task.course_id)
    if task.task_type.value == "workspace":
        # Selbstheilung: Stub-Skripte direkt anlegen, wenn der Datei-Baum
        # leer ist (z. B. per Typ-Wechsel gerade erst Workspace geworden).
        try:
            workspace_service.ensure_system_stubs(session, task)
        except Exception as e:  # noqa: BLE001 — Stubs dürfen den Save nicht blockieren
            logger.warning("System-Stubs (task %s): %s", task.id, e)
        _schedule_workspace_sync(task.id)

    return {"message": "Aufgabe aktualisiert.", "task": {"id": task.id, "title": task.title}}


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Aufgabe löschen (nur Tutor/Admin des Kurses)."""
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")
    
    # Prüfen, ob User den Kurs der Aufgabe bearbeiten darf
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == task.course_id)
    ).first()
    if not membership or membership.role_in_course not in (CourseRole.PROF, CourseRole.TUTOR):
        raise HTTPException(403, "Keine Berechtigung, diese Aufgabe zu löschen.")

    task_course_id = task.course_id
    task_was_workspace = task.task_type.value == "workspace"
    # Agent-Ressourcen + lokale Dateien aufräumen, BEVOR der Task aus der
    # DB gelöscht wird (Instanz ist danach expired/detached)
    if task_was_workspace:
        try:
            workspace_service.on_task_deleted(session, task)
        except Exception as e:  # noqa: BLE001
            logger.warning("Workspace-Aufräumen (task %s) fehlgeschlagen: %s", task_id, e)
        # Workspace-DB-Rows löschen (NOT NULL-FKs ohne Relationship-Cascade)
        workspace_service.delete_task_db_rows(session, task)

    for sub in task.submissions:
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
    return {"message": "Aufgabe '" + task.title + "' gelöscht."}


def _copy_workspace_content(session: Session, src: Task, dst: Task) -> None:
    """Workspace-Inhalt der Aufgabe (Disk-Dateien + DB-Rows) auf die Kopie
    übertragen.

    NICHT kopiert: ``workspace_assets_status`` (Per-Agenten-Buildstatus —
    die Kopie bekommt ihren eigenen Asset-Sync + Init-Build über
    ``_schedule_workspace_sync``) und ``WorkspaceRun`` (Lauf-Historie).
    """
    src_dir = task_workspace_dir(src.id)
    if src_dir.is_dir():
        shutil.copytree(src_dir, task_workspace_dir(dst.id))

    for f in session.exec(
        select(TaskWorkspaceFile).where(TaskWorkspaceFile.task_id == src.id)
    ).all():
        session.add(TaskWorkspaceFile(
            task_id=dst.id,
            path=f.path,
            size=f.size,
            checksum=f.checksum,
            access=f.access,
            is_binary=f.is_binary,
            sort_order=f.sort_order,
        ))
    for fo in session.exec(
        select(TaskWorkspaceFolder).where(TaskWorkspaceFolder.task_id == src.id)
    ).all():
        session.add(TaskWorkspaceFolder(
            task_id=dst.id, path=fo.path, access=fo.access))
    for o in session.exec(
        select(TaskWorkspaceOrder).where(TaskWorkspaceOrder.task_id == src.id)
    ).all():
        session.add(TaskWorkspaceOrder(
            task_id=dst.id, path=o.path, sort_order=o.sort_order))


@router.post("/tasks/{task_id}/duplicate")
async def duplicate_task(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Aufgabe duplizieren (nur Tutor/Admin des Kurses).

    Kopiert alle Daten der Aufgabe (ohne Einreichungen, Feedback und Hints)
    und fügt die Kopie direkt hinter dem Original ein.
    Workspace-Aufgaben: zusätzlich Einstellungen, Dateien und Zugriffsklassen;
    Asset-Sync + Init-Build laufen im Hintergrund für die Kopie.
    """
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    # Prüfen, ob User den Kurs der Aufgabe bearbeiten darf
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == task.course_id)
    ).first()
    if not membership or membership.role_in_course not in (CourseRole.PROF, CourseRole.TUTOR):
        raise HTTPException(403, "Keine Berechtigung, diese Aufgabe zu duplizieren.")

    body = await request.json()
    new_title = (body.get("title") or "").strip()
    if not new_title:
        raise HTTPException(400, "Titel darf nicht leer sein.")

    # Kopie direkt hinter dem Original: alle Aufgaben danach um eins nach hinten schieben
    for other in session.exec(
        select(Task)
        .where(Task.course_id == task.course_id)
        .where(Task.display_order > task.display_order)
    ).all():
        other.display_order += 1
        session.add(other)

    new_task = Task(
        course_id=task.course_id,
        created_by=user.id,  # type: ignore[arg-type]
        title=new_title,
        task_type=task.task_type,
        description=task.description,
        model_solution=task.model_solution,
        max_points=task.max_points,
        max_attempts=task.max_attempts,
        deadline=task.deadline,
        code_template=task.code_template,
        test_code=task.test_code,
        mc_data=task.mc_data,
        is_visible=task.is_visible,
        hints_enabled=task.hints_enabled,
        # Workspace-Einstellungen (für Text/Code-Tasks irrelevant)
        workspace_timeout=task.workspace_timeout,
        workspace_cpu=task.workspace_cpu,
        workspace_memory=task.workspace_memory,
        workspace_disk_quota=task.workspace_disk_quota,
        workspace_internet=task.workspace_internet,
        workspace_main_file=task.workspace_main_file,
        workspace_engines=task.workspace_engines,
        workspace_image=task.workspace_image,
        display_order=task.display_order + 1,
    )
    session.add(new_task)
    session.flush()  # ID für den Workspace-Dateipfad; Fehler → Rollback

    if task.task_type.value == "workspace":
        try:
            _copy_workspace_content(session, task, new_task)
        except Exception:
            shutil.rmtree(task_workspace_dir(new_task.id), ignore_errors=True)
            raise

    session.commit()
    session.refresh(new_task)
    sync_media_usages(session, task.course_id)

    if task.task_type.value == "workspace":
        _schedule_workspace_sync(new_task.id)

    return {
        "message": f"Aufgabe '{new_task.title}' dupliziert.",
        "task": {
            "id": new_task.id,
            "title": new_task.title,
            "task_type": new_task.task_type.value,
            "display_order": new_task.display_order,
        },
    }


@router.patch("/tasks/{task_id}/visibility")
async def toggle_task_visibility(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Sichtbarkeit einer Aufgabe umschalten (nur PROF/TUTOR)."""
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")
    
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == task.course_id)
    ).first()
    if not membership or membership.role_in_course not in (CourseRole.PROF, CourseRole.TUTOR):
        raise HTTPException(403, "Keine Berechtigung, die Sichtbarkeit zu ändern.")
    
    body = await request.json()
    new_visibility = body.get("is_visible", not task.is_visible)
    task.is_visible = new_visibility
    
    session.add(task)
    session.commit()
    session.refresh(task)
    
    status_msg = "sichtbar" if task.is_visible else "versteckt"
    return {"message": f"Aufgabe ist jetzt {status_msg}.", "is_visible": task.is_visible}


@router.post("/tasks/{task_id}/reset-own-submissions")
async def reset_own_submissions(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Loescht alle eigenen Einreichungen fuer eine Aufgabe (nur PROF/TUTOR).

    Ermoeoglicht Tutoren/PROFs, ihre eigenen Tests zurueckzusetzen, um
    die Aufgabe ausfuehrlicher testen zu koennen (Student-View).
    """
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == task.course_id)
    ).first()
    if not membership or membership.role_in_course not in (CourseRole.PROF, CourseRole.TUTOR):
        raise HTTPException(403, "Keine Berechtigung.")

    # Finde alle eigenen Einreichungen (inkl. Feedback)
    own_subs = session.exec(
        select(Submission)
        .where(Submission.task_id == task_id)
        .where(Submission.student_id == user.id)
    ).all()

    count = 0
    for sub in own_subs:
        for fb in sub.feedback_list:
            session.delete(fb)
        session.delete(sub)
        count += 1

    session.commit()
    return {"message": f"{count} Einreichung(en) zurueckgesetzt."}


class TaskReorderRequest(SQLModel):
    task_ids: list[int]


@router.patch("/courses/{course_id}/tasks/reorder")
async def reorder_tasks(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Reihenfolge der Aufgaben in einem Kurs aendern (nur PROF/TUTOR)."""
    _user, _cid = user_and_course

    body = await request.json()
    task_ids = body.get("task_ids", [])

    if not task_ids:
        return {"message": "Keine Aufgaben zum Verschieben angegeben."}

    # Bestaetige, dass alle Aufgaben zum Kurs gehoeren
    for tid in task_ids:
        task = session.get(Task, tid)
        if not task or task.course_id != course_id:
            raise HTTPException(400, f"Aufgabe {tid} gehoert nicht zu diesem Kurs.")

    # Reihenfolge aktualisieren
    for idx, tid in enumerate(task_ids):
        task = session.get(Task, tid)
        if task:
            task.display_order = idx
            session.add(task)

    session.commit()

    return {"message": "Aufgaben-Reihenfolge aktualisiert."}


def _annotation_matches(annotation: Optional[str], filter_text: str) -> bool:
    """Annotations-Filter: alle per Leerzeichen getrennten Wörter des Filters
    müssen als Wortanfang (Präfix, Groß-/Kleinschreibung egal) in der
    Annotation vorkommen — AND-Verknüpfung. Bindestrich gehört zum Wort.
    So matcht "ÜG" auch "ÜG1"/"ÜG-1", aber "1" nicht "Jahrgang 2001"."""
    terms = [t.casefold() for t in filter_text.split()]
    if not terms or not annotation:
        return False
    words = [w.casefold() for w in annotation.split()]
    return all(any(word.startswith(term) for word in words) for term in terms)


@router.get("/courses/{course_id}/overview")
async def get_course_overview(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Übersichtstabelle: Alle Studenten x Aufgaben mit Punkten."""
    user, _ = user_and_course

    # Alle sichtbaren Aufgaben des Kurses
    tasks = session.exec(
        select(Task)
        .where(Task.course_id == course_id)
        .where(Task.is_visible == True)
        .order_by(Task.display_order.asc())  # type: ignore[attr-defined]
    ).all()

    # Alle Studenten des Kurses
    memberships = session.exec(
        select(UserCourse)
        .where(UserCourse.course_id == course_id)
        .where(UserCourse.role_in_course == CourseRole.STUDENT)
    ).all()

    # Annotations-Filter (z.B. Übungsgruppe): mehrere Leerzeichen-getrennte
    # Wörter, jedes muss als Wort in der Annotation vorkommen (AND).
    annotation_filter = request.query_params.get("annotation_filter", "").strip()
    if annotation_filter:
        memberships = [m for m in memberships if _annotation_matches(m.annotation, annotation_filter)]

    students = [m.user for m in memberships]
    annotation_by_user = {m.user_id: m.annotation for m in memberships}

    # Filter aus Query-Parametern
    filter_text = request.query_params.get("filter_text", "").strip()
    type_filter = request.query_params.get("type_filter", "").strip()

    if filter_text:
        tasks = [t for t in tasks if filter_text.lower() in t.title.lower()]
    if type_filter:
        tasks = [t for t in tasks if t.task_type.value == type_filter]

    # Scores berechnen
    scores = {}  # { student_id: { task_id: points } }
    has_override = {}  # { student_id: { task_id: bool } }
    has_submitted = {}  # { student_id: { task_id: bool } }

    for student in students:
        scores[student.id] = {}
        has_override[student.id] = {}
        has_submitted[student.id] = {}
        for task in tasks:
            subs = session.exec(
                select(Submission)
                .where(Submission.task_id == task.id)
                .where(Submission.student_id == student.id)
                .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
            ).all()

            human_points = 0.0
            llm_points = 0.0
            override_exists = False
            if subs:
                latest_sub = subs[0]  # newest
                has_submitted[student.id][task.id] = True
                for fb in latest_sub.feedback_list:
                    if fb.source == FeedbackSource.HUMAN:
                        human_points = max(human_points, fb.points_earned)
                        override_exists = True
                    else:
                        llm_points = max(llm_points, fb.points_earned)

            point_val = human_points if override_exists else llm_points
            scores[student.id][task.id] = point_val
            has_override[student.id][task.id] = override_exists

    # Gesamtprozent pro Student
    max_total = sum(t.max_points for t in tasks)
    student_list = []
    for student in students:
        total = sum(scores[student.id].get(t.id, 0) for t in tasks)
        student_list.append({
            "id": student.id,
            "username": student.username,
            "name": student.name,
            "total_points": total,
            "annotation": annotation_by_user.get(student.id),
        })

    return {
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "task_type": t.task_type.value,
                "max_points": t.max_points,
            }
            for t in tasks
        ],
        "students": student_list,
        "scores": scores,
        "has_override": has_override,
        "has_submitted": has_submitted,
        "max_total": max_total,
    }


# ═══════════════════════════════════════════════════════════════════
# LLM-ASSISTED CREATION
# ═══════════════════════════════════════════════════════════════════

@router.post("/courses/{course_id}/tasks/ai-generate")
async def ai_generate_task(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """
    LLM generiert/ändert die angeforderten Felder einer Aufgabe (einheitlich).

    Request:
        {
            "task_type": "code",
            "topic": "Rekursion",
            "difficulty": "mittel",
            "max_points": 10,
            "title": "...",
            "description": "...",
            "model_solution": "...",
            "code_template": "...",
            "generate_fields": {
                "title": true,
                "description": true,
                "solution": true,
                "template": true,   // nur Code-Aufgaben
                "tests": true        // nur Code-Aufgaben
            }
        }
    """
    body = await request.json()

    task_type = body.get("task_type", "text")
    gen = body.get("generate_fields", {}) or {}

    gen_title = bool(gen.get("title"))
    gen_description = bool(gen.get("description"))
    # Musterlösung gilt nicht für MC — dort definieren korrekte Antworten +
    # Feedback + Punkte je Frage die Bewertung eindeutig.
    gen_solution = bool(gen.get("solution")) and task_type != "mc"
    # Template/Tests gelten nur für Code-Aufgaben
    gen_template = bool(gen.get("template")) and task_type == "code"
    gen_tests = bool(gen.get("tests")) and task_type == "code"
    # Workspace-Aufgabe: Umgebung (Timeout/Limits/Internet/Artefakte)
    # + Dateien. Ungespeicherte Aufgaben werden nach dem LLM-Call
    # automatisch angelegt (Titel aus LLM/Formular/Thema).
    gen_env = bool(gen.get("env")) and task_type == "workspace"
    gen_files = bool(gen.get("files")) and task_type == "workspace"
    # MC: Fragen inkl. Optionen + Feedback je Option
    gen_questions = bool(gen.get("questions")) and task_type == "mc"

    if not (gen_title or gen_description or gen_solution or gen_template or gen_tests
            or gen_env or gen_files or gen_questions):
        raise HTTPException(400, "Keine Felder angefordert.")

    current_title = (body.get("title") or "").strip()
    current_description = (body.get("description") or "").strip()
    current_solution = (body.get("model_solution") or "").strip()
    current_template = (body.get("code_template") or "").strip()

    # EIN LLM-Call pro Aufgabe (Single-Prompt): alle angeforderten Felder
    # entstehen in einem Durchlauf und passen automatisch zueinander.
    # Reihenfolge der LLM-Keys = Abarbeitungsreihenfolge: Implementierung
    # zuerst, dann Musterlösung/Kriterien (bezogen auf die konkrete
    # Implementierung), dann Aufgabenstellung/Titel.
    llm_cfg = get_effective_llm_config(session, user_and_course[1])

    response = {
        "title": "",
        "description": "",
        "model_solution": "",
        "code_template": "",
        "public_tests": "",
        "private_tests": "",
        "mc_data": None,
    }
    latency_ms = 0

    # Kontext für konsistente Notation & Querverweise: Skript-Kapitel
    # (nur mit Zusammenfassung, da Labels dort drinstecken) + alle Medien.
    script_chapters = [
        {"title": s.title, "summary": (s.summary or "").strip()}
        for s in session.exec(
            select(ScriptSection)
            .where(ScriptSection.course_id == course_id)
            .order_by(ScriptSection.display_order.asc())  # type: ignore[union-attr]
        ).all()
        if (s.summary or "").strip()
    ][:20]
    course_media = all_media_for_course(session, course_id)
    references = build_references_text(session, course_id)

    if task_type == "text":
        # ── Text-Aufgabe: Titel/Aufgabenstellung/Musterlösung ──
        text_fields = [f for f, active in (
            ("title", gen_title),
            ("description", gen_description),
            ("model_solution", gen_solution),
        ) if active]
        result = await llm_service.generate_task_fields(
            topic=body.get("topic", ""),
            difficulty=body.get("difficulty", "mittel"),
            task_type=task_type,
            max_points=body.get("max_points", 10),
            generate_fields=text_fields,
            current_title=current_title,
            current_description=current_description,
            current_model_solution=current_solution,
            code_template=current_template,
            script_chapters=script_chapters,
            course_media=course_media,
            references=references,
            config=llm_cfg,
        )

        if not result.get("success"):
            raise HTTPException(500, f"LLM-Fehler: {result.get('error', 'Unbekannter Fehler')}")

        data = result.get("data") or {}
        latency_ms = result.get("latency_ms", 0)

        # NUR angeforderte Felder aus der LLM-Antwort übernehmen.
        if gen_title:
            response["title"] = (data.get("title") or "").strip()
        if gen_description:
            response["description"] = (data.get("description") or "").strip()
        if gen_solution:
            response["model_solution"] = (data.get("model_solution") or "").strip()

    elif task_type == "code":
        # ── Code-Aufgabe: Vorlage/Tests/Lösung/Beschreibung/Titel ──
        code_fields: list[str] = []
        if gen_template:
            code_fields.append("code_template")
        if gen_tests:
            code_fields += ["public_tests", "private_tests"]
        if gen_solution:
            code_fields.append("model_solution")
        if gen_description:
            code_fields.append("description")
        if gen_title:
            code_fields.append("title")
        result = await llm_service.generate_code_task_fields(
            topic=body.get("topic", ""),
            difficulty=body.get("difficulty", "mittel"),
            max_points=body.get("max_points", 10),
            generate_fields=code_fields,
            current_title=current_title,
            current_description=current_description,
            current_model_solution=current_solution,
            current_code_template=current_template,
            script_chapters=script_chapters,
            course_media=course_media,
            references=references,
            config=llm_cfg,
        )

        if not result.get("success"):
            raise HTTPException(500, f"LLM-Fehler (Code): {result.get('error', 'Unbekannter Fehler')}")

        data = result.get("data") or {}
        latency_ms = result.get("latency_ms", 0)

        if gen_template:
            response["code_template"] = data.get("code_template", "")
        if gen_tests:
            response["public_tests"] = data.get("public_tests", "")
            response["private_tests"] = data.get("private_tests", "")
        if gen_solution:
            response["model_solution"] = (data.get("model_solution") or "").strip()
        if gen_description:
            response["description"] = (data.get("description") or "").strip()
        if gen_title:
            response["title"] = (data.get("title") or "").strip()

    elif task_type == "mc":
        # ── MC-Aufgabe: Titel/Einleitung/Musterlösung/Fragen. Das LLM legt
        # auch das Feedback je Option an (wird Studenten nach dem Einreichen
        # angezeigt). penalty_enabled bleibt die manuelle Tutor-Einstellung.
        mc_fields: list[str] = []
        if gen_questions:
            mc_fields.append("mc_questions")
        if gen_solution:
            mc_fields.append("model_solution")
        if gen_description:
            mc_fields.append("description")
        if gen_title:
            mc_fields.append("title")

        req_task_id = body.get("task_id")
        mc_task = session.get(Task, req_task_id) if req_task_id else None
        if mc_task is not None and mc_task.course_id != course_id:
            mc_task = None

        # Bestehende Fragen als JSON-Kontext (für Änderungs-Wünsche)
        current_questions = ""
        current_penalty = False
        if mc_task and mc_task.mc_data:
            try:
                existing_mc = parse_mc_data(mc_task.mc_data)
                current_penalty = existing_mc.get("penalty_enabled", False)
                current_questions = json.dumps(
                    _mc_tutor_view(existing_mc), ensure_ascii=False)
            except McValidationError:
                pass

        result = await llm_service.generate_mc_task_fields(
            topic=body.get("topic", ""),
            difficulty=body.get("difficulty", "mittel"),
            max_points=body.get("max_points", 10),
            generate_fields=mc_fields,
            current_title=current_title,
            current_description=current_description,
            current_model_solution=current_solution,
            current_questions=current_questions,
            script_chapters=script_chapters,
            course_media=course_media,
            references=references,
            config=llm_cfg,
        )

        if not result.get("success"):
            raise HTTPException(500, f"LLM-Fehler (MC): {result.get('error', 'Unbekannter Fehler')}")

        data = result.get("data") or {}
        latency_ms = result.get("latency_ms", 0)

        if gen_title:
            response["title"] = (data.get("title") or "").strip()
        if gen_description:
            response["description"] = (data.get("description") or "").strip()
        if gen_solution:
            response["model_solution"] = (data.get("model_solution") or "").strip()
        if gen_questions:
            mc_payload = {
                "penalty_enabled": current_penalty,
                "questions": data.get("mc_questions"),
            }
            try:
                _mc_validate(mc_payload)
            except McValidationError as e:
                raise HTTPException(
                    502, f"LLM hat ungültige MC-Fragen geliefert: {e} — bitte erneut versuchen.")
            response["mc_data"] = mc_payload

    elif task_type == "workspace":
        # ── Workspace-Aufgabe: ein LLM-Call für alle angeforderten Felder.
        # Dateien/Umwelt werden direkt auf Disk/DB geschrieben; Titel/
        # Aufgabenstellung/Lösungsskizze übernimmt die UI in das Formular.
        # Ungespeicherte Aufgaben werden NACH dem LLM-Call angelegt.
        ws_fields: list[str] = []
        if gen_files:
            ws_fields += ["files", "folders"]
        if gen_env:
            ws_fields.append("env")
        if gen_solution:
            ws_fields.append("model_solution")
        if gen_description:
            ws_fields.append("description")
        if gen_title:
            ws_fields.append("title")

        req_task_id = body.get("task_id")
        task = session.get(Task, req_task_id) if req_task_id else None
        if task is not None and task.course_id != course_id:
            task = None

        # Image-/Engine-Wahl (workspace_image/-engines/proposed_image_spec):
        # PFLICHT, wenn die Umgebung generiert wird oder die Aufgabe (noch)
        # kein Image bzw. keine Engine hat (z. B. NEUE Aufgabe mit Dateien).
        # Bei einer bestehenden, vollständig konfigurierten Aufgabe bleibt
        # die Image-/Engine-Wahl unangetastet, wenn „env“ nicht angefordert
        # ist — die LLM-Ausgabe zu diesen Feldern wird dann ignoriert.
        require_image_selection = gen_env or (
            gen_files
            and (task is None
                 or not (task.workspace_image and task.workspace_engines))
        )

        # Bestehende Dateien als LLM-Kontext (Text, mit Größen-Caps).
        # NUR echte Task-Dateien (DB-Zeilen): Init-Artefakte (Ergebnisse von
        # .init.sh/.init_hidden.sh) liegen auf der Compute-Engine (Asset-Dir,
        # per Init-Manifest getrackt) und sind NICHT Teil des Task-Dateibaums
        # — sie kommen daher nie in das LLM-Kontext.
        current_files: list[dict] = []
        total_chars = 0
        if task is not None:
            for row in workspace_service.task_files(session, task):
                try:
                    file_bytes = file_disk_path(task.id, row.path).read_bytes()
                except OSError:
                    continue
                try:
                    text = file_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    text = "(Binärdatei)"
                if total_chars >= 120_000:
                    current_files.append({"path": row.path, "content": "(nicht geladen — Kontext-Limit)"})
                else:
                    if len(text) > 20_000:
                        text = text[:20_000] + "\n(… gekürzt …)"
                    total_chars += len(text)
                    current_files.append({"path": row.path, "content": text})
        # "delete_files" ist nur mit vorhandenen Dateien sinnvoll (sonst gibt
        # es nichts zu löschen) — daher nur dann anfordern.
        if gen_files and current_files:
            ws_fields.append("delete_files")
        # Aktuelle Umgebungsfelder als LLM-Kontext
        current_env: dict | None = None
        if task is not None:
            current_env = {
                "workspace_timeout": task.workspace_timeout,
                "workspace_cpu": task.workspace_cpu,
                "workspace_memory": task.workspace_memory,
                "workspace_disk_quota": task.workspace_disk_quota,
                "workspace_internet": task.workspace_internet,
                "workspace_main_file": task.workspace_main_file or "",
            }
        # Engine-/Image-Spec-Kontext:
        # das LLM wählt aus registrierten Engines + existierenden Specs oder
        # schlägt eine neue Spec vor (proposed_image_spec, UI bestätigt).
        from services import image_spec_service
        from services.compute_client import ComputeAgentError
        ws_engines: list[dict] = []
        ws_specs: list[dict] = []
        ws_engine_names: set[str] = set()
        if require_image_selection:
            ws_status = workspace_service.status(session, course_id)
            ws_engines = ws_status.get("agents") or []
            if not ws_engines:
                raise HTTPException(
                    400, "Keine Compute-Engine registriert — bitte zuerst in den "
                         "Einstellungen (Admin-Konsole oder Kurs) eine Engine anlegen.")
            ws_engine_names = {a["name"] for a in ws_engines}
            ws_specs = image_spec_service.list_specs(session, course_id)
            # LLM-Kontext: pro Engine die installierten Image-Specs (mit
            # Dockerfile), damit das LLM ein passendes Image+Engine-Paar wählt.
            # Offline-Engine → installierte Images unbekannt (leer).
            for a in ws_engines:
                a["installed_specs"] = []
            if ws_specs:
                for a in ws_engines:
                    try:
                        statuses = workspace_service.client_for(a).images_spec_status(
                            [{"name": s["name"], "dockerfile": s["dockerfile"]}
                             for s in ws_specs])
                    except ComputeAgentError:
                        continue
                    for s, st in zip(ws_specs, statuses):
                        if isinstance(st, dict) and st.get("installed"):
                            a["installed_specs"].append(
                                {"name": s["name"], "dockerfile": s["dockerfile"]})
        ws_result = await llm_service.generate_workspace_task_fields(
            topic=body.get("topic", ""),
            difficulty=body.get("difficulty", "mittel"),
            max_points=body.get("max_points", 10),
            generate_fields=ws_fields,
            current_title=current_title,
            current_description=current_description,
            current_model_solution=current_solution,
            current_env=current_env,
            current_files=current_files or None,
            engines=ws_engines,
            image_specs=ws_specs,
            script_chapters=script_chapters,
            course_media=course_media,
            references=references,
            require_image_selection=require_image_selection,
            config=llm_cfg,
        )
        if not ws_result.get("success"):
            raise HTTPException(
                500, f"LLM-Fehler (Workspace): {ws_result.get('error', 'Unbekannter Fehler')}")
        latency_ms = latency_ms + ws_result.get("latency_ms", 0)

        ws_data = ws_result.get("data") or {}
        if gen_title:
            response["title"] = (ws_data.get("title") or "").strip()
        if gen_description:
            response["description"] = (ws_data.get("description") or "").strip()
        if gen_solution:
            response["model_solution"] = (ws_data.get("model_solution") or "").strip()

        try:
            # Ausgabe zu Feldern, die das LLM NICHT bearbeiten darf (nicht
            # in der ZULÄSSIGEN-FELDER-Liste), wird still ignoriert.
            cleaned = validate_workspace_generation(
                ws_data,
                allowed=set(ws_fields) | (set(IMAGE_SELECTION_FIELDS)
                                          if require_image_selection else set()),
            )
        except ValueError as e:
            raise HTTPException(422, f"LLM-Output ungültig: {e}") from e

        # "From scratch"-Generierung: Aufgabe NACH dem LLM-Call anlegen,
        # damit Titel/Aufgabenstellung/Lösungsskizze direkt aus der Antwort
        # kommen. NUR wenn dafür eine Task-ID gebraucht wird (env/files) —
        # reine Textfeld-Regenerierung ohne Aufgabe bleibt bei der UI.
        task_created = False
        if task is None and (gen_env or gen_files):
            existing_tasks = session.exec(
                select(Task)
                .where(Task.course_id == course_id)
                .order_by(Task.display_order.desc())  # type: ignore[attr-defined]
            ).all()
            next_order = existing_tasks[0].display_order + 1 if existing_tasks else 0
            try:
                ws_max_points = int(body.get("max_points", 10))
            except (TypeError, ValueError):
                ws_max_points = 10
            topic_seed = (body.get("topic") or "").strip()
            task = Task(
                course_id=course_id,
                created_by=user_and_course[0].id,  # type: ignore[arg-type]
                title=(response.get("title") or current_title
                       or (topic_seed if topic_seed and topic_seed != "allgemein" else "")
                       or "Neue Workspace-Aufgabe"),
                task_type=TaskType.WORKSPACE,
                description=(response.get("description") or current_description) or None,
                model_solution=(response.get("model_solution") or current_solution) or None,
                max_points=ws_max_points,
                display_order=next_order,
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            sync_media_usages(session, course_id)
            task_created = True

        if task is None:
            # Nur Textfelder angefordert, keine bestehende Aufgabe → nichts
            # zu schreiben; die UI übernimmt die Antwort in das Formular.
            response["latency_ms"] = latency_ms
            return response

        ws_changed = False
        # LLM kann „env“ auch null/{} liefern → nur anwenden, wenn ein
        # nicht-leeres Objekt da ist (sonst bleiben die bestehenden Werte).
        if gen_env and cleaned.get("env"):
            _apply_workspace_env_fields(task, cleaned["env"])
            response["workspace_timeout"] = task.workspace_timeout
            response["workspace_cpu"] = task.workspace_cpu
            response["workspace_memory"] = task.workspace_memory
            response["workspace_disk_quota"] = task.workspace_disk_quota
            response["workspace_internet"] = task.workspace_internet
            response["workspace_main_file"] = task.workspace_main_file
            ws_changed = True
        if gen_files:
            written: list[str] = []
            edited: list[str] = []
            deleted: list[str] = []
            file_warnings: list[str] = []
            for f in cleaned.get("files") or []:
                p = _safe_task_path(f["path"])
                is_edit = "edits" in f
                applied = 0
                if is_edit:
                    # Stellenseitige Bearbeitung einer bestehenden Datei
                    if workspace_service.get_task_file(session, task, p) is None:
                        raise HTTPException(
                            422, f"LLM-Edits für „{f['path']}“ nicht möglich: "
                                 "Datei existiert nicht in der Aufgabe.")
                    try:
                        existing = file_disk_path(task.id, p).read_text(
                            encoding="utf-8")
                    except (OSError, UnicodeDecodeError) as e:
                        raise HTTPException(
                            422, f"LLM-Edits für „{f['path']}“ nicht möglich: "
                                 "keine lesbare Textdatei (Binärdatei?).") from e
                    try:
                        new_content, applied, warns = file_edits.apply_file_edits(
                            existing, f["edits"])
                    except file_edits.FileEditError as e:
                        raise HTTPException(422, str(e)) from e
                    file_warnings.extend(warns)
                    file_bytes = new_content.encode("utf-8")
                else:
                    file_bytes = f["content"].encode("utf-8")
                _check_workspace_size(session, task, len(file_bytes))
                try:
                    workspace_service.save_task_file(
                        session, task, p, file_bytes, access=f.get("access"))
                except ValueError as e:
                    raise HTTPException(422, str(e)) from e
                if is_edit:
                    edited.append(f"{f['path']} ({applied} Edits)")
                else:
                    written.append(f["path"])
            # Datei-Löschungen ("delete_files"): NUR echte Task-Dateien (DB-
            # Zeilen) — Init-Artefakte (keine DB-Zeile) werden übersprungen.
            for p in cleaned.get("delete_files") or []:
                sp = _safe_task_path(p)
                if workspace_service.get_task_file(session, task, sp) is None:
                    file_warnings.append(
                        f"„{p}“ nicht gelöscht: existiert nicht als Task-Datei "
                        "(ggf. .init.sh-Ergebnis — wird vom Init-Build verwaltet).")
                    continue
                workspace_service.delete_task_file(session, task, sp)
                deleted.append(sp)
            response["workspace_files"] = written
            if edited:
                response["workspace_files_edited"] = edited
            if deleted:
                response["workspace_files_deleted"] = deleted
            if file_warnings:
                response["workspace_file_warnings"] = file_warnings
            if written or edited or deleted:
                ws_changed = True
            # Ordner-Zugriffsklassen (Erbung auf alle Dateien darunter)
            folders = cleaned.get("folders")
            if folders:
                for p, a in folders.items():
                    try:
                        workspace_service.set_folder_access(session, task, p, a)
                    except ValueError as e:
                        raise HTTPException(422, str(e)) from e
                ws_changed = True
        if ws_changed:
            session.add(task)
            session.commit()
            _schedule_workspace_sync(task.id)
        if task_created:
            response["task_created"] = True
            response["task_id"] = task.id
        # Engine + Image-Spec: LLM-Namen müssen existieren (sonst verwerfen)
        if require_image_selection:
            if "workspace_image" in cleaned:
                name = cleaned["workspace_image"]
                if name and image_spec_service.resolve_spec(session, course_id, name):
                    task.workspace_image = name
                    session.add(task)
                    session.commit()
                response["workspace_image"] = cleaned["workspace_image"] \
                    if (name and task.workspace_image == name) else None
            if "workspace_engines" in cleaned:
                names = [n for n in (cleaned["workspace_engines"] or [])
                         if n in ws_engine_names]
                task.workspace_engines = json.dumps(names) if names else None
                session.add(task)
                session.commit()
                response["workspace_engines"] = names or None
        if cleaned.get("proposed_image_spec"):
            response["proposed_image_spec"] = cleaned["proposed_image_spec"]

    response["latency_ms"] = latency_ms
    return response


# ═══════════════════════════════════════════════════════════════════
# SUBMISSION REVIEW
# ═══════════════════════════════════════════════════════════════════

@router.get("/courses/{course_id}/tasks/{task_id}/students")
async def get_task_students(
    course_id: int,
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Gibt alle Studenten eines Kurses zurück, die für eine Aufgabe Einreichungen haben."""
    # Zugriff prüfen
    _check_course_role(user, course_id, session)

    task = session.get(Task, task_id)
    if not task or task.course_id != course_id:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    # Alle Studenten des Kurses
    memberships = session.exec(
        select(UserCourse)
        .where(UserCourse.course_id == course_id)
        .where(UserCourse.role_in_course == CourseRole.STUDENT)
    ).all()
    students = [m.user for m in memberships]

    # Für jeden Student prüfen, ob Einreichungen existieren
    result = []
    for student in students:
        subs = session.exec(
            select(Submission)
            .where(Submission.task_id == task_id)
            .where(Submission.student_id == student.id)
            .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
        ).all()

        has_submissions = len(subs) > 0
        has_override = False
        latest_points = 0.0

        if subs:
            latest_sub = subs[0]
            human_points = 0.0
            llm_points = 0.0
            for fb in latest_sub.feedback_list:
                if fb.source == FeedbackSource.HUMAN:
                    human_points = max(human_points, fb.points_earned)
                    has_override = True
                elif fb.source == FeedbackSource.LLM:
                    llm_points = max(llm_points, fb.points_earned)
            latest_points = human_points if has_override else llm_points

        result.append({
            "id": student.id,
            "name": student.name,
            "username": student.username,
            "has_submissions": has_submissions,
            "latest_points": latest_points,
            "has_override": has_override,
        })

    # Sort by name
    result.sort(key=lambda s: s["name"].lower())

    return {"students": result}


@router.get("/courses/{course_id}/tasks/{task_id}/students/{student_id}/submissions")
async def get_student_submissions(
    course_id: int,
    task_id: int,
    student_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Gibt alle Einreichungen eines Students für eine Aufgabe zurück (für Tutor-Bewertung)."""
    # Zugriff prüfen
    _check_course_role(user, course_id, session)

    task = session.get(Task, task_id)
    if not task or task.course_id != course_id:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    student = session.get(User, student_id)
    if not student:
        raise HTTPException(404, "Student nicht gefunden.")

    # Submissions laden
    subs = session.exec(
        select(Submission)
        .where(Submission.task_id == task.id)
        .where(Submission.student_id == student.id)
        .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
    ).all()

    submissions = []
    for sub in subs:
        feedback = []
        for fb in sub.feedback_list:
            giver_name = None
            if fb.giver_id:
                giver = session.get(User, fb.giver_id)
                if giver:
                    giver_name = giver.name
            elif fb.source == FeedbackSource.LLM:
                giver_name = "LLM"

            feedback.append({
                "id": fb.id,
                "source": fb.source.value,
                "points_earned": fb.points_earned,
                "comment": fb.comment,
                "giver": giver_name,
                "created_at": fb.created_at.isoformat() if fb.created_at else "",
            })

        submissions.append({
            "id": sub.id,
            "solution": sub.solution,
            "code_solution": sub.code_solution,
            # MC: persistiertes Grading-Ergebnis (JSON-String) — die
            # Review-Seite rendert daraus dieselbe Karte wie die
            # Student-View (bleibt stabil bei späteren Aufgaben-Edits).
            "mc_result": sub.mc_result,
            "workspace_snapshot": bool(sub.workspace_snapshot),
            "attempt_number": sub.attempt_number,
            "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else "",
            "status": sub.status.value,
            "solve_time_seconds": sub.solve_time_seconds,
            "feedback": feedback,
        })

    return {
        "task": {
            "id": task.id,
            "title": task.title,
            "task_type": task.task_type.value,
            "description": task.description,
            "model_solution": task.model_solution,
            "max_points": task.max_points,
        },
        "student": {
            "id": student.id,
            "name": student.name,
            "username": student.username,
        },
        "submissions": submissions,
    }


@router.post("/tasks/{task_id}/submissions/{submission_id}/feedback")
async def add_feedback_to_submission(
    task_id: int,
    submission_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Tutor gibt Feedback fuer eine Einreichung.
    Wenn der Tutor bereits eine Bewertung fuer diese Einreichung hat, wird diese aktualisiert.
    Ansonsten wird eine neue angelegt.
    """
    submission = session.get(Submission, submission_id)
    if not submission or submission.id is None or submission.task_id != task_id:
        raise HTTPException(404, "Einreichung nicht gefunden.")

    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    # Zugriff prüfen
    _check_course_role(user, task.course_id, session)

    body = await request.json()
    points = float(body.get("points_earned", 0))
    comment = body.get("comment", "").strip()

    # MC: Auto-Bewertung ohne Kommentar ist üblich → leeres Feedback erlaubt
    if not comment and task.task_type.value != "mc":
        raise HTTPException(400, "Feedback-Text darf nicht leer sein.")

    if points < 0 or points > task.max_points:
        raise HTTPException(400, f"Punkte muessen zwischen 0 und {task.max_points} liegen.")

    # Pro Einreichung maximal eine Tutor-Bewertung.
    # Wenn eine existiert: loeschen und neu erstellen (damit giver_id und created_at korrekt sind)
    existing_feedbacks = session.exec(
        select(Feedback)
        .where(Feedback.submission_id == submission.id)
        .where(Feedback.source == FeedbackSource.HUMAN)
    ).all()

    for existing in existing_feedbacks:
        session.delete(existing)

    # Neue Bewertung anlegen
    feedback = Feedback(
        submission_id=submission.id,
        source=FeedbackSource.HUMAN,
        points_earned=points,
        comment=comment,
        giver_id=user.id,
    )
    session.add(feedback)

    # Status auf OVERRIDDEN setzen
    submission.status = SubmissionStatus.OVERRIDDEN
    session.add(submission)

    session.commit()
    session.refresh(feedback)

    # Berechne latest_points des Students für diese Aufgabe (wie student.py)
    student_subs = session.exec(
        select(Submission)
        .where(Submission.task_id == task_id)
        .where(Submission.student_id == submission.student_id)
        .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
    ).all()
    latest_points = 0.0
    if student_subs:
        latest_sub = student_subs[0]  # newest
        human_points = 0.0
        llm_points = 0.0
        override_exists = False
        for fb in latest_sub.feedback_list:
            if fb.source == FeedbackSource.HUMAN:
                human_points = max(human_points, fb.points_earned)
                override_exists = True
            elif fb.source == FeedbackSource.LLM:
                llm_points = max(llm_points, fb.points_earned)
        latest_points = human_points if override_exists else llm_points

    return {
        "id": feedback.id,
        "source": feedback.source.value,
        "points_earned": feedback.points_earned,
        "comment": feedback.comment,
        "giver": user.name,
        "created_at": feedback.created_at.isoformat() if feedback.created_at else "",
        "latest_points": latest_points,
        "max_points": task.max_points,
    }


# ═══════════════════════════════════════════════════════════════════
# EXCEL-EXPORT
# ═══════════════════════════════════════════════════════════════════

@router.post("/courses/{course_id}/export-excel")
async def export_excel(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Excel-Export der Übersichtstabelle: Alle Studenten x Aufgaben mit Punkten."""
    _, _ = user_and_course

    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    # Query-Parameter
    filter_text = request.query_params.get("filter_text", "").strip()
    type_filter = request.query_params.get("type_filter", "").strip()

    # Alle sichtbaren Aufgaben des Kurses
    tasks = list(session.exec(
        select(Task)
        .where(Task.course_id == course_id)
        .where(Task.is_visible == True)
        .order_by(Task.display_order.asc())  # type: ignore[attr-defined]
    ).all())

    # Filter nach Suchbegriff
    if filter_text:
        tasks = [t for t in tasks if filter_text.lower() in t.title.lower()]
    if type_filter:
        tasks = [t for t in tasks if t.task_type.value == type_filter]

    # Alle Studenten des Kurses
    memberships = session.exec(
        select(UserCourse)
        .where(UserCourse.course_id == course_id)
        .where(UserCourse.role_in_course == CourseRole.STUDENT)
    ).all()

    # Annotations-Filter (z.B. Übungsgruppe) — identisch zu get_course_overview
    annotation_filter = request.query_params.get("annotation_filter", "").strip()
    if annotation_filter:
        memberships = [m for m in memberships if _annotation_matches(m.annotation, annotation_filter)]

    students = [m.user for m in memberships]
    annotations = {m.user_id: m.annotation for m in memberships}

    # Scores berechnen
    scores = {}  # { student_id: { task_id: points } }
    for student in students:
        scores[student.id] = {}
        for task in tasks:
            subs = session.exec(
                select(Submission)
                .where(Submission.task_id == task.id)
                .where(Submission.student_id == student.id)
                .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
            ).all()

            human_points = 0.0
            llm_points = 0.0
            override_exists = False
            if subs:
                latest_sub = subs[0]
                for fb in latest_sub.feedback_list:
                    if fb.source == FeedbackSource.HUMAN:
                        human_points = max(human_points, fb.points_earned)
                        override_exists = True
                    else:
                        llm_points = max(llm_points, fb.points_earned)

            point_val = human_points if override_exists else llm_points
            scores[student.id][task.id] = point_val

    # Excel-Datei generieren
    excel_bytes = export_service.generate_overview_bytes(
        course_name=course.name,
        students=students,
        tasks=tasks,
        scores=scores,
        session=session,
        filter_text=filter_text or None,
        annotations=annotations,
    )

    filename = f"punktestand_{course.name.replace(' ', '_')}.xlsx"
    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ═══════════════════════════════════════════════════════════════════
# KURS-PERFORMANCE-REPORT (LLM)
# ═══════════════════════════════════════════════════════════════════

@router.post("/courses/{course_id}/generate-report")
async def generate_course_report(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Generiert einen detaillierten Performance-Report für den Kurs via LLM.

    Sammelt alle Daten der gefilterten Aufgaben (Aufgaben, Submissions, Feedback,
    Hinweise) und lässt das LLM einen strukturierten Markdown-Report erstellen.
    """
    user, _ = user_and_course

    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    # Filter aus Query-Parametern (identisch zu get_course_overview)
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

    # Alle Studenten des Kurses
    memberships = session.exec(
        select(UserCourse)
        .where(UserCourse.course_id == course_id)
        .where(UserCourse.role_in_course == CourseRole.STUDENT)
    ).all()

    # Annotations-Filter (z.B. Übungsgruppe) — identisch zu get_course_overview
    annotation_filter = request.query_params.get("annotation_filter", "").strip()
    if annotation_filter:
        memberships = [m for m in memberships if _annotation_matches(m.annotation, annotation_filter)]

    students = [m.user for m in memberships]

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

    # Studentendaten formatieren (Submissions + Feedback + Hinweise)
    students_lines = []
    for student in students:
        students_lines.append(f"### {student.name} ({student.username})")

        has_submissions = False
        for task in tasks:
            # Alle Submissions für diesen Student bei dieser Aufgabe
            subs = session.exec(
                select(Submission)
                .where(Submission.task_id == task.id)
                .where(Submission.student_id == student.id)
                .order_by(Submission.submitted_at.asc())  # type: ignore[attr-defined]
            ).all()

            if not subs:
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

            students_lines.append("")
            students_lines.append(f"- **{task.title}**:")
            students_lines.append(f"  Punkte: {best_points}/{task.max_points}, Versuche: {total_attempts}")
            if solve_times:
                students_lines.append(f"  Bearbeitungsdauer: Ø {avg_time/60:.1f}min, Max {max_time/60:.1f}min")

            # Feedback-Kommentare der letzten Submission (leere Kommentare
            # überspringen — bei MC ist der Kommentar per Design leer)
            for fb in latest_sub.feedback_list:
                if not fb.comment:
                    continue
                source_label = "LLM" if fb.source == FeedbackSource.LLM else "Tutor"
                students_lines.append(f"  [{source_label}]: {fb.comment[:300]}")

            # MC: Feedback-Texte liegen in mc_result → kompakte Zusammenfassung
            # für das LLM (wird NICHT in der Feedback-UI angezeigt)
            if task.task_type.value == "mc" and latest_sub.mc_result:
                try:
                    mc_lines = mc_report_summary(json.loads(latest_sub.mc_result))
                    students_lines.extend("  " + ln for ln in mc_lines)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Hinweis-Anfragen für diese Aufgabe
            hints = session.exec(
                select(HintExchange)
                .where(HintExchange.task_id == task.id)
                .where(HintExchange.student_id == student.id)
            ).all()
            if hints:
                students_lines.append(f"  Hinweise angefragt: {len(hints)} mal")
                for h in hints[:3]:
                    students_lines.append(f"    Frage: {h.question[:200]}")
                    students_lines.append(f"    Antwort: {h.llm_response[:200]}")

        if not has_submissions:
            students_lines.append("  **Keine Einreichungen**")

        students_lines.append("")

    students_data = "\n".join(students_lines)

    # LLM-Config ermitteln
    llm_config = get_effective_llm_config(session, course_id)

    # Report generieren
    logger.info(f"Generate course report for {course.name}: {len(tasks)} tasks, {len(students)} students")
    result = await llm_service.generate_course_report(
        course_name=course.name,
        tasks_data=tasks_data,
        students_data=students_data,
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


# ═══════════════════════════════════════════════════════════════
# WORKSPACE DATEIEN (Tutor-Datei-Manager)
#
# Der Dateibaum = die reale Container-Dateistruktur. Jede Datei/Ordner
# hat eine Zugriffs-Klasse (s. docs/user-guide.md §3.3):
#   ✏️ edit      → public, editierbar (im Student-Volume)
#   🔒 readonly  → public, read-only (shared auf dem Agenten, ro-Mount)
#   👤 hidden    → privat (Student sieht es nie, nur Grading)
# Effektive Klasse = restriktivste von (eigene, Ordner-Vorfahren).
# ═══════════════════════════════════════════════════════════════════

def _safe_task_path(path: str) -> str:
    """Pfad validieren (relativ, kein ..). Liefert die bereinigte Form."""
    p = str(path).replace("\\", "/").lstrip("/")
    if not p or any(x == ".." for x in p.split("/")):
        raise HTTPException(400, "Ungültiger Dateipfad")
    return p


def _require_workspace_task(task: Task) -> None:
    if task.task_type.value != "workspace":
        raise HTTPException(400, "Keine Workspace-Aufgabe.")


def _init_artifact_data(session: Session, task: Task) -> dict | None:
    """Roh-Response des Agenten ({files: [...], dirs: [...]}) — Manifest
    vom ersten erreichbaren Pool-Agenten. None: kein Init oder Agent
    nicht erreichbar (degradierter Modus: UI zeigt keine [init]-Einträge).
    """
    from services.compute_client import ComputeAgentError
    if not task_has_init(task):
        return None
    for agent in _task_pool_agents(session, task):
        try:
            return workspace_service.client_for(agent).init_artifacts(
                task.course_id, task.id, "all")
        except ComputeAgentError:
            continue
    return None


def _init_artifact_lookup(session: Session, task: Task) -> dict[str, dict]:
    """Init-Artefakt-DATEIEN (Manifest) als Map
    {path: {scope, size, is_binary}}."""
    out: dict[str, dict] = {}
    for e in (_init_artifact_data(session, task) or {}).get("files", []):
        p = str(e.get("path") or "")
        if p:
            out[p] = {"scope": e.get("scope"), "size": e.get("size", 0),
                      "is_binary": bool(e.get("is_binary"))}
    return out


def _init_dir_lookup(session: Session, task: Task) -> dict[str, str]:
    """Init-ORDNER (Manifest-Vor-/Nach-Diff des letzten Builds) als Map
    {path: scope} — exakt die Ordner, die .init.sh angelegt hat
    (inkl. leerer)."""
    out: dict[str, str] = {}
    for e in (_init_artifact_data(session, task) or {}).get("dirs", []):
        p = str(e.get("path") or "")
        if p:
            out[p] = str(e.get("scope") or "")
    return out


def _drop_init_artifact_entries(session: Session, task: Task,
                                paths: list[str]) -> None:
    """Init-Manifest-Einträge auf ALLEN Pool-Agenten entfernen (bestrebt).

    Heilt getarnte Task-Dateien: ein Upload, der während eines laufenden
    Init-Builds sync-te wurde, ist fälschlich im Manifest gelandet — der
    Eintrag (und die Datei auf dem Agenten) wird hier entfernt. Der
    nächste Init-Build schreibt das Manifest neu; vom .init.sh wirklich
    erzeugte Dateien entstehen beim Rebuild erneut.
    """
    from services.compute_client import ComputeAgentError
    for p in paths:
        for agent in _task_pool_agents(session, task):
            try:
                workspace_service.client_for(agent).remove_init_artifact(
                    task.course_id, task.id, p)
            except ComputeAgentError as e:
                # 404 = auf diesem Agenten nicht gelistet; Agent
                # unerreichbar = degradierter Modus (wie Lookup).
                # Nicht still schlucken: ein 404 auf ALLEN Agenten bei
                # vorher positivem Lookup deutet auf einen veralteten
                # Agent-Code hin (Endpoint fehlt) — das Manifest wird
                # dann nicht geheilt und die Datei bleibt „[init]“.
                logger.warning(
                    "Init-Heilung (task %s, %s) auf %s fehlgeschlagen: %s (%s)",
                    task.id, p, agent["name"], e.status, e.message)


def _reject_init_artifact(session: Session, task: Task, path: str) -> None:
    """Mutationen auf [init]-Artefakte ablehnen (read-only).

    Init-Artefakte (Ergebnisse von .init.sh/.init_hidden.sh) liegen auf dem
    Agenten und entstehen nur per neuem Init-Build — sie sind im
    Tutor-Dateibaum virtuell und read-only.

    Ausnahme: echte Task-Dateien (DB-Row am selben Pfad). Ein
    Manifest-Eintrag dazu ist ein Sync/Build-Race-Artefakt (Upload während
    laufendem Build) — der Eintrag wird entfernt und die Mutation läuft
    normal durch (der nächste Init-Build schreibt das Manifest neu).
    """
    if path not in _init_artifact_lookup(session, task):
        return
    if workspace_service.get_task_file(session, task, path) is None:
        raise HTTPException(
            403, "Init-Artefakt ist read-only (per neuem Init-Build neu erzeugen).")
    _drop_init_artifact_entries(session, task, [path])


def _reject_init_dir(session: Session, task: Task, p: str) -> None:
    """Mutationen an/in Init-Ordnern ablehnen (read-only): Datei/Ordner
    anlegen, hinein verschieben, löschen, Zugriffs-Klasse ändern.
    Init-Ordner = Manifest-Einträge aus dem Vor-/Nach-Diff des letzten
    Builds — exakt die Ordner, die .init.sh angelegt hat (eine explizite
    Tutor-Zeile dazu kann nicht entstehen: Anlegen ist hier blockt,
    und der Build zählt nur Ordner, die vorher nicht existierten)."""
    init_dirs = _init_dir_lookup(session, task)
    if not init_dirs:
        return
    q = str(p)
    while q:
        if q in init_dirs:
            raise HTTPException(
                403, "Init-Artefakt ist read-only (per neuem Init-Build neu erzeugen).")
        q = q.rsplit("/", 1)[0] if "/" in q else ""


def _assert_path_free(session: Session, task: Task, p: str) -> None:
    """Neuanlage blockieren, wenn Pfad bereits existiert (Datei ODER Ordner)."""
    file_paths = {f.path for f in workspace_service.task_files(session, task)}
    disk = file_disk_path(task.id, p)
    if p in file_paths or disk.is_file():
        raise HTTPException(409, "Datei existiert bereits.")
    fm = workspace_service.folder_map(session, task)
    if p in fm or disk.is_dir():
        raise HTTPException(409, "Ordner existiert bereits.")


_INIT_ACCESS_BY_SCOPE = {"shared": "readonly", "seed": "edit", "private": "hidden"}


def _init_artifact_entries(session: Session, task: Task,
                           order_map: dict[str, int] | None = None) -> list[dict]:
    """Init-Artefakte (vom Agenten-Manifest) als virtuelle [init]-Einträge
    im Dateibaum — am REALEN Pfad (Ziel-Klasse entscheidet den Speicherort):
    shared→🔒, seed→✏️, private→👤. Read-only, nur per neuem Init-Build
    änderbar. Ohne Agenten: [] (Baum zeigt dann nur die DB-Dateien).

    ``order_map``: gepflegte Anzeige-Reihenfolge (task_workspace_orders);
    ohne Eintrag = Ende der Datei-Gruppe (sort_order None)."""
    existing = {f.path for f in workspace_service.task_files(session, task)}
    out = []
    for p, info in sorted(_init_artifact_lookup(session, task).items()):
        if p in existing:
            continue  # DB-Datei gewinnt (init-Artefakt wurde überschrieben)
        acc = _INIT_ACCESS_BY_SCOPE.get(info.get("scope"), "readonly")
        out.append({
            "id": f"init:{info.get('scope')}:{p}",
            "path": p,
            "size": info.get("size", 0),
            "access": acc,
            "file_access": acc,
            "is_binary": info.get("is_binary", False),
            "sort_order": (order_map or {}).get(p),
            "init": True,
            "updated_at": None,
        })
    return out


def _init_dir_entries(session: Session, task: Task) -> list[dict]:
    """Init-Ordner (Manifest) als virtuelle [init]-Zeilen im Ordner-Baum
    — Access erbt den Ziel-Ordner (Store): shared→🔒, seed→✏️ (Root =
    editierbar), private→👤. Explizite Tutor-Zeilen gewinnen (dann kein
    init-Flag → editierbar)."""
    fm = workspace_service.folder_map(session, task)
    out = []
    for p, scope in sorted(_init_dir_lookup(session, task).items()):
        if p in fm:
            continue
        acc = _INIT_ACCESS_BY_SCOPE.get(scope, "readonly")
        out.append({"path": p, "access": acc, "own": None, "init": True})
    return out


def _read_init_artifact(session: Session, task: Task, path: str,
                        scope: str) -> Response:
    """Inhalt eines Init-Artefakts (raw bytes + MIME-Typ, damit Medien
    wie PNGs in der Preview erscheinen). Pool-Agenten der Reihe nach."""
    from services.compute_client import ComputeAgentError
    last_err: ComputeAgentError | None = None
    for agent in _task_pool_agents(session, task):
        try:
            _size, data = workspace_service.client_for(agent).init_artifact_file(
                task.course_id, task.id, scope, path)
        except ComputeAgentError as e:
            last_err = e
            continue
        return Response(
            content=data,
            media_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )
    raise HTTPException(502, last_err.message if last_err else "Keine Compute-Engine verfügbar")


def _check_workspace_size(session: Session, task: Task, extra_bytes: int) -> None:
    """Gesamtvolumen des Aufgaben-Workspaces begrenzen (512 MB)."""
    existing = sum(f.size for f in workspace_service.task_files(session, task))
    if existing + extra_bytes > MAX_WORKSPACE_TOTAL_BYTES:
        raise HTTPException(
            413, "Workspace-Volumen überschritten (max. 512 MB gesamt).")


@router.get("/tasks/{task_id}/workspace/files")
async def list_workspace_files(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Dateibaum der Workspace-Aufgabe (alle Klassen, inkl. hidden).

    ``files``: effektive Klasse ``access`` + explizite ``file_access``.
    ``folders``: explizite Ordner-Klassen (effektiv + eigene Setzung).
    Dazu die Init-Artefakte (.init.sh/.init_hidden.sh-Ergebnisse) als
    virtuelle read-only [init]-Einträge am realen Pfad (ohne Agenten: leer)."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    files = workspace_service.task_files(session, task)
    fm = workspace_service.folder_map(session, task)
    om = workspace_service.order_map(session, task)
    out_files = [{
        "id": f.id,
        "path": f.path,
        "size": f.size,
        "access": effective_file_access(f.path, f.access, fm),
        "file_access": f.access,
        "is_binary": f.is_binary,
        "sort_order": f.sort_order,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    } for f in files]
    out_files.extend(_init_artifact_entries(session, task, om))
    out_folders = [{
        "path": p,
        "access": effective_folder_access(p, fm),
        "own": fm.get(p),
    } for p in sorted(fm)]
    out_folders.extend(_init_dir_entries(session, task))
    # no-store: nach Moves/Deletes darf der Browser keine gecachte (alte)
    # Liste zeigen, sonst wirken Dateien scheinbar „dupliziert“.
    return JSONResponse(
        content={
            "files": out_files,
            "folders": out_folders,
            "folder_order": om,  # Ordner + [init]-Artefakte (Anzeige nur)
            "total": sum(f["size"] for f in out_files),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/tasks/{task_id}/workspace/files/{path:path}")
async def read_workspace_file(
    task_id: int,
    path: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Datei-Inhalt laden (für den Editor). Binarys: als octet-stream.

    Fehlende Disk-Dateien, die als [init]-Artefakt gelistet sind, kommen
    vom Agenten (MIME-typisiert, damit Medien wie PNGs in der Preview
    erscheinen)."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    p = _safe_task_path(path)
    disk = file_disk_path(task.id, p)
    if not disk.is_file():
        art = _init_artifact_lookup(session, task).get(p)
        if art:
            return _read_init_artifact(session, task, p, str(art.get("scope") or "shared"))
        raise HTTPException(404, "Datei nicht gefunden.")
    if disk.stat().st_size > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß für den Editor (max. 50 MB).")
    data = disk.read_bytes()
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
async def write_workspace_file(
    task_id: int,
    path: str,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Textdatei speichern (Editor-Save)."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    p = _safe_task_path(path)
    _reject_init_artifact(session, task, p)
    body = await request.json()
    content = body.get("content")
    if not isinstance(content, str):
        raise HTTPException(422, "Feld 'content' (String) fehlt.")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß (max. 50 MB).")
    _check_workspace_size(session, task, len(data))
    row = workspace_service.save_task_file(session, task, p, data)
    _schedule_workspace_sync(task.id)
    fm = workspace_service.folder_map(session, task)
    return {"ok": True, "path": p, "size": row.size,
            "access": effective_file_access(p, row.access, fm)}


@router.post("/tasks/{task_id}/workspace/files")
async def create_workspace_file(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Neue Textdatei anlegen ({path, content})."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    body = await request.json()
    if body.get("path") in (None, ""):
        raise HTTPException(422, "Feld 'path' fehlt.")
    p = _safe_task_path(str(body["path"]))
    _reject_init_artifact(session, task, p)
    _reject_init_dir(session, task, p)
    _assert_path_free(session, task, p)
    content = body.get("content") or ""
    if not isinstance(content, str):
        raise HTTPException(422, "Feld 'content' muss ein String sein.")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß (max. 50 MB).")
    _check_workspace_size(session, task, len(data))
    row = workspace_service.save_task_file(session, task, p, data)
    _schedule_workspace_sync(task.id)
    fm = workspace_service.folder_map(session, task)
    return {"ok": True, "path": p, "size": row.size,
            "access": effective_file_access(p, row.access, fm)}


@router.post("/tasks/{task_id}/workspace/files/move")
async def move_workspace_file(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Datei verschieben/umbenennen ({src, dst}) — Klassen-Wechsel = Sichtbarkeit."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    body = await request.json()
    src = _safe_task_path(str(body.get("src") or ""))
    dst = _safe_task_path(str(body.get("dst") or ""))
    arts = _init_artifact_lookup(session, task)
    blocked = [q for q in (src, dst) if q in arts]
    if blocked:
        real = {f.path for f in workspace_service.task_files(session, task)}
        _drop_init_artifact_entries(session, task,
                                    [q for q in blocked if q in real])
        if any(q not in real for q in blocked):
            raise HTTPException(
                403, "Init-Artefakt ist read-only (per neuem Init-Build neu erzeugen).")
    # DB-Datei in einen Init-Ordner zu verschieben würde ihn zu einem
    # gemischten (editierbaren) Ordner machen → blocken.
    _reject_init_dir(session, task, dst)
    try:
        row = workspace_service.move_task_file(session, task, src, dst)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _schedule_workspace_sync(task.id)
    fm = workspace_service.folder_map(session, task)
    return {"ok": True, "path": row.path, "size": row.size,
            "access": effective_file_access(row.path, row.access, fm)}


@router.post("/tasks/{task_id}/workspace/reorder")
async def reorder_workspace(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Anzeige-Reihenfolge eines Ordners per Drag & Drop ({order: [Pfade]}).

    Dateien, Ordner UND [init]-Artefakte sind sortierbar (gemischt in
    einer Gruppe) — rein kosmetisch, keine Datei-/Sync-/Grading-Änderung.
    """
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    body = await request.json()
    order = body.get("order")
    if not isinstance(order, list) or not order or len(order) > 500:
        raise HTTPException(422, "Feld 'order' (Liste von Pfaden) fehlt bzw. zu lang.")
    paths = [_safe_task_path(str(p)) for p in order]
    try:
        workspace_service.reorder_workspace(
            session, task, paths,
            virtual_paths=set(_init_artifact_lookup(session, task)))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/tasks/{task_id}/workspace/upload")
async def upload_workspace_file(
    task_id: int,
    file: UploadFile = File(...),
    path: str = Form(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Binary-Upload (Datasets etc.)."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    p = _safe_task_path(path)
    _reject_init_artifact(session, task, p)
    _reject_init_dir(session, task, p)
    _assert_path_free(session, task, p)
    data = await file.read()
    if len(data) > MAX_WORKSPACE_TOTAL_BYTES:
        raise HTTPException(413, "Upload zu groß (max. 512 MB).")
    _check_workspace_size(session, task, len(data))
    row = workspace_service.save_task_file(session, task, p, data)
    _schedule_workspace_sync(task.id)
    fm = workspace_service.folder_map(session, task)
    return {"ok": True, "path": p, "size": row.size,
            "access": effective_file_access(p, row.access, fm)}


@router.delete("/tasks/{task_id}/workspace/files/{path:path}")
async def delete_workspace_file(
    task_id: int,
    path: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Datei aus dem Aufgaben-Workspace entfernen."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    p = _safe_task_path(path)
    _reject_init_artifact(session, task, p)
    workspace_service.delete_task_file(session, task, p)
    _schedule_workspace_sync(task.id)
    return {"ok": True}


@router.post("/tasks/{task_id}/workspace/access")
async def set_workspace_access(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Zugriffs-Klasse einer Datei oder eines Ordners setzen.

    Body: {path, is_folder, access: null|"readonly"|"hidden"}
    (null = Datei: erben / Ordner: edit).
    """
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    body = await request.json()
    p = _safe_task_path(str(body.get("path") or ""))
    _reject_init_artifact(session, task, p)
    if body.get("is_folder"):
        # Klasse setzen legt eine explizite Zeile an → der Init-Ordner
        # wäre nicht mehr read-only.
        _reject_init_dir(session, task, p)
    access = body.get("access")
    if access not in (None, "readonly", "hidden"):
        raise HTTPException(422,
                            "access muss null, 'readonly' oder 'hidden' sein.")
    try:
        if body.get("is_folder"):
            workspace_service.set_folder_access(session, task, p, access)
        else:
            workspace_service.set_file_access(session, task, p, access)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _schedule_workspace_sync(task.id)
    return {"ok": True}


@router.post("/tasks/{task_id}/workspace/folders/move")
async def move_workspace_folder(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Ordner verschieben/umbenennen ({src, dst}) — DB-Rows (Dateien +
    Ordner-Klassen) werden mitgezogen."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    body = await request.json()
    src = _safe_task_path(str(body.get("src") or ""))
    dst = _safe_task_path(str(body.get("dst") or ""))
    arts = _init_artifact_lookup(session, task)
    blocked = [a for a in arts if a == src or a.startswith(src + "/")]
    if blocked:
        # Race-Geister (Task-Dateien, die fälschlich im Manifest sind):
        # Einträge entfernen; echte Init-Artefakte bleiben read-only.
        real = {f.path for f in workspace_service.task_files(session, task)}
        _drop_init_artifact_entries(
            session, task, [q for q in blocked if q in real])
        if any(q not in real for q in blocked):
            raise HTTPException(
                403, "Enthält Init-Artefakte — diese sind read-only "
                     "(per neuem Init-Build neu erzeugen).")
    # Ziel in/an einen Init-Ordner → blocken (Deckung mit delete: ein
    # Init-Ordner hat Artefakte im Subtree und wird oben schon blockt).
    _reject_init_dir(session, task, dst)
    try:
        workspace_service.move_folder(session, task, src, dst)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _schedule_workspace_sync(task.id)
    return {"ok": True}


@router.post("/tasks/{task_id}/workspace/folders")
async def create_workspace_folder(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """(Möglichlicherweise leeren) Ordner anlegen ({path}) — persistiert
    als Disk-Verzeichnis + explizite Zeile (access=NULL=edit), bleibt also
    auch leer sichtbar (Baum, Student-Ansicht, frische Volumes)."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    body = await request.json()
    p = _safe_task_path(str(body.get("path") or ""))
    _reject_init_artifact(session, task, p)
    _reject_init_dir(session, task, p)
    try:
        workspace_service.create_task_folder(session, task, p)
    except ValueError as e:
        raise HTTPException(409 if "bereits" in str(e) else 400, str(e))
    _schedule_workspace_sync(task.id)
    return {"ok": True}


@router.delete("/tasks/{task_id}/workspace/folders/{path:path}")
async def delete_workspace_folder(
    task_id: int,
    path: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Ordner-Subtree entfernen: Disk (Verzeichnis inkl. Inhalt) + alle
    DB-Rows (Dateien + Ordner-Klassen, auch verschachtelt)."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    p = _safe_task_path(path)
    _reject_init_dir(session, task, p)
    arts = _init_artifact_lookup(session, task)
    blocked = [a for a in arts if a == p or a.startswith(p + "/")]
    if blocked:
        # Race-Geister (Task-Dateien, die fälschlich im Manifest sind):
        # Einträge entfernen; echte Init-Artefakte bleiben read-only.
        real = {f.path for f in workspace_service.task_files(session, task)}
        _drop_init_artifact_entries(
            session, task, [q for q in blocked if q in real])
        if any(q not in real for q in blocked):
            raise HTTPException(
                403, "Enthält Init-Artefakte — diese sind read-only "
                     "(per neuem Init-Build neu erzeugen).")
    try:
        workspace_service.delete_task_folder(session, task, p)
    except ValueError as e:
        raise HTTPException(404 if "nicht gefunden" in str(e) else 400, str(e))
    _schedule_workspace_sync(task.id)
    return {"ok": True}


@router.post("/tasks/{task_id}/workspace/sync")
async def sync_workspace(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Asset-Sync + Init-Build erzwungen neu ausführen (Sync-&-Init-Button).

    Im Gegensatz zum automatischen Sync bei Task-Save (idempotent über
    den init-Hash) wird der Init-Build hier erzwungen: .init.sh läuft
    komplett neu, die alten Init-Artefakte werden zuerst gelöscht und
    dann neu erzeugt (Manifest = exakt das Ergebnis dieses Laufs).
    Achtung: Downloads im Skript werden bei einem Force-Run wiederholt.

    Läuft im Hintergrund; der Status ist danach in
    task.workspace_assets_status (per GET /tasks/{id} pollen).
    """
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    not_ready = workspace_service.validate_task_ready(session, task)
    if not_ready:
        raise HTTPException(400, not_ready)
    _schedule_workspace_sync(task.id, force=True)
    return {"ok": True, "message": "Sync & Init gestartet."}


@router.post("/tasks/{task_id}/workspace/init")
async def workspace_init_build(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Task-Image (.init.sh) erzwungen neu bauen — Force-Rebuild-Button.

    Der Init-Build ist normalerweise über den init-Hash idempotent;
    hier wird er erzwungen (Skript läuft komplett neu, alte Init-
    Artefakte werden gelöscht + neu erzeugt). Läuft wie der Sync im
    Hintergrund (on_task_saved → init_build je Agent).
    """
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    not_ready = workspace_service.validate_task_ready(session, task)
    if not_ready:
        raise HTTPException(400, not_ready)
    _schedule_workspace_sync(task.id, force=True)
    return {"ok": True, "message": "Init-Build gestartet."}


@router.get("/tasks/{task_id}/workspace/init-status")
async def workspace_init_status(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Live-Init-Status je Engine (none|ready|building|failed|idle).

    Wird von der Aufgaben-UI gepollt, solange ein Init-Build läuft
    (bzw. nach einem Force-Rebuild).
    """
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    from services.compute_client import ComputeAgentError
    if not task_has_init(task):
        return {"agents": [], "status": "none"}
    from services import image_spec_service
    try:
        resolved = image_spec_service.resolve_task_image(session, task)
    except image_spec_service.ImageSpecError as e:
        return {"agents": [], "status": "failed", "error": str(e)}
    if resolved is None:
        return {"agents": [], "status": "failed",
                "error": "Workspace-Image nicht auflösbar (Image-Spec fehlt?)"}
    init_hash = task_init_hash(session, task, resolved["image"])
    agents = _task_pool_agents(session, task)
    out = []
    for agent in agents:
        try:
            st = await asyncio.to_thread(
                workspace_service.client_for(agent).init_status,
                task.course_id, task.id, init_hash)
        except ComputeAgentError as e:
            st = {"status": "failed", "error": e.message}
        st["agent"] = agent["name"]
        st["url"] = agent["url"]
        out.append(st)
    overall = "ready"
    for st in out:
        if st.get("status") in ("failed", "building"):
            overall = st["status"]
            break
        if st.get("status") == "idle":
            overall = "idle"
    return {"agents": out, "status": overall, "init_hash": init_hash}


@router.post("/tasks/{task_id}/workspace/init/stop")
async def workspace_init_stop(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Aktiven Init-Build abbrechen (je Engine des Pools)."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    from services.compute_client import ComputeAgentError
    from services import image_spec_service
    if not task_has_init(task):
        return {"ok": True, "stopped": []}
    try:
        resolved = image_spec_service.resolve_task_image(session, task)
    except image_spec_service.ImageSpecError:
        resolved = None
    if resolved is None:
        return {"ok": True, "stopped": []}
    init_hash = task_init_hash(session, task, resolved["image"])
    agents = _task_pool_agents(session, task)
    stopped = []
    for agent in agents:
        try:
            await asyncio.to_thread(
                workspace_service.client_for(agent).init_stop,
                task.course_id, task.id, init_hash)
            stopped.append(agent["name"])
        except ComputeAgentError:
            pass
    return {"ok": True, "stopped": stopped}


# ═══════════════════════════════════════════════════════════════════
# WORKSPACE: MUSTERLÖSUNG-TESTLAUF (Tutor, 🧪-Button)
# ═══════════════════════════════════════════════════════════════════

async def _test_run_from_db(session: Session, user: User, run_id: str,
                            stale: bool = False) -> dict:
    """Persistierten Testlauf aus der DB zurückgeben (Agent-404-Fallback).

    stale=True: Agent kennt den Lauf nicht mehr (z. B. nach Neustart) →
    noch „running“ stehende Rows werden auf „killed“ gesetzt.
    """
    run = session.exec(select(WorkspaceRun).where(
        WorkspaceRun.run_id == run_id,
        WorkspaceRun.student_id == user.id,
    )).first()
    if run is None:
        raise HTTPException(404, "Lauf nicht gefunden.")
    if stale and run.status == WorkspaceRunStatus.RUNNING:
        run.status = WorkspaceRunStatus.KILLED
        run.finished_at = datetime.now()
        run.stderr = ((run.stderr or "") +
                      "\n[agent] Lauf nach Agent-Neustart nicht mehr verfügbar").strip()
        session.add(run)
        session.commit()
        session.refresh(run)
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "exit_code": run.exit_code,
        "stdout": run.stdout or "",
        "stderr": run.stderr or "",
    }


@router.post("/tasks/{task_id}/workspace/test-run")
async def workspace_test_run(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Musterlösung-Testlauf (🧪): ephemerer Container mit frischem
    Volume, alle Task-Dateien inkl. 👤 injiziert, führt .test_solution.sh
    aus (Overlay .solution/ + run.sh + test.sh + .test_private.sh).
    Das Lauf-Workspace wird nach dem finalen Status deterministisch
    gelöscht."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    if not workspace_service.is_enabled(session, task.course_id):
        raise HTTPException(503, "Compute für diesen Kurs ist nicht aktiv.")
    from services.compute_client import ComputeAgentError
    agent = workspace_service.pick_task_agent(session, task)
    if agent is None:
        raise HTTPException(503, workspace_service.pick_agent_error(
            session, task.course_id, workspace_service.task_engine_names(task)))
    files = {f.path for f in workspace_service.task_files(session, task)}
    if TEST_SOLUTION_SCRIPT not in files:
        raise HTTPException(
            400, f"Kein {TEST_SOLUTION_SCRIPT} hinterlegt — die Musterlösung "
                 "kann nicht getestet werden.")
    client = workspace_service.client_for(agent)
    key = test_run_key(task)
    command = f"bash {TEST_SOLUTION_SCRIPT}"
    try:
        starter = workspace_service.test_run_starter_files(session, task)
        folders = workspace_service.materialize_folders(
            session, task, include_hidden=True)
        spec = workspace_service.workspace_spec_for_agent(session, task, agent)
        await asyncio.to_thread(
            ensure_fresh_workspace, client, key, spec, starter, folders)
        result = await asyncio.to_thread(
            client.start_run, key, command, task.workspace_timeout)
    except ComputeAgentError as e:
        raise HTTPException(
            e.status if e.status in (400, 404, 409, 413, 422) else 502,
            e.message)
    workspace_service.record_run(session, task, user.id, command, result)
    return {
        "run_id": result.get("run_id"),
        "async": True,
        "status": result.get("status", "queued"),
    }


@router.get("/tasks/{task_id}/workspace/test-runs/{run_id}")
async def workspace_test_run_status(
    task_id: int,
    run_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Status eines Testlaufs (Live-Log); beim finalen Status räumt das
    Backend das ephemere Lauf-Workspace deterministisch auf."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    from services.compute_client import ComputeAgentError
    agent = workspace_service.pick_task_agent(session, task)
    if agent is None:
        return await _test_run_from_db(session, user, run_id, stale=True)
    client = workspace_service.client_for(agent)
    key = test_run_key(task)
    try:
        status = await asyncio.to_thread(client.run_status, key, run_id)
    except ComputeAgentError as e:
        if e.status == 404:
            return await _test_run_from_db(session, user, run_id, stale=True)
        raise HTTPException(502, e.message)
    # Normalisierung: Agent liefert stdout/stderr als Zeilenliste
    for field in ("stdout", "stderr"):
        if isinstance(status.get(field), list):
            status[field] = "\n".join(status[field])
    run = session.exec(select(WorkspaceRun).where(
        WorkspaceRun.run_id == run_id,
        WorkspaceRun.student_id == user.id,
    )).first()
    if run is not None:
        workspace_service.update_run_from_status(session, task, status)
    if status.get("status") not in ("running", "queued"):
        try:
            await asyncio.to_thread(client.delete_workspace, key)
        except ComputeAgentError:
            pass
    return status


@router.post("/tasks/{task_id}/workspace/test-runs/{run_id}/stop")
async def workspace_test_run_stop(
    task_id: int,
    run_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Laufenden Musterlösung-Testlauf stoppen."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    from services.compute_client import ComputeAgentError
    agent = workspace_service.pick_task_agent(session, task)
    if agent is None:
        raise HTTPException(503, "Kein Compute-Agent verfügbar.")
    client = workspace_service.client_for(agent)
    key = test_run_key(task)
    try:
        await asyncio.to_thread(client.stop_run, key, run_id)
    except ComputeAgentError as e:
        if e.status == 409:
            raise HTTPException(404, "Lauf nicht (mehr) aktiv.")
        raise HTTPException(502, e.message)
    return {"ok": True}


@router.get("/tasks/{task_id}/workspace/compute-status")
async def workspace_compute_status(
    task_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Compute-Status für die Aufgaben-UI (Banner degradierte Modus)."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    status = workspace_service.status(session, task.course_id)
    agent = workspace_service.pick_task_agent(session, task)
    status["task_agent"] = agent["name"] if agent else None
    return status


@router.get("/tasks/{task_id}/package")
async def download_task_package(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Tutor-Download-Paket (Aufgaben-Vorlage): Vollpaket inkl. .solution/
    .tests/ und compose mit verify-Service. ?role=student liefert die
    Student-Variante (ohne private Dateien) zum Weitergeben."""
    from services.compose_gen import build_package
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    role = request.query_params.get("role") or "tutor"
    if role not in ("tutor", "student"):
        role = "tutor"
    try:
        fname, data = await asyncio.to_thread(build_package, task, role, None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Response(
        content=data,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/tasks/{task_id}/submissions/{submission_id}/package")
async def download_submission_package(
    task_id: int,
    submission_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Tutor-Download-Paket einer Einreichung (Student-Dateien + private
    Dateien + verify-Service). ?role=student → nur die Student-Variante
    (z.B. zur Rückgabe an den Studenten)."""
    from services.compose_gen import build_package
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    submission = session.get(Submission, submission_id)
    if not submission or submission.task_id != task.id:
        raise HTTPException(404, "Einreichung nicht gefunden.")
    role = request.query_params.get("role") or "tutor"
    if role not in ("tutor", "student"):
        role = "tutor"
    try:
        fname, data = await asyncio.to_thread(build_package, task, role, submission)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Response(
        content=data,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ═══════════════════════════════════════════════════════════════════
# WORKSPACE: RUN-HISTORIE (Tutor)
# ═══════════════════════════════════════════════════════════════════


@router.get("/tasks/{task_id}/submissions/{submission_id}/runs")
async def list_submission_runs(
    task_id: int,
    submission_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Run-Historie einer Einreichung (Grading-Läufe), neueste zuerst."""
    task = await _load_workspace_task(task_id, session, user)
    _require_workspace_task(task)
    submission = session.get(Submission, submission_id)
    if not submission or submission.task_id != task.id:
        raise HTTPException(404, "Einreichung nicht gefunden.")
    runs = session.exec(
        select(WorkspaceRun)
        .where(WorkspaceRun.submission_id == submission.id)
        .order_by(WorkspaceRun.started_at.desc())  # type: ignore[attr-defined]
    ).all()[:20]
    return {
        "runs": [
            {
                "id": r.id,
                "run_id": r.run_id,
                "command": r.command,
                "status": r.status.value,
                "exit_code": r.exit_code,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "stdout": (r.stdout or "")[-20_000:],
                "stderr": (r.stderr or "")[-20_000:],
            }
            for r in runs
        ]
    }


# ═══════════════════════════════════════════════════════════════════
# KURS-COMPUTE-SETTINGS (PROF/ADMIN)
# ═══════════════════════════════════════════════════════════════════

def _check_course_prof_admin(user: User, course_id: int, session: Session) -> None:
    """PROF im Kurs oder globaler Admin (Compute-Settings ändern)."""
    if user.role == GlobalUserRole.ADMIN:
        return
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()
    if not membership or membership.role_in_course != CourseRole.PROF:
        raise HTTPException(403, "Nur PROF/Admin dürfen die Compute-Settings ändern.")


def _compute_settings_payload(session: Session, course_id: int) -> dict:
    """Kurs-Overrides (None = global) + effektiv gültige Konfiguration
    (Agenten ohne API-Key)."""
    cs = session.exec(
        select(CourseSettings).where(CourseSettings.course_id == course_id)
    ).first()
    effective = get_effective_compute_config(session, course_id)
    return {
        "overrides": {
            "compute_enabled": cs.compute_enabled if cs else None,
            "compute_gpu_enabled": cs.compute_gpu_enabled if cs else None,
            "compute_agents": cs.compute_agents if cs else None,
        },
        "effective": {
            "enabled": effective["enabled"],
            "gpu_enabled": effective["gpu_enabled"],
            "source": effective["source"],
            "agents": [
                {k: v for k, v in a.items() if k != "key"}
                for a in effective["agents"]
            ],
        },
    }


@router.get("/courses/{course_id}/compute-settings")
async def get_compute_settings(
    course_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kurs-Overrides + effektiv gültige Compute-Konfiguration."""
    _check_course_prof_admin(user, course_id, session)
    return _compute_settings_payload(session, course_id)


@router.get("/courses/{course_id}/compute/engines")
async def course_compute_engines(
    course_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Effektive Compute-Engines des Kurses mit Health + GPU (detektiert).

    Für die UI-Auswahl (Task-Editor: Engine-Dropdown, Kurs-Settings:
    Install-Picker). Health-Cache (30 s) wird bewusst genutzt.

    Read-only: PROF, TUTOR (Task-Editor: Engine-Auswahl) und Admin.
    """
    _check_course_role(user, course_id, session)
    return workspace_service.status(session, course_id)


class CourseComputeTestRequest(SQLModel):
    url: Optional[str] = ""
    key: Optional[str] = ""
    name: Optional[str] = ""


@router.post("/courses/{course_id}/compute/test")
async def course_compute_test(
    course_id: int,
    data: CourseComputeTestRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Einzelne Engine testen (ohne Cache, frische Health-Abfrage).

    url+key: direkt aus dem Formular (neue, noch nicht gespeicherte
    Kurs-Engine); sonst name: aus der effektiven Registry — so lassen
    sich auch globale Engines testen, deren Keys der Kurs-UI verborgen
    bleiben.
    """
    _check_course_prof_admin(user, course_id, session)
    from services.compute_client import ComputeAgentError, ComputeClient

    url = (data.url or "").strip().rstrip("/")
    key = data.key or ""
    if not url and data.name:
        agent = next(
            (a for a in workspace_service.get_agents(session, course_id)
             if a["name"] == data.name),
            None,
        )
        if agent is None:
            raise HTTPException(404, f"Engine {data.name!r} ist nicht registriert.")
        url = agent["url"]
        key = agent["key"]
    if not url.startswith("http://") and not url.startswith("https://"):
        return {"ok": False, "error": "URL muss mit http:// beginnen."}
    client = ComputeClient(url=url, key=key or None)
    try:
        info = client.health(timeout=8.0)
        return {
            "ok": bool(info.get("docker")),
            "docker": bool(info.get("docker")),
            "gpu": bool(info.get("gpu")),
            "gpus": info.get("gpus") or [],
            "gpu_info": info.get("gpu_info") or "",
            "workspaces": info.get("workspaces"),
            "error": None if info.get("docker") else "Docker-Daemon nicht erreichbar",
        }
    except ComputeAgentError as e:
        return {"ok": False, "docker": False, "gpu": False,
                "workspaces": None, "error": e.message}


@router.put("/courses/{course_id}/compute-settings")
async def put_compute_settings(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kurs-Overrides setzen. `null` = Reset auf Global/Standard.

    Raw-JSON statt Pydantic, damit `null` (Reset) von "Feld fehlt"
    unterscheidbar bleibt.
    """
    _check_course_prof_admin(user, course_id, session)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Ungültiges JSON.") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON-Objekt erwartet.")

    cs = session.exec(
        select(CourseSettings).where(CourseSettings.course_id == course_id)
    ).first()
    if cs is None:
        cs = CourseSettings(course_id=course_id)

    for key in ("compute_enabled", "compute_gpu_enabled"):
        if key in body:
            val = body[key]
            if val is not None and not isinstance(val, bool):
                raise HTTPException(422, f"{key} muss true/false/null sein.")
            setattr(cs, key, val)

    if "compute_agents" in body:
        val = body["compute_agents"]
        if val is not None:
            if not isinstance(val, str) or not val.strip():
                raise HTTPException(422, "compute_agents muss JSON-Text oder null sein.")
            try:
                parsed = json.loads(val)
            except json.JSONDecodeError as e:
                raise HTTPException(422, f"compute_agents: Ungültiges JSON ({e})") from e
            if not isinstance(parsed, list) or any(not isinstance(a, dict) for a in parsed):
                raise HTTPException(422, "compute_agents muss eine JSON-Liste von Objekten sein.")
        cs.compute_agents = val if val else None

    session.add(cs)
    session.commit()
    return {"ok": True, **_compute_settings_payload(session, course_id)}