"""
Content Versions: generische Versions-History für bearbeitbare Elemente.

Ein `ContentVersion` ist ein Snapshot ({title, content, summary, …}) eines
Elements. Über (entity_type, entity_id) ist die History elementtyp-agnostisch —
so können Skript-Kapitel, Slide-Decks, Applets und Aufgaben dieselbe
Logik + dieselben API-Endpoints wiederverwenden.

Wiederverwendung über "Adapter": Jeder Elementtyp registriert drei Funktionen
(load / course_id_of / apply). `apply` setzt den Snapshot auf das Element
(Speichern inkl. Validierung + Seiteneffekten wie sync_media_usages). Damit
sind die Versions-Endpoints zugleich der Autosave-Pfad: ein einziger Request
speichert das Element UND legt/aktualisiert die aktuelle Version.

Semantik:
- create_version  → Element wird mit dem Snapshot gespeichert + neue Version
- update_version  → Element wird mit dem Snapshot gespeichert + Version geupdatet
- restore_version → Snapshot eines alten Eintrags wird auf das Element
  angewendet (gespeichert) und als neue (aktuelle) Version hinterlegt,
  sodass immer die neueste Version = aktueller Zustand ist.
"""

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

from fastapi import HTTPException
from sqlmodel import Session, select

from config import VERSION_SNAPSHOT_DIR

from database.models import (
    ContentVersion,
    CourseMaterial,
    CourseMedia,
    CourseRole,
    GlobalUserRole,
    MaterialType,
    ScriptSection,
    Task,
    TaskType,
    User,
    UserCourse,
)
from services import media_service
from services.media_service import sync_media_usages
from services.slides_service import SlideError, parse_slides
from services.workspace_service import (
    apply_workspace_env_fields,
    check_image_spec,
    parse_engine_list,
    schedule_task_sync,
    validate_workspace_provided,
    workspace_service,
)


# ─── Berechtigung ─────────────────────────────────────────────────
def _require_course_tutor(user: User, session: Session, course_id: int) -> None:
    """PROF/TUTOR-Check (Admin darf immer) für das Element-Kurs."""
    if user.role == GlobalUserRole.ADMIN:
        return
    m = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()
    if not m or m.role_in_course not in (CourseRole.PROF, CourseRole.TUTOR):
        raise HTTPException(403, "Keine Berechtigung, dieses Element zu bearbeiten.")


# ─── Entity-Adapter ───────────────────────────────────────────────
@dataclass
class _Adapter:
    type: str
    load: Callable[[Session, int], Optional[object]]       # Element oder None
    course_id_of: Callable[[object], int]                   # Kurs des Elements
    apply: Callable[[Session, object, dict], None]          # Snapshot → Element


def _script_load(session: Session, entity_id: int):
    return session.get(ScriptSection, entity_id)


