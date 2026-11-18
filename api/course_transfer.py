"""
AICampus Transfer: Export/Import von Kurs-Materialien als ZIP (PROF/Admin).

  GET  /api/courses/{course_id}/transfer/export-preview  → Checkbox-Liste
  POST /api/courses/{course_id}/transfer/export          → ZIP-Download
  POST /api/courses/{course_id}/transfer/import-upload   → Zip-Upload → Preview
  POST /api/courses/{course_id}/transfer/import          → Import (Token + Auswahl)

Logik: services/course_transfer_service.py
"""

import logging
import shutil
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlmodel import Session

from config import IMPORT_MAX_ZIP_BYTES, IMPORT_MIN_FREE_DISK_FACTOR, TRANSFER_DIR
from database.base import get_session
from database.models import Course, CourseRole, User
from services.auth_service import require_course_access
from services import course_transfer_service as cts

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["AICampus Transfer"])

SECTION_KEYS = cts.SECTION_ORDER


def _get_course(session: Session, course_id: int) -> Course:
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")
    return course


def _selection(body: dict) -> dict:
    return {
        k: [v for v in (body.get(k) or []) if isinstance(v, (int, str)) and v != ""]
        for k in SECTION_KEYS
    }


@router.get("/courses/{course_id}/transfer/export-preview")
async def export_preview(
    course_id: int,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    course = _get_course(session, course_id)
    return cts.export_preview(session, course)


@router.post("/courses/{course_id}/transfer/export")
async def export_zip(
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    course = _get_course(session, course_id)
    body = await request.json()
    selection = _selection(body)
    if not any(selection.values()):
        raise HTTPException(400, "Bitte mindestens einen Inhalt auswählen.")
    try:
        buf, filename = cts.build_export_zip(session, course, selection)
    except cts.TransferError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("AICampus-Export fehlgeschlagen (Kurs %s)", course_id)
        raise HTTPException(500, f"Export fehlgeschlagen: {e}")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/courses/{course_id}/transfer/import-upload")
async def import_upload(
    course_id: int,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
    file: UploadFile = File(...),
):
    """Zip-Upload → Staging + Preview (wird per „import" final importiert)."""
    user, _ = user_and_course

    # Streamen (1-MB-Blöcke), Größe beim Schreiben überwachen
    tmp_dir = TRANSFER_DIR / "incoming"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"upload_{course_id}_{user.id}.zip"
    written = 0
    try:
        with tmp_path.open("wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if written > IMPORT_MAX_ZIP_BYTES:
                    raise HTTPException(
                        413, f"Zip-Datei ist größer als das erlaubte Limit "
                             f"({IMPORT_MAX_ZIP_BYTES // (1024 * 1024)} MB)."
                    )
        if not zipfile.is_zipfile(tmp_path):
            raise HTTPException(400, "Die hochgeladene Datei ist kein gültiges Zip-Archiv.")
        if shutil.disk_usage(TRANSFER_DIR).free < written * IMPORT_MIN_FREE_DISK_FACTOR:
            raise HTTPException(507, "Nicht genügend freier Festplattenspeicher für den Import.")
        result = cts.stage_zip_upload(course_id, tmp_path)
    except cts.TransferError as e:
        raise HTTPException(400, str(e))
    finally:
        tmp_path.unlink(missing_ok=True)
    return result


@router.post("/courses/{course_id}/transfer/import")
async def import_run(
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    user, _ = user_and_course
    course = _get_course(session, course_id)
    body = await request.json()
    token = str(body.get("token") or "")
    if not token or not token.isalnum() or len(token) > 64:
        raise HTTPException(400, "Ungültiges Token — bitte ZIP erneut hochladen.")
    selection = _selection(body)
    if not any(selection.values()):
        raise HTTPException(400, "Bitte mindestens einen Inhalt auswählen.")
    try:
        report = cts.run_import(session, user, course, token, selection)
    except cts.TransferError as e:
        raise HTTPException(400, str(e))
    return report
