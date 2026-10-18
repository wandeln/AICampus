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
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import func
from sqlmodel import Session, delete, select

from config import MEDIA_DIR
from database.base import get_session
from database.models import ContentVersion, CourseMedia, CourseRole, MediaUsage, ScriptSection, User
from services.auth_service import require_course_access
from services.llm_service import LLMService
from services.settings_resolver import get_effective_llm_config
from services import media_service
from services import version_service
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


# Filter-Werte für die Einbunde-Zahl (mit der Frontend-Auswahl in
# templates/course/media.html synchron halten).
_USAGE_COUNT_FILTERS = {
    "none": lambda n: n == 0,
    "exactly-1": lambda n: n == 1,
    "at-least-1": lambda n: n >= 1,
    "more-than-1": lambda n: n >= 2,
}
_MEDIA_FILTERS = ("all", "duplicate", "no-desc", *_USAGE_COUNT_FILTERS)
_MEDIA_SORTS = ("newest", "oldest", "title-asc", "title-desc")


def _filtered_media(
    session: Session,
    course_id: int,
    q: str | None,
    media_filter: str | None,
    counts: dict[str, RefInfo],
) -> list[CourseMedia]:
    """Alle (noch unsortierten) Medien des Kurses, gefiltert nach Suche/Filter."""
    stmt = select(CourseMedia).where(CourseMedia.course_id == course_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (CourseMedia.title.ilike(like))  # type: ignore[attr-defined]
            | (CourseMedia.llm_description.ilike(like))  # type: ignore[attr-defined]
        )
    media_list = list(session.exec(stmt).all())
    if not media_list:
        return []
    if media_filter in _USAGE_COUNT_FILTERS:
        usage_counts = dict(
            session.exec(
                select(MediaUsage.media_id, func.count())
                .where(MediaUsage.media_id.in_([m.id for m in media_list]))  # type: ignore[attr-defined]
                .group_by(MediaUsage.media_id)  # type: ignore[attr-defined]
            ).all()
        )
        pred = _USAGE_COUNT_FILTERS[media_filter]
        media_list = [m for m in media_list if pred(usage_counts.get(m.id, 0))]  # type: ignore[arg-type]
        if not media_list:
            return []
    if media_filter == "no-desc":
        media_list = [m for m in media_list if not (m.llm_description or "").strip()]
    if media_filter == "duplicate":
        media_list = [
            m
            for m in media_list
            if counts.get(m.file_path.rsplit("/", 1)[-1], {}).get("duplicates")
        ]
    return media_list


def _sorted_media(media_list: list[CourseMedia], sort: str | None) -> list[CourseMedia]:
    """Sortierung wie in der Frontend-Auswahl; ID als Tiebreaker für stabile Seiten."""
    created = lambda m: m.created_at or datetime.min
    if sort == "oldest":
        media_list.sort(key=lambda m: (created(m), m.id))
    elif sort == "title-asc":
        media_list.sort(key=lambda m: (m.title.casefold(), m.id))
    elif sort == "title-desc":
        media_list.sort(key=lambda m: (m.title.casefold(), m.id), reverse=True)
    else:  # "newest" (Default)
        media_list.sort(key=lambda m: (created(m), m.id), reverse=True)
    return media_list


