"""
Medien-Endpoints: Upload, Liste, Bearbeiten, Löschen (je Kurs).

Rollen: PROF (im Kurs) + Admin. Die Tab-Seite ist nur für PROF/Admin sichtbar;
Tutoren erhalten Medien über PROF (Berechtigung ggf. später erweiterbar).

Dateien landen in data/media/course_{id}/ mit UUID-Namen.
Der Versand läuft über die authentifizierte Route GET /media/{course_id}/{filename}
(siehe main.py) — kein Static-Mount, damit Medien versteckter Inhalte
nicht ohne Kurs-Membership abrufbar sind.
"""

import base64
import re
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlmodel import Session, delete, select

from config import MEDIA_DIR
from database.base import get_session
from database.models import ContentVersion, CourseMedia, CourseRole, MediaUsage, ScriptSection, User
from services.auth_service import require_course_access
from services.llm_service import LLMService
from services.settings_resolver import get_effective_llm_config
from services import media_service
from services.media_service import RefInfo
from services.content_edits import ContentEditError, _resolve_span

router = APIRouter(prefix="/api", tags=["Medien"])
llm_service = LLMService()

# Referenzbild für die Applet-Generierung (Medium aus der Bibliothek oder
# hochgeladen/gezeichnet): nur Rasterformate (SVG kein Vision-Input), und der
# Base64-Payload bleibt unterhalb des 10-MB-Upload-Limits der Frontend.
REFERENCE_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp"}
MAX_REFERENCE_IMAGE_B64 = 14 * 1024 * 1024  # ≈ 10 MB Binär → 13.4 MB Base64
_REFERENCE_IMAGE_URL_RE = re.compile(r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)$")


def _apply_html_edits(html: str, edits: object) -> str:
    """Wendet die LLM-Edit-Liste („html_edits“) auf das Applet-HTML an.

    Analog zu „content_edits“ im Skript (api/script.py): nur „replace_span“
    mit eindeutig im HTML vorkommendem „old“-Anker. Wirft HTTPException(400),
    wenn ein Edit ungültig oder unklar ist (Anker nicht gefunden/mehrdeutig,
    Überlappung, unbekanntes op) — dann bleibt das HTML unverändert."""
    if not isinstance(edits, list) or not edits:
        raise HTTPException(400, "LLM-Antwort ungültig: „html_edits“ ist keine (nicht-leere) Liste von Edit-Objekten.")
    spans: list[tuple[int, int, str]] = []  # (start, end, Ersetzung)
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise HTTPException(400, f"LLM-Edit nicht anwendbar: Edit {i + 1} ist kein Objekt.")
        op = str(edit.get("op") or "").strip()
        if op != "replace_span":
            raise HTTPException(400, f"LLM-Edit nicht anwendbar: unbekanntes „op“ {op!r} (Edit {i + 1}).")
        old = str(edit.get("old") or "")
        new = str(edit.get("new") or "")
        try:
            s, e = _resolve_span(html, old)
        except ContentEditError as err:
            raise HTTPException(400, str(err))
        spans.append((s, e, new))
    # Überlappungs-Check: Einfügepunkte (0 Breite) konfligieren nur, wenn sie
    # STRENG innerhalb eines anderen Spans liegen.
    for a in range(len(spans)):
        for b in range(a + 1, len(spans)):
            a1, a2, _ = spans[a]
            b1, b2, _ = spans[b]
            if a1 == a2 or b1 == b2:
                p = a1 if a1 == a2 else b1
                lo, hi = (b1, b2) if a1 == a2 else (a1, a2)
                if lo < p < hi:
                    raise HTTPException(400, "LLM-Edit nicht anwendbar: Edits überschneiden sich.")
            elif a1 < b2 and b1 < a2:
                raise HTTPException(400, "LLM-Edit nicht anwendbar: Edits überschneiden sich.")
    spans.sort(key=lambda sp: (sp[0], sp[1]))
    parts: list[str] = []
    pos = 0
    for s, e, repl in spans:
        parts.append(html[pos:s])
        parts.append(repl)
        pos = e
    parts.append(html[pos:])
    return "".join(parts)


