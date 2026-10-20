"""
Content Versions: generische Versions-History für bearbeitbare Elemente.

Ein `ContentVersion` ist ein Snapshot ({title, content, summary, …}) eines
Elements. Über (entity_type, entity_id) ist die History elementtyp-agnostisch —
so können Skript-Kapitel, Slide-Decks, Applets und Aufgaben dieselbe
Logik + dieselben API-Endpoints wiederverwenden (Quellen: nur MCP-Edits).

Wiederverwendung über "Adapter": Jeder Elementtyp registriert drei Funktionen
(load / course_id_of / apply). `apply` setzt den Snapshot auf das Element
(Speichern inkl. Validierung + Seiteneffekten wie sync_media_usages). Damit
sind die Versions-Endpoints zugleich der Autosave-Pfad: ein einziger Request
speichert das Element UND legt/aktualisiert die aktuelle Version.

Semantik:
- create_version  → Element wird mit dem Snapshot gespeichert + neue Version
- update_version  → Element wird mit dem Snapshot gespeichert + Version geupdatet
- snapshot_current → Version des AKTUELLEN Zustands ohne Element-Änderung:
  „Erstellt“ bei Element-Anlage (mark_created), „MCP Edit“ nach MCP-Edits
- restore_version → Snapshot eines alten Eintrags wird auf das Element
  angewendet (gespeichert) und als neue (aktuelle) Version hinterlegt,
  sodass immer die neueste Version = aktueller Zustand ist.
"""

import json
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
    CourseReference,
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
from services import notifications
from services.mc_service import (
    McValidationError,
    mc_total_points,
    parse_mc_data,
)
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
    # Feld „media“ (früher „html“ — alte Dev-Snapshots ohne Migration); das
    # Format leitet replace_applet aus der Datei-Extension ab.
    content = snapshot.get("media") or snapshot.get("html") or ""
    if not content.strip():
        raise HTTPException(400, "Der Applet-Code darf nicht leer sein.")
    fmt = media_service.applet_format_of(entity.file_path)
    try:
        media_service.validate_applet_content(content, fmt)
    except HTTPException:
        # Rollback über einen Formatwechsel: der Content gehört ins andere
        # Format → Datei erst umbenennen (Referenzen reparieren), dann schreiben.
        other = "svg" if fmt == "html" else "html"
        media_service.validate_applet_content(content, other)  # wirft bei echtem Fehler
        media_service.switch_applet_format(session, entity.course_id, entity, other)
    # Gleicher Dateipfad → Markdown-Referenzen bleiben gültig.
    rel_path, mime, size = media_service.replace_applet(entity.file_path, content)
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
    tt = snapshot.get("text_template")
    entity.text_template = None if tt in (None, "") else tt
    entity.is_visible = bool(snapshot.get("is_visible", False))
    entity.hints_enabled = bool(snapshot.get("hints_enabled", True))
    # Multiple-Choice: mc_data validieren (gleiche Regeln wie
    # create/update_task). Bei Fragen überschreibt die Punkte-Summe der
    # Fragen die eingegebenen Max.-Punkte; bei anderen Typen wird
    # mc_data entsorgt.
    raw_mc = snapshot.get("mc_data")
    if task_type == TaskType.MC:
        if raw_mc is None or (isinstance(raw_mc, str) and not raw_mc.strip()):
            entity.mc_data = None
        else:
            if not isinstance(raw_mc, str):
                raise HTTPException(400, "mc_data muss ein JSON-String sein.")
            try:
                mc_parsed = parse_mc_data(raw_mc)
            except McValidationError as e:
                raise HTTPException(400, f"Multiple-Choice-Fehler: {e}")
            entity.mc_data = json.dumps(mc_parsed, ensure_ascii=False)
            entity.max_points = mc_total_points(mc_parsed)
    elif entity.mc_data:
        entity.mc_data = None
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


def _reference_load(session: Session, entity_id: int):
    return session.get(CourseReference, entity_id)


