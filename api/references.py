"""
Kurs-Quellen (BibTeX-artige Bibliothek): CRUD + BibTeX-Import (PROF/TUTOR/Admin).

Quellen werden im Markdown-Inhalt per @cite:{key} / @citet:{key} / @citep:{key}
zitiert. Auflösung, Nummerierung (kursweit stabil = display_order) und das
Quellenverzeichnis rendert der Client-Renderer (static/js/markdown-renderer.js);
Datenquelle ist das "references"-Feld des script-refmap-Endpoints (api/script.py).
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, delete, select

from database.base import get_session
from database.models import ContentVersion, CourseReference, CourseRole, User
from services import bibtex as bib
from services import notifications
from services import version_service
from services.auth_service import require_course_access

router = APIRouter(prefix="/api", tags=["Quellen"])

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")  # @cite:{key}-Syntax erlaubt keine ':' o. ä.


class ReferenceBody(BaseModel):
    key: str = ""
    entry_type: str = "misc"
    authors: list | str = ""  # Liste ODER Trenn-String ("and" / Komma)
    title: str = ""
    year: str = ""
    venue: str = ""
    detail: str = ""
    address: str = ""
    doi: str = ""
    url: str = ""
    note: str = ""
    description: str = ""


class BibImportBody(BaseModel):
    bibtex: str


# ─── Helfer ─────────────────────────────────────────────────────────


def _next_order(session: Session, course_id: int) -> int:
    rows = session.exec(
        select(CourseReference.display_order)
        .where(CourseReference.course_id == course_id)
    ).all()
    return (max(rows) + 1) if rows else 0


def _apply_fields(ref: CourseReference, body: ReferenceBody) -> None:
    ref.key = body.key.strip()
    ref.entry_type = (body.entry_type or "misc").strip()[:50]
    ref.authors = bib.split_authors(body.authors) if isinstance(body.authors, str) else (
        [str(a).strip() for a in body.authors if str(a).strip()]
    )
    ref.title = body.title.strip()[:500]
    ref.year = body.year.strip()[:10]
    ref.venue = body.venue.strip()[:300]
    ref.detail = body.detail.strip()[:200]
    ref.address = body.address.strip()[:200]
    ref.doi = body.doi.strip()[:200]
    ref.url = body.url.strip()[:500]
    ref.note = body.note.strip()[:500]
    ref.description = (body.description or "").strip()


def _ref_to_dict(r: CourseReference) -> dict:
    return {
        "id": r.id,
        "key": r.key,
        "entry_type": r.entry_type,
        "authors": r.authors or [],
        "authors_display": " & ".join(r.authors or []),
        "title": r.title or "",
        "year": r.year or "",
        "venue": r.venue or "",
        "detail": r.detail or "",
        "address": r.address or "",
        "doi": r.doi or "",
        "url": r.url or "",
        "note": r.note or "",
        "description": r.description or "",
        "display_order": r.display_order,
        "entry": bib.format_entry(
            r.authors or [], r.title or "", r.year or "",
            r.venue or "", r.detail or "", r.doi or "", r.url or "",
        ),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _get_ref(session: Session, course_id: int, ref_id: int) -> CourseReference:
    ref = session.get(CourseReference, ref_id)
    if not ref or ref.course_id != course_id:
        raise HTTPException(404, "Quelle nicht gefunden.")
    return ref


# ─── Endpunkte ──────────────────────────────────────────────────────


@router.get("/courses/{course_id}/references")
async def list_references(
    course_id: int,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Alle Quellen des Kurses (Reihenfolge = Zitations-Nummer)."""
    refs = session.exec(
        select(CourseReference)
        .where(CourseReference.course_id == course_id)
        .order_by(CourseReference.display_order.asc())  # type: ignore[attr-defined]
        .order_by(CourseReference.id.asc())  # type: ignore[attr-defined]
    ).all()
    return [_ref_to_dict(r) for r in refs]


@router.post("/courses/{course_id}/references")
async def create_reference(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Neue Quelle anlegen (manuell)."""
    user, _ = user_and_course
    body = ReferenceBody(**(await request.json()))
    key = (body.key or "").strip()
    if not _KEY_RE.fullmatch(key):
        raise HTTPException(
            400, "Ungültiger Quellen-Schlüssel (erlaubt: Buchstaben, Ziffern, _ und -)."
        )
    exists = session.exec(
        select(CourseReference)
        .where(CourseReference.course_id == course_id)
        .where(CourseReference.key == key)
    ).first()
    if exists:
        raise HTTPException(409, f"Der Schlüssel '{key}' existiert bereits in diesem Kurs.")

    ref = CourseReference(
        course_id=course_id, created_by=user.id,  # type: ignore[arg-type]
        display_order=_next_order(session, course_id),
    )
    _apply_fields(ref, body)
    session.add(ref)
    session.commit()
    session.refresh(ref)
    version_service.mark_created(session, user, "reference", ref.id)
    return _ref_to_dict(ref)


@router.put("/courses/{course_id}/references/{ref_id}")
async def update_reference(
    course_id: int,
    ref_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Quelle bearbeiten (alle Felder, inkl. Schlüssel)."""
    ref = _get_ref(session, course_id, ref_id)
    body = ReferenceBody(**(await request.json()))
    new_key = (body.key or "").strip()
    if not _KEY_RE.fullmatch(new_key):
        raise HTTPException(
            400, "Ungültiger Quellen-Schlüssel (erlaubt: Buchstaben, Ziffern, _ und -)."
        )
    if new_key != ref.key:
        clash = session.exec(
            select(CourseReference)
            .where(CourseReference.course_id == course_id)
            .where(CourseReference.key == new_key)
        ).first()
        if clash:
            raise HTTPException(409, f"Der Schlüssel '{new_key}' existiert bereits in diesem Kurs.")
    _apply_fields(ref, body)
    session.add(ref)
    session.commit()
    session.refresh(ref)
    # Live-Update: offene Skript-/Slides-Previews aktualisieren die Refmap
    # (Zitationen lösen sonst erst nach Reload auf).
    notifications.push_content_updated(session, course_id, "reference", ref_id)
    return _ref_to_dict(ref)


@router.delete("/courses/{course_id}/references/{ref_id}")
async def delete_reference(
    course_id: int,
    ref_id: int,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Quelle löschen (existierende @cite:-Referenzen im Inhalt zeigen ❓)."""
    ref = _get_ref(session, course_id, ref_id)
    key = ref.key
    # Versions-History der Quelle mit aufräumen
    session.exec(delete(ContentVersion)
                 .where(ContentVersion.entity_type == "reference")
                 .where(ContentVersion.entity_id == ref_id))
    session.delete(ref)
    session.commit()
    # Live-Update: Zitationen in offenen Previews zeigen jetzt ❓.
    notifications.push_content_updated(session, course_id, "reference", ref_id)
    return {"message": f"Quelle '{key}' gelöscht."}


@router.patch("/courses/{course_id}/references/reorder")
async def reorder_references(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Reihenfolge der Quellen ändern (nur PROF/TUTOR).

    ACHTUNG: Die Zitations-Nummern = Position in der Reihenfolge —
    ein Verschieben ändert damit die Nummern aller Quellen.
    """
    body = await request.json()
    ref_ids = body.get("reference_ids", [])
    if not ref_ids:
        return {"message": "Keine Quellen zum Verschieben angegeben."}
    for rid in ref_ids:
        ref = session.get(CourseReference, rid)
        if not ref or ref.course_id != course_id:
            raise HTTPException(400, f"Quelle {rid} gehört nicht zu diesem Kurs.")
    for idx, rid in enumerate(ref_ids):
        ref = session.get(CourseReference, rid)
        if ref:
            ref.display_order = idx
            session.add(ref)
    session.commit()
    # Live-Update: Nummern = Position → alle Zitationen haben sich geändert.
    # entity_id wird vom Client ignoriert (nur Trigger).
    notifications.push_content_updated(session, course_id, "reference", int(ref_ids[0]))
    return {"message": "Quellen-Reihenfolge aktualisiert."}


@router.post("/courses/{course_id}/references/import-bibtex")
async def import_bibtex(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Eingefügtes BibTeX einlesen; vorhandene Schlüssel werden übersprungen."""
    user, _ = user_and_course
    body = BibImportBody(**(await request.json()))
    entries = bib.parse_bibtex(body.bibtex or "")
    imported = 0
    skipped = 0
    errors: list[str] = []
    created_refs: list[CourseReference] = []
    order = _next_order(session, course_id)
    for e in entries:
        key = e["key"]
        if not _KEY_RE.fullmatch(key):
            errors.append(
                f"'{key}': ungültiger Schlüssel (erlaubt: Buchstaben, Ziffern, _ und -) — übersprungen."
            )
            continue
        exists = session.exec(
            select(CourseReference)
            .where(CourseReference.course_id == course_id)
            .where(CourseReference.key == key)
        ).first()
        if exists:
            skipped += 1
            continue
        data = bib.entry_to_reference(e)
        ref = CourseReference(
            course_id=course_id, created_by=user.id, key=key,  # type: ignore[arg-type]
            display_order=order, **data,
        )
        session.add(ref)
        created_refs.append(ref)
        order += 1
        imported += 1
    session.commit()
    for r in created_refs:
        version_service.mark_created(session, user, "reference", r.id)
    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "message": f"{imported} Quellen importiert"
                   + (f", {skipped} übersprungen (Key existiert bereits)" if skipped else "")
                   + ".",
    }