def _get_media(session: Session, course_id: int, media_id: int) -> CourseMedia:
    media = session.get(CourseMedia, media_id)
    if not media or media.course_id != course_id:
        raise HTTPException(404, "Medium nicht gefunden.")
    return media


def _media_to_dict(
    m: CourseMedia,
    usages: list[MediaUsage],
    counts: dict[str, RefInfo],
    section_ids: dict[str, int] | None = None,
) -> dict:
    fname = m.file_path.rsplit("/", 1)[-1]
    c = counts.get(fname, {"total": 0, "duplicates": []})
    section_ids = section_ids or {}

    def usage_dict(u: MediaUsage) -> dict:
        # Skript-Kapitel: Ort ist als "Skript: <Titel>" gespeichert —
        # Kapitel-ID wird per Titel aufgelöst (keine DB-Spalte nötig).
        section_id = None
        if u.location.startswith("Skript: "):
            section_id = section_ids.get(u.location.removeprefix("Skript: "))
        return {
            "location": u.location,
            "task_id": u.task_id,
            "material_id": u.material_id,
            "section_id": section_id,
        }

    return {
        "id": m.id,
        "title": m.title,
        "url": media_service.media_url(m),
        "media_type": m.media_type,
        "mime_type": m.mime_type,
        "file_size": m.file_size,
        "llm_description": m.llm_description,
        "usages": [usage_dict(u) for u in usages],
        "ref_count": c["total"],
        "duplicate_in": c["duplicates"],
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _load_media_payload(session: Session, course_id: int) -> list[dict]:
    media_list = session.exec(
        select(CourseMedia)
        .where(CourseMedia.course_id == course_id)
        .order_by(CourseMedia.created_at.desc())  # type: ignore[attr-defined]
    ).all()
    if not media_list:
        return []
    usages = session.exec(
        select(MediaUsage).where(MediaUsage.media_id.in_([m.id for m in media_list]))  # type: ignore[attr-defined]
    ).all()
    counts = media_service.reference_counts(session, course_id)
    # Titel → Kapitel-ID, um "Skript: <Titel>"-Vorkommen zu verlinken.
    section_ids = {
        s.title: s.id
        for s in session.exec(
            select(ScriptSection).where(ScriptSection.course_id == course_id)
        ).all()
    }
    return [
        _media_to_dict(m, [u for u in usages if u.media_id == m.id], counts, section_ids)
        for m in media_list
    ]


@router.get("/courses/{course_id}/media")
async def list_media(
    course_id: int,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Alle Medien des Kurses inkl. Verwendungs-Orten."""
    return _load_media_payload(session, course_id)


@router.post("/courses/{course_id}/media")
async def upload_media(
    course_id: int,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    llm_description: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Medium hochladen (PNG/JPG/WebP/GIF, max. 5 MB)."""
    user, _ = user_and_course
    data = await file.read()
    rel_path, mime, size = media_service.save_upload(course_id, file.filename or "upload.png", data)

    default_title = (file.filename or "Medium").rsplit(".", 1)[0]
    media = CourseMedia(
        course_id=course_id,
        title=(title or "").strip() or default_title,
        file_path=rel_path,
        media_type="image",
        mime_type=mime,
        file_size=size,
        llm_description=(llm_description or "").strip() or None,
        created_by=user.id,  # type: ignore[arg-type]
    )
    session.add(media)
    session.commit()
    session.refresh(media)

    counts = media_service.reference_counts(session, course_id)
    return {
        "message": f"Medium '{media.title}' hochgeladen.",
        "media": _media_to_dict(media, [], counts),
    }


@router.put("/courses/{course_id}/media/{media_id}/file")
async def replace_media_file(
    course_id: int,
    media_id: int,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Ersetzt die Datei eines Mediums (gleicher Pfad → Markdown-Referenzen bleiben gültig)."""
    media = _get_media(session, course_id, media_id)
    data = await file.read()
    rel_path, mime, size = media_service.replace_file(media.file_path, file.filename or "upload.png", data)

    media.file_path = rel_path
    media.mime_type = mime
    media.file_size = size
    session.add(media)
    session.commit()
    session.refresh(media)

    counts = media_service.reference_counts(session, course_id)
    return {"message": "Datei ersetzt.", "media": _media_to_dict(media, [], counts)}


# ─── Applets (LLM-generierte, self-contained HTML-Medien) ──────────────
#
# Das Applet-Studio (course/applet_studio.html) nutzt diese Endpoints:
# LLM-Generierung (speichert NICHT) → manuell/LLM anpassen → Speichern
# (neu anlegen oder ersetzen). Dateien: data/media/course_{id}/<uuid>.html.
# Einbettung über <iframe sandbox="allow-scripts"> (Medienbibliothek,
# Skript, Aufgaben — siehe markdown-renderer.js).

@router.post("/courses/{course_id}/media/applet-llm")
async def generate_applet_llm(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """LLM erzeugt/ändert Applet-HTML + Titel + Beschreibung (speichert NICHT).

    Body: {"prompt": str, "existing_html": str (optional, für Refinement),
           "media_id": int (optional — Bild-Medium aus der Bibliothek als Referenz),
           "image_data_url": str (optional — hochgeladenes/gezeichnetes Bild),
           "console_errors": list[str] (optional — Konsole-Fehler aus der Preview)}
    media_id und image_data_url sind gegenseitig exklusiv.
    """
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "Bitte gib an, was das Applet zeigen soll.")
    existing_html = (body.get("existing_html") or "").strip()
    raw_media_id = body.get("media_id")
    image_data_url = (body.get("image_data_url") or "").strip()

    # Konsole-Fehler aus der Preview (Diagnose-Hinweis für das LLM): nur
    # nicht-leere Strings, je max. 2000 Zeichen, insgesamt max. 10 Fehler.
    raw_console_errors = body.get("console_errors")
    console_errors: Optional[list[str]] = None
    if raw_console_errors is not None:
        if not isinstance(raw_console_errors, list) or len(raw_console_errors) > 20:
            raise HTTPException(400, "Ungültiges Feld „console_errors“ (Liste mit max. 20 Fehler-Strings erwartet).")
        console_errors = [str(e).strip()[:2000] for e in raw_console_errors if str(e).strip()][:10] or None

    # ── Referenzbild auflösen (Medium aus der Bibliothek ODER Upload) ──
    image_url: Optional[str] = None
    if raw_media_id is not None and image_data_url:
        raise HTTPException(400, "Bitte wähle entweder ein Medium aus der Bibliothek ODER ein hochgeladenes Bild — nicht beides.")
    if raw_media_id is not None:
        try:
            ref_media = _get_media(session, course_id, int(raw_media_id))
        except (TypeError, ValueError):
            raise HTTPException(400, "Ungültige Medium-ID.")
        if ref_media.media_type != "image" or ref_media.mime_type not in REFERENCE_IMAGE_MIMES:
            raise HTTPException(400, "Das gewählte Medium kann nicht als Referenzbild verwendet werden (nur Bilder: PNG, JPEG, WebP).")
        ref_path = MEDIA_DIR / ref_media.file_path
        if not ref_path.is_file():
            raise HTTPException(404, "Die Datei des Referenzmediums wurde nicht gefunden.")
        image_url = f"data:{ref_media.mime_type};base64," + base64.b64encode(ref_path.read_bytes()).decode("ascii")
    elif image_data_url:
        m = _REFERENCE_IMAGE_URL_RE.fullmatch(image_data_url)
        if not m:
            raise HTTPException(400, "Ungültiges Referenzbild (PNG/JPEG/WebP als Data-URL erwartet).")
        if len(m.group(2)) > MAX_REFERENCE_IMAGE_B64:
            raise HTTPException(413, "Referenzbild zu groß (max. ca. 10 MB).")
        image_url = image_data_url

    llm_cfg = dict(get_effective_llm_config(session, course_id))

    # DB-Connection vor dem langen LLM-Call freigeben (sonst hält die
    # Session sie die gesamte Generierung; s. database/base.py). Der
    # Rollback beendet nur das Read-Transaction — ORM-Objekte bleiben nutzbar.
    session.rollback()

    result = await llm_service.generate_applet(
        prompt=prompt,
        existing_html=existing_html,
        image_data_url=image_url,
        console_errors=console_errors,
        config=llm_cfg,
    )
    if not result.get("success"):
        raise HTTPException(502, f"LLM-Fehler: {result.get('error', 'Unbekannter Fehler')}")

    data = result.get("data", {})
    html = (data.get("html") or "").strip()
    html_edits = data.get("html_edits")
    edits_applied = 0
    if not html and html_edits:
        if not existing_html:
            raise HTTPException(400, "LLM lieferte stellenweise Edits („html_edits“), aber es existiert noch kein Applet zum Editieren. Bitte erneut versuchen.")
        html = _apply_html_edits(existing_html, html_edits)
        edits_applied = len(html_edits)
    if not html:
        raise HTTPException(502, "Das LLM hat weder „html“ noch „html_edits“ geliefert.")
    try:
        media_service.validate_applet_html(html)
    except HTTPException as e:
        raise HTTPException(502, f"Das LLM hat kein gültiges Applet-HTML geliefert: {e.detail}")

    return {
        "html": html,
        "title": (data.get("title") or "").strip(),
        "description": (data.get("description") or "").strip(),
        "edits_applied": edits_applied,
    }


@router.post("/courses/{course_id}/media/applet")
async def create_applet_media(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Neues Applet-Medium anlegen (HTML aus dem Applet-Studio).

    Body: {"title": str, "llm_description": str, "html": str}
    """
    user, _ = user_and_course
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Titel darf nicht leer sein.")
    description = (body.get("llm_description") or "").strip()

    rel_path, mime, size = media_service.save_applet(course_id, body.get("html") or "")

    media = CourseMedia(
        course_id=course_id,
        title=title,
        file_path=rel_path,
        media_type="applet",
        mime_type=mime,
        file_size=size,
        llm_description=description or None,
        created_by=user.id,  # type: ignore[arg-type]
    )
    session.add(media)
    session.commit()
    session.refresh(media)

    counts = media_service.reference_counts(session, course_id)
    return {"message": f"Applet '{media.title}' erstellt.", "media": _media_to_dict(media, [], counts)}


@router.put("/courses/{course_id}/media/{media_id}/applet")
async def update_applet_media(
    course_id: int,
    media_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Ersetzt das HTML eines Applets (gleicher Pfad → Markdown-Referenzen bleiben gültig).

    Body: {"html": str, "title": str (optional), "llm_description": str (optional)}
    """
    media = _get_media(session, course_id, media_id)
    if media.media_type != "applet":
        raise HTTPException(400, "Dieses Medium ist kein Applet.")
    body = await request.json()

    if body.get("html"):
        rel_path, mime, size = media_service.replace_applet(media.file_path, body["html"])
        media.file_path = rel_path
        media.mime_type = mime
        media.file_size = size
    if "title" in body:
        title = (body.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "Titel darf nicht leer sein.")
        media.title = title
    if "llm_description" in body:
        media.llm_description = (body.get("llm_description") or "").strip() or None

    session.add(media)
    session.commit()
    session.refresh(media)

    counts = media_service.reference_counts(session, course_id)
    return {"message": "Applet aktualisiert.", "media": _media_to_dict(media, [], counts)}


@router.get("/courses/{course_id}/media/{media_id}/applet")
async def get_applet_media(
    course_id: int,
    media_id: int,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Applet-Quelle (HTML + Metadaten) für das Applet-Studio."""
    media = _get_media(session, course_id, media_id)
    if media.media_type != "applet":
        raise HTTPException(400, "Dieses Medium ist kein Applet.")
    path = media_service.MEDIA_DIR / media.file_path
    if not path.is_file():
        raise HTTPException(404, "Datei nicht gefunden.")
    return {
        "id": media.id,
        "title": media.title,
        "llm_description": media.llm_description or "",
        "html": path.read_text(encoding="utf-8"),
        "url": media_service.media_url(media),
    }


@router.post("/courses/{course_id}/media/{media_id}/generate-metadata")
async def generate_media_metadata(
    course_id: int,
    media_id: int,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Erzeugt per LLM Titel + Beschreibung für ein vorhandenes Medium und speichert sie."""
    media = _get_media(session, course_id, media_id)
    path = media_service.MEDIA_DIR / media.file_path
    if not path.is_file():
        raise HTTPException(404, "Datei nicht gefunden.")

    image_base64 = base64.b64encode(path.read_bytes()).decode("ascii")
    llm_cfg = get_effective_llm_config(session, course_id)

    # DB-Connection vor dem langen LLM-Call freigeben (sonst hält die
    # Session sie die gesamte Generierung; s. database/base.py). Der
    # Rollback beendet nur das Read-Transaction — ORM-Objekte bleiben nutzbar.
    session.rollback()

    result = await llm_service.describe_media_image(
        image_base64=image_base64,
        mime_type=media.mime_type or "image/png",
        config=llm_cfg,
    )
    if not result.get("success"):
        raise HTTPException(502, f"LLM-Fehler: {result.get('error', 'Unbekannter Fehler')}")

    title = result.get("data", {}).get("title", "").strip()
    description = result.get("data", {}).get("description", "").strip()
    if title:
        media.title = title
    if description:
        media.llm_description = description
    session.add(media)
    session.commit()
    session.refresh(media)

    counts = media_service.reference_counts(session, course_id)
    return {
        "message": "Titel und Beschreibung generiert.",
        "media": _media_to_dict(media, [], counts),
    }


@router.patch("/courses/{course_id}/media/{media_id}")
async def update_media(
    course_id: int,
    media_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Titel / Beschreibung ändern."""
    media = _get_media(session, course_id, media_id)
    body = await request.json()

    if "title" in body:
        title = (body.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "Titel darf nicht leer sein.")
        media.title = title
    if "llm_description" in body:
        val = body.get("llm_description")
        media.llm_description = (val or "").strip() or None

    session.add(media)
    session.commit()
    session.refresh(media)
    return {"message": "Medium aktualisiert.", "media": _media_to_dict(media, [], {})}


@router.delete("/courses/{course_id}/media/{media_id}")
async def delete_media(
    course_id: int,
    media_id: int,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Medium löschen (Datei + DB + Usage-Einträge)."""
    media = _get_media(session, course_id, media_id)

    if media.media_type == "applet":
        session.exec(
            delete(ContentVersion)
            .where(ContentVersion.entity_type == "applet")
            .where(ContentVersion.entity_id == media.id)
        )
    for u in session.exec(select(MediaUsage).where(MediaUsage.media_id == media.id)).all():
        session.delete(u)
    session.delete(media)
    session.commit()

    media_service.delete_file(media.file_path)
    return {"message": f"Medium '{media.title}' gelöscht."}


@router.post("/courses/{course_id}/media/{media_id}/duplicate")
async def duplicate_applet_media(
    course_id: int,
    media_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Applet duplizieren (neue Datei mit neuem UUID-Namen → Markdown-Referenzen
    des Originals bleiben unangetastet).

    Body: {"title": str (optional, Default: „<Original-Titel> (Kopie)“)}
    """
    user, _ = user_and_course
    media = _get_media(session, course_id, media_id)
    if media.media_type != "applet":
        raise HTTPException(400, "Nur Applets können dupliziert werden.")
    body = await request.json()
    title = (body.get("title") or "").strip() or media.title + " (Kopie)"
    if not title:
        raise HTTPException(400, "Titel darf nicht leer sein.")

    path = media_service.MEDIA_DIR / media.file_path
    if not path.is_file():
        raise HTTPException(404, "Datei nicht gefunden.")
    rel_path, mime, size = media_service.save_applet(course_id, path.read_text(encoding="utf-8"))

    new_media = CourseMedia(
        course_id=course_id,
        title=title,
        file_path=rel_path,
        media_type="applet",
        mime_type=mime,
        file_size=size,
        llm_description=media.llm_description,
        created_by=user.id,  # type: ignore[arg-type]
    )
    session.add(new_media)
    session.commit()
    session.refresh(new_media)

    counts = media_service.reference_counts(session, course_id)
    return {"message": f"Applet '{new_media.title}' dupliziert.", "media": _media_to_dict(new_media, [], counts)}
