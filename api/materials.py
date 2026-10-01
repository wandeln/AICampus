"""
Kurs-Material: Vorlesungsskript & Slides (Markdown).

Regeln:
- Skript: existiert als Kapitel (course_script_sections); ein legacy
  script-Material bleibt pro Kurs einzig.
- Slides: mehrere Slide-Decks pro Kurs erlaubt (Reihenfolge: display_order).
- Studenten sehen nur sichtbare Materialien (is_visible=True).
- Anlegen/Bearbeiten: PROF/TUTOR; Löschen: PROF. Admin darf alles.
- Slides = Markdown-Deck, Folien durch eine Zeile `---` getrennt
  (Format & Validierung: services.slides_service).
- Nach jeder Content-Änderung: sync_media_usages() (Medien-Einbindung).
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, delete, func, select

from database.base import get_session
from database.models import (
    ContentVersion,
    CourseMaterial,
    CourseRole,
    GlobalUserRole,
    MaterialType,
    User,
    UserCourse,
)
from services.auth_service import get_current_user, require_course_access
from services.media_service import sync_media_usages
from services.slides_service import SlideError, parse_slides, slide_count, strip_slide_notes

router = APIRouter(prefix="/api", tags=["Skript & Slides"])

LABELS = {MaterialType.SCRIPT: "Skript", MaterialType.SLIDES: "Slides"}


def _get_membership(session: Session, user: User, course_id: int) -> Optional[UserCourse]:
    if user.role == GlobalUserRole.ADMIN:
        return None  # Admin braucht keine Membership
    return session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()


def _check_member(user: User, session: Session, course_id: int) -> None:
    """Jedes Kurs-Mitglied (oder Admin) darf lesen."""
    if user.role == GlobalUserRole.ADMIN:
        return
    if not _get_membership(session, user, course_id):
        raise HTTPException(403, "Du bist kein Mitglied dieses Kurses.")


def _is_tutor(user: User, session: Session, course_id: int) -> bool:
    if user.role == GlobalUserRole.ADMIN:
        return True
    m = _get_membership(session, user, course_id)
    return bool(m and m.role_in_course in (CourseRole.PROF, CourseRole.TUTOR))


def _get_material(session: Session, course_id: int, material_id: int) -> CourseMaterial:
    material = session.get(CourseMaterial, material_id)
    if not material or material.course_id != course_id:
        raise HTTPException(404, "Material nicht gefunden.")
    return material


def _material_to_dict(m: CourseMaterial, include_content: bool = False) -> dict:
    d = {
        "id": m.id,
        "title": m.title,
        "material_type": m.material_type.value,
        "is_visible": m.is_visible,
        "display_order": m.display_order,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }
    if m.material_type == MaterialType.SLIDES:
        d["slide_count"] = slide_count(m.content)
        d["summary"] = m.summary  # Interne LLM-Zusammenfassung (nicht für Studenten)
    if include_content:
        d["content"] = m.content
    return d


# ─── Lesen (alle Mitglieder) ─────────────────────────────────────

@router.get("/courses/{course_id}/materials")
async def list_materials(
    course_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Materialien des Kurses (Studenten nur sichtbare)."""
    _check_member(user, session, course_id)
    is_tutor = _is_tutor(user, session, course_id)

    materials = session.exec(
        select(CourseMaterial)
        .where(CourseMaterial.course_id == course_id)
        .order_by(CourseMaterial.display_order.asc(), CourseMaterial.id.asc())  # type: ignore[attr-defined]
    ).all()
    return [
        _material_to_dict(m)
        for m in materials
        if is_tutor or m.is_visible
    ]


