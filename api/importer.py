"""
Kurs-Material-Import (Zip) für PROFs und Admins.

Mehrstufiger LLM-Wizard: (1) Dateianalyse/Staging (mehrfache Zips mergbar),
(2) Medien-Import, (3) Quellen-Import, (3b) LLM-Quellen-Extraktion, (4) Skript-
Kapitel-Planner, (4b) Folien-Planner, (5) Skript-Generierung, (6) Slide-Decks.
Die eigentliche Job-Ausführung liegt in services/import_service.py (stateless
Runner mit Pause/Resume/Cancel).

Diese Module stellt nur die REST-Endpunkte bereit:
  POST   /api/courses/{course_id}/import                     (Zip-Upload, 202; erneut = Merge)
  POST   /api/courses/{course_id}/import/files                (Einzeldatei-Upload, 202; erneut = Merge)
  GET    /api/courses/{course_id}/import                      (State/Polling)
  POST   /api/courses/{course_id}/import/media                (auswählen/skip)
  POST   /api/courses/{course_id}/import/references           (auswählen/skip)
  POST   /api/courses/{course_id}/import/ref_extract          (LLM-Quellen-Extraktion)
  POST   /api/courses/{course_id}/import/plan                 (Skript-Planner starten)
  POST   /api/courses/{course_id}/import/slides_plan          (Folien-Planner starten)
  PUT    /api/courses/{course_id}/import/plan                 (Pläne manuell speichern)
  POST   /api/courses/{course_id}/import/script               (generieren/skip)
  POST   /api/courses/{course_id}/import/slides               (generieren/skip)
  POST   /api/courses/{course_id}/import/pause|resume|cancel  ({stage})
  GET    /api/courses/{course_id}/import/preview?path=        (Bild/PDF-Vorschau)
  DELETE /api/courses/{course_id}/import/files                (gestagte Dateien entfernen)
  DELETE /api/courses/{course_id}/import                      (Import + Staging löschen)

Upload-Formate:
  Zip-Upload:  RAW Body (rohe Zip-Bytes, kein FormData/multipart!),
               Dateiname via Header X-File-Name.
  Datei-Upload: Multipart-Formular mit mehreren Teilen „files“ (PDF, TeX,
               Markdown, Office, Bilder, BibTeX — see IMPORT_LOOSE_FILE_EXTS).
"""

import asyncio
import logging
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from config import IMPORT_DIR, IMPORT_MAX_ZIP_BYTES, IMPORT_MIN_FREE_DISK_FACTOR
from database.base import get_session
from database.models import CourseImport
from services import import_service
from api.course_members import require_prof_or_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Kurs-Import (PROF/Admin)"])


# ─── Request-Modelle ──────────────────────────────────────────────

class MediaStartBody(BaseModel):
    paths: Optional[list[str]] = None
    skip: bool = False


class ReferencesStartBody(BaseModel):
    keys: Optional[list[str]] = None
    skip: bool = False


class PlanSaveBody(BaseModel):
    chapters: Optional[list[Any]] = None  # Skript-Plan
    decks: Optional[list[Any]] = None  # Folien-Plan


class ScriptStartBody(BaseModel):
    skip: bool = False


class SlidesStartBody(BaseModel):
    delete_original: bool = False
    skip: bool = False


class StageControlBody(BaseModel):
    stage: str


class FilesDeleteBody(BaseModel):
    paths: list[str]


# ─── Helfer ───────────────────────────────────────────────────────

def _get_import(session: Session, course_id: int) -> Optional[CourseImport]:
    return session.exec(
        select(CourseImport).where(CourseImport.course_id == course_id)
    ).first()


def _require_import(session: Session, course_id: int) -> CourseImport:
    imp = _get_import(session, course_id)
    if not imp:
        raise HTTPException(404, "Kein Import für diesen Kurs vorhanden.")
    assert imp.id is not None
    return imp


def _status(imp: CourseImport, stage: str) -> str:
    return getattr(imp, f"{stage}_status")


# Job-Functionen pro Stufe (für Resume: stateless Neustart mit (course_id, import_id))
_STAGE_JOBS = {
    "filemap": import_service.run_filemap_job,
    "media": import_service.run_media_job,
    "references": import_service.run_references_job,
    "ref_extract": import_service.run_ref_extract_job,
    "plan": import_service.run_planner_job,
    "slides_plan": import_service.run_slides_planner_job,
    "script": import_service.run_script_job,
    "slides": import_service.run_slides_job,
}


