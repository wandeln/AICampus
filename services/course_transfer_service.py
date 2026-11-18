"""
AICampus Transfer: Round-Trip-Export/Import von Kurs-Materialien (PROF/Admin).

Export: ZIP mit human-readable JSON + Dateien (Medien, Workspace-, Upload-Dateien).
Import: ZIP-Upload → Staging → Sektions-Preview → ausgewählte Inhalte importieren.

Sektionen: script (Kapitel), slides (Decks + Theme), tasks (alle Typen),
media (Dateien), references (Quellen). Alle sind im UI separat wählbar.

Format: manifest.json ({format, version}). Unbekannte Sektionen/Felder werden
beim Import ignoriert (graceful Vorwärts-/Rückwärtskompatibilität); neue
Inhaltstypen = neues Top-Level-Verzeichnis + Eintrag in SECTION_ORDER.

Staging: data/aicampus_transfer/course_{id}/{token}/ (upload.zip + extracted/).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlmodel import Session, func, select

from config import (
    IMPORT_MAX_ENTRIES,
    IMPORT_MAX_EXTRACTED_BYTES,
    IMPORT_MAX_SINGLE_FILE_BYTES,
    IMPORT_MAX_ZIP_BYTES,
    MEDIA_DIR,
    TRANSFER_DIR,
    UPLOAD_TASK_DIR,
    WORKSPACE_DIR,
)
from database.models import (
    Course,
    CourseMaterial,
    CourseMedia,
    CourseReference,
    CourseSlidesTheme,
    MaterialType,
    ScriptSection,
    Task,
    TaskType,
    TaskWorkspaceFile,
    TaskWorkspaceFolder,
    TaskWorkspaceOrder,
    User,
)
from services import media_service, version_service, workspace_service
from services.timeutil import utc_iso

logger = logging.getLogger(__name__)

FORMAT = "aicampus-course-export"
VERSION = 1

SECTION_ORDER = ["script", "slides", "tasks", "media", "references"]

# Medien-Referenz im Markdown: /media/{course_id}/<datei>
_MEDIA_REF_RE = re.compile(r"/media/(\d+)/([A-Za-z0-9._\-]+)")


class TransferError(ValueError):
    """Benutzerfehler (→ HTTP 400)."""


def _normalize_selection(selection: dict) -> dict:
    """Client-Auswahl (Strings/Ints) → Sets (Ints + ggf. „__theme__“)."""
    out: dict[str, set] = {}
    for k in SECTION_ORDER:
        s: set = set()
        for v in selection.get(k) or []:
            if v == "__theme__":
                s.add("__theme__")
            else:
                try:
                    s.add(int(v))
                except (TypeError, ValueError):
                    pass
        out[k] = s
    return out


# ─── Helfer ──────────────────────────────────────────────────────────────

def _dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_json_if(path: Path, warnings: Optional[list[str]]) -> Optional[Any]:
    if not path.is_file():
        return None
    try:
        return _load_json(path)
    except (OSError, json.JSONDecodeError) as e:
        if warnings is not None:
            warnings.append(f"{path.name} ist beschädigt — Sektion übersprungen.")
        return None


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _json_field(v: Any) -> Any:
    """DB-Feld mit JSON-String (mc_data, workspace_engines) → geparsed."""
    if v is None or isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _slugify(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:max_len].strip("-") or "kurs"


def rewrite_media_urls(
    text: Optional[str],
    src_course_id: Any,
    dst_course_id: int,
    fname_map: dict[str, str],
) -> Optional[str]:
    """/media/{src}/<fname> → /media/{dst}/<neu> — nur für importierte Medien.

    fname_map: Quelldateiname → Zieldateiname (identisch außer bei
    Content-Dedup, wo auf das vorhandene Medium im Zielkurs gemappt wird).
    """
    if not text or not fname_map or not src_course_id:
        return text
    try:
        src = int(src_course_id)
    except (TypeError, ValueError):
        return text
    if src == dst_course_id:
        return text

    def sub(m: re.Match) -> str:
        cid, fname = int(m.group(1)), m.group(2)
        if cid == src and fname in fname_map:
            return f"/media/{dst_course_id}/{fname_map[fname]}"
        return m.group(0)

    return _MEDIA_REF_RE.sub(sub, text)


def staging_dir(course_id: int, token: str) -> Path:
    return TRANSFER_DIR / f"course_{course_id}" / token


def _clean_course_staging(course_id: int) -> None:
    base = TRANSFER_DIR / f"course_{course_id}"
    if base.is_dir():
        for d in base.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)


# ─── Task-Feld-Handling ──────────────────────────────────────────────────

_TASK_FIELDS = (
    "title", "task_type", "description", "model_solution", "max_points",
    "max_attempts", "deadline", "text_template", "is_visible", "display_order",
    "hints_enabled", "auto_grading_enabled", "workspace_timeout", "workspace_cpu",
    "workspace_memory", "workspace_disk_quota", "workspace_internet",
    "workspace_main_file", "workspace_engines", "workspace_image",
    "mc_data", "upload_template", "upload_solution_file",
)


def _task_to_dict(task: Task) -> dict:
    d: dict[str, Any] = {"id": task.id}
    for f in _TASK_FIELDS:
        v = getattr(task, f)
        if f == "task_type":
            v = v.value
        elif f in ("mc_data", "workspace_engines"):
            v = _json_field(v)
        d[f] = v
    return d


def _workspace_meta(session: Session, task_id: int) -> dict:
    files = session.exec(
        select(TaskWorkspaceFile).where(TaskWorkspaceFile.task_id == task_id)
    ).all()
    folders = session.exec(
        select(TaskWorkspaceFolder).where(TaskWorkspaceFolder.task_id == task_id)
    ).all()
    orders = session.exec(
        select(TaskWorkspaceOrder).where(TaskWorkspaceOrder.task_id == task_id)
    ).all()
    return {
        "files": [
            {"path": f.path, "size": f.size, "checksum": f.checksum,
             "access": f.access, "is_binary": f.is_binary, "sort_order": f.sort_order}
            for f in files
        ],
        "folders": [{"path": f.path, "access": f.access} for f in folders],
        "orders": [{"path": o.path, "sort_order": o.sort_order} for o in orders],
    }


def _max_display_order(session: Session, model, course_id: int) -> int:
    return session.exec(
        select(func.max(model.display_order)).where(model.course_id == course_id)
    ).one() or 0


# ─── Export ──────────────────────────────────────────────────────────────

def export_preview(session: Session, course: Course) -> dict:
    """Alle aktiven Inhalte mit Default-Auswahl (Medien: nur verwendete)."""
    cid = course.id
    sections: dict[str, list[dict]] = {k: [] for k in SECTION_ORDER}

    for s in session.exec(
        select(ScriptSection)
        .where(ScriptSection.course_id == cid)
        .order_by(ScriptSection.display_order.asc(), ScriptSection.id.asc())
    ).all():
        sections["script"].append(
            {"id": s.id, "title": s.title, "visible": s.is_visible, "checked": True}
        )

    for m in session.exec(
        select(CourseMaterial)
        .where(CourseMaterial.course_id == cid)
        .where(CourseMaterial.material_type == MaterialType.SLIDES)
        .order_by(CourseMaterial.display_order.asc(), CourseMaterial.id.asc())
    ).all():
        sections["slides"].append(
            {"id": m.id, "title": m.title, "visible": m.is_visible, "checked": True}
        )
    theme = session.exec(
        select(CourseSlidesTheme).where(CourseSlidesTheme.course_id == cid)
    ).first()
    if theme is not None:
        sections["slides"].append(
            {"id": "__theme__", "title": "Foliendesign (Theme)", "checked": True}
        )

    for t in session.exec(
        select(Task)
        .where(Task.course_id == cid)
        .order_by(Task.display_order.asc(), Task.id.asc())
    ).all():
        sections["tasks"].append(
            {"id": t.id, "title": t.title, "type": t.task_type.value, "checked": True}
        )

    counts = media_service.reference_counts(session, cid)
    refmap = media_service.reference_map_by_entity(session, cid)
    for m in session.exec(
        select(CourseMedia)
        .where(CourseMedia.course_id == cid)
        .order_by(CourseMedia.id.asc())
    ).all():
        fname = m.file_path.rsplit("/", 1)[-1]
        total = (counts.get(fname) or {}).get("total", 0)
        sections["media"].append({
            "id": m.id, "title": m.title, "type": m.media_type,
            "size": m.file_size, "usage_count": total, "checked": total > 0,
            "used_by": refmap.get(fname, {}),
        })

    for r in session.exec(
        select(CourseReference)
        .where(CourseReference.course_id == cid)
        .order_by(CourseReference.display_order.asc(), CourseReference.id.asc())
    ).all():
        sections["references"].append(
            {"id": r.id, "title": r.title or r.key, "key": r.key, "checked": True}
        )

    return {"course": {"id": cid, "name": course.name}, "sections": sections}


def build_export_zip(session: Session, course: Course, selection: dict) -> tuple[io.BytesIO, str]:
    """ZIP bauen (nur ausgewählte Inhalte). Returns (BytesIO, Dateiname)."""
    selected = _normalize_selection(selection)
    cid = course.id
    buf = io.BytesIO()

    # Temp-Verzeichnis auf dem Data-Volume (/tmp im Container kann zu klein sein)
    with tempfile.TemporaryDirectory(prefix="aicampus_export_", dir=TRANSFER_DIR) as tmp:
        root = Path(tmp)

        _dump_json(root / "manifest.json", {
            "format": FORMAT,
            "version": VERSION,
            "exported_at": utc_iso(datetime.now(timezone.utc)),
            "source_course_id": cid,
            "source_course_name": course.name,
        })

        # ── Skript ──
        if selected["script"]:
            sections = session.exec(
                select(ScriptSection).where(ScriptSection.course_id == cid)
            ).all()
            by_id = {s.id: s for s in sections}
            ordered = [by_id[i] for i in sorted(selected["script"] & by_id.keys())
                       if by_id[i] is not None]
            # Display-Reihenfolge beibehalten
            ordered.sort(key=lambda s: (s.display_order, s.id or 0))
            if ordered:
                _dump_json(root / "script" / "sections.json", [
                    {
                        "title": s.title, "content": s.content,
                        "is_visible": s.is_visible, "display_order": s.display_order,
                        "summary": s.summary,
                    }
                    for s in ordered
                ])

        # ── Slides ──
        if selected["slides"]:
            decks = session.exec(
                select(CourseMaterial)
                .where(CourseMaterial.course_id == cid)
                .where(CourseMaterial.material_type == MaterialType.SLIDES)
            ).all()
            by_id = {m.id: m for m in decks if m.id in selected["slides"]}
            ordered = sorted(by_id.values(), key=lambda m: (m.display_order, m.id or 0))
            if ordered:
                _dump_json(root / "slides" / "decks.json", [
                    {
                        "title": m.title, "content": m.content,
                        "is_visible": m.is_visible, "display_order": m.display_order,
                        "summary": m.summary,
                    }
                    for m in ordered
                ])
            if "__theme__" in selected["slides"]:
                theme = session.exec(
                    select(CourseSlidesTheme).where(CourseSlidesTheme.course_id == cid)
                ).first()
                if theme is not None:
                    _dump_json(root / "slides" / "theme.json", {"theme": theme.theme})

        # ── Aufgaben ──
        if selected["tasks"]:
            tasks = session.exec(select(Task).where(Task.course_id == cid)).all()
            by_id = {t.id: t for t in tasks}
            ordered = sorted(
                (by_id[i] for i in selected["tasks"] & by_id.keys()),
                key=lambda t: (t.display_order, t.id or 0),
            )
            if ordered:
                tasks_dir = root / "tasks"
                _dump_json(tasks_dir / "tasks.json", [_task_to_dict(t) for t in ordered])
                meta = {
                    str(t.id): _workspace_meta(session, t.id)
                    for t in ordered if t.task_type == TaskType.WORKSPACE
                }
                _dump_json(tasks_dir / "meta.json", meta)
                for t in ordered:
                    if t.task_type == TaskType.WORKSPACE:
                        src = WORKSPACE_DIR / str(t.id)
                        if src.is_dir():
                            shutil.copytree(
                                src, tasks_dir / "workspace" / str(t.id), dirs_exist_ok=True
                            )
                    elif t.task_type == TaskType.UPLOAD:
                        src = UPLOAD_TASK_DIR / str(t.id)
                        if src.is_dir():
                            shutil.copytree(
                                src, tasks_dir / "uploads" / str(t.id), dirs_exist_ok=True
                            )

        # ── Medien ──
        if selected["media"]:
            counts = media_service.reference_counts(session, cid)
            media = session.exec(
                select(CourseMedia).where(CourseMedia.course_id == cid)
            ).all()
            by_id = {m.id: m for m in media}
            entries, media_dir = [], root / "media"
            files_dir = media_dir / "files"
            files_dir.mkdir(parents=True, exist_ok=True)
            for m in sorted((by_id[i] for i in selected["media"] & by_id.keys()),
                            key=lambda x: x.id or 0):
                fname = m.file_path.rsplit("/", 1)[-1]
                src = MEDIA_DIR / m.file_path
                if not src.is_file():
                    continue  # Datei fehlt auf Disc → überspringen
                shutil.copy2(src, files_dir / fname)
                entries.append({
                    "id": m.id, "title": m.title, "media_type": m.media_type,
                    "mime_type": m.mime_type, "file_size": m.file_size,
                    "content_hash": m.content_hash,
                    "llm_description": m.llm_description,
                    "file": fname,
                    "usage_count": (counts.get(fname) or {}).get("total", 0),
                })
            if entries:
                _dump_json(media_dir / "media.json", entries)

        # ── Quellen ──
        if selected["references"]:
            refs = session.exec(
                select(CourseReference).where(CourseReference.course_id == cid)
            ).all()
            by_id = {r.id: r for r in refs}
            ordered = sorted(
                (by_id[i] for i in selected["references"] & by_id.keys()),
                key=lambda r: (r.display_order, r.id or 0),
            )
            if ordered:
                _dump_json(root / "references" / "references.json", [
                    {
                        "key": r.key, "entry_type": r.entry_type,
                        "authors": r.authors, "title": r.title, "year": r.year,
                        "venue": r.venue, "detail": r.detail, "address": r.address,
                        "doi": r.doi, "url": r.url, "note": r.note,
                        "description": r.description,
                        "display_order": r.display_order,
                    }
                    for r in ordered
                ])

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(root).as_posix())

    filename = f"aicampus_export_{_slugify(course.name)}_{datetime.now():%Y%m%d}.zip"
    buf.seek(0)  # Zeiger nach Zip-Write am Ende — sonst streamt die Response leer
    return buf, filename


# ─── Import: Staging + Preview ───────────────────────────────────────────

def _extract_zip(zip_path: Path, extracted: Path) -> None:
    """Zip sicher extrahieren (Zip-Slip/Symlink/Größen-Schutz, wie import_service)."""
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > IMPORT_MAX_ENTRIES:
            raise TransferError(f"Zip enthält zu viele Dateien (max. {IMPORT_MAX_ENTRIES}).")
        base = extracted.resolve()
        for info in infos:
            name = info.filename
            if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                raise TransferError(f"Ungültiger Dateiname im Zip: {name}")
            target = (extracted / name).resolve()
            if not target.is_relative_to(base):
                raise TransferError(f"Ungültiger Dateiname im Zip (Pfad außerhalb): {name}")
            mode = (info.external_attr >> 28) & 0o170000
            if mode in (0o120000, 0o140000, 0o010000):
                raise TransferError(f"Symlink/Special-Datei im Zip: {name}")
            if info.file_size > IMPORT_MAX_SINGLE_FILE_BYTES:
                raise TransferError(f"Datei zu groß im Zip: {name}")
            total += info.file_size
            if total > IMPORT_MAX_EXTRACTED_BYTES:
                raise TransferError("Zip entpackt zu viel Daten (max. 1 GB).")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)


def stage_zip_upload(course_id: int, zip_path: Path) -> dict:
    """Zip validieren + extrahieren → Staging. Returns {token, manifest, preview}."""
    if zip_path.stat().st_size > IMPORT_MAX_ZIP_BYTES:
        raise TransferError(
            f"Zip zu groß (max. {IMPORT_MAX_ZIP_BYTES // (1024 * 1024)} MB)."
        )
    if not zipfile.is_zipfile(zip_path):
        raise TransferError("Die Datei ist kein gültiges Zip-Archiv.")

    token = uuid.uuid4().hex
    _clean_course_staging(course_id)
    staging = staging_dir(course_id, token)
    extracted = staging / "extracted"
    extracted.mkdir(parents=True)
    staged_zip = staging / "upload.zip"
    shutil.move(str(zip_path), str(staged_zip))

    _extract_zip(staged_zip, extracted)

    manifest_path = extracted / "manifest.json"
    if not manifest_path.is_file():
        raise TransferError(
            "Ungültiges AICampus-Export-Format (manifest.json fehlt). "
            "Nur Zips aus dem AICampus-Export können importiert werden."
        )
    try:
        manifest = _load_json(manifest_path)
    except json.JSONDecodeError:
        raise TransferError("manifest.json ist kein gültiges JSON.")
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise TransferError(
            f"Ungültiges Format (erwartet „{FORMAT}“, "
            f"gefunden {manifest.get('format') if isinstance(manifest, dict) else '?'})."
        )
    version = manifest.get("version")
    if not isinstance(version, int) or version < 1:
        raise TransferError("manifest.json enthält keine gültige Version.")

    preview = _import_preview(extracted, manifest.get("source_course_id") if isinstance(manifest.get("source_course_id"), int) else None)
    if not any(preview["sections"].values()):
        raise TransferError("Zip enthält keine importierbaren Inhalte.")
    return {
        "token": token,
        "manifest": {
            "version": version,
            "source_course_id": manifest.get("source_course_id"),
            "source_course_name": manifest.get("source_course_name"),
        },
        "preview": preview,
    }


def _import_preview(extracted: Path, src_course_id: Optional[int]) -> dict:
    """Seiten der Preview aus den extrahierten JSONs bauen (IDs = Listenindizes)."""
    sections: dict[str, list[dict]] = {k: [] for k in SECTION_ORDER}

    script_raw = _as_list(_load_json_if(extracted / "script" / "sections.json", None))
    decks_raw = _as_list(_load_json_if(extracted / "slides" / "decks.json", None))
    tasks_raw = _as_list(_load_json_if(extracted / "tasks" / "tasks.json", None))

    for i, item in enumerate(script_raw):
        if not isinstance(item, dict):
            continue
        sections["script"].append({
            "id": i, "title": str(item.get("title") or f"Kapitel {i + 1}"),
            "visible": bool(item.get("is_visible", True)), "checked": True,
        })

    for i, item in enumerate(decks_raw):
        if not isinstance(item, dict):
            continue
        sections["slides"].append({
            "id": i, "title": str(item.get("title") or f"Deck {i + 1}"),
            "visible": bool(item.get("is_visible", True)), "checked": True,
        })
    if (extracted / "slides" / "theme.json").is_file():
        sections["slides"].append(
            {"id": "__theme__", "title": "Foliendesign (Theme)", "checked": True}
        )

    for i, item in enumerate(tasks_raw):
        if not isinstance(item, dict):
            continue
        sections["tasks"].append({
            "id": i, "title": str(item.get("title") or f"Aufgabe {i + 1}"),
            "type": str(item.get("task_type") or "?"), "checked": True,
        })

    # Welche Medien referenzieren welche Inhalte im ZIP (Listenindizes)?
    ref_re = re.compile(
        rf"/media/{src_course_id}/([A-Za-z0-9._\-]+)" if src_course_id
        else r"/media/[^/]+/([A-Za-z0-9._\-]+)"
    )
    used: dict[str, dict[str, set[int]]] = {}

    def mark(text: Any, kind: str, idx: int) -> None:
        for fname in ref_re.findall(str(text or "")):
            used.setdefault(fname, {}).setdefault(kind, set()).add(idx)

    for i, it in enumerate(tasks_raw):
        if isinstance(it, dict):
            mark(it.get("description"), "task", i)
    for i, it in enumerate(decks_raw):
        if isinstance(it, dict):
            mark(it.get("content"), "slide_deck", i)
    for i, it in enumerate(script_raw):
        if isinstance(it, dict):
            mark(it.get("content"), "script_section", i)

    media_entries = _as_list(_load_json_if(extracted / "media" / "media.json", None))
    files_dir = extracted / "media" / "files"
    for i, item in enumerate(media_entries):
        if not isinstance(item, dict):
            continue
        fname = str(item.get("file") or "")
        sections["media"].append({
            "id": i, "title": str(item.get("title") or fname or f"Medium {i + 1}"),
            "type": str(item.get("media_type") or "image"),
            "size": item.get("file_size"),
            "usage_count": item.get("usage_count") or 0,
            "file": fname,
            "file_present": bool(fname and (files_dir / fname).is_file()),
            "used_by": {k: sorted(v) for k, v in used.get(fname, {}).items()},
            "checked": True,
        })

    for i, item in enumerate(_as_list(_load_json_if(extracted / "references" / "references.json", None))):
        if not isinstance(item, dict):
            continue
        sections["references"].append({
            "id": i, "title": str(item.get("title") or item.get("key") or f"Quelle {i + 1}"),
            "key": str(item.get("key") or ""), "checked": True,
        })

    return {"sections": sections}


# ─── Import: Ausführung ──────────────────────────────────────────────────

def run_import(session: Session, user: User, course: Course, token: str, selection: dict) -> dict:
    """Ausgewählte Inhalte aus dem gestagten Zip importieren. Returns Report."""
    staging = staging_dir(course.id, token)
    extracted = staging / "extracted"
    if not extracted.is_dir():
        raise TransferError("Import-Dateien nicht (mehr) verfügbar — bitte ZIP erneut hochladen.")
    manifest = _load_json_if(extracted / "manifest.json", None)
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise TransferError("Import-Dateien beschädigt — bitte ZIP erneut hochladen.")

    selected = _normalize_selection(selection)
    dst_id = course.id
    src_course_id = manifest.get("source_course_id")
    report: dict[str, Any] = {
        "imported": {k: 0 for k in SECTION_ORDER},
        "warnings": [],
    }

    def chosen_indices(section: str, data: list) -> list[int]:
        return sorted(
            i for i in selected[section]
            if isinstance(i, int) and 0 <= i < len(data)
        )

    # ── 1. Medien zuerst (für URL-Rewriting) ──
    # Dedup per Content-Hash: identisches Medium existiert bereits im Zielkurs
    # (z. B. zweiter Import derselben Zip) → wiederverwenden statt duplizieren.
    fname_map: dict[str, str] = {}  # Quelldateiname → Zieldateiname
    media_reused = 0
    media_entries = _as_list(_load_json_if(extracted / "media" / "media.json", None))
    for idx in chosen_indices("media", media_entries):
        entry = media_entries[idx]
        fname = str(entry.get("file") or "")
        src_file = extracted / "media" / "files" / fname if fname else None
        title = str(entry.get("title") or fname or f"Medium {idx + 1}")
        if not (fname and src_file is not None and src_file.is_file()):
            report["warnings"].append(f"Medium übersprungen (Datei fehlt im Zip): {title}")
            continue
        data = src_file.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        existing = session.exec(
            select(CourseMedia)
            .where(CourseMedia.course_id == dst_id)
            .where(CourseMedia.content_hash == digest)
        ).first()
        if existing is not None:
            fname_map[fname] = existing.file_path.rsplit("/", 1)[-1]
            media_reused += 1
            continue
        dst_dir = MEDIA_DIR / f"course_{dst_id}"
        dst_dir.mkdir(parents=True, exist_ok=True)
        (dst_dir / fname).write_bytes(data)
        session.add(CourseMedia(
            course_id=dst_id,
            title=title[:300],
            file_path=f"course_{dst_id}/{fname}",
            media_type=str(entry.get("media_type") or "image")[:50],
            mime_type=str(entry.get("mime_type") or "application/octet-stream")[:100],
            file_size=len(data),
            content_hash=digest,
            llm_description=(
                str(entry["llm_description"])[:2000] if entry.get("llm_description") else None
            ),
            created_by=user.id,
        ))
        fname_map[fname] = fname
        report["imported"]["media"] += 1
    session.commit()
    report["media_reused"] = media_reused
    if media_reused:
        report["warnings"].append(
            f"{media_reused} Medien mit identischem Inhalt bereits vorhanden — wiederverwendet."
        )

    def rw(text: Optional[str]) -> Optional[str]:
        return rewrite_media_urls(text, src_course_id, dst_id, fname_map)

    # ── 2. Skript-Kapitel ──
    sections_data = _as_list(_load_json_if(extracted / "script" / "sections.json", report["warnings"]))
    new_sections: list[ScriptSection] = []
    for idx in chosen_indices("script", sections_data):
        item = sections_data[idx]
        if not isinstance(item, dict):
            continue
        s = ScriptSection(
            course_id=dst_id,
            title=str(item.get("title") or f"Kapitel {idx + 1}")[:300],
            content=rw(str(item.get("content") or "")) or "",
            is_visible=bool(item.get("is_visible", True)),
            display_order=_max_display_order(session, ScriptSection, dst_id) + len(new_sections) + 1,
            summary=str(item.get("summary") or "")[:2000],
            created_by=user.id,
        )
        session.add(s)
        new_sections.append(s)
    session.commit()
    for s in new_sections:
        version_service.mark_created(session, user, "script_section", s.id)
    report["imported"]["script"] = len(new_sections)

    # ── 3. Slides ──
    decks_data = _as_list(_load_json_if(extracted / "slides" / "decks.json", report["warnings"]))
    new_decks: list[CourseMaterial] = []
    for idx in chosen_indices("slides", decks_data):
        item = decks_data[idx]
        if not isinstance(item, dict):
            continue
        m = CourseMaterial(
            course_id=dst_id,
            title=str(item.get("title") or f"Deck {idx + 1}")[:300],
            material_type=MaterialType.SLIDES,
            content=rw(str(item.get("content") or "")) or "",
            is_visible=bool(item.get("is_visible", True)),
            display_order=_max_display_order(session, CourseMaterial, dst_id) + len(new_decks) + 1,
            summary=str(item.get("summary") or "")[:2000],
            created_by=user.id,
        )
        session.add(m)
        new_decks.append(m)
    session.commit()
    for m in new_decks:
        version_service.mark_created(session, user, "slide_deck", m.id)
    report["imported"]["slides"] = len(new_decks)

    if "__theme__" in selected["slides"]:
        existing_theme = session.exec(
            select(CourseSlidesTheme).where(CourseSlidesTheme.course_id == dst_id)
        ).first()
        theme_path = extracted / "slides" / "theme.json"
        if existing_theme is not None:
            report["warnings"].append(
                "Foliendesign (Theme) übersprungen — der Kurs hat bereits ein Design."
            )
        elif theme_path.is_file():
            theme_data = _load_json_if(theme_path, report["warnings"])
            theme = (theme_data or {}).get("theme") if isinstance(theme_data, dict) else None
            if isinstance(theme, dict):
                session.add(CourseSlidesTheme(course_id=dst_id, theme=theme))
                session.commit()

    # ── 4. Quellen ──
    refs_data = _as_list(_load_json_if(extracted / "references" / "references.json", report["warnings"]))
    existing_keys = {
        r.key: r for r in session.exec(
            select(CourseReference).where(CourseReference.course_id == dst_id)
        ).all()
    }
    new_refs: list[CourseReference] = []
    for idx in chosen_indices("references", refs_data):
        item = refs_data[idx]
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()[:100]
        if not key:
            report["warnings"].append(
                f"Quelle übersprungen (kein Key): {item.get('title') or f'Quelle {idx + 1}'}"
            )
            continue
        if key in existing_keys:
            report["warnings"].append(
                f"Quelle „{key}“ übersprungen — existiert bereits im Kurs."
            )
            continue
        r = CourseReference(
            course_id=dst_id,
            key=key,
            entry_type=str(item.get("entry_type") or "misc")[:50],
            authors=item.get("authors") if isinstance(item.get("authors"), list) else [],
            title=str(item.get("title") or "")[:500],
            year=str(item.get("year") or "")[:10],
            venue=str(item.get("venue") or "")[:300],
            detail=str(item.get("detail") or "")[:200],
            address=str(item.get("address") or "")[:200],
            doi=str(item.get("doi") or "")[:200],
            url=str(item.get("url") or "")[:500],
            note=str(item.get("note") or "")[:500],
            description=str(item.get("description") or ""),
            display_order=_max_display_order(session, CourseReference, dst_id) + len(new_refs) + 1,
            created_by=user.id,
        )
        session.add(r)
        existing_keys[key] = r
        new_refs.append(r)
    session.commit()
    for r in new_refs:
        version_service.mark_created(session, user, "reference", r.id)
    report["imported"]["references"] = len(new_refs)

    # ── 5. Aufgaben ──
    tasks_data = _as_list(_load_json_if(extracted / "tasks" / "tasks.json", report["warnings"]))
    meta_raw = _load_json_if(extracted / "tasks" / "meta.json", report["warnings"])
    meta = meta_raw if isinstance(meta_raw, dict) else {}
    new_tasks: list[tuple[int, Task]] = []
    for idx in chosen_indices("tasks", tasks_data):
        item = tasks_data[idx]
        if not isinstance(item, dict):
            continue
        try:
            ttype = TaskType(str(item.get("task_type") or ""))
        except ValueError:
            report["warnings"].append(
                f"Aufgabe übersprungen (unbekannter Typ {item.get('task_type')!r}): "
                f"{item.get('title') or f'Aufgabe {idx + 1}'}"
            )
            continue

        def s(field: str, limit: int) -> Optional[str]:
            v = item.get(field)
            return str(v)[:limit] if v else None

        task = Task(
            course_id=dst_id,
            title=str(item.get("title") or f"Aufgabe {idx + 1}")[:300],
            task_type=ttype,
            description=rw(str(item.get("description") or "")) or "",
            model_solution=rw(str(item["model_solution"])) if item.get("model_solution") else None,
            max_points=int(item.get("max_points") or 0),
            max_attempts=item.get("max_attempts") if isinstance(item.get("max_attempts"), int) else None,
            deadline=item.get("deadline") or None,
            text_template=rw(str(item["text_template"])) if item.get("text_template") else None,
            is_visible=bool(item.get("is_visible", False)),
            display_order=_max_display_order(session, Task, dst_id) + len(new_tasks) + 1,
            hints_enabled=bool(item.get("hints_enabled", True)),
            auto_grading_enabled=bool(item.get("auto_grading_enabled", True)),
            workspace_timeout=int(item.get("workspace_timeout") or 900),
            workspace_cpu=float(item.get("workspace_cpu") or 2.0),
            workspace_memory=str(item.get("workspace_memory") or "4g")[:10],
            workspace_disk_quota=float(item.get("workspace_disk_quota") or 1.0),
            workspace_internet=bool(item.get("workspace_internet", False)),
            workspace_main_file=s("workspace_main_file", 200),
            workspace_engines=(
                json.dumps(item["workspace_engines"])
                if isinstance(item.get("workspace_engines"), (list, dict))
                else (str(item["workspace_engines"]) if item.get("workspace_engines") else None)
            ),
            workspace_image=s("workspace_image", 50),
            mc_data=(
                json.dumps(item["mc_data"], ensure_ascii=False)
                if isinstance(item.get("mc_data"), (dict, list))
                else (str(item["mc_data"]) if item.get("mc_data") else None)
            ),
            upload_template=s("upload_template", 500),
            upload_solution_file=s("upload_solution_file", 500),
            created_by=user.id,
        )
        session.add(task)
        new_tasks.append((idx, task))
    session.commit()

    # 5b. Task-Dateien (Workspace/Upload) + Meta-Rows
    workspace_sync_ids: list[int] = []
    for idx, task in new_tasks:
        item = tasks_data[idx]
        orig_id = item.get("id")
        if orig_id is None:
            continue
        orig_key = str(orig_id)
        if task.task_type == TaskType.WORKSPACE:
            src_dir = extracted / "tasks" / "workspace" / orig_key
            dst_dir = WORKSPACE_DIR / str(task.id)
            if src_dir.is_dir():
                shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
            else:
                report["warnings"].append(
                    f"Workspace-Dateien für „{task.title}“ fehlen im Zip."
                )
            m = meta.get(orig_key) or {}
            for f in _as_list(m.get("files")):
                if not isinstance(f, dict) or not f.get("path"):
                    continue
                session.add(TaskWorkspaceFile(
                    task_id=task.id,
                    path=str(f["path"])[:500],
                    size=int(f.get("size") or 0),
                    checksum=f.get("checksum"),
                    access=f.get("access"),
                    is_binary=bool(f.get("is_binary", False)),
                    sort_order=f.get("sort_order"),
                ))
            for f in _as_list(m.get("folders")):
                if not isinstance(f, dict) or not f.get("path"):
                    continue
                session.add(TaskWorkspaceFolder(
                    task_id=task.id, path=str(f["path"])[:500], access=f.get("access"),
                ))
            for o in _as_list(m.get("orders")):
                if not isinstance(o, dict) or not o.get("path"):
                    continue
                session.add(TaskWorkspaceOrder(
                    task_id=task.id, path=str(o["path"])[:500],
                    sort_order=int(o.get("sort_order") or 0),
                ))
            session.commit()
            workspace_sync_ids.append(task.id)
        elif task.task_type == TaskType.UPLOAD:
            src_dir = extracted / "tasks" / "uploads" / orig_key
            if src_dir.is_dir():
                shutil.copytree(
                    src_dir, UPLOAD_TASK_DIR / str(task.id), dirs_exist_ok=True
                )
            elif task.upload_template or task.upload_solution_file:
                report["warnings"].append(
                    f"Upload-Dateien für „{task.title}“ fehlen im Zip."
                )

    # 5c. Initial-Versionen (nach Dateien: Task-Snapshot erfasst Workspace-Bau)
    for idx, task in new_tasks:
        version_service.mark_created(session, user, "task", task.id)
    report["imported"]["tasks"] = len(new_tasks)

    # 5d. Image-Spec-Warnungen (existiert im Zielkurs?)
    if workspace_sync_ids:
        from services import image_spec_service
        for _, task in new_tasks:
            if task.task_type == TaskType.WORKSPACE and task.workspace_image:
                if image_spec_service.resolve_spec(
                        session, dst_id, task.workspace_image) is None:
                    report["warnings"].append(
                        f"Aufgabe „{task.title}“: Image-Spec „{task.workspace_image}“ "
                        "existiert nicht im Zielkurs — Umgebung nachträglich wählen."
                    )

    # ── 6. Nachbereitung ──
    if fname_map:
        media_service.sync_media_usages(session, dst_id)
    for tid in workspace_sync_ids:
        workspace_service.schedule_task_sync(tid)

    # Staging räumen
    shutil.rmtree(staging, ignore_errors=True)
    course_base = staging.parent
    if course_base.is_dir() and not any(course_base.iterdir()):
        course_base.rmdir()
    return report