@router.get("/courses/{course_id}/materials/{material_id}")
async def get_material(
    course_id: int,
    material_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Einzelnes Material inkl. Markdown-Content."""
    _check_member(user, session, course_id)
    material = _get_material(session, course_id, material_id)

    is_tutor = _is_tutor(user, session, course_id)
    if not material.is_visible and not is_tutor:
        raise HTTPException(404, "Material nicht gefunden.")

    d = _material_to_dict(material, include_content=True)
    if material.material_type == MaterialType.SLIDES and not is_tutor:
        # Sprechernotizen nicht an Nicht-Tutoren ausliefern → landen auch
        # nicht im DOM der Präsentation/Vorschau.
        d["content"] = strip_slide_notes(material.content)
    return d


# ─── Schreiben (PROF/TUTOR) ──────────────────────────────────────

@router.post("/courses/{course_id}/materials")
async def create_material(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Skript oder Slide-Deck anlegen (mehrere Decks pro Kurs erlaubt)."""
    user, _ = user_and_course
    body = await request.json()

    try:
        mtype = MaterialType(body.get("material_type"))
    except ValueError:
        raise HTTPException(400, "material_type muss 'script' oder 'slides' sein.")

    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Titel darf nicht leer sein.")

    content = body.get("content") or ""
    if mtype == MaterialType.SLIDES:
        try:
            parse_slides(content)
        except SlideError as e:
            raise HTTPException(400, str(e))

    if mtype == MaterialType.SCRIPT:
        existing = session.exec(
            select(CourseMaterial)
            .where(CourseMaterial.course_id == course_id)
            .where(CourseMaterial.material_type == mtype)
        ).first()
        if existing:
            raise HTTPException(409, f"Es existiert bereits ein {LABELS[mtype]} für diesen Kurs.")
        display_order = 0
    else:
        # Slide-Decks: an die nächste freie Position anhängen
        last = session.exec(
            select(func.max(CourseMaterial.display_order))
            .where(CourseMaterial.course_id == course_id)
            .where(CourseMaterial.material_type == MaterialType.SLIDES)
        ).one()
        display_order = (last or 0) + 1

    material = CourseMaterial(
        course_id=course_id,
        title=title,
        material_type=mtype,
        content=content,
        is_visible=bool(body.get("is_visible", True)),
        display_order=display_order,
        created_by=user.id,  # type: ignore[arg-type]
    )
    session.add(material)
    session.commit()
    session.refresh(material)
    sync_media_usages(session, course_id)

    return {"message": f"{LABELS[mtype]} '{material.title}' erstellt.", "material": _material_to_dict(material)}


@router.put("/courses/{course_id}/materials/{material_id}")
async def update_material(
    course_id: int,
    material_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Skript/Slides aktualisieren (Titel, Content, Sichtbarkeit, Summary)."""
    material = _get_material(session, course_id, material_id)
    body = await request.json()

    if "title" in body:
        title = (body.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "Titel darf nicht leer sein.")
        material.title = title
    if "content" in body:
        content = body.get("content") or ""
        if material.material_type == MaterialType.SLIDES:
            try:
                parse_slides(content)
            except SlideError as e:
                raise HTTPException(400, str(e))
        material.content = content
    if "is_visible" in body:
        material.is_visible = bool(body["is_visible"])
    if "display_order" in body and isinstance(body["display_order"], int):
        material.display_order = body["display_order"]
    if "summary" in body and isinstance(body["summary"], str):
        material.summary = body["summary"][:2000]

    material.updated_at = datetime.now(timezone.utc)
    session.add(material)
    session.commit()
    session.refresh(material)
    sync_media_usages(session, course_id)

    return {"message": f"{LABELS[material.material_type]} aktualisiert.", "material": _material_to_dict(material)}


@router.delete("/courses/{course_id}/materials/{material_id}")
async def delete_material(
    course_id: int,
    material_id: int,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Skript/Slides löschen (nur PROF)."""
    material = _get_material(session, course_id, material_id)
    label = LABELS[material.material_type]
    title = material.title

    # Slide-Decks: Versions-History mit löschen (Skripte nutzen Kapitel, nicht Material)
    if material.material_type == MaterialType.SLIDES:
        session.exec(
            delete(ContentVersion)
            .where(ContentVersion.entity_type == "slide_deck")
            .where(ContentVersion.entity_id == material_id)
        )

    session.delete(material)
    session.commit()
    sync_media_usages(session, course_id)

    return {"message": f"{label} '{title}' gelöscht."}


@router.patch("/courses/{course_id}/materials/reorder")
async def reorder_materials(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Reihenfolge der Slide-Decks eines Kurses ändern (nur PROF/TUTOR)."""
    body = await request.json()
    material_ids = body.get("material_ids")
    if not isinstance(material_ids, list) or not material_ids:
        raise HTTPException(400, "material_ids muss eine nicht-leere Liste sein.")

    decks = session.exec(
        select(CourseMaterial)
        .where(CourseMaterial.course_id == course_id)
        .where(CourseMaterial.material_type == MaterialType.SLIDES)
    ).all()
    by_id = {m.id: m for m in decks}

    for idx, mid in enumerate(material_ids):
        m = by_id.get(mid)
        if not m:
            raise HTTPException(404, f"Slide-Deck {mid} nicht gefunden.")
        m.display_order = idx
        session.add(m)
    session.commit()

    return {"message": "Deck-Reihenfolge aktualisiert."}