def _script_apply(session: Session, entity: ScriptSection, snapshot: dict) -> None:
    title = (snapshot.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Titel darf nicht leer sein.")
    entity.title = title
    entity.content = snapshot.get("content") or ""
    entity.summary = snapshot.get("summary") or ""
    entity.updated_at = datetime.now(timezone.utc)
    session.add(entity)
    session.commit()
    session.refresh(entity)
    sync_media_usages(session, entity.course_id)


def _slide_load(session: Session, entity_id: int):
    m = session.get(CourseMaterial, entity_id)
    if not m or m.material_type != MaterialType.SLIDES:
        return None
    return m


def _slide_apply(session: Session, entity: CourseMaterial, snapshot: dict) -> None:
    title = (snapshot.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Titel darf nicht leer sein.")
    content = snapshot.get("content") or ""
    try:
        parse_slides(content)
    except SlideError as e:
        raise HTTPException(400, str(e))
    entity.title = title
    entity.content = content
    entity.summary = snapshot.get("summary") or ""
    entity.updated_at = datetime.now(timezone.utc)
    session.add(entity)
    session.commit()
    session.refresh(entity)
    sync_media_usages(session, entity.course_id)


def _applet_load(session: Session, entity_id: int):
    m = session.get(CourseMedia, entity_id)
    if not m or m.media_type != "applet":
        return None
    return m


def _applet_apply(session: Session, entity: CourseMedia, snapshot: dict) -> None:
    title = (snapshot.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Titel darf nicht leer sein.")
    html = snapshot.get("html") or ""
    if not html.strip():
        raise HTTPException(400, "Der HTML-Code darf nicht leer sein.")
    # Gleicher Dateipfad → Markdown-Referenzen bleiben gültig.
    rel_path, mime, size = media_service.replace_applet(entity.file_path, html)
    entity.title = title
    entity.file_path = rel_path
    entity.mime_type = mime
    entity.file_size = size
    entity.llm_description = (snapshot.get("llm_description") or "").strip() or None
    session.add(entity)
    session.commit()
    session.refresh(entity)
    sync_media_usages(session, entity.course_id)


def _task_load(session: Session, entity_id: int):
    return session.get(Task, entity_id)


def _task_apply(session: Session, entity: Task, snapshot: dict) -> None:
    """Versions-Snapshot auf die Aufgabe anwenden (gleiche Validierung wie
    create/update_task). Der Agent-Sync wird dabei automatisch mit
    angestoßen — aber nur, wenn die Aufgabe lauffähig ist
    (Image-Spec + Engine-Pool gesetzt)."""
    title = (snapshot.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Titel darf nicht leer sein.")
    entity.title = title
    try:
        task_type = TaskType(snapshot.get("task_type") or "text")
    except ValueError:
        raise HTTPException(400, f"Ungültiger Aufgabentyp: {snapshot.get('task_type')!r}")
    entity.task_type = task_type
    entity.description = snapshot.get("description") or ""
    ms = snapshot.get("model_solution")
    entity.model_solution = None if ms in (None, "") else ms
    try:
        mp = int(snapshot.get("max_points"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Max. Punkte muss eine Zahl sein.")
    if mp < 0:
        raise HTTPException(400, "Max. Punkte muss mindestens 0 sein.")
    entity.max_points = mp
    ma = snapshot.get("max_attempts")
    entity.max_attempts = None if ma in (None, "") else int(ma)
    dl = snapshot.get("deadline")
    entity.deadline = None if dl in (None, "") else dl
    ct = snapshot.get("code_template")
    entity.code_template = None if ct in (None, "") else ct
    entity.test_code = snapshot.get("test_code")
    entity.is_visible = bool(snapshot.get("is_visible", False))
    entity.hints_enabled = bool(snapshot.get("hints_enabled", True))
    if task_type == TaskType.WORKSPACE:
        apply_workspace_env_fields(entity, snapshot)
        entity.workspace_engines = parse_engine_list(snapshot.get("workspace_engines"))
        entity.workspace_image = check_image_spec(
            session, entity.course_id, snapshot.get("workspace_image"))
    validate_workspace_provided(session, entity)
    entity.updated_at = datetime.now(timezone.utc)
    session.add(entity)
    session.commit()
    session.refresh(entity)
    sync_media_usages(session, entity.course_id)
    if task_type == TaskType.WORKSPACE:
        # Selbstheilung: Stub-Skripte direkt anlegen, wenn der Datei-Baum
        # leer ist (z. B. per Typ-Wechsel gerade erst Workspace geworden) —
        # unabhängig vom lauffähigen Zustand (Image/Engine), damit der Tutor
        # die Skripte sofort sieht.
        try:
            workspace_service.ensure_system_stubs(session, entity)
        except Exception as e:  # noqa: BLE001 — Stubs dürfen den Save nicht blockieren
            logger.warning("System-Stubs (task %s): %s", entity.id, e)
        if workspace_service.validate_task_ready(session, entity) is None:
            schedule_task_sync(entity.id)


ADAPTERS: dict[str, _Adapter] = {
    "script_section": _Adapter(
        type="script_section",
        load=_script_load,
        course_id_of=lambda e: e.course_id,
        apply=_script_apply,
    ),
    "slide_deck": _Adapter(
        type="slide_deck",
        load=_slide_load,
        course_id_of=lambda e: e.course_id,
        apply=_slide_apply,
    ),
    "applet": _Adapter(
        type="applet",
        load=_applet_load,
        course_id_of=lambda e: e.course_id,
        apply=_applet_apply,
    ),
    "task": _Adapter(
        type="task",
        load=_task_load,
        course_id_of=lambda e: e.course_id,
        apply=_task_apply,
    ),
}


# ─── Hilfsfunktionen ──────────────────────────────────────────────
def _get_adapter(entity_type: str) -> _Adapter:
    a = ADAPTERS.get(entity_type)
    if not a:
        raise HTTPException(400, "Unbekannter Elementtyp.")
    return a


def _version_to_dict(v: ContentVersion, include_snapshot: bool = False) -> dict:
    d = {
        "id": v.id,
        "name": v.name,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "updated_at": v.updated_at.isoformat() if v.updated_at else None,
    }
    if include_snapshot:
        d["snapshot"] = v.snapshot
    return d


def _load_entity_checked(session: Session, user: User, entity_type: str, entity_id: int):
    """Element laden + Tutor-Berechtigung prüfen. → (adapter, entity)."""
    adapter = _get_adapter(entity_type)
    entity = adapter.load(session, entity_id)
    if entity is None:
        raise HTTPException(404, "Element nicht gefunden.")
    _require_course_tutor(user, session, adapter.course_id_of(entity))
    return adapter, entity


def _get_version_checked(session: Session, user: User, version_id: int):
    """Version laden + über das Element die Tutor-Berechtigung prüfen.
    → (version, adapter, entity)."""
    v = session.get(ContentVersion, version_id)
    if not v:
        raise HTTPException(404, "Version nicht gefunden.")
    adapter, entity = _load_entity_checked(session, user, v.entity_type, v.entity_id)
    return v, adapter, entity


def _capture_task_files(session: Session, v: ContentVersion) -> None:
    """Workspace-Dateibau in Task-Versionen aufnehmen (Version B):
    bei Workspace-Aufgaben tar + Meta-JSON sichern, bei anderen
    Aufgabentypen die Referenz veralten (None). Auto-Save feuert bei
    jeder Tipp-Pause — der Digest-Check in capture_version_snapshot
    überspringt das Rewrite, wenn sich nichts geändert hat.

    Fehler dürfen den Auto-Save nicht blockieren (Feld-Snapshot bleibt
    unabhängig davon zuverlässig erhalten).
    """
    if v.entity_type != "task":
        return
    task = session.get(Task, v.entity_id)
    if task is None:
        return
    try:
        new_rel = workspace_service.capture_version_snapshot(
            session, task, v.id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Version-Dateibau (task %s, version %s): %s",
                       task.id, v.id, e)
        return
    if v.file_snapshot and not new_rel:
        # Aufgabentyp ≠ Workspace: verwaister Snapshot-Ordner aufräumen
        shutil.rmtree(VERSION_SNAPSHOT_DIR / str(v.id), ignore_errors=True)
    v.file_snapshot = new_rel
    session.add(v)
    session.commit()
    session.refresh(v)


# ─── Operationen ──────────────────────────────────────────────────
def list_versions(session: Session, user: User, entity_type: str, entity_id: int) -> list[dict]:
    _load_entity_checked(session, user, entity_type, entity_id)  # nur Berechtigungs-Check
    rows = session.exec(
        select(ContentVersion)
        .where(ContentVersion.entity_type == entity_type)
        .where(ContentVersion.entity_id == entity_id)
        .order_by(ContentVersion.updated_at.desc(), ContentVersion.id.desc())  # type: ignore[attr-defined]
    ).all()
    # Autor je Version (created_by) — einmalige Batch-Lookup der Namen.
    user_ids = {v.created_by for v in rows if v.created_by}
    authors = ({
        u.id: (u.name or u.username)
        for u in session.exec(select(User).where(User.id.in_(user_ids))).all()  # type: ignore[attr-defined]
    } if user_ids else {})
    out = []
    for v in rows:
        d = _version_to_dict(v)
        d["author"] = authors.get(v.created_by)
        out.append(d)
    return out


def create_version(session: Session, user: User, entity_type: str, entity_id: int, snapshot: dict) -> dict:
    """Element mit Snapshot speichern + neue Version anlegen (Autosave-Start)."""
    adapter, entity = _load_entity_checked(session, user, entity_type, entity_id)
    adapter.apply(session, entity, snapshot)
    v = ContentVersion(
        entity_type=entity_type,
        entity_id=entity_id,
        snapshot=dict(snapshot or {}),
        created_by=user.id,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    _capture_task_files(session, v)
    return _version_to_dict(v)


def update_version(session: Session, user: User, version_id: int, snapshot: dict) -> dict:
    """Element mit Snapshot speichern + aktuelle Version aktualisieren (Autosave)."""
    v, adapter, entity = _get_version_checked(session, user, version_id)
    adapter.apply(session, entity, snapshot)
    v.snapshot = dict(snapshot or {})
    v.updated_at = datetime.now(timezone.utc)
    session.add(v)
    session.commit()
    session.refresh(v)
    _capture_task_files(session, v)
    return _version_to_dict(v)


def rename_version(session: Session, user: User, version_id: int, name: str) -> dict:
    v, _, _ = _get_version_checked(session, user, version_id)
    v.name = (name or "").strip()[:200]
    session.add(v)
    session.commit()
    session.refresh(v)
    return _version_to_dict(v)


def delete_version(session: Session, user: User, version_id: int) -> dict:
    v, _, _ = _get_version_checked(session, user, version_id)
    session.delete(v)
    session.commit()
    if v.file_snapshot:
        shutil.rmtree(VERSION_SNAPSHOT_DIR / str(v.id), ignore_errors=True)
    return {"message": "Version gelöscht."}


def restore_version(session: Session, user: User, version_id: int) -> dict:
    """Alten Snapshot wiederherstellen: auf das Element anwenden (speichern)
    und als neue (aktuelle) Version hinterlegen. Liefert die neue Version
    inkl. Snapshot, damit der Editor clientseitig aktualisiert werden kann."""
    v, adapter, entity = _get_version_checked(session, user, version_id)
    snapshot = dict(v.snapshot or {})
    # Workspace-Dateibau ZUERST wiederherstellen (vor den Feldern, damit
    # ein fehlgeschlagener Restore keinen Mixed-State hinterlässt — die
    # DB-Rows rollen ohne Commit automatisch zurück, der Disk-Bau über
    # das Backup).
    restored_files = False
    backup = None
    if v.file_snapshot and v.entity_type == "task":
        try:
            backup = workspace_service.restore_version_snapshot(
                session, entity, v.file_snapshot)
            restored_files = True
            adapter.apply(session, entity, snapshot)
            workspace_service.discard_version_snapshot_backup(backup)
        except Exception:
            workspace_service.rollback_version_snapshot_backup(entity.id, backup)
            raise
    else:
        adapter.apply(session, entity, snapshot)
    nv = ContentVersion(
        entity_type=v.entity_type,
        entity_id=v.entity_id,
        snapshot=snapshot,
        created_by=user.id,
    )
    session.add(nv)
    session.commit()
    session.refresh(nv)
    # Die neue (aktuelle) Version trägt denselben Datei-Stand — als
    # eigene Kopie (einfache Delete-Semantik).
    if restored_files:
        try:
            nv.file_snapshot = workspace_service.copy_version_snapshot(v.id, nv.id)
            session.add(nv)
            session.commit()
            session.refresh(nv)
        except Exception as e:  # noqa: BLE001
            logger.warning("Version-Snapshot-Kopie (version %s→%s): %s",
                           v.id, nv.id, e)
    return _version_to_dict(nv, include_snapshot=True)
