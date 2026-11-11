"""
User-Settings: Name / Passwort ändern, Profilbild verwalten, Account löschen.

- PATCH  /api/auth/settings      → Eigene Einstellungen aktualisieren
- POST   /api/auth/settings/avatar → Profilbild hochladen/ersetzen
- POST   /api/auth/settings/avatar/edit → Profilbild per Qwen-Image-Edit bearbeiten
- DELETE /api/auth/settings/avatar → Profilbild entfernen
- DELETE /api/auth/settings/account → Eigene Account löschen (Username-Bestätigung)
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from config import BASE_DIR
from database.base import get_session
from database.models import AvatarHistoryEntry, GlobalUserRole, User
from services import image_service, media_service, user_service
from services.auth_service import (
    get_current_user,
    hash_password,
    verify_password,
)
from services.settings_resolver import get_effective_llm_config
from services.timeutil import utc_iso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/settings", tags=["User-Settings"])

# Ergebnis-Größe der Avatar-Bearbeitung (wird klein angezeigt — 512 px reichen)
AVATAR_EDIT_SIZE = "512x512"

# Feste Preset-Anweisung für den Button "AICampus-Stil anwenden":
# eigener Kopf + Look des Standard-Avatars (blauer 3D-Studio-Hintergrund,
# Studio-Licht) + Pixar-Film-Style.
AVATAR_CAMPUS_STYLE_PROMPT = (
    "Stylisiere den Kopf auf dem Bild im Stil eines Pixar-Film-3D-Charakters: "
    "stylisiert, kinoreif, weiche glänzende Materialien. Gesichtszüge und "
    "Wiedererkennung des Menschen bleiben klar erhalten. Hintergrund und "
    "Beleuchtung wie bei einem sauberen 3D-Produktrender: glatter blauer "
    "Verlauf als Studio-Hintergrund, weiches professionelles Studio-Licht. "
    "Kopf und Schultern, zentriert, quadratische Komposition."
)


class SettingsUpdate(BaseModel):
    current_password: str = Field(default="", max_length=128)
    new_name: str = Field(default="", max_length=200)
    new_password: str = Field(default="", max_length=128)


class DeleteAccountRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)


class ProfileUpdate(BaseModel):
    profile: str = Field(default="", max_length=50000)


@router.patch("/")
async def update_settings(
    data: SettingsUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """
    Eigene Einstellungen aktualisieren.

    - `new_name` → Name ändern (kein Passwort nötig)
    - `new_password` → Passwort ändern (aktuelles Passwort als Bestätigung nötig)
    """
    name_changed = False
    password_changed = False

    # ─── Name ändern ───────────────────────────────────────────
    if data.new_name:
        stripped = data.new_name.strip()
        if not stripped:
            raise HTTPException(400, "Name darf nicht leer sein.")
        if stripped != user.name:
            user.name = stripped
            name_changed = True

    # ─── Passwort ändern ───────────────────────────────────────
    if data.new_password:
        if not data.current_password:
            raise HTTPException(
                400,
                "Aktuelles Password ist erforderlich, um das Password zu ändern.",
            )

        # LDAP-User haben keinen DB-Password-Hash
        if not user.password_hash:
            raise HTTPException(
                400,
                "Dein Account verwendet LDAP-Authentifizierung. "
                "Passwortänderungen sind hier nicht möglich.",
            )

        if not verify_password(data.current_password, user.password_hash):
            raise HTTPException(
                401,
                "Das aktuelle Password ist falsch.",
            )

        if data.new_password != data.current_password:
            user.password_hash = hash_password(data.new_password)
            password_changed = True

    if not name_changed and not password_changed:
        return {"message": "Keine Änderungen vorgenommen."}

    session.add(user)
    session.commit()
    session.refresh(user)

    messages = []
    if name_changed:
        messages.append("Name wurde aktualisiert.")
    if password_changed:
        messages.append("Password wurde geändert.")

    display_role = "Admin" if user.role == GlobalUserRole.ADMIN else "User"
    return {
        "message": " ".join(messages),
        "user": _user_dict(user, display_role),
    }


@router.patch("/profile")
async def update_profile(
    data: ProfileUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Eigenen Steckbrief (Markdown) speichern — öffentlich unter /u/<username>."""
    text = data.profile.strip()
    user.profile = text or None
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"message": "Steckbrief wurde gespeichert."}


