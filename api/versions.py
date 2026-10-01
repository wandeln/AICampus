"""
Versions-History API (generisch für alle Elementtypen).

Elementtypen: entity_type + entity_id (aktuell "script_section", "slide_deck";
später u. a. Applets/Aufgaben — Adapter in services/version_service.py).

Die Endpoints sind zugleich der Autosave-Pfad: create/update wenden den
Snapshot auf das Element an (Element wird gespeichert) UND legen bzw.
aktualisieren die Version an. → 1 Request pro Autosave.

- GET    /api/versions/{entity_type}/{entity_id}   Liste (neueste zuerst)
- POST   /api/versions/{entity_type}/{entity_id}   neue Version (Session-Start)
- PUT    /api/versions/{version_id}                Snapshot-Update (Autosave)
- PATCH  /api/versions/{version_id}/name           umbenennen
- DELETE /api/versions/{version_id}                löschen
- POST   /api/versions/{version_id}/restore        wiederherstellen

Nur PROF/TUTOR/Admin des jeweiligen Element-Kurses.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session

from database.base import get_session
from database.models import User
from services.auth_service import get_current_user
from services import version_service

router = APIRouter(prefix="/api", tags=["Versions-History"])


class SnapshotBody(BaseModel):
    snapshot: dict = {}


class NameBody(BaseModel):
    name: str = ""


def _entity_or_404(entity_type: str, entity_id: int) -> None:
    if entity_type not in version_service.ADAPTERS:
        raise HTTPException(400, "Unbekannter Elementtyp.")


# ─── Lesen ────────────────────────────────────────────────────────

@router.get("/versions/{entity_type}/{entity_id}")
async def list_versions(
    entity_type: str,
    entity_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Alle Versionen eines Elements (neueste zuerst)."""
    _entity_or_404(entity_type, entity_id)
    return {"versions": version_service.list_versions(session, user, entity_type, entity_id)}


# ─── Verwaltung ───────────────────────────────────────────────────
# WICHTIG: /versions/{version_id}/restore MUSS vor der generischen
# POST /versions/{entity_type}/{entity_id} registriert sein, sonst
# matched Starlette den Pfad zuerst auf {entity_type}/{entity_id}
# (entity_id="restore" ist kein int → 422).

@router.post("/versions/{version_id}/restore")
async def restore_version(
    version_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Version wiederherstellen (Element wird auf den Snapshot gesetzt;
    der Restore wird als neue aktuelle Version hinterlegt)."""
    return {"version": version_service.restore_version(session, user, version_id)}


# ─── Autosave (Element speichern + Version) ───────────────────────

@router.post("/versions/{entity_type}/{entity_id}")
async def create_version(
    entity_type: str,
    entity_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Neue Version anlegen (Bearbeitungs-Session startet); Element wird
    mit dem Snapshot gespeichert."""
    _entity_or_404(entity_type, entity_id)
    body = await request.json()
    snapshot = body.get("snapshot") or {}
    return {"version": version_service.create_version(session, user, entity_type, entity_id, snapshot)}


@router.put("/versions/{version_id}")
async def update_version(
    version_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Aktuelle Version aktualisieren (Autosave); Element wird mit dem
    Snapshot gespeichert."""
    body = await request.json()
    snapshot = body.get("snapshot") or {}
    return {"version": version_service.update_version(session, user, version_id, snapshot)}


# ─── Verwaltung ───────────────────────────────────────────────────

@router.patch("/versions/{version_id}/name")
async def rename_version(
    version_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Version umbenennen (leerer Name → zurück zum Zeitstempel-Default)."""
    body = await request.json()
    return {"version": version_service.rename_version(session, user, version_id, body.get("name") or "")}


@router.delete("/versions/{version_id}")
async def delete_version(
    version_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    return version_service.delete_version(session, user, version_id)