# ─── Upload + State ───────────────────────────────────────────────

@router.post("/courses/{course_id}/import", status_code=202)
async def start_import(
    request: Request,
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Zip-Upload (RAW-Body) → Staging; startet die Datei-Analyse (filemap).

    Bei bestehendem Import wird die neue Zip in dieselbe Staging gemerged
    (mehrere Zips pro Import); die filemap läuft dann idempotent neu.
    """
    user, course_id, _ = auth
    zip_name = Path(request.headers.get("x-file-name") or "upload.zip").name[:300]
    existing = _get_import(session, course_id)

    # Größen-/Platz-Checks vor dem Streamen (wenn Content-Length bekannt)
    total = int(request.headers.get("content-length") or 0)
    if total > IMPORT_MAX_ZIP_BYTES:
        raise HTTPException(
            413, f"Zip-Datei zu groß (max. {IMPORT_MAX_ZIP_BYTES // (1024 * 1024)} MB)."
        )
    if total > 0 and shutil.disk_usage(IMPORT_DIR).free < total * IMPORT_MIN_FREE_DISK_FACTOR:
        raise HTTPException(507, "Nicht genügend freier Festplattenspeicher für den Import.")

    is_merge = existing is not None
    if is_merge:
        active = [
            s for s in import_service.IMPORT_STAGES
            if _status(existing, s) in ("running", "paused")
        ]
        if active:
            raise HTTPException(
                409, "Es läuft gerade ein Import-Job – bitte zuerst pausieren oder abbrechen."
            )
        staging = import_service.staging_dir(course_id, existing.job_id)
        staging.mkdir(parents=True, exist_ok=True)
        n = len(list(staging.glob("upload*.zip"))) + 1
        zip_path = staging / f"upload_{n}.zip"
        job_id = existing.job_id
    else:
        job_id = uuid.uuid4().hex  # 32 Hex-Zeichen
        staging = import_service.staging_dir(course_id, job_id)
        staging.mkdir(parents=True, exist_ok=True)
        zip_path = staging / "upload_1.zip"

    # Streamen (1-MB-Blöcke liefert Starlette), Größe beim Schreiben überwachen
    written = 0
    try:
        with zip_path.open("wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                written += len(chunk)
                if written > IMPORT_MAX_ZIP_BYTES:
                    raise HTTPException(413, "Zip-Datei ist größer als das erlaubte Limit.")
        if not zipfile.is_zipfile(zip_path):
            raise HTTPException(400, "Die hochgeladene Datei ist kein gültiges Zip-Archiv.")
    except HTTPException:
        # Bei Merge nur die neue Zip löschen — die bestehende Staging bleibt erhalten!
        zip_path.unlink(missing_ok=True)
        if not is_merge:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    except OSError:
        zip_path.unlink(missing_ok=True)
        if not is_merge:
            shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(500, "Upload konnte nicht gespeichert werden.")

    if is_merge:
        imp = existing
        report = dict(imp.report or {})
        uploads = list(report.get("uploads") or [imp.zip_name])
        if zip_name not in uploads:
            uploads.append(zip_name)
        # Alte Extraktions-Fehler aus misslungenen Läufen bereinigen
        report = {
            **report,
            "uploads": uploads,
            "errors": [
                e for e in (report.get("errors") or [])
                if not import_service.report_entry_msg(e).startswith("Zip-Extraktion fehlgeschlagen")
            ],
        }
        imp.report = report
        imp.updated_at = datetime.now()
        session.add(imp)
        session.commit()
        session.refresh(imp)
        assert imp.id is not None
        import_service.spawn_job(import_service.run_filemap_job(course_id, imp.id))
        return {
            "message": (
                f"Weitere Zip-Datei hinzugefügt ({zip_name}) – "
                "die Dateianalyse wird fortgesetzt."
            ),
            "import_id": imp.id,
        }

    imp = CourseImport(
        course_id=course_id, job_id=job_id, zip_name=zip_name, created_by=user.id,
        report={"uploads": [zip_name]},
    )
    session.add(imp)
    session.commit()
    session.refresh(imp)
    assert imp.id is not None

    import_service.spawn_job(import_service.run_filemap_job(course_id, imp.id))
    return {"message": "Import gestartet.", "import_id": imp.id}


@router.post("/courses/{course_id}/import/files", status_code=202)
async def start_import_files(
    request: Request,
    files: list[UploadFile] = File(...),
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Einzeldatei-Upload (Multipart, mehrere Teile „files“) → dieselbe
    Staging wie der Zip-Upload; startet die Datei-Analyse (filemap).

    Nur Dateitypen, die das Import-System verarbeitet (PDF, TeX, Markdown,
    Office, Bilder, BibTeX) — Zips laufen über den RAW-Body-Endpunkt.
    Bei bestehendem Import wird gemerged; die filemap läuft idempotent neu.
    """
    user, course_id, _ = auth
    if not files or not any((f.filename or "").strip() for f in files):
        raise HTTPException(400, "Keine Datei ausgewählt.")
    existing = _get_import(session, course_id)

    # Größen-/Platz-Checks vor dem Speichern (Content-Length = ganzer Multipart-Body)
    total = int(request.headers.get("content-length") or 0)
    if total > IMPORT_MAX_ZIP_BYTES:
        raise HTTPException(
            413, f"Upload zu groß (max. {IMPORT_MAX_ZIP_BYTES // (1024 * 1024)} MB)."
        )
    if total > 0 and shutil.disk_usage(IMPORT_DIR).free < total * IMPORT_MIN_FREE_DISK_FACTOR:
        raise HTTPException(507, "Nicht genügend freier Festplattenspeicher für den Import.")

    if existing is not None:
        active = [
            s for s in import_service.IMPORT_STAGES
            if _status(existing, s) in ("running", "paused")
        ]
        if active:
            raise HTTPException(
                409, "Es läuft gerade ein Import-Job – bitte zuerst pausieren oder abbrechen."
            )
        staging = import_service.staging_dir(course_id, existing.job_id)
        job_id = existing.job_id
        is_merge = True
    else:
        job_id = uuid.uuid4().hex  # 32 Hex-Zeichen
        staging = import_service.staging_dir(course_id, job_id)
        is_merge = False

    try:
        _added, warnings = await asyncio.to_thread(
            import_service.store_loose_files,
            staging,
            [(f.filename or "", f.file) for f in files],
        )
    except ValueError as exc:
        # Bei neuem Import die (leere bzw. teilweise) Staging aufräumen;
        # bei Merge bleibt das Staging erhalten (wie beim Zip-Upload).
        if not is_merge:
            shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(400, str(exc))
    except OSError as exc:
        logger.error("Import-Datei-Upload fehlgeschlagen: %s", exc)
        if not is_merge:
            shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(500, "Upload konnte nicht gespeichert werden.")

    names = [Path(f.filename or "").name for f in files]
    display = names[0] if len(names) == 1 else f"{len(names)} Dateien"

    if is_merge:
        imp = existing
        report = dict(imp.report or {})
        uploads = list(report.get("uploads") or [imp.zip_name])
        for n in names:
            if n not in uploads:
                uploads.append(n)
        # Alte Extraktions-Fehler aus misslungenen Läufen bereinigen
        report = {
            **report,
            "uploads": uploads,
            "errors": [
                e for e in (report.get("errors") or [])
                if not import_service.report_entry_msg(e).startswith("Zip-Extraktion fehlgeschlagen")
            ],
        }
        imp.report = report
        imp.updated_at = datetime.now()
        session.add(imp)
        session.commit()
        session.refresh(imp)
        assert imp.id is not None
        if warnings:
            import_service.append_report(imp.id, warnings=warnings)
        import_service.spawn_job(import_service.run_filemap_job(course_id, imp.id))
        return {
            "message": (
                f"Weitere Datei(n) hinzugefügt ({display}) – "
                "die Dateianalyse wird fortgesetzt."
            ),
            "import_id": imp.id,
        }

    imp = CourseImport(
        course_id=course_id, job_id=job_id, zip_name=display[:300], created_by=user.id,
        report={"uploads": names},
    )
    session.add(imp)
    session.commit()
    session.refresh(imp)
    assert imp.id is not None
    if warnings:
        import_service.append_report(imp.id, warnings=warnings)

    import_service.spawn_job(import_service.run_filemap_job(course_id, imp.id))
    return {"message": "Import gestartet.", "import_id": imp.id}


@router.get("/courses/{course_id}/import")
async def get_import(
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Aktueller Import-State für die UI (Polling)."""
    _, course_id, _ = auth
    imp = _get_import(session, course_id)
    if imp and imp.filemap_status == "done" and not (imp.report or {}).get("references_detected"):
        # Lazy Quellen-Detektion (einmalig nach Filemap-Ende; greift auch für
        # ältere Imports, deren Manifest noch keine .bib-Typen kannte).
        refs = await asyncio.to_thread(import_service.detect_references, course_id, imp.id)
        if refs is not None:
            imp.reference_map = refs
            report = dict(imp.report or {})
            report["references_detected"] = True
            imp.report = report
            session.add(imp)
            session.commit()
            session.refresh(imp)
    return {"import": import_service.serialize_import(imp) if imp else None}


# ─── Stufe 2: Medien ──────────────────────────────────────────────

@router.post("/courses/{course_id}/import/media")
async def start_media(
    body: MediaStartBody,
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Ausgewählte Medien in den Kurs importieren (oder Stufe überspringen)."""
    _, course_id, _ = auth
    imp = _require_import(session, course_id)

    if imp.filemap_status != "done":
        raise HTTPException(400, "Die Dateianalyse muss zuerst abgeschlossen sein.")
    if _status(imp, "media") in ("running", "paused"):
        raise HTTPException(409, "Der Medien-Import läuft bereits.")

    if body.skip:
        import_service.set_stage_status(imp.id, "media", "skipped")
        return {"message": "Medien-Import übersprungen."}

    if not body.paths:
        raise HTTPException(400, "Keine Medien ausgewählt.")

    by_path = {m["path"]: m for m in (imp.manifest or [])}
    for p in body.paths:
        m = by_path.get(p)
        # Extension-Fallback: altes Manifest kennt svg/eps noch nicht als Typ
        if not m or (m.get("type") not in ("image", "figure_pdf", "svg", "eps")
                     and not p.lower().endswith((".svg", ".eps"))):
            raise HTTPException(400, f"Ungültiger Medien-Pfad: {p}")

    paths = list(dict.fromkeys(body.paths))  # dedup, Reihenfolge erhalten
    # Auswahl persistieren → Resume ohne Parameter liest sie aus report.
    # Alte Fehler-Warnings aus vorherigen (misslungenen) Läufe bereinigen.
    report = dict(imp.report or {})
    report["warnings"] = [w for w in (report.get("warnings") or [])
                          if not import_service.report_entry_msg(w).startswith("Medium nicht importiert")]
    imp.report = {**report, "media_options": {"paths": paths}}
    imp.updated_at = datetime.now()
    session.add(imp)
    session.commit()

    import_service.set_stage_status(imp.id, "media", "running")
    import_service.spawn_job(import_service.run_media_job(course_id, imp.id, paths))
    return {"message": f"{len(paths)} Medien werden importiert."}


# ─── Stufe 3: Quellen ──────────────────────────────────────────────

@router.post("/courses/{course_id}/import/references")
async def start_references(
    body: ReferencesStartBody,
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Ausgewählte Quellen in die Kurs-Quellenbibliothek importieren (oder Stufe überspringen)."""
    _, course_id, _ = auth
    imp = _require_import(session, course_id)

    if imp.filemap_status != "done":
        raise HTTPException(400, "Die Dateianalyse muss zuerst abgeschlossen sein.")
    if _status(imp, "references") in ("running", "paused"):
        raise HTTPException(409, "Der Quellen-Import läuft bereits.")
    if not (imp.report or {}).get("references_detected"):
        refs = await asyncio.to_thread(import_service.detect_references, course_id, imp.id)
        if refs is not None:
            imp.reference_map = refs
            report = dict(imp.report or {})
            report["references_detected"] = True
            imp.report = report
            session.add(imp)
            session.commit()

    if body.skip:
        import_service.set_stage_status(imp.id, "references", "skipped")
        return {"message": "Quellen-Import übersprungen."}

    if not body.keys:
        raise HTTPException(400, "Keine Quellen ausgewählt.")

    refmap = imp.reference_map or {}
    for k in body.keys:
        if k not in refmap:
            raise HTTPException(400, f"Unbekannte Quelle: {k}")

    keys = list(dict.fromkeys(body.keys))  # dedup, Reihenfolge erhalten
    # Auswahl persistieren → Resume ohne Parameter liest sie aus report.
    # Alte Fehler-Warnings aus vorherigen (misslungenen) Läufen bereinigen.
    report = dict(imp.report or {})
    report["warnings"] = [w for w in (report.get("warnings") or [])
                          if not import_service.report_entry_msg(w).startswith("Quelle nicht importiert")]
    imp.report = {**report, "references_options": {"keys": keys}}
    imp.updated_at = datetime.now()
    session.add(imp)
    session.commit()

    import_service.set_stage_status(imp.id, "references", "running")
    import_service.spawn_job(import_service.run_references_job(course_id, imp.id, keys))
    return {"message": f"{len(keys)} Quellen werden importiert."}


# ─── Stufe 3b: LLM-Quellen-Extraktion ───────────────────────────

@router.post("/courses/{course_id}/import/ref_extract")
async def start_ref_extract(
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """LLM-Quellen-Extraktion aus Text- und Bib-Dateien starten (mit Dedup)."""
    _, course_id, _ = auth
    imp = _require_import(session, course_id)

    if imp.filemap_status != "done":
        raise HTTPException(400, "Die Dateianalyse muss zuerst abgeschlossen sein.")
    if _status(imp, "ref_extract") in ("running", "paused"):
        raise HTTPException(409, "Die Quellen-Extraktion läuft bereits.")

    import_service.set_stage_status(imp.id, "ref_extract", "running")
    import_service.spawn_job(import_service.run_ref_extract_job(course_id, imp.id))
    return {"message": "LLM-Quellen-Extraktion gestartet."}


# ─── Stufe 4: Kapitel-Plan ────────────────────────────────────────

@router.post("/courses/{course_id}/import/plan")
async def start_planner(
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Agentic Planner starten → Vorschlag für Kapitel-Überschriften."""
    _, course_id, _ = auth
    imp = _require_import(session, course_id)

    if imp.filemap_status != "done":
        raise HTTPException(400, "Die Dateianalyse muss zuerst abgeschlossen sein.")
    if _status(imp, "plan") in ("running", "paused"):
        raise HTTPException(409, "Der Planer läuft bereits.")
    if not any(m.get("read_path") for m in (imp.manifest or [])):
        raise HTTPException(
            400, "Keine Text-Dateien im Import – es kann kein Kapitel-Plan erstellt werden."
        )

    import_service.set_stage_status(imp.id, "plan", "running")
    import_service.spawn_job(import_service.run_planner_job(course_id, imp.id))
    return {"message": "Planner gestartet."}


@router.post("/courses/{course_id}/import/slides_plan")
async def start_slides_planner(
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Agentic Folien-Planner starten → Vorschlag für die Slide-Deck-Struktur."""
    _, course_id, _ = auth
    imp = _require_import(session, course_id)

    if imp.filemap_status != "done":
        raise HTTPException(400, "Die Dateianalyse muss zuerst abgeschlossen sein.")
    if _status(imp, "slides_plan") in ("running", "paused"):
        raise HTTPException(409, "Der Folien-Planer läuft bereits.")
    if not any(m.get("read_path") for m in (imp.manifest or [])):
        raise HTTPException(
            400, "Keine Text-Dateien im Import – es kann kein Folien-Plan erstellt werden."
        )

    import_service.set_stage_status(imp.id, "slides_plan", "running")
    import_service.spawn_job(import_service.run_slides_planner_job(course_id, imp.id))
    return {"message": "Folien-Planner gestartet."}


@router.put("/courses/{course_id}/import/plan")
async def save_plan(
    body: PlanSaveBody,
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Skript- und/oder Folien-Plan manuell speichern (Umbenennen/Reihenfolge/aktivieren).

    Bestehende Verknüpfungen (section_id/material_id) und Statuse werden
    pro Index aus dem client-seitigen Plan übernommen (eigene UI → 1:1-Mapping).
    """
    _, course_id, _ = auth
    imp = _require_import(session, course_id)

    if body.chapters is None and body.decks is None:
        raise HTTPException(400, "Nichts zu speichern (chapters und/oder decks angeben).")
    if _status(imp, "plan") in ("running", "paused"):
        raise HTTPException(409, "Der Plan kann nicht geändert werden, während der Planer läuft.")

    message = ""
    if body.chapters is not None:
        normalized = import_service.normalize_plan(body.chapters)
        for i, ch in enumerate(normalized):
            if i < len(body.chapters) and isinstance(body.chapters[i], dict):
                raw = body.chapters[i]
                if isinstance(raw.get("section_id"), int):
                    ch["section_id"] = raw["section_id"]
                if isinstance(raw.get("material_id"), int):
                    ch["material_id"] = raw["material_id"]
                for key in ("script_status", "slides_status"):
                    if raw.get(key) in ("pending", "done", "error"):
                        ch[key] = raw[key]
                for key in ("script_error", "slides_error"):
                    if isinstance(raw.get(key), str):
                        ch[key] = raw[key][:500]
        imp.chapter_plan = normalized
        if normalized and _status(imp, "plan") in ("pending", "error"):
            imp.plan_status = "done"  # manuell erstellter Plan zählt als fertig
        message = f"Plan gespeichert ({len(normalized)} Kapitel)."
    if body.decks is not None:
        decks = import_service.normalize_slides_plan(body.decks)
        for i, d in enumerate(decks):
            if i < len(body.decks) and isinstance(body.decks[i], dict):
                raw = body.decks[i]
                if isinstance(raw.get("material_id"), int):
                    d["material_id"] = raw["material_id"]
                if raw.get("slides_status") in ("pending", "done", "error"):
                    d["slides_status"] = raw["slides_status"]
                if isinstance(raw.get("slides_error"), str):
                    d["slides_error"] = raw["slides_error"][:500]
        imp.slides_plan = decks
        message += f" Folien-Plan: {len(decks)} Decks."
    imp.updated_at = datetime.now()
    session.add(imp)
    session.commit()
    return {"message": message}


# ─── Stufe 4: Skript ──────────────────────────────────────────────

@router.post("/courses/{course_id}/import/script")
async def start_script(
    body: Optional[ScriptStartBody] = None,
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Aktive Kapitel wortgetreu in Skript-Sektionen konvertieren (oder überspringen)."""
    _, course_id, _ = auth
    imp = _require_import(session, course_id)

    if imp.filemap_status != "done":
        raise HTTPException(400, "Die Dateianalyse muss zuerst abgeschlossen sein.")
    if _status(imp, "script") in ("running", "paused"):
        raise HTTPException(409, "Die Skript-Generierung läuft bereits.")

    if body and body.skip:
        import_service.set_stage_status(imp.id, "script", "skipped")
        return {"message": "Skript-Generierung übersprungen."}

    if not any(c.get("enabled") for c in (imp.chapter_plan or [])):
        raise HTTPException(400, "Kein aktives Kapitel im Plan – bitte im Plan aktivieren.")

    import_service.set_stage_status(imp.id, "script", "running")
    import_service.spawn_job(import_service.run_script_job(course_id, imp.id))
    return {"message": "Skript-Generierung gestartet."}


# ─── Stufe 5: Folien ──────────────────────────────────────────────

@router.post("/courses/{course_id}/import/slides")
async def start_slides(
    body: SlidesStartBody,
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Slide-Decks für aktive Kapitel generieren (oder Stufe überspringen)."""
    _, course_id, _ = auth
    imp = _require_import(session, course_id)

    if imp.filemap_status != "done":
        raise HTTPException(400, "Die Dateianalyse muss zuerst abgeschlossen sein.")
    if _status(imp, "slides") in ("running", "paused"):
        raise HTTPException(409, "Die Folien-Generierung läuft bereits.")

    if body.skip:
        import_service.set_stage_status(imp.id, "slides", "skipped")
        return {"message": "Folien-Generierung übersprungen."}

    if not any(d.get("enabled") for d in import_service.get_slides_plan(imp)):
        raise HTTPException(400, "Kein aktives Deck im Folien-Plan – bitte im Folien-Plan aktivieren.")

    imp.report = {**(imp.report or {}), "slides_options": {"delete_original": body.delete_original}}
    imp.updated_at = datetime.now()
    session.add(imp)
    session.commit()

    import_service.set_stage_status(imp.id, "slides", "running")
    import_service.spawn_job(import_service.run_slides_job(course_id, imp.id))
    return {"message": "Folien-Generierung gestartet."}


# ─── Pause / Resume / Cancel ──────────────────────────────────────

def _control_stage(imp: CourseImport, stage: str) -> str:
    """Stufe validieren und als Key zurückgeben (Status wird vom Caller abgefragt)."""
    if stage not in import_service.IMPORT_STAGES:
        raise HTTPException(400, f"Unbekannte Stufe: {stage}")
    return stage


@router.post("/courses/{course_id}/import/pause")
async def pause_stage(
    body: StageControlBody,
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Aktive Stufe pausieren (Job stoppt am nächsten Gate)."""
    _, course_id, _ = auth
    imp = _require_import(session, course_id)
    stage = _control_stage(imp, body.stage)
    if _status(imp, stage) != "running":
        raise HTTPException(400, "Stufe läuft nicht – kann nicht pausiert werden.")

    import_service.set_stage_status(imp.id, stage, "paused")
    return {"message": f"Stufe {stage} pausiert."}


@router.post("/courses/{course_id}/import/resume")
async def resume_stage(
    body: StageControlBody,
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Gepauste/interruptete Stufe fortsetzen (Job wird stateless neu gestartet)."""
    _, course_id, _ = auth
    imp = _require_import(session, course_id)
    stage = _control_stage(imp, body.stage)
    if _status(imp, stage) not in ("paused", "interrupted"):
        raise HTTPException(400, "Stufe ist nicht pausiert – kann nicht fortgesetzt werden.")

    import_service.set_stage_status(imp.id, stage, "running")
    import_service.spawn_job(_STAGE_JOBS[stage](course_id, imp.id))
    return {"message": f"Stufe {stage} wird fortgesetzt."}


@router.post("/courses/{course_id}/import/cancel")
async def cancel_stage(
    body: StageControlBody,
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Aktive/gedrosselte Stufe abbrechen (bereits fertige Units bleiben erhalten)."""
    _, course_id, _ = auth
    imp = _require_import(session, course_id)
    stage = _control_stage(imp, body.stage)
    if _status(imp, stage) not in ("running", "paused"):
        raise HTTPException(400, "Stufe läuft nicht – kann nicht abgebrochen werden.")

    import_service.set_stage_status(imp.id, stage, "cancelled")
    return {"message": f"Stufe {stage} abgebrochen."}


# ─── Preview + Löschen ────────────────────────────────────────────

@router.get("/courses/{course_id}/import/preview")
async def preview_file(
    path: str = Query(...),
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Vorschau einer Medien-Datei aus dem Staging (Bilder + PDF, strikt validiert)."""
    _, course_id, _ = auth
    imp = _require_import(session, course_id)

    p = import_service.resolve_preview_path(course_id, imp.id, path)
    if p is None or not p.is_file():
        raise HTTPException(404, "Datei nicht gefunden.")

    ext = p.suffix.lower()
    if ext == ".pdf":
        return FileResponse(p, media_type="application/pdf")
    if ext == ".svg":
        # sanitized ausliefern — rohes SVG im Tab könnte Skripte ausführen
        sanitized = import_service.sanitize_svg(p.read_bytes())
        if "error" in sanitized:
            raise HTTPException(415, f"SVG-Vorschau nicht möglich: {sanitized['error']}")
        return Response(content=sanitized["data"], media_type="image/svg+xml")
    media_type = import_service.IMAGE_MIME.get(ext, "application/octet-stream")
    return FileResponse(p, media_type=media_type)


# ─── Staging-Dateien ────────────────────────────────────────────

@router.delete("/courses/{course_id}/import/files")
async def delete_staged_files(
    body: FilesDeleteBody,
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Gestagte Dateien entfernen (Staging + Manifest + Digests + Maps)."""
    _, course_id, _ = auth
    imp = _require_import(session, course_id)

    active = [
        s for s in import_service.IMPORT_STAGES
        if _status(imp, s) in ("running", "paused")
    ]
    if active:
        raise HTTPException(
            409, "Es läuft gerade ein Job – bitte zuerst pausieren oder abbrechen."
        )
    if not body.paths:
        raise HTTPException(400, "Keine Pfade angegeben.")

    n = await asyncio.to_thread(
        import_service.remove_staged_files,
        course_id, imp.id, list(dict.fromkeys(body.paths)),
    )
    return {"message": f"{n} Datei(en) entfernt.", "removed": n}


@router.delete("/courses/{course_id}/import")
async def delete_import(
    session: Session = Depends(get_session),
    auth: tuple = Depends(require_prof_or_admin()),
):
    """Import-Zeile + Staging-Verzeichnis löschen (nur wenn keine Stufe aktiv)."""
    _, course_id, _ = auth
    imp = _require_import(session, course_id)

    active = [
        s for s in import_service.IMPORT_STAGES
        if _status(imp, s) in ("running", "paused")
    ]
    if active:
        raise HTTPException(
            409, "Es läuft gerade ein Job – bitte zuerst pausieren oder abbrechen."
        )

    import_service.delete_import(course_id, imp.id)
    return {"message": "Import gelöscht."}