def _user_dict(user: User, display_role: str) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "name": user.name,
        "role": display_role,
        "avatar": media_service.avatar_url(user.avatar),
    }


@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Profilbild hochladen (PNG/JPG/WebP/GIF, max. 2 MB). Ersetzt ein bestehendes Bild."""
    data = await file.read()
    rel_path, _mime = media_service.save_avatar(file.filename or "avatar.png", data)

    # altes Bild bleibt auf der Platte — es gehört jetzt zum Verlauf
    if user.avatar:
        media_service.record_avatar_version(session, user.id, user.avatar)

    user.avatar = rel_path
    session.add(user)
    session.commit()
    session.refresh(user)

    display_role = "Admin" if user.role == GlobalUserRole.ADMIN else "User"
    return {"message": "Profilbild wurde aktualisiert.", "user": _user_dict(user, display_role)}


class AvatarEditRequest(BaseModel):
    instruction: str = Field(default="", max_length=2000)
    campus_style: bool = False


@router.post("/avatar/edit")
async def edit_avatar(
    data: AvatarEditRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """
    Profilbild per Qwen-Image-Edit bearbeiten (ersetzt das aktuelle Bild).

    - `instruction`: freie Anweisung (z. B. "füge eine Sonnenbrille hinzu")
    - `campus_style`: Preset — Kopf im AICampus-Look (blauer 3D-Hintergrund,
      Studio-Licht, Pixar-Style), eigene Gesichtszüge bleiben erhalten.
    Ausgangsbild ist das eigene Profilbild; ohne eigenes Bild wird das
    Standard-Profilbild bearbeitet und das Ergebnis wird das eigene.
    """
    prompt = (
        AVATAR_CAMPUS_STYLE_PROMPT
        if data.campus_style
        else data.instruction.strip()
    )
    if not prompt:
        raise HTTPException(400, "Bitte eine Bearbeitungsanweisung eingeben.")

    # Ausgangsbild: eigenes Profilbild, sonst das Standard-Profilbild
    if user.avatar:
        path = media_service.resolve_avatar_path(user.avatar.rsplit("/", 1)[-1])
        if path is None:
            raise HTTPException(404, "Profilbild-Datei nicht gefunden.")
        base_bytes = path.read_bytes()
        base_mime = media_service.ALLOWED_MEDIA.get(Path(user.avatar).suffix, "image/png")
    else:
        base_bytes = (BASE_DIR / "static" / "default-avatar.png").read_bytes()
        base_mime = "image/png"
    base_bytes, ds_mime = image_service.downscale_for_edit(base_bytes)
    if ds_mime:
        base_mime = ds_mime

    llm_cfg = dict(get_effective_llm_config(session))
    session.rollback()  # DB-Verbindung für den (langen) AI-Call freigeben
    result = await image_service.edit_image(
        prompt=prompt,
        base_image=(base_bytes, base_mime),
        size=AVATAR_EDIT_SIZE,
        config=llm_cfg,
    )
    if not result.get("success"):
        raise HTTPException(502, f"Image-Fehler: {result.get('error', 'Unbekannter Fehler')}")
    png_bytes = result["data"]["png_bytes"]

    rel_path, _ = media_service.save_avatar("avatar.png", png_bytes)
    if user.avatar:
        # nur das EIGENE Bild sichern (bleibt auf der Platte als History-Datei);
        # das Default-Bild ist eine geteilte Static-Datei und gehört nicht in den Verlauf
        media_service.record_avatar_version(session, user.id, user.avatar)
    user.avatar = rel_path
    session.add(user)
    session.commit()
    session.refresh(user)

    display_role = "Admin" if user.role == GlobalUserRole.ADMIN else "User"
    return {"message": "Profilbild wurde bearbeitet.", "user": _user_dict(user, display_role)}


@router.delete("/avatar")
async def delete_avatar(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Profilbild entfernen (bleibt im Verlauf — per History wiederherstellbar)."""
    if not user.avatar:
        return {"message": "Kein Profilbild vorhanden.", "user": _user_dict(user, "Admin" if user.role == GlobalUserRole.ADMIN else "User")}

    # Datei bleibt auf der Platte — sie gehört jetzt zum Verlauf
    media_service.record_avatar_version(session, user.id, user.avatar)
    user.avatar = None
    session.add(user)
    session.commit()
    session.refresh(user)

    display_role = "Admin" if user.role == GlobalUserRole.ADMIN else "User"
    return {"message": "Profilbild wurde entfernt.", "user": _user_dict(user, display_role)}


