"""
Image-Spec-Endpoints: globale Specs (Admin) + Kurs-Specs (Prof) +
Engine-Image-Proxy.

Rollen:
- Admin: globale Specs verwalten (CRUD + LLM-Generierung), Installation auf
  globalen UND Kurs-Engines (course_id im Body).
- Prof: Kurs-Specs verwalten, Installation nur auf KURS-Engines.
  (Globale Specs sind im Kurs lesbar, editierbar nur über die Admin-Konsole.)
- Tutor: nur Lesezugriff auf Kurs-Specs + Engine-Status (Task-Editor:
  Image-Spec-/Engine-Auswahl für Workspace-Aufgaben).

Engine-Auswahl für Installation: `engine` = Name aus der effektiven
Agent-Registry (Kurs-Override > Global > .env).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from database.base import get_session
from database.models import (
    Course,
    CourseRole,
    GlobalUserRole,
    ImageSpec,
    User,
    UserCourse,
)
from services import image_spec_service
from services.auth_service import get_current_user, require_global_admin
from services.compute_client import ComputeAgentError
from services.llm_service import LLMService
from services.settings_resolver import get_effective_llm_config
from services.workspace_service import workspace_service

router = APIRouter(tags=["Image-Specs"])
logger = logging.getLogger(__name__)
_llm = LLMService()


# ── Helpers ────────────────────────────────────────────────────────

def _check_course_prof_admin(user: User, course_id: int,
                             session: Session) -> Course:
    """PROF im Kurs oder globaler Admin."""
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")
    if user.role == GlobalUserRole.ADMIN:
        return course
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()
    if not membership or membership.role_in_course != CourseRole.PROF:
        raise HTTPException(403, "Nur PROF/Admin dürfen Image-Specs verwalten.")
    return course


def _check_course_tutor(user: User, course_id: int,
                        session: Session) -> Course:
    """PROF/TUTOR im Kurs oder globaler Admin (Lesezugriff für den Task-Editor)."""
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")
    if user.role == GlobalUserRole.ADMIN:
        return course
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()
    if not membership or membership.role_in_course not in (CourseRole.PROF, CourseRole.TUTOR):
        raise HTTPException(403, "Nur PROF/TUTOR/Admin dürfen auf diese Daten zugreifen.")
    return course


def _get_spec_or_404(session: Session, spec_id: int) -> ImageSpec:
    row = image_spec_service.get_by_id(session, spec_id)
    if row is None:
        raise HTTPException(404, "Image-Spec nicht gefunden.")
    return row


def _engine_spec_status(client, session: Session,
                        course_id: Optional[int]) -> dict[str, dict]:
    """spec_id → {refs, installed, building} auf einer Engine (UI-Filter).

    Optional: bei Fehlern (z. B. ältere Agent-Version ohne /images/spec-status)
    leer — die UI zeigt dann alle Specs als installierbar (altes Verhalten).
    """
    specs = image_spec_service.list_specs(session, course_id)
    if not specs:
        return {}
    try:
        statuses = client.images_spec_status(
            [{"name": s["name"], "dockerfile": s["dockerfile"]} for s in specs])
    except ComputeAgentError:
        return {}
    return {str(s["id"]): st for s, st in zip(specs, statuses)
            if isinstance(st, dict)}


def _json_body(request_body: dict, *fields: str) -> dict:
    for f in fields:
        if not str(request_body.get(f) or "").strip():
            raise HTTPException(422, f"Feld '{f}' fehlt.")
    return request_body


def _engine_images_error(e: ComputeAgentError) -> HTTPException:
    """Alte Agent-Version ohne /images-Endpoints → freundliche Meldung."""
    if e.status in (404, 405):
        return HTTPException(
            400, "Der Compute-Agent unterstützt die Image-Verwaltung noch nicht "
                 "(Agent-Update erforderlich).")
    status = e.status if e.status < 500 else 502
    return HTTPException(status, e.message)


def _agent_by_name(session: Session, course_id: Optional[int], name: str) -> dict:
    agents = workspace_service.get_agents(session, course_id)
    for a in agents:
        if a["name"] == name:
            return a
    scope_txt = f"Kurs {course_id}" if course_id else "global"
    raise HTTPException(404, f"Engine {name!r} ist nicht registriert ({scope_txt}).")


def _save_spec(body: dict, session: Session, scope: str,
               created_by: Optional[int]) -> dict:
    name = str(body.get("name") or "").strip()
    dockerfile = str(body.get("dockerfile") or "").strip()
    if not name:
        raise HTTPException(422, "Feld 'name' fehlt.")
    if not dockerfile:
        raise HTTPException(422, "Feld 'dockerfile' fehlt.")
    try:
        row = image_spec_service.save_spec(session, scope, name, dockerfile,
                                           created_by=created_by)
    except image_spec_service.ImageSpecError as e:
        raise HTTPException(422, str(e))
    return image_spec_service.spec_info(row)


async def _generate_spec(session: Session, body: dict,
                         course_id: Optional[int] = None) -> dict:
    """LLM-Dockerfile-Entwurf (validiert, aber NICHT gespeichert — UI bestätigt).

    Body: {description, name, current_dockerfile?} — mit current_dockerfile
    passt das LLM die bestehende Spec an statt einer neuen zu erzeugen.
    """
    description = str(body.get("description") or "").strip()
    if not description:
        raise HTTPException(422, "Feld 'description' fehlt.")
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(
            422, "Feld 'name' fehlt (Dockerfiles enthalten keinen Namen).")
    current_dockerfile = str(body.get("current_dockerfile") or "").strip() or None
    cfg = get_effective_llm_config(session, course_id)
    result = await _llm.generate_image_spec(description=description, name=name,
                                            current_dockerfile=current_dockerfile,
                                            config=cfg)
    if not result.get("success"):
        raise HTTPException(500, result.get("error", "LLM-Fehler bei der Spec-Generierung"))
    return {
        "dockerfile": result["dockerfile"],
        "name": result["name"],
    }


# ── Admin: globale Image-Specs ────────────────────────────────────

@router.get("/api/admin/image-specs")
async def admin_list_specs(
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    return {"specs": image_spec_service.list_specs(session)}


@router.post("/api/admin/image-specs")
async def admin_create_spec(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    body = await request.json()
    return _save_spec(body, session, "global", user.id)


@router.post("/api/admin/image-specs/generate")
async def admin_generate_spec(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    body = await request.json()
    return await _generate_spec(session, body)


@router.put("/api/admin/image-specs/{spec_id}")
async def admin_update_spec(
    spec_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    row = _get_spec_or_404(session, spec_id)
    if row.scope != "global":
        raise HTTPException(403, "Nur globale Specs hier editierbar.")
    body = await request.json()
    name = str(body.get("name") or row.name).strip()
    try:
        row = image_spec_service.save_spec(session, row.scope, name,
                                           str(body.get("dockerfile") or ""),
                                           created_by=user.id)
    except image_spec_service.ImageSpecError as e:
        raise HTTPException(422, str(e))
    return image_spec_service.spec_info(row)


@router.delete("/api/admin/image-specs/{spec_id}")
async def admin_delete_spec(
    spec_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    row = _get_spec_or_404(session, spec_id)
    if row.scope != "global":
        raise HTTPException(403, "Nur globale Specs hier löschtbar.")
    try:
        image_spec_service.delete_spec(session, spec_id)
    except image_spec_service.ImageSpecError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


# ── Admin: Engines + Installation ─────────────────────────────────

@router.get("/api/admin/compute/engines/{name}/images")
async def admin_engine_images(
    name: str,
    course_id: Optional[int] = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    """Images einer Engine (Agent-Proxy). course_id: Engine eines Kurses."""
    agent = _agent_by_name(session, course_id, name)
    try:
        client = workspace_service.client_for(agent)
        return {
            "images": client.images_list(),
            "spec_status": _engine_spec_status(client, session, course_id),
        }
    except ComputeAgentError as e:
        raise _engine_images_error(e)


@router.delete("/api/admin/compute/engines/{name}/images")
async def admin_remove_engine_image(
    name: str,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    """Image (Tag) von einer globalen Engine löschen (Speicherplatz freigeben).

    Body: {ref, force?} — nur das konkrete Docker-Image wird entfernt,
    die Engine-Verbindung bleibt bestehen. force beendet/entfernt
    Container, die das Image nutzen (Workspace-Dateien bleiben im Volume).
    """
    body = _json_body(await request.json(), "ref")
    agent = _agent_by_name(session, None, name)
    try:
        result = workspace_service.client_for(agent).images_remove(
            str(body["ref"]).strip(), force=bool(body.get("force")))
    except ComputeAgentError as e:
        raise _engine_images_error(e)
    result.setdefault("ok", True)
    return result


@router.post("/api/admin/image-specs/{spec_id}/install")
async def admin_install_spec(
    spec_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    """Spec auf einer Engine installieren (idempotent; Build im Hintergrund).

    Body: {engine, course_id?} — ohne course_id: globale Engine-Registry.
    """
    _get_spec_or_404(session, spec_id)
    body = _json_body(await request.json(), "engine")
    agent = _agent_by_name(session, body.get("course_id"),
                           str(body["engine"]).strip())
    row = image_spec_service.get_by_id(session, spec_id)
    try:
        result = workspace_service.client_for(agent).images_install(
            {"name": row.name, "dockerfile": row.dockerfile})
    except ComputeAgentError as e:
        raise _engine_images_error(e)
    result["engine"] = agent["name"]
    return result


@router.delete("/api/admin/image-specs/{spec_id}/install")
async def admin_uninstall_spec(
    spec_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    """Image-Tag von einer Engine entfernen. Body: {engine, ref, force?, course_id?}."""
    body = _json_body(await request.json(), "engine", "ref")
    agent = _agent_by_name(session, body.get("course_id"),
                           str(body["engine"]).strip())
    try:
        result = workspace_service.client_for(agent).images_remove(
            str(body["ref"]).strip(), force=bool(body.get("force")))
    except ComputeAgentError as e:
        raise _engine_images_error(e)
    result.setdefault("ok", True)
    return result


# ── Kurs: Image-Specs (Prof) ──────────────────────────────────────

@router.get("/api/courses/{course_id}/image-specs")
async def course_list_specs(
    course_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kurs-Specs + globale Specs (Kurs zuerst).

    Read-only: PROF, TUTOR (Task-Editor: Image-Spec-Auswahl) und Admin.
    """
    _check_course_tutor(user, course_id, session)
    return {"specs": image_spec_service.list_specs(session, course_id)}


@router.post("/api/courses/{course_id}/image-specs")
async def course_create_spec(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    _check_course_prof_admin(user, course_id, session)
    body = await request.json()
    return _save_spec(body, session, str(course_id), user.id)


@router.post("/api/courses/{course_id}/image-specs/generate")
async def course_generate_spec(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    _check_course_prof_admin(user, course_id, session)
    body = await request.json()
    return await _generate_spec(session, body, course_id=course_id)


@router.put("/api/courses/{course_id}/image-specs/{spec_id}")
async def course_update_spec(
    course_id: int,
    spec_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    _check_course_prof_admin(user, course_id, session)
    row = _get_spec_or_404(session, spec_id)
    if row.scope != str(course_id):
        raise HTTPException(
            403, "Nur Kurs-Specs hier editierbar (globale Specs: Admin-Konsole).")
    body = await request.json()
    name = str(body.get("name") or row.name).strip()
    try:
        row = image_spec_service.save_spec(session, row.scope, name,
                                           str(body.get("dockerfile") or ""),
                                           created_by=user.id)
    except image_spec_service.ImageSpecError as e:
        raise HTTPException(422, str(e))
    return image_spec_service.spec_info(row)


@router.delete("/api/courses/{course_id}/image-specs/{spec_id}")
async def course_delete_spec(
    course_id: int,
    spec_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    _check_course_prof_admin(user, course_id, session)
    row = _get_spec_or_404(session, spec_id)
    if row.scope != str(course_id):
        raise HTTPException(
            403, "Nur Kurs-Specs hier löschbar (globale Specs: Admin-Konsole).")
    try:
        image_spec_service.delete_spec(session, spec_id)
    except image_spec_service.ImageSpecError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


# ── Kurs: Engines + Installation (nur KURS-Engines) ───────────────

@router.get("/api/courses/{course_id}/compute/engines/{name}/images")
async def course_engine_images(
    course_id: int,
    name: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    _check_course_tutor(user, course_id, session)
    agent = _agent_by_name(session, course_id, name)
    try:
        client = workspace_service.client_for(agent)
        return {
            "images": client.images_list(),
            "spec_status": _engine_spec_status(client, session, course_id),
        }
    except ComputeAgentError as e:
        raise _engine_images_error(e)


@router.delete("/api/courses/{course_id}/compute/engines/{name}/images")
async def course_remove_engine_image(
    course_id: int,
    name: str,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Image (Tag) von einer Engine löschen (Speicherplatz freigeben).

    Body: {ref, force?} — nur das konkrete Docker-Image wird entfernt,
    die Engine-Verbindung bleibt bestehen. force beendet/entfernt
    Container, die das Image nutzen (Workspace-Dateien bleiben im Volume).
    """
    _check_course_prof_admin(user, course_id, session)
    body = _json_body(await request.json(), "ref")
    agent = _agent_by_name(session, course_id, name)
    try:
        result = workspace_service.client_for(agent).images_remove(
            str(body["ref"]).strip(), force=bool(body.get("force")))
    except ComputeAgentError as e:
        raise _engine_images_error(e)
    result.setdefault("ok", True)
    return result


@router.post("/api/courses/{course_id}/image-specs/{spec_id}/install")
async def course_install_spec(
    course_id: int,
    spec_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Spec (Kurs oder global) auf einer KURS-Engine installieren.

    Body: {engine}
    """
    _check_course_prof_admin(user, course_id, session)
    row = _get_spec_or_404(session, spec_id)
    body = _json_body(await request.json(), "engine")
    agent = _agent_by_name(session, course_id, str(body["engine"]).strip())
    try:
        result = workspace_service.client_for(agent).images_install(
            {"name": row.name, "dockerfile": row.dockerfile})
    except ComputeAgentError as e:
        raise _engine_images_error(e)
    result["engine"] = agent["name"]
    return result


@router.delete("/api/courses/{course_id}/image-specs/{spec_id}/install")
async def course_uninstall_spec(
    course_id: int,
    spec_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Image-Tag von einer KURS-Engine entfernen. Body: {engine, ref, force?}."""
    _check_course_prof_admin(user, course_id, session)
    body = _json_body(await request.json(), "engine", "ref")
    agent = _agent_by_name(session, course_id, str(body["engine"]).strip())
    try:
        result = workspace_service.client_for(agent).images_remove(
            str(body["ref"]).strip(), force=bool(body.get("force")))
    except ComputeAgentError as e:
        raise _engine_images_error(e)
    result.setdefault("ok", True)
    return result