def _reference_apply(session: Session, entity: CourseReference, snapshot: dict) -> None:
    title = (snapshot.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Titel darf nicht leer sein.")
    entity.key = snapshot.get("key") or entity.key
    entity.entry_type = snapshot.get("entry_type") or "misc"
    entity.authors = snapshot.get("authors") or []
    entity.title = title
    entity.year = snapshot.get("year") or ""
    entity.venue = snapshot.get("venue") or ""
    entity.detail = snapshot.get("detail") or ""
    entity.address = snapshot.get("address") or ""
    entity.doi = snapshot.get("doi") or ""
    entity.url = snapshot.get("url") or ""
    entity.note = snapshot.get("note") or ""
    entity.description = snapshot.get("description") or ""
    session.add(entity)
    session.commit()
    session.refresh(entity)


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
    "reference": _Adapter(
        type="reference",
        load=_reference_load,
        course_id_of=lambda e: e.course_id,
        apply=_reference_apply,
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


def _current_snapshot(entity_type: str, entity) -> dict:
    """Snapshot des aktuellen Element-Zustands (gleiche Felder wie die
    jeweiligen apply-Adapter)."""
    if entity_type in ("script_section", "slide_deck"):
        return {
            "title": entity.title,
            "content": entity.content or "",
            "summary": entity.summary or "",
        }
    if entity_type == "applet":
        path = media_service.MEDIA_DIR / entity.file_path
        if not path.is_file():
            raise HTTPException(404, "Applet-Datei fehlt (Element defekt).")
        return {
            "title": entity.title,
            "media": path.read_text(encoding="utf-8"),
            "llm_description": entity.llm_description or "",
        }
    if entity_type == "reference":
        return {
            "key": entity.key,
            "entry_type": entity.entry_type or "misc",
            "authors": entity.authors or [],
            "title": entity.title or "",
            "year": entity.year or "",
            "venue": entity.venue or "",
            "detail": entity.detail or "",
            "address": entity.address or "",
            "doi": entity.doi or "",
            "url": entity.url or "",
            "note": entity.note or "",
            "description": entity.description or "",
        }
    if entity_type == "task":
        return {
            "title": entity.title,
            "task_type": entity.task_type.value,
            "description": entity.description or "",
            "model_solution": entity.model_solution or "",
            "max_points": entity.max_points,
            "max_attempts": entity.max_attempts,
            "deadline": entity.deadline or "",
            "text_template": entity.text_template or "",
            "is_visible": entity.is_visible,
            "hints_enabled": entity.hints_enabled,
            "mc_data": entity.mc_data or "",
            # Workspace-Konfig mitsichern: _task_apply setzt engines/image
            # beim Restore bedingungslos aus dem Snapshot.
            "workspace_timeout": entity.workspace_timeout,
            "workspace_cpu": entity.workspace_cpu,
            "workspace_memory": entity.workspace_memory,
            "workspace_disk_quota": entity.workspace_disk_quota,
            "workspace_internet": entity.workspace_internet,
            "workspace_main_file": entity.workspace_main_file or "",
            "workspace_engines": entity.workspace_engines,
            "workspace_image": entity.workspace_image,
        }
    raise HTTPException(400, "Zustands-Snapshots sind für diesen Elementtyp nicht unterstützt.")


def _push_live_update(session: Session, entity_type: str, entity) -> None:
    """SSE-Live-Update: Version aus dem aktuellen Zustand angelegt/gespeichert
    → alle offenen Kurs-Seiten (MCP, Browser-Editor, LLM — gleicher Pfad)
    refetchen. Defensiv: push_content_updated ist selbst try/except umhüllt."""
    course_id = getattr(entity, "course_id", None)
    if course_id is None:
        return
    notifications.push_content_updated(session, int(course_id), entity_type, int(entity.id))
    # Eingebettetes Applet → auch die Seiten, die es einbetten, updaten
    # (Skript-Kapitel / Slide-Deck / Aufgabe rendern es inline).
    # origin_type: Das Element-Markdown bleibt identisch — Empfänger müssen
    # deshalb ihren Content-Identitäts-Check bypassen (sonst stilles No-op).
    if entity_type == "applet":
        fname = (getattr(entity, "file_path", "") or "").rsplit("/", 1)[-1]
        if fname:
            for t, i in media_service.referencing_entities(session, int(course_id), fname):
                notifications.push_content_updated(session, int(course_id), t, i,
                                                   origin_type="applet", origin_media=fname)


def snapshot_current(
    session: Session,
    user: User,
    entity_type: str,
    entity_id: int,
    name: str = "",
    skip_if_same_as_latest: bool = False,
) -> Optional[dict]:
    """Version aus dem AKTUELLEN Element-Zustand anlegen (Element bleibt
    unverändert) — z. B. „MCP Edit“ nach einem MCP-Edit oder „Erstellt“
    bei der Element-Anlage. Rollback: restore_version().

    `skip_if_same_as_latest`: Wenn der Zustand exakt der letzten Version
    entspricht (z. B. MCP-Edit ohne tatsächliche Änderung), wird keine
    Version angelegt → None."""
    _adapter, entity = _load_entity_checked(session, user, entity_type, entity_id)
    snapshot = _current_snapshot(entity_type, entity)
    if skip_if_same_as_latest:
        latest = session.exec(
            select(ContentVersion)
            .where(ContentVersion.entity_type == entity_type)
            .where(ContentVersion.entity_id == entity_id)
            .order_by(ContentVersion.updated_at.desc(), ContentVersion.id.desc())  # type: ignore[attr-defined]
        ).first()
        if latest is not None and latest.snapshot == snapshot:
            logger.info(
                "snapshot_current: %s %s — keine neue Version (identisch mit v%s).",
                entity_type, entity_id, latest.id,
            )
            return None
    v = ContentVersion(
        entity_type=entity_type,
        entity_id=entity_id,
        snapshot=snapshot,
        name=(name or "").strip()[:200],
        created_by=user.id,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    _capture_task_files(session, v)
    _push_live_update(session, entity_type, entity)
    return _version_to_dict(v)


def mark_created(session: Session, user: User, entity_type: str, entity_id: int,
                 name: Optional[str] = None) -> Optional[dict]:
    """Initial-Version (Default „Erstellt“) für neu angelegte Elemente
    (Web-UI, LLM, MCP, Duplikate). Fehler dürfen die Element-Anlage nicht
    blockieren."""
    label = (name or "").strip()[:200] or "Erstellt"
    try:
        return snapshot_current(session, user, entity_type, entity_id, name=label)
    except Exception as e:  # noqa: BLE001
        logger.warning("Initial-Version '%s' fehlgeschlagen (%s %s): %s",
                       label, entity_type, entity_id, e)
        return None


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
    _push_live_update(session, entity_type, entity)
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
    _push_live_update(session, v.entity_type, entity)
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
    _push_live_update(session, v.entity_type, entity)
    return _version_to_dict(nv, include_snapshot=True)