@router.get("/avatar/history")
async def list_avatar_history(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Verlauf der eigenen Profilbilder (neueste zuerst)."""
    entries = session.exec(
        select(AvatarHistoryEntry)
        .where(AvatarHistoryEntry.user_id == user.id)
        .order_by(AvatarHistoryEntry.id.desc())
    ).all()
    return {"items": [
        {
            "id": e.id,
            "url": media_service.avatar_url(e.file_path),
            "created_at": utc_iso(e.created_at),
        }
        for e in entries
    ]}


@router.post("/avatar/history/{entry_id}/restore")
async def restore_avatar_history(
    entry_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """History-Eintrag wieder als Profilbild setzen (aktuelles Bild wandert in den Verlauf)."""
    entry = session.get(AvatarHistoryEntry, entry_id)
    if not entry or entry.user_id != user.id:
        raise HTTPException(404, "Verlaufseintrag nicht gefunden.")
    path = media_service.resolve_avatar_path(entry.file_path.rsplit("/", 1)[-1])
    if path is None:
        session.delete(entry)
        session.commit()
        raise HTTPException(410, "Die Bilddatei fehlt — der Eintrag wurde entfernt.")
    # aktuelles Bild bleibt auf der Platte — es gehört jetzt zum Verlauf
    if user.avatar and user.avatar != entry.file_path:
        media_service.record_avatar_version(session, user.id, user.avatar)
    user.avatar = entry.file_path
    session.delete(entry)
    session.add(user)
    session.commit()
    session.refresh(user)

    display_role = "Admin" if user.role == GlobalUserRole.ADMIN else "User"
    return {"message": "Profilbild wurde wiederhergestellt.", "user": _user_dict(user, display_role)}


@router.delete("/avatar/history/{entry_id}")
async def delete_avatar_history_entry(
    entry_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Einen Verlaufseintrag endgültig entfernen (Zeile + Datei)."""
    entry = session.get(AvatarHistoryEntry, entry_id)
    if not entry or entry.user_id != user.id:
        raise HTTPException(404, "Verlaufseintrag nicht gefunden.")
    rel = entry.file_path
    session.delete(entry)
    session.commit()
    media_service.delete_avatar_file(rel)
    return {"message": "Verlaufseintrag wurde entfernt."}


@router.delete("/account")
async def delete_account(
    data: DeleteAccountRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """
    Eigenen Account löschen (Soft Delete + Anonymisierung, DSGVO-konform).

    Persönliche Daten (Foren-Posts, Einreichungen, Tokens, …) werden
    endgültig gelöscht, der Account wird anonymisiert. Von dem User
    erstellte Kurse und Kurs-Inhalte bleiben erhalten (Institutsdaten).

    Sicherheit: Username muss als Bestätigung mitgeschickt werden.
    Admin-Accounts können nicht selbst gelöscht werden (letzter-Admin-
    Lockout — im Admin-Bereich von einem anderen Admin möglich).
    """
    if user.role == GlobalUserRole.ADMIN:
        raise HTTPException(
            403,
            "Admin-Accounts können nicht selbst gelöscht werden. "
            "Bitte lass deinen Account von einem anderen Admin entfernen.",
        )

    if data.username.strip() != user.username:
        raise HTTPException(
            400,
            "Der eingegebene Username stimmt nicht mit deinem Account überein.",
        )

    username = user_service.soft_delete_user(session, user)
    logger.info("[Account] User '%s' hat seinen Account selbst gelöscht.", username)
    return {
        "message": (
            "Dein Account wurde anonymisiert und deine persönlichen Daten gelöscht. "
            "Von dir erstellte Kurse und Kurs-Inhalte bleiben erhalten."
        )
    }