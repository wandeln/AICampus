"""
Content Versions: generische Versions-History für bearbeitbare Elemente.

Ein `ContentVersion` ist ein Snapshot ({title, content, summary, …}) eines
Elements. Über (entity_type, entity_id) ist die History elementtyp-agnostisch —
so können Skript-Kapitel, Slide-Decks und (später) Applets/Aufgaben dieselbe
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

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from database.models import (
    ContentVersion,
    CourseMaterial,
    CourseRole,
    GlobalUserRole,
    MaterialType,
    ScriptSection,
    User,
    UserCourse,
)
from services.media_service import sync_media_usages
from services.slides_service import SlideError, parse_slides


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


# ─── Operationen ──────────────────────────────────────────────────
def list_versions(session: Session, user: User, entity_type: str, entity_id: int) -> list[dict]:
    _load_entity_checked(session, user, entity_type, entity_id)  # nur Berechtigungs-Check
    rows = session.exec(
        select(ContentVersion)
        .where(ContentVersion.entity_type == entity_type)
        .where(ContentVersion.entity_id == entity_id)
        .order_by(ContentVersion.updated_at.desc(), ContentVersion.id.desc())  # type: ignore[attr-defined]
    ).all()
    return [_version_to_dict(v) for v in rows]


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
    return {"message": "Version gelöscht."}


def restore_version(session: Session, user: User, version_id: int) -> dict:
    """Alten Snapshot wiederherstellen: auf das Element anwenden (speichern)
    und als neue (aktuelle) Version hinterlegen. Liefert die neue Version
    inkl. Snapshot, damit der Editor clientseitig aktualisiert werden kann."""
    v, adapter, entity = _get_version_checked(session, user, version_id)
    snapshot = dict(v.snapshot or {})
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
    return _version_to_dict(nv, include_snapshot=True)