def _load_media_payload(
    session: Session,
    course_id: int,
    q: str | None = None,
    media_filter: str | None = None,
    sort: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> list[dict] | dict:
    """Medien des Kurses inkl. Verwendungs-Orten (für die Medien-Übersicht).

    Ohne `limit`: vollständige Liste (ältere Consumers, z.B. Applet-Studio).
    Mit `limit` (+ `offset`): Paginierung → {"items", "total", "has_more"}.
    Die aufwändigen Verwendungs-Details werden nur für die aktuelle Seite
    geladen — bei 100–1000 Medien pro Kurs bleibt so der Server beim
    Scroll-Nachladen der Medien-Übersicht entlastet.
    """
    # Referenz-Infos pro Datei (kurs-weit; Basis für „duplikat“-Filter und -Warnungen).
    counts = media_service.reference_counts(session, course_id)
    media_list = _sorted_media(
        _filtered_media(session, course_id, q, media_filter, counts), sort
    )
    total = len(media_list)
    offset = max(0, offset)
    if limit is None:
        page = media_list
    else:
        page = media_list[offset:offset + limit]
    if not page:
        if limit is None:
            return []
        return {"items": [], "total": total, "has_more": False}
    usages = session.exec(
        select(MediaUsage).where(MediaUsage.media_id.in_([m.id for m in page]))  # type: ignore[attr-defined]
    ).all()
    # Titel → Kapitel-ID, um "Skript: <Titel>"-Vorkommen zu verlinken.
    section_ids = {
        s.title: s.id
        for s in session.exec(
            select(ScriptSection).where(ScriptSection.course_id == course_id)
        ).all()
        if s.id is not None
    }
    items = [
        _media_to_dict(m, [u for u in usages if u.media_id == m.id], counts, section_ids)
        for m in page
    ]
    if limit is None:
        return items
    return {"items": items, "total": total, "has_more": offset + len(items) < total}


@router.get("/courses/{course_id}/media")
async def list_media(
    course_id: int,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
    q: Optional[str] = None,
    media_filter: Optional[str] = Query(None, alias="filter"),
    sort: Optional[str] = None,
    offset: int = 0,
    limit: Optional[int] = Query(None, ge=1, le=200),
):
    """Medien des Kurses inkl. Verwendungs-Orten.

    Ohne `limit`: vollständige Liste (Backwards-Compat, z.B. Applet-Studio,
    Slides-Bildauswahl). Mit `limit` (+ `offset`): Paginierung →
    {"items", "total", "has_more"} — so lädt die Medien-Übersicht nur noch
    die sichtbare Seite nach. `q`/`filter`/`sort` filtern & sortieren
    server-seitig (unklare Werte werden ignoriert).
    """
    q = q.strip() if q and q.strip() else None
    if media_filter is not None and media_filter not in _MEDIA_FILTERS:
        media_filter = None
    if sort is not None and sort not in _MEDIA_SORTS:
        sort = None
    return _load_media_payload(
        session, course_id, q=q, media_filter=media_filter, sort=sort, offset=offset, limit=limit
    )


@router.post("/courses/{course_id}/media")
async def upload_media(
    course_id: int,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    llm_description: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Medium hochladen (PNG/JPG/WebP/GIF, max. 5 MB; SVG → Applet, sanitized)."""
    user, _ = user_and_course
    data = await file.read()
    if Path(file.filename or "").suffix.lower() == ".svg":
        rel_path, mime, size = media_service.save_svg_upload(course_id, data)
        media_type = "applet"
    else:
        rel_path, mime, size = media_service.save_upload(course_id, file.filename or "upload.png", data)
        media_type = "image"

    default_title = (file.filename or "Medium").rsplit(".", 1)[0]
    media = CourseMedia(
        course_id=course_id,
        title=(title or "").strip() or default_title,
        file_path=rel_path,
        media_type=media_type,
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
    """LLM erzeugt/ändert Applet-Code (HTML oder SVG) + Titel + Beschreibung (speichert NICHT).

    Body: {"prompt": str, "format": "html"|"svg" (optional, Default html),
           "existing_media": str (optional, für Refinement),
           "current_title": str (optional — vorhandener Titel, Kontext für das LLM),
           "current_description": str (optional — vorhandene Beschreibung, Kontext für das LLM),
           "generate_fields": {"title": true, "description": true, "media": true}
           (optional — Default: alle; das LLM darf nur die aktivierten
           Felder ändern, nicht aktivierte bleiben unverändert),
           "media_id": int (optional — Bild-Medium aus der Bibliothek als Referenz),
           "image_data_url": str (optional — hochgeladenes/gezeichnetes Bild),
           "console_errors": list[str] (optional — Konsole-Fehler aus der Preview, nur HTML)}
    media_id und image_data_url sind gegenseitig exklusiv.
    """
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "Bitte gib an, was das Applet zeigen soll.")
    fmt = (body.get("format") or "html").strip().lower()
    if fmt not in ("html", "svg"):
        raise HTTPException(400, "„format“ muss „html“ oder „svg“ sein.")
    gen = body.get("generate_fields", {}) or {}
    generate_fields = [f for f in ("title", "description", "media") if gen.get(f)]
    if not generate_fields:
        raise HTTPException(400, "Keine Felder angefordert.")
    existing_media = (body.get("existing_media") or "").strip()
    current_title = (body.get("current_title") or "").strip()
    current_description = (body.get("current_description") or "").strip()
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
        generate_fields=generate_fields,
        existing_media=existing_media,
        current_title=current_title,
        current_description=current_description,
        image_data_url=image_url,
        console_errors=console_errors,
        config=llm_cfg,
        format=fmt,
    )
    if not result.get("success"):
        raise HTTPException(502, f"LLM-Fehler: {result.get('error', 'Unbekannter Fehler')}")

    data = result.get("data", {})
    response: dict = {"edits_applied": 0}
    if "title" in generate_fields:
        response["title"] = (data.get("title") or "").strip()
    if "description" in generate_fields:
        response["description"] = (data.get("description") or "").strip()
    if "media" in generate_fields:
        media_content = (data.get("media") or "").strip()
        media_edits = data.get("media_edits")
        if not media_content and media_edits:
            if not existing_media:
                raise HTTPException(400, "LLM lieferte stellenweise Edits („media_edits“), aber es existiert noch kein Applet zum Editieren. Bitte erneut versuchen.")
            media_content = _apply_html_edits(existing_media, media_edits)
            response["edits_applied"] = len(media_edits)
        if media_content:
            if len(media_content.encode("utf-8")) > media_service.MAX_APPLET_BYTES:
                raise HTTPException(502, f"Das LLM hat einen Applet-Code geliefert, der das Limit von {media_service.MAX_APPLET_BYTES // 1024} KB überschreitet. Bitte mit einer präziseren Anfrage erneut versuchen.")
            try:
                media_service.validate_applet_content(media_content, fmt)
            except HTTPException as e:
                raise HTTPException(502, f"Das LLM hat keinen gültigen Applet-Code geliefert: {e.detail}")
            response["media"] = media_content
        elif not existing_media:
            # Neues Applet: ohne Code wäre nichts da — Code ist Pflicht.
            raise HTTPException(502, "Das LLM hat weder „media“ noch „media_edits“ geliefert.")
        # sonst: LLM hat den Code-Schlüssel weggelassen → Code bleibt unverändert
    return response


@router.post("/courses/{course_id}/media/applet")
async def create_applet_media(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Neues Applet-Medium anlegen (HTML oder SVG aus dem Applet-Studio).

    Body: {"title": str, "llm_description": str, "media": str,
           "format": "html"|"svg" (optional, Default html)}
    """
    user, _ = user_and_course
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Titel darf nicht leer sein.")
    description = (body.get("llm_description") or "").strip()
    fmt = (body.get("format") or "html").strip().lower()
    if fmt not in ("html", "svg"):
        raise HTTPException(400, "„format“ muss „html“ oder „svg“ sein.")

    rel_path, mime, size = media_service.save_applet(course_id, body.get("media") or "", fmt)

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
    version_service.mark_created(session, user, "applet", media.id)

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
    """Ersetzt den Code eines Applets (gleicher Pfad → Markdown-Referenzen
    bleiben gültig). Das Format leitet sich aus der Datei-Extension ab.

    Body: {"media": str (optional), "title": str (optional), "llm_description": str (optional)}
    """
    media = _get_media(session, course_id, media_id)
    if media.media_type != "applet":
        raise HTTPException(400, "Dieses Medium ist kein Applet.")
    body = await request.json()

    if body.get("media"):
        rel_path, mime, size = media_service.replace_applet(media.file_path, body["media"])
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
    """Applet-Quelle (HTML oder SVG + Metadaten) für das Applet-Studio."""
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
        "media": path.read_text(encoding="utf-8"),
        "format": media_service.applet_format_of(media.file_path),
        "url": media_service.media_url(media),
    }


@router.post("/courses/{course_id}/media/{media_id}/applet-format")
async def switch_applet_format_endpoint(
    course_id: int,
    media_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Format eines Applets wechseln (html↔svg).

    Benennt die Datei um (gleiche UUID, andere Extension) und repariert die
    Markdown-Referenzen in Skript/Folien/Aufgaben. Der Code bleibt
    unverändert — die eigentliche Umwandlung macht das LLM (Prompt im
    Applet-Studio). Vorher wird eine Version „Formatwechsel“ angelegt
    (Rückgängig: list_versions + restore_version).

    Body: {"format": "html"|"svg"}
    """
    user, _ = user_and_course
    media = _get_media(session, course_id, media_id)
    if media.media_type != "applet":
        raise HTTPException(400, "Dieses Medium ist kein Applet.")
    body = await request.json()
    fmt = (body.get("format") or "").strip().lower()
    if fmt not in ("html", "svg"):
        raise HTTPException(400, "„format“ muss „html“ oder „svg“ sein.")
    old_format = media_service.applet_format_of(media.file_path)
    if old_format == fmt:
        raise HTTPException(400, f"Das Applet hat bereits das Format „{fmt}“.")
    # Sicherheits-Snapshot des alten Zustands (Rollback über die Versions-History)
    version_service.snapshot_current(session, user, "applet", media.id, name=f"Formatwechsel → {fmt}")
    media_service.switch_applet_format(session, course_id, media, fmt)

    target = "ein HTML-Applet" if fmt == "html" else "ein SVG"
    counts = media_service.reference_counts(session, course_id)
    return {
        "message": f"Format auf „{fmt}“ gewechselt — bitte passe den Code entsprechend an (manuell oder per LLM, z. B. „Bitte konvertiere das folgende {old_format} möglichst getreu in {target}“).",
        "media": _media_to_dict(media, [], counts),
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

    # SVG (Vektor) zuerst rasterisieren — die Vision-API kann kein rohes SVG.
    if media.mime_type == "image/svg+xml":
        png = media_service.svg_to_png(path.read_bytes())
        image_base64 = base64.b64encode(png).decode("ascii")
        vision_mime = "image/png"
    else:
        image_base64 = base64.b64encode(path.read_bytes()).decode("ascii")
        vision_mime = media.mime_type or "image/png"
    llm_cfg = get_effective_llm_config(session, course_id)

    # DB-Connection vor dem langen LLM-Call freigeben (sonst hält die
    # Session die DB-Connection die gesamte Generierung; s. database/base.py). Der
    # Rollback beendet nur das Read-Transaction — ORM-Objekte bleiben nutzbar.
    session.rollback()

    result = await llm_service.describe_media_image(
        image_base64=image_base64,
        mime_type=vision_mime,
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
    rel_path, mime, size = media_service.save_applet(
        course_id, path.read_text(encoding="utf-8"),
        media_service.applet_format_of(media.file_path),
    )

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
    version_service.mark_created(session, user, "applet", new_media.id)

    counts = media_service.reference_counts(session, course_id)
    return {"message": f"Applet '{new_media.title}' dupliziert.", "media": _media_to_dict(new_media, [], counts)}
