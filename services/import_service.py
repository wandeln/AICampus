"""
Kurs-Material-Import (Zip) — 3-stufiger LLM-Wizard (PROF/Admin).

Stufen (Status pro Stufe in CourseImport — der Status IST das Steuerungs-Flag):
  filemap     : Zip-Extraktion (mehrere Zips mergbar) + Manifest + Sidecars
                (docx/pptx/pdf → .md) + LLM-Struktur-Digests (summary + headings
                pro Text-Chunk) — die "Staging"-Phase (Dateien sind ab hier löschbar)
  media       : ausgewählte Medien in die Kurs-Medienbibliothek + LLM-Beschreibung
  references  : ausgewählte Quellen in die Kurs-Quellenbibliothek
  ref_extract : agentic LLM-Extraktion aller Quellen (Text + .bib/.bbl + \\cite-Keys)
                inkl. Deduplizierung + "wann zitieren"-Beschreibungen
  plan        : agentic Skript-Kapitel-Planner (LLM-Action-Loop) → chapter_plan
  slides_plan : agentic Folien-Planner (LLM-Action-Loop) → slides_plan
  script      : agentic Quellen-Sammlung + wortgetreue Kapitel-Konvertierung
                → ScriptSection (Markdown)
  slides      : agentic Quellen-Sammlung + Slide-Decks → CourseMaterial (SLIDES)

Stateless Runner: Pause/Resume/Cancel/Neustart laufen über denselben Code-Pfad.
Der Job liest den Stufen-Status stateless aus der DB (frische Session pro
Gate/Unit): running = weiter, paused = warten, cancelled/interrupted = stoppen.
Fertige Units werden bei Resume übersprungen (Idempotenz via file_map/media_map/
section_id/material_id in chapter_plan).

Staging: data/imports/course_{id}/{job_id}/ (upload.zip, extracted/, sidecars/)
Sicherheit: Zip-Slip/Symlink-Schutz, Größen-Limits, Sanitizer für LLM-Output,
Security-Block in allen Prompts, frische DB-Session nie über await gehalten.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlmodel import Session, select

from config import (
    IMPORT_DIR,
    IMPORT_MAX_ZIP_BYTES,
    IMPORT_MAX_SINGLE_FILE_BYTES,
    IMPORT_MAX_ENTRIES,
    IMPORT_MAX_EXTRACTED_BYTES,
    IMPORT_LLM_CONCURRENCY,
    IMPORT_FILE_CONCURRENCY,
    IMPORT_CHUNK_CHARS,
    CHAPTER_INPUT_WARN_CHARS,
    IMPORT_SLIDE_DECK_SOURCE_WARN_CHARS,
    IMPORT_READ_CHARS,
    IMPORT_READ_LINES,
    IMPORT_PLAN_MAX_ITERATIONS,
    IMPORT_PLAN_TOOL_BUDGET,
    IMPORT_GATHER_MAX_ITERATIONS,
    IMPORT_PNG_DPI,
    IMPORT_PNG_MAX_DIM,
    IMPORT_EMBED_MIN_PX,
    IMPORT_SLIDE_DECK_MAX_SLIDES_1TO1,
    IMPORT_TIMEOUT_ANALYSIS,
    IMPORT_TIMEOUT_MEDIA,
    IMPORT_TIMEOUT_PLAN,
    IMPORT_TIMEOUT_CONVERT,
    MEDIA_DIR,
)
from database.base import engine
from database.models import (
    CourseImport,
    CourseMaterial,
    CourseMedia,
    CourseReference,
    MaterialType,
    ScriptSection,
)
from services import bibtex as bib
from services import media_service
from services.llm_service import LLMService, SLIDES_MAX_TOKENS
from services.references_service import build_references_text
from services.settings_resolver import get_effective_llm_config
from services.slides_service import (
    SlideError,
    parse_slides,
    slide_count,
)

logger = logging.getLogger(__name__)

llm_service = LLMService()

# ═══════════════════════════════════════════════════════════════════
# Job-Spawn — starke Task-Referenz gegen GC
# ═══════════════════════════════════════════════════════════════════

_JOB_TASKS: set[asyncio.Task] = set()


def _job_task_done(t: asyncio.Task) -> None:
    _JOB_TASKS.discard(t)
    if not t.cancelled():
        exc = t.exception()
        if exc is not None:
            logger.error("Import-Job-Task unerwartet mit Fehler beendet: %s", exc)


def spawn_job(coro) -> asyncio.Task:
    """asyncio.create_task + starke Referenz, damit der Job nicht GC'd wird.

    Das Event-Loop hält nur Weak-References auf Tasks. Hält eine Request-
    Handler-Funktion den Task nur lokal (z. B. `_ = asyncio.create_task(...)`),
    kann der Garbage Collector den Task stillsam abräumen, während er noch
    läuft — der Job stirbt ohne Log, der Stufen-Status bleibt "running".
    """
    t = asyncio.create_task(coro)
    _JOB_TASKS.add(t)
    t.add_done_callback(_job_task_done)
    return t

# Reihenfolge der Stufen (für UI/Validierung)
IMPORT_STAGES = ("filemap", "media", "references", "ref_extract", "plan",
                 "slides_plan", "script", "slides")

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

# Endungen, die als Einzeldateien (ohne Zip-Archiv) importiert werden können —
# genau die Typen, die build_manifest sinnvoll klassifiziert.
IMPORT_LOOSE_FILE_EXTS = (
    ".pdf", ".tex", ".md", ".markdown", ".docx", ".pptx", ".odt", ".odp",
    ".bib", ".bbl", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".eps",
)

IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
TEXT_TYPES = ("tex", "markdown", "docx", "pptx", "pdf_doc")
MAX_MEDIA_PAGES = 5  # figure_pdf: max. Seiten, die als eigene Medien importiert werden


# ═══════════════════════════════════════════════════════════════════
# Pfade
# ═══════════════════════════════════════════════════════════════════

def staging_dir(course_id: int, job_id: str) -> Path:
    return IMPORT_DIR / f"course_{course_id}" / job_id


def course_staging_root(course_id: int) -> Path:
    return IMPORT_DIR / f"course_{course_id}"


# ═══════════════════════════════════════════════════════════════════
# DB-Helfer (kurze Sessions, JSON-Spalten immer NEU zuweisen!)
# ═══════════════════════════════════════════════════════════════════

def job_snapshot(import_id: int) -> Optional[dict]:
    """Einmaliger Snapshot des Imports für einen Job (keine Session über await!)."""
    with Session(engine) as s:
        imp = s.get(CourseImport, import_id)
        if not imp:
            return None
        return {
            "job_id": imp.job_id,
            "created_by": imp.created_by,
            "manifest": [dict(m) for m in (imp.manifest or [])],
            "file_map": {k: dict(v) for k, v in (imp.file_map or {}).items()},
            "media_map": {k: dict(v) for k, v in (imp.media_map or {}).items()},
            "reference_map": {k: dict(v) for k, v in (imp.reference_map or {}).items()},
            "chapter_plan": [dict(c) for c in (imp.chapter_plan or [])],
            "slides_plan": [dict(d) for d in (imp.slides_plan or [])],
            "report": dict(imp.report or {}),
        }


def set_stage_status(import_id: int, stage: str, status: str) -> None:
    with Session(engine) as s:
        imp = s.get(CourseImport, import_id)
        if not imp:
            return
        setattr(imp, f"{stage}_status", status)
        imp.updated_at = datetime.now()
        s.add(imp)
        s.commit()


def stage_status(import_id: int, stage: str) -> Optional[str]:
    with Session(engine) as s:
        imp = s.get(CourseImport, import_id)
        if not imp:
            return None
        return getattr(imp, f"{stage}_status")


def _mutate(
    import_id: int,
    fn,
):
    """Öffnet eine Session, wendet fn(imp) an und committet (best effort)."""
    try:
        with Session(engine) as s:
            imp = s.get(CourseImport, import_id)
            if not imp:
                return
            fn(imp)
            imp.updated_at = datetime.now()
            s.add(imp)
            s.commit()
    except Exception:
        logger.exception("Import %s: DB-Update fehlgeschlagen", import_id)


def set_progress(
    import_id: int,
    *,
    current: Optional[str] = None,
    stage: Optional[str] = None,
    stage_total: Optional[int] = None,
    stage_done_delta: int = 0,
    stage_failed_delta: int = 0,
    stage_reset: bool = False,
    unit_key: Optional[str] = None,
    unit_done: Optional[int] = None,
    unit_total: Optional[int] = None,
) -> None:
    def fn(imp: CourseImport):
        p = dict(imp.progress or {})
        if current is not None:
            p["current"] = current
        if stage is not None:
            stages = dict(p.get("stages") or {})
            st = dict(stages.get(stage) or {"total": 0, "done": 0, "failed": 0})
            if stage_reset:
                st["done"] = 0
                st["failed"] = 0
            if stage_total is not None:
                st["total"] = stage_total
            st["done"] = st.get("done", 0) + stage_done_delta
            st["failed"] = st.get("failed", 0) + stage_failed_delta
            stages[stage] = st
            p["stages"] = stages
        if unit_key is not None:
            units = dict(p.get("units") or {})
            un = dict(units.get(unit_key) or {})
            if unit_done is not None:
                un["done"] = unit_done
            if unit_total is not None:
                un["total"] = unit_total
            units[unit_key] = un
            p["units"] = units
        imp.progress = p

    _mutate(import_id, fn)


def report_entry_msg(entry) -> str:
    """Report-Eintrag ([ts, msg] bzw. Legacy-String) → Nachrichtentext."""
    if isinstance(entry, (list, tuple)) and len(entry) > 1:
        return str(entry[1])
    return str(entry)


def append_report(
    import_id: int,
    *,
    warnings: Optional[list[str]] = None,
    errors: Optional[list[str]] = None,
    log: Optional[list[str]] = None,
    summary: Optional[str] = None,
) -> None:
    def fn(imp: CourseImport):
        r = dict(imp.report or {})
        ts = datetime.now().strftime("%H:%M:%S")
        warns = list(r.get("warnings") or [])
        errs = list(r.get("errors") or [])
        for w in warnings or []:
            if w not in [report_entry_msg(p) for p in warns]:
                warns.append([ts, w])
        for e in errors or []:
            if e not in [report_entry_msg(p) for p in errs]:
                errs.append([ts, e])
        r["warnings"] = warns[:200]
        r["errors"] = errs[:200]
        if log:
            logs = list(r.get("log") or [])
            logs.extend([ts, m] for m in log)
            r["log"] = logs[-300:]
        if summary is not None:
            r["summary"] = summary
        imp.report = r

    _mutate(import_id, fn)


def set_filemap_entry(import_id: int, path: str, entry: dict) -> None:
    def fn(imp: CourseImport):
        fm = dict(imp.file_map or {})
        fm[path] = entry
        imp.file_map = fm

    _mutate(import_id, fn)


def get_filemap_entry(import_id: int, path: str) -> dict:
    with Session(engine) as s:
        imp = s.get(CourseImport, import_id)
        if not imp:
            return {}
        return dict((imp.file_map or {}).get(path) or {})


def set_filemap_chunk(import_id: int, path: str, idx: int, chunk: dict) -> None:
    def fn(imp: CourseImport):
        fm = dict(imp.file_map or {})
        entry = dict(fm.get(path) or {"status": "running"})
        chunks = [dict(c) for c in (entry.get("chunks") or [])]
        while len(chunks) <= idx:
            chunks.append({"start_line": 0, "end_line": 0})
        chunks[idx] = chunk
        entry["chunks"] = chunks
        fm[path] = entry
        imp.file_map = fm

    _mutate(import_id, fn)


def set_manifest(import_id: int, manifest: list[dict]) -> None:
    def fn(imp: CourseImport):
        imp.manifest = manifest

    _mutate(import_id, fn)


def set_media_map_entry(import_id: int, path: str, entry: dict) -> None:
    def fn(imp: CourseImport):
        mm = dict(imp.media_map or {})
        mm[path] = entry
        imp.media_map = mm

    _mutate(import_id, fn)


def get_media_map_entry(import_id: int, path: str) -> dict:
    with Session(engine) as s:
        imp = s.get(CourseImport, import_id)
        if not imp:
            return {}
        return dict((imp.media_map or {}).get(path) or {})


def set_reference_map_entry(import_id: int, key: str, patch: dict) -> None:
    """Quellen-Map-Eintrag ergänzen (MERGE — Bib-Daten bleiben erhalten)."""
    def fn(imp: CourseImport):
        rm = dict(imp.reference_map or {})
        cur = dict(rm.get(key) or {})
        cur.update(patch)
        rm[key] = cur
        imp.reference_map = rm

    _mutate(import_id, fn)


def set_chapter_plan(import_id: int, chapters: list[dict]) -> None:
    def fn(imp: CourseImport):
        imp.chapter_plan = chapters

    _mutate(import_id, fn)


def update_plan_chapter(import_id: int, idx: int, patch: dict) -> None:
    def fn(imp: CourseImport):
        plan = [dict(c) for c in (imp.chapter_plan or [])]
        if 0 <= idx < len(plan):
            plan[idx].update(patch)
            imp.chapter_plan = plan

    _mutate(import_id, fn)


def get_slides_plan(imp: CourseImport) -> list[dict]:
    """Folien-Plan; legacy (noch kein slides_plan): 1:1-Ableitung aus dem Skript-Plan."""
    if imp.slides_plan:
        return [dict(d) for d in imp.slides_plan]
    return _derive_slides_plan(imp.chapter_plan or [])


def set_slides_plan(import_id: int, decks: list[dict]) -> None:
    def fn(imp: CourseImport):
        imp.slides_plan = decks

    _mutate(import_id, fn)


def update_slides_deck(import_id: int, idx: int, patch: dict) -> None:
    def fn(imp: CourseImport):
        plan = [dict(d) for d in (imp.slides_plan or [])]
        if 0 <= idx < len(plan):
            plan[idx].update(patch)
            imp.slides_plan = plan

    _mutate(import_id, fn)


def get_llm_config(course_id: int, timeout: int) -> dict:
    """Effektive LLM-Config (1× pro Job laden) + Feature-Timeout.

    Nutzt die Public-Config (wenn konfiguriert): Import-Jobs dürfen teure
    LLM-/Vision-Calls nicht auf der lokalen GPU ausführen — das lokale Modell
    ist für das Grading von Studentendaten da und würde sonst verdrängt werden.
    """
    with Session(engine) as s:
        cfg = get_effective_llm_config(s, course_id)
    cfg["timeout"] = timeout
    if cfg.get("api_url_public"):
        cfg["api_url"] = cfg["api_url_public"]
        if cfg.get("api_key_public"):
            cfg["api_key"] = cfg["api_key_public"]
        if cfg.get("model_public"):
            cfg["model"] = cfg["model_public"]
    return cfg


# ═══════════════════════════════════════════════════════════════════
# Gate + Unit-Loop (Pause/Resume/Cancel stateless über DB)
# ═══════════════════════════════════════════════════════════════════

async def gate(import_id: int, stage: str) -> bool:
    """Wartet, solange die Stufe gepausht ist.

    Returns: True = weiter (running), False = stoppen
    (cancelled/interrupted/ gelöscht / andere Terminal-Status).
    """
    while True:
        status = stage_status(import_id, stage)
        if status == "running":
            return True
        if status is None or status in (
            "pending", "skipped", "done", "error", "cancelled", "interrupted",
        ):
            return False
        # paused → warten
        await asyncio.sleep(1.0)


async def run_units(
    import_id: int,
    stage: str,
    units: list[dict],
    process,
    *,
    concurrency: int = 1,
    label: Optional[callable] = None,
) -> dict:
    """Führt Units aus (parallelität über Semaphore gesteuert).

    process(unit) ist async; per-Unit-Fehler werden isoliert (von process()
    selbst protokolliert) — hier nur unerwartete Exceptions auffangen.
    Returns: {"stopped": bool, "crashed": [unit_labels]}
    """
    def unit_label(u: dict) -> str:
        return label(u) if label else str(u.get("path") or u.get("title") or u)

    crashed: list[str] = []
    stopped = {"v": False}

    async def worker(u: dict):
        if stopped["v"]:
            return
        if not await gate(import_id, stage):
            stopped["v"] = True
            return
        try:
            await process(u)
        except Exception as e:
            logger.exception("Import %s: Unit %s fehlgeschlagen", import_id, unit_label(u))
            crashed.append(unit_label(u))

    if concurrency <= 1:
        for u in units:
            await worker(u)
            if stopped["v"]:
                break
    else:
        sem = asyncio.Semaphore(concurrency)

        async def guarded(u: dict):
            async with sem:
                await worker(u)

        await asyncio.gather(*(guarded(u) for u in units))

    return {"stopped": stopped["v"], "crashed": crashed}


# ═══════════════════════════════════════════════════════════════════
# Zip-Extraktion (sicher) + Manifest
# ═══════════════════════════════════════════════════════════════════

def safe_extract_zip(
    zip_path: Path,
    extracted: Path,
    skip: Optional[set[str]] = None,
    existing: Optional[set[str]] = None,
) -> tuple[list[dict], list[str]]:
    """Zip sicher extrahieren (Zip-Slip/Symlink/Größen-Schutz).

    Merge-Support (weitere Zips in denselben Staging):
    - skip:     Pfade, die der Nutzer aus der Stage gelöscht hat (nie wieder extrahieren)
    - existing: bereits extrahierte Pfade; bei Identität (SHA256) wird übersprungen,
                bei Kollision umbenannt ("name (2).ext")

    Returns: ([{path, size}], warnings). Raises ValueError bei harten Limits.
    """
    warnings: list[str] = []
    entries: list[dict] = []
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > IMPORT_MAX_ENTRIES:
            raise ValueError(f"Zip enthält zu viele Dateien (max. {IMPORT_MAX_ENTRIES}).")
        for info in infos:
            name = info.filename
            if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                warnings.append(f"Eintrag übersprungen (ungültiger Pfad): {name}")
                continue
            rel = Path(name)
            target = (extracted / rel).resolve()
            if not target.is_relative_to(extracted.resolve()):
                warnings.append(f"Eintrag übersprungen (Pfad außerhalb): {name}")
                continue
            # Symlinks (und andere Sockets/FIFOs) aus external_attr erkennen
            mode = (info.external_attr >> 28) & 0o170000
            if mode == 0o120000 or mode == 0o140000 or mode == 0o010000:
                warnings.append(f"Eintrag übersprungen (Symlink/Special): {name}")
                continue
            if info.file_size > IMPORT_MAX_SINGLE_FILE_BYTES:
                warnings.append(
                    f"Datei übersprungen (zu groß, max. {IMPORT_MAX_SINGLE_FILE_BYTES // (1024*1024)} MB): {name}"
                )
                continue
            if skip and name in skip:
                continue  # von der Stage gelöscht → nicht wiederholen
            total += info.file_size
            if total > IMPORT_MAX_EXTRACTED_BYTES:
                raise ValueError("Zip entpackt zu viel Daten (max. 1 GB).")
            # Merge: vorhandene Datei? identisch → überspringen, sonst umbenennen
            final_rel = rel.as_posix()
            if existing is not None:
                if (extracted / final_rel).is_file():
                    with zf.open(info) as src:
                        h = hashlib.sha256()
                        for block in iter(lambda: src.read(1024 * 1024), b""):
                            h.update(block)
                    if h.hexdigest() == _sha256_file(extracted / final_rel):
                        entries.append({"path": final_rel, "size": info.file_size})
                        continue
                    cand, k = rel, 2
                    while (extracted / cand).exists():
                        cand = Path(str(cand.parent) + f"/{cand.stem} ({k}){cand.suffix}" if str(cand.parent) != "." else f"{cand.stem} ({k}){cand.suffix}")
                        k += 1
                    final_rel = cand.as_posix()
                    warnings.append(f"Dateikollision — als „{final_rel}“ extrahiert: {name}")
                    existing.add(final_rel)
            target = (extracted / final_rel).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            entries.append({"path": final_rel, "size": info.file_size})
    return entries, warnings


def store_loose_files(
    staging: Path,
    items: list[tuple[str, Any]],
) -> tuple[list[dict], list[str]]:
    """Einzeldatei-Uploads (ohne Zip) → extracted/ mit denselben Merge-
    Semantiken wie die Zip-Extraktion: identischer Inhalt (SHA256) wird
    übersprungen, Namenskollisionen werden umbenannt ("name (2).ext").

    items: [(Dateiname, binärer Stream)]. Limits: max.
    IMPORT_MAX_SINGLE_FILE_BYTES pro Datei, IMPORT_MAX_ZIP_BYTES gesamt.

    Returns: ([{path, size}], warnings). Raises ValueError bei
    ungültigem Namen/Dateityp oder überschrittenen Limits (Temp-Dateien
    werden dabei bereinigt).
    """
    extracted = staging / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)
    added: list[dict] = []
    warnings: list[str] = []
    total = 0
    pending: list[Path] = []  # Temp-Dateien (bereinigt in finally)
    try:
        for orig_name, stream in items:
            name = Path(orig_name or "").name  # Pfade aus dem Namen herausschneiden
            if not name or name.startswith(".") or len(name) > 300:
                raise ValueError(f"Ungültiger Dateiname: {orig_name!r}")
            ext = Path(name).suffix.lower()
            if ext == ".zip":
                raise ValueError(f"Zip-Dateien bitte über den Zip-Upload senden: {name}")
            if ext not in IMPORT_LOOSE_FILE_EXTS:
                raise ValueError(f"Dateityp wird nicht unterstützt: {name}")

            tmp = extracted / f".incoming-{uuid.uuid4().hex}"
            pending.append(tmp)
            size = 0
            with open(tmp, "wb") as out:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > IMPORT_MAX_SINGLE_FILE_BYTES:
                        raise ValueError(
                            f"Datei zu groß (max. {IMPORT_MAX_SINGLE_FILE_BYTES // (1024 * 1024)} MB): {name}"
                        )
                    total += size
                    if total > IMPORT_MAX_ZIP_BYTES:
                        raise ValueError(
                            f"Upload insgesamt zu groß (max. {IMPORT_MAX_ZIP_BYTES // (1024 * 1024)} MB)."
                        )
                    out.write(block)

            # Merge: vorhandene Datei? identisch → überspringen, sonst umbenennen
            final = Path(name)
            if (extracted / final).is_file():
                if _sha256_file(tmp) == _sha256_file(extracted / final):
                    warnings.append(f"Datei bereits vorhanden, übersprungen: {name}")
                    continue
                cand, k = final, 2
                while (extracted / cand).exists():
                    cand = Path(f"{cand.stem} ({k}){cand.suffix}")
                    k += 1
                final = cand
                warnings.append(f"Dateikollision — als „{final.as_posix()}“ übernommen: {name}")
            target = extracted / final
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.rename(target)
            added.append({"path": final.as_posix(), "size": size})
    finally:
        for t in pending:
            t.unlink(missing_ok=True)
    return added, warnings


_INC_GRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")


def _pdf_info(path: Path) -> tuple[int, int]:
    """(Seitenanzahl, Textzeichen [nur erste 5 Seiten]) eines PDFs."""
    import fitz  # pymupdf

    doc = fitz.open(str(path))
    try:
        pages = len(doc)
        chars = 0
        for page in doc[:5]:
            chars += len(page.get_text())
        return pages, chars
    finally:
        doc.close()


def _describe_image_position(rect, page_rect) -> str:
    """Lage-Beschreibung eines Bild-Rects relativ zur Seite (Input für M3-Prompt)."""
    try:
        if page_rect is None or page_rect.width <= 0 or page_rect.height <= 0:
            return "unbekannt"
        if (rect.width * rect.height) / (page_rect.width * page_rect.height) >= 0.8:
            return "ganze Seite"
        cx = (rect.x0 + rect.x1) / 2 / page_rect.width
        cy = (rect.y0 + rect.y1) / 2 / page_rect.height
        col = "links" if cx < 1 / 3 else ("rechts" if cx > 2 / 3 else "zentral")
        row = "oben" if cy < 1 / 3 else ("unten" if cy > 2 / 3 else "mittig")
        return f"{row} {col}"
    except Exception:
        return "unbekannt"


def _analyze_pdf_doc(path: Path, need_text: bool) -> dict:
    """Einmaliger Vollscan eines PDF-Dokuments (M1/M2, ohne LLM).

    need_text=True speichert den Seitentext (für die Sidecar); beim Resume
    (Sidecar existiert) läuft der Scan ohne Textspeicherung.

    Returns: {page_count, no_text_pages, pages: [{no, text, images: [{idx,
    xref, width_px, height_px, position}]}]} — images pro Seite in visueller
    Reihenfolge (y, dann x; idx 1-basiert).
    """
    import fitz  # pymupdf

    doc = fitz.open(str(path))
    try:
        no_text_pages = 0
        pages_out: list[dict] = []
        for i, page in enumerate(doc, 1):
            text = page.get_text().strip()
            if not text:
                no_text_pages += 1
            prect = page.rect
            imgs: list[dict] = []
            seen: set[int] = set()
            for im in page.get_images(full=True):
                xref = int(im[0])
                if xref in seen:
                    continue
                seen.add(xref)
                rects = page.get_image_rects(xref)
                if not rects:
                    continue
                rect = max(rects, key=lambda r: r.width * r.height)
                imgs.append({
                    "xref": xref,
                    "width_px": int(im[2] or 0),
                    "height_px": int(im[3] or 0),
                    "position": _describe_image_position(rect, prect),
                    "sort": (float(rect.y0), float(rect.x0)),
                })
            imgs.sort(key=lambda d: d["sort"])
            for n, d in enumerate(imgs, 1):
                d["idx"] = n
            pages_out.append({"no": i, "text": text if need_text else None, "images": imgs})
        return {"page_count": len(doc), "no_text_pages": no_text_pages, "pages": pages_out}
    finally:
        doc.close()


def _classify_entry(ext: str, pages: Optional[int], text_chars: Optional[int], included: bool) -> str:
    if ext in IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        if included or (pages is not None and pages <= 4 and (text_chars or 0) < 200):
            return "figure_pdf"
        return "pdf_doc"
    if ext == ".tex":
        return "tex"
    if ext in (".md", ".markdown"):
        return "markdown"
    if ext == ".docx":
        return "docx"
    if ext == ".pptx":
        return "pptx"
    return "other"


def build_manifest(staging: Path, entries: list[dict]) -> tuple[list[dict], list[str]]:
    """Manifest bauen: Klassifizierung + Sidecar-Konvertierung + line_count.

    read_path ist relativ zum Staging-Root (extracted/<rel> oder sidecars/<rel>.md).
    """
    warnings: list[str] = []
    extracted = staging / "extracted"
    sidecars = staging / "sidecars"

    # 1) includegraphics-Pfade aus allen .tex-Dateien sammeln (figure_pdf-Erkennung)
    included: set[str] = set()
    for e in entries:
        if e["path"].endswith(".tex"):
            p = extracted / e["path"]
            if p.is_file():
                try:
                    tex = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for ref in _INC_GRAPHICS_RE.findall(tex):
                    included.add(ref.strip())
                    base = ref.strip().rsplit("/", 1)[-1]
                    included.add(base)

    def _is_included(path: str) -> bool:
        base = path.rsplit("/", 1)[-1]
        return path in included or base in included

    manifest: list[dict] = []
    for e in entries:
        path = e["path"]
        p = extracted / path
        ext = p.suffix.lower()
        m: dict[str, Any] = {"path": path, "size": e["size"], "type": "other"}
        read_path: Optional[str] = None

        if ext in IMAGE_EXTS:
            m["type"] = "image"
        elif ext == ".svg":
            m["type"] = "svg"
        elif ext == ".eps":
            m["type"] = "eps"
        elif ext == ".pdf":
            try:
                pages, chars = _pdf_info(p)
            except Exception as exc:  # kaputtes PDF
                m["type"] = "figure_pdf" if _is_included(path) else "pdf_doc"
                m["error"] = f"PDF nicht lesbar: {exc}"
                warnings.append(f"PDF nicht lesbar: {path} ({exc})")
                manifest.append(m)
                continue
            m["pages"] = pages
            m["type"] = _classify_entry(ext, pages, chars, _is_included(path))
            if m["type"] == "pdf_doc":
                read_path = f"sidecars/{path}.md"
                sp = sidecars / f"{path}.md"
                need_text = not sp.is_file()
                # M1/M2: Vollscan — Textschicht-Zählung (UI) + eingebettete
                # Bilder (Medien-Kandidaten + M3-Triage-Input)
                analysis: Optional[dict] = None
                try:
                    analysis = _analyze_pdf_doc(p, need_text)
                except Exception as exc:
                    warnings.append(f"PDF-Analyse fehlgeschlagen: {path} ({exc})")
                if analysis:
                    m["no_text_pages"] = analysis["no_text_pages"]
                unique_paths: list[str] = []
                img_pages: list[dict] = []
                if analysis:
                    try:
                        unique_paths, img_pages = _extract_pdf_media(p, path, analysis, extracted)
                    except Exception as exc:
                        warnings.append(f"PDF-Bild-Extraktion fehlgeschlagen: {path} ({exc})")
                    if img_pages:
                        m["pdf_images"] = img_pages
                        m["embedded_images"] = len(unique_paths)
                if not sp.is_file():
                    try:
                        sp.parent.mkdir(parents=True, exist_ok=True)
                        if analysis:
                            page_markers = {
                                pg["seite"]: [im["pfad"] for im in pg["images"]]
                                for pg in img_pages
                            }
                            sp.write_text(_pdf_sidecar_md(analysis, page_markers), encoding="utf-8")
                        else:
                            sp.write_text(_pdf_to_md(p), encoding="utf-8")
                    except Exception as exc:
                        m["type"] = "other"
                        read_path = None
                        warnings.append(f"PDF-Konvertierung fehlgeschlagen: {path} ({exc})")
                for mp in unique_paths:
                    manifest.append({
                        "path": mp,
                        "size": int((extracted / mp).stat().st_size) if (extracted / mp).is_file() else 0,
                        "type": "image",
                        # wird von der M3-Triage (keep/latex/noise) aktualisiert
                        "triage": "unbekannt",
                    })
        elif ext == ".tex":
            m["type"] = "tex"
            m["read_path"] = f"extracted/{path}"
            read_path = m["read_path"]
            try:
                if "\\documentclass" in p.read_text(encoding="utf-8", errors="replace"):
                    m["main_tex"] = True
            except OSError:
                pass
        elif ext in (".bib", ".bbl"):
            # Quellen-Bibliothek: deterministisch geparst (Quellen-Detektion),
            # kein LLM-Digest nötig → bewusst ohne read_path.
            m["type"] = "bib"
        elif ext in (".md", ".markdown"):
            m["type"] = "markdown"
            m["read_path"] = f"extracted/{path}"
            read_path = m["read_path"]
        elif ext in (".docx", ".pptx", ".odt", ".odp"):
            m["type"] = ext.lstrip(".")
            read_path = f"sidecars/{path}.md"
            sp = sidecars / f"{path}.md"
            if not sp.is_file():
                try:
                    sp.parent.mkdir(parents=True, exist_ok=True)
                    media_dir = f"{path}.media"
                    if ext == ".docx":
                        text = _docx_to_md(p, media_dir)
                    elif ext == ".pptx":
                        text = _pptx_to_md(p, media_dir)
                    elif ext == ".odt":
                        text = _odt_to_md(p)
                    else:
                        text = _odp_to_md(p)
                    sp.write_text(text, encoding="utf-8")
                except Exception as exc:
                    m["type"] = "other"
                    read_path = None
                    warnings.append(f"{ext}-Konvertierung fehlgeschlagen: {path} ({exc})")
            # Eingebettete Office-Medien extrahieren: eigene Manifest-Einträge
            # (Pfad = <office>.media/<Datei>), importierbar in der Medien-Stufe;
            # die Sidecar-Bild-Marker referenzieren exakt diese Pfade.
            for base in _extract_office_media(p, extracted / f"{path}.media", _OFFICE_MEDIA_PREFIXES[ext]):
                mp = f"{path}.media/{base}"
                mp_ext = Path(base).suffix.lower()
                manifest.append({
                    "path": mp,
                    "size": int((extracted / mp).stat().st_size) if (extracted / mp).is_file() else 0,
                    "type": "image" if mp_ext in IMAGE_EXTS else ("svg" if mp_ext == ".svg" else ("eps" if mp_ext == ".eps" else "other")),
                })
        else:
            m["type"] = "other"
            warnings.append(f"Dateityp wird ignoriert (kein unterstütztes Format): {path}")

        if read_path:
            rp = staging / read_path
            if rp.is_file():
                m["read_path"] = read_path
                try:
                    m["line_count"] = len(rp.read_text(encoding="utf-8", errors="replace").splitlines())
                except OSError:
                    m["line_count"] = 0
            else:
                m.pop("read_path", None)
        manifest.append(m)
    return manifest, warnings


def _pdf_to_md(path: Path) -> str:
    import fitz  # pymupdf

    doc = fitz.open(str(path))
    try:
        out = []
        for i, page in enumerate(doc, 1):
            out.append(f"%% Seite {i} %%")
            out.append(page.get_text().strip())
        return "\n\n".join(out)
    finally:
        doc.close()


def _normalize_pdf_image(data: bytes, ext: str, dest: Path) -> Optional[Path]:
    """Extrahiertes PDF-Bild nach dest schreiben; exotische Formate/
    Colorspaces (z.B. CMYK-JPEG, JBig2, 16-Bit) per PIL nach PNG konvertieren.

    Returns: geschriebener Pfad (ggf. andere Extension) oder None (undekodierbar).
    """
    import io
    from PIL import Image

    ext = ext.lower()
    try:
        im = Image.open(io.BytesIO(data))
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif") and im.mode in ("RGB", "RGBA", "L", "LA", "P"):
            dest.write_bytes(data)
            return dest
        dest = dest.with_suffix(".png")
        im.convert("RGB").save(dest, format="PNG")
        return dest
    except Exception:
        return None


def _extract_pdf_media(
    src: Path, src_rel: str, analysis: dict, extracted: Path
) -> tuple[list[str], list[dict]]:
    """Eingebettete PDF-Bilder nach extracted/<src_rel>.media/ extrahieren
    (idempotent, M2). Deterministische Namen img-<seite>-<xref>.<ext>
    (seite = Erstvorkommen); Dedup per Content-Hash (wiederkehrende
    Logos/Hintergründe landen so nur einmal). Filter: max. Kante ≥
    IMPORT_EMBED_MIN_PX. Fullpage-Bilder bleiben (kein Inhalt-Filter, D2).

    Returns: (eindeutige Medien-Pfade für das Manifest, Seiten-Liste
    [{seite, images: [{idx, xref, pfad, position, width_px, height_px}]}]
    — nur bestandene Bilder; Input für M3-Triage + Sidecar-Marker).
    """
    import fitz  # pymupdf

    media_dir = extracted / f"{src_rel}.media"
    seen_hash: dict[str, str] = {}
    unique_paths: list[str] = []
    img_pages: list[dict] = []
    doc = fitz.open(str(src))
    try:
        for pg in analysis["pages"]:
            if not pg["images"]:
                continue
            page_imgs: list[dict] = []
            for im in pg["images"]:
                if max(im["width_px"], im["height_px"]) < IMPORT_EMBED_MIN_PX:
                    continue
                try:
                    info = doc.extract_image(im["xref"])
                except Exception:
                    continue
                data = info.get("image") or b""
                if not data:
                    continue
                h = hashlib.sha256(data).hexdigest()
                fname = seen_hash.get(h)
                if fname is None:
                    media_dir.mkdir(parents=True, exist_ok=True)
                    cand = media_dir / f"img-{pg['no']}-{im['xref']}.{info.get('ext') or 'bin'}"
                    if cand.is_file():
                        fname = cand.name
                    else:
                        saved = _normalize_pdf_image(data, info.get("ext") or "", cand)
                        if saved is None:
                            continue
                        fname = saved.name
                    seen_hash[h] = fname
                    unique_paths.append(f"{src_rel}.media/{fname}")
                page_imgs.append({
                    "idx": im["idx"],
                    "xref": im["xref"],
                    "pfad": f"{src_rel}.media/{fname}",
                    "position": im["position"],
                    "width_px": im["width_px"],
                    "height_px": im["height_px"],
                })
            if page_imgs:
                img_pages.append({"seite": pg["no"], "images": page_imgs})
        return unique_paths, img_pages
    finally:
        doc.close()


def _pdf_sidecar_md(analysis: dict, page_markers: dict[int, list[str]]) -> str:
    """Markdown-Sidecar aus der Textschicht + Bild-Markern in visueller
    Reihenfolge (M2-Fallback ohne Transkription; M3 überschreibt pro Seite)."""
    out: list[str] = []
    for pg in analysis["pages"]:
        marks = page_markers.get(pg["no"]) or []
        body = pg.get("text") or ""
        if marks:
            markers = "\n".join(f"![Bild]({mp})" for mp in marks)
            body = f"{body}\n\n{markers}" if body else markers
        out.append(f"%% Seite {pg['no']} %%")
        out.append(body)
    return "\n\n".join(out)


def _docx_para_images(para: "Paragraph", doc: "Document") -> list[str]:
    """Eingebettete Bild-Basenames eines docx-Paragraphs (in Reihenfolge)."""
    from docx.oxml.ns import qn

    names: list[str] = []
    for blip in para._p.findall(".//" + qn("a:blip")):
        rid = blip.get(qn("r:embed"))
        if not rid:
            continue
        part = doc.part.related_parts.get(rid)
        if part is None:
            continue
        base = str(getattr(part, "partname", "") or "").rsplit("/", 1)[-1]
        if base and base not in names:
            names.append(base)
    return names


def _docx_to_md(path: Path, media_dir: str = "") -> str:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from docx.oxml.ns import qn

    doc = Document(str(path))
    out: list[str] = []
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            para = Paragraph(child, doc)
            text = para.text.strip()
            if text:
                style = (para.style.name or "") if para.style is not None else ""
                sl = style.lower()
                if sl.startswith("heading"):
                    level = re.sub(r"\D", "", sl)
                    hashes = "#" * min(int(level or 2) + 1, 6)
                    out.append(f"{hashes} {text}")
                elif sl.startswith("title"):
                    out.append(f"# {text}")
                elif sl.startswith("list bullet") or sl.startswith("list paragraph"):
                    out.append(f"- {text}")
                elif sl.startswith("list number"):
                    out.append(f"1. {text}")
                else:
                    out.append(text)
            # Bild-Marker: extrahierte Office-Medien am Dokumentort verankern
            # (Pfad = Manifest-Pfad der extrahierten Datei)
            if media_dir:
                for base in _docx_para_images(para, doc):
                    out.append(f"![Bild]({media_dir}/{base})")
        elif child.tag == qn("w:tbl"):
            table = Table(child, doc)
            rows = []
            for row in table.rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                rows.append("| " + " | ".join(cells) + " |")
            if len(rows) >= 2:
                header = rows[0]
                n_cols = header.count("|") - 1
                rows.insert(1, "| " + " | ".join(["---"] * max(1, n_cols)) + " |")
            out.extend(rows)
            out.append("")
    return "\n".join(out)


def _pptx_slide_images(slide) -> list[str]:
    """Eingebettete Bild-Basenames einer pptx-Folie (via Slide-Part-Relationships)."""
    names: list[str] = []
    for rel in slide.part.rels.values():
        if "image" not in rel.reltype:
            continue
        base = str(getattr(rel.target_part, "partname", "") or "").rsplit("/", 1)[-1]
        if base and base not in names:
            names.append(base)
    return names


def _pptx_to_md(path: Path, media_dir: str = "") -> str:
    from pptx import Presentation

    prs = Presentation(str(path))
    out: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        out.append(f"%% Folie {i} %%")
        texts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                for para in shape.text_frame.paragraphs:
                    t = "".join(r.text for r in para.runs).strip()
                    if t:
                        texts.append(t)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    line = " | ".join(cells).strip(" |")
                    if line.strip():
                        texts.append(line)
        out.extend(texts)
        # Bild-Marker: extrahierte Office-Medien am Folienort verankern
        # (Pfad = Manifest-Pfad der extrahierten Datei)
        if media_dir:
            for base in _pptx_slide_images(slide):
                out.append(f"![Bild]({media_dir}/{base})")
        try:
            if slide.has_notes_slide:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
                if notes:
                    out.append(f"(Sprechernotiz: {notes.replace(chr(10), ' ')})")
        except Exception:
            pass
        out.append("")
    return "\n".join(out)


_ODT_TEXT_P = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p"


def _odt_to_md(path: Path) -> str:
    """Best-effort-Text aus einer odt-Datei (content.xml, Absätze; ohne Bild-Marker)."""
    import xml.etree.ElementTree as ET
    import zipfile

    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("content.xml"))
    out: list[str] = []
    for p in root.iter(_ODT_TEXT_P):
        t = "".join(p.itertext()).strip()
        if t:
            out.append(t)
    return "\n".join(out)


def _odp_to_md(path: Path) -> str:
    """Best-effort-Text aus einer odp-Datei (Folien-Titel/Texte; ohne Bild-Marker)."""
    import xml.etree.ElementTree as ET
    import zipfile

    with zipfile.ZipFile(path) as z:
        slide_names = sorted(
            (n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
            key=lambda n: int(re.search(r"slide(\d+)\.xml$", n).group(1)),
        )
        out: list[str] = []
        for n in slide_names:
            num = int(re.search(r"slide(\d+)\.xml$", n).group(1))
            out.append(f"%% Folie {num} %%")
            root = ET.fromstring(z.read(n))
            for el in root.iter():
                if el.tag.endswith("}p"):
                    t = "".join(el.itertext()).strip()
                    if t:
                        out.append(t)
            out.append("")
    return "\n".join(out)


_OFFICE_MEDIA_PREFIXES = {
    ".docx": ("word/media/",),
    ".pptx": ("ppt/media/",),
    ".odt": ("Pictures/", "Object/"),
    ".odp": ("Pictures/", "Object/"),
}


def _extract_office_media(src: Path, dest_dir: Path, prefixes: tuple[str, ...]) -> list[str]:
    """Eingebettete Medien einer Office-Zip (docx/pptx/odt/odp) nach dest_dir
    extrahieren. Returns: extrahierte Basenames."""
    import zipfile

    out: list[str] = []
    try:
        with zipfile.ZipFile(src) as z:
            for name in z.namelist():
                if not any(name.startswith(pref) for pref in prefixes):
                    continue
                base = name.rsplit("/", 1)[-1]
                if not base or base in out or base.startswith("~$"):
                    continue
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / base
                if not dest.is_file():
                    with z.open(name) as fsrc:
                        dest.write_bytes(fsrc.read())
                out.append(base)
    except Exception:
        pass
    return out


# ═══════════════════════════════════════════════════════════════════
# Chunking + Digest-Verifikation
# ═══════════════════════════════════════════════════════════════════

_HEADING_LINE_RE = re.compile(
    r"^\s*(#{1,6}\s|\\(?:part|chapter|section|subsection|subsubsection|paragraph|title)\b|%%\s*(?:Folie|Seite)\s*\d+\s*%%)"
)


def chunk_lines(lines: list[str], max_chars: int) -> list[tuple[int, int]]:
    """Teilt Zeilen in Chunks ≤ max_chars (weich) ein.

    Schnittpunkte: bevorzugt Heading-Zeilen, sonst Leerzeilen (1-basiert, inclusive).
    """
    if not lines:
        return []
    chunks: list[tuple[int, int]] = []
    start = 1
    cur = 0
    last_blank = 1
    last_heading = 1
    for i, line in enumerate(lines, 1):
        l = len(line) + 1
        if cur + l > max_chars and i > start:
            # Heading-Schnitt nur, wenn nicht zu weit zurück liegt
            if i - last_heading <= max(10, max_chars // 4000) and last_heading > start:
                cut = last_heading
            else:
                cut = last_blank if last_blank > start else i
            chunks.append((start, cut - 1))
            start = cut
            cur = 0
        cur += l
        if not line.strip():
            last_blank = i + 1
        if _HEADING_LINE_RE.match(line):
            last_heading = i
    chunks.append((start, len(lines)))
    return [c for c in chunks if c[1] >= c[0]]


def _norm_ws(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().casefold()


def _heading_core(s: Any) -> str:
    r"""Überschrift auf ihren reinen Text reduzieren (##/\section{}/%% Folie N %%)."""
    t = str(s or "").strip()
    m = re.match(r"^#{1,6}\s*(.*)$", t)
    if m:
        t = m.group(1)
    m = re.match(r"^\\(?:part|chapter|section|subsection|subsubsection|paragraph)\s*(?:\[[^\]]*\])?\{(.*)\}", t)
    if m:
        t = m.group(1)
    m = re.match(r"^%%\s*(?:Folie|Seite)\s*\d+\s*%%\s*(.*)$", t)
    if m:
        t = m.group(1) or t
    return _norm_ws(t)


def verify_headings(
    headings: Any, lines: list[str], start: int, end: int
) -> list[dict]:
    """Verifiziert LLM-Headings gegen den Chunk-Text (fuzzy, ±15 Zeilen).

    Unverifizierbare Headings werden entfernt. Cap: 60 pro Chunk.
    """
    if not isinstance(headings, list):
        return []
    lo, hi = max(1, start), min(end, len(lines))
    seg = [(n, _norm_ws(lines[n - 1]), _heading_core(lines[n - 1])) for n in range(lo, hi + 1)]
    verified: list[dict] = []
    for h in headings:
        if not isinstance(h, dict):
            continue
        text = str(h.get("text") or "").strip()
        if not text:
            continue
        level = h.get("level")
        level = int(level) if isinstance(level, (int, float)) and 1 <= int(level) <= 6 else 2
        t_norm, t_core = _norm_ws(text), _heading_core(text)
        target: Optional[int] = None
        claimed = h.get("line")
        claimed = int(claimed) if isinstance(claimed, (int, float)) else None
        if claimed is not None and lo <= claimed <= hi:
            for n, ln, lc in seg:
                if abs(n - claimed) <= 15 and (ln == t_norm or lc == t_core or (len(t_core) > 3 and t_core in ln)):
                    target = n
                    break
        if target is None and len(t_core) > 3:
            for n, ln, lc in seg:
                if ln == t_norm or lc == t_core or t_core in ln:
                    target = n
                    break
        if target is None:
            continue  # nicht im Chunk nachweisbar → verwerfen
        verified.append({"text": text[:200], "line": target, "level": level})
        if len(verified) >= 60:
            break
    return verified


# ═══════════════════════════════════════════════════════════════════
# STUFE 1: filemap (Extraktion + Manifest + Sidecars + LLM-Digests)
# ═══════════════════════════════════════════════════════════════════

def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _read_slice(p: Path, start: int, end: int) -> str:
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    s = max(1, min(start, len(lines) or 1))
    e = min(end, len(lines))
    return "\n".join(lines[s - 1 : e])


async def run_filemap_job(course_id: int, import_id: int) -> None:
    """Stufe 1 (Staging): alle Zips extrahieren (Merge), Manifest + Sidecars bauen,
    LLM-Digests für neue/geänderte Text-Dateien erstellen (idempotent)."""
    logger.info("Import %s (Kurs %s): filemap gestartet", import_id, course_id)
    try:
        set_stage_status(import_id, "filemap", "running")
        set_progress(import_id, current="Dateien werden extrahiert …")

        snap = job_snapshot(import_id)
        if snap is None:
            return
        staging = staging_dir(course_id, snap["job_id"])
        extracted = staging / "extracted"

        # 1) Extraktion ALLER noch nicht extrahierten Zips (Merge-Support)
        try:
            zips = sorted(staging.glob("upload*.zip"))
            # Ohne Zips geht's weiter, wenn Einzeldatei-Uploads bereits in
            # extracted/ liegen; nur komplett leeres Staging ist ein Fehler.
            has_files = extracted.is_dir() and any(extracted.rglob("*"))
            if not zips and not has_files:
                raise ValueError("Keine Dateien im Staging gefunden.")
            done_zips = set((snap["report"].get("extracted_zips") or []))
            deleted = set((snap["report"].get("deleted_paths") or []))
            existing = {
                p.relative_to(extracted).as_posix()
                for p in extracted.rglob("*") if p.is_file()
            } if extracted.is_dir() else set()
            for z in zips:
                if z.name in done_zips:
                    continue
                entries, warnings = await asyncio.to_thread(
                    safe_extract_zip, z, extracted, deleted, existing
                )
                for w in warnings:
                    append_report(import_id, warnings=[w])
                for e in entries:
                    existing.add(e["path"])
                # erst nach erfolgreicher Extraktion als fertig markieren (Resume-Sicherheit)
                def _mk(name: str):
                    def fn(imp: CourseImport):
                        r = dict(imp.report or {})
                        dz = list(r.get("extracted_zips") or [])
                        if name not in dz:
                            dz.append(name)
                            imp.report = {**r, "extracted_zips": dz}
                    return fn

                _mutate(import_id, _mk(z.name))
        except (ValueError, zipfile.BadZipFile, OSError) as exc:
            append_report(import_id, errors=[f"Zip-Extraktion fehlgeschlagen: {exc}"])
            set_stage_status(import_id, "filemap", "error")
            return

        # 2) Manifest + Sidecars aus dem kompletten extracted/-Verzeichnis
        set_progress(import_id, current="Manifest wird erstellt …")
        disk_entries: list[dict] = []
        for p in sorted(extracted.rglob("*")):
            if p.is_file():
                rel_parts = p.relative_to(extracted).parts
                # Synthetische Medienverzeichnisse (<doc>.media/ — Office-Media
                # + PDF-M2) gehören zum Eltern-Dokument: aus dem Disk-Scan
                # ausnehmen, sonst Doppel-Manifest-Einträge ab dem 2. Lauf.
                if any(part.endswith(".media") for part in rel_parts[:-1]):
                    continue
                disk_entries.append(
                    {"path": p.relative_to(extracted).as_posix(), "size": p.stat().st_size}
                )
        manifest, warnings2 = await asyncio.to_thread(build_manifest, staging, disk_entries)
        for w in warnings2:
            append_report(import_id, warnings=[w])
        set_manifest(import_id, manifest)
        # Counter pro Run neu: beim Resume werden fertige Units erneut als
        # done gezählt, alte Läufe dürfen sich nicht aufaddieren.
        set_progress(import_id, stage="filemap", stage_total=len(manifest), stage_reset=True)

        if not await gate(import_id, "filemap"):
            return

        # 3) LLM-Digests der Text-Dateien
        text_entries = [m for m in manifest if m.get("read_path")]
        llm_cfg = get_llm_config(course_id, IMPORT_TIMEOUT_ANALYSIS)
        if text_entries:
            await _digest_files(import_id, staging, text_entries, llm_cfg)
            if not await gate(import_id, "filemap"):
                return

        # Ergebnis: done (auch bei 0 Text-Dateien) / error (alle Dateien fehlgeschlagen)
        with Session(engine) as s:
            imp = s.get(CourseImport, import_id)
            fm = dict(imp.file_map or {}) if imp else {}
        failed = [p for p, e in fm.items() if e.get("status") == "error"]
        if text_entries and len(failed) == len(text_entries):
            set_stage_status(import_id, "filemap", "error")
            append_report(import_id, errors=[f"Alle {len(failed)} Text-Dateien konnten nicht analysiert werden."])
        else:
            set_stage_status(import_id, "filemap", "done")
            set_progress(import_id, current="Dateianalyse abgeschlossen.")
        logger.info("Import %s: filemap abgeschlossen", import_id)
    except Exception as exc:
        logger.exception("Import %s: filemap Job fehlgeschlagen", import_id)
        append_report(import_id, errors=[f"filemap-Job intern fehlgeschlagen: {exc}"])
        if stage_status(import_id, "filemap") == "running":
            set_stage_status(import_id, "filemap", "error")


async def _digest_files(
    import_id: int, staging: Path, text_entries: list[dict], llm_cfg: dict
) -> None:
    """LLM-Struktur-Digests pro Text-Datei (parallel, mit LLM-Semaphore)."""
    file_sem = asyncio.Semaphore(IMPORT_FILE_CONCURRENCY)
    llm_sem = asyncio.Semaphore(IMPORT_LLM_CONCURRENCY)

    async def process(entry: dict) -> None:
        path, read_path = entry["path"], entry["read_path"]
        unit_key = f"filemap:{path}"
        async with file_sem:
            if not await gate(import_id, "filemap"):
                return
            p = staging / read_path
            try:
                text = await asyncio.to_thread(_read_text, p)
            except OSError as exc:
                set_filemap_entry(import_id, path, {"status": "error", "chunks": [], "error": str(exc)})
                set_progress(import_id, stage="filemap", stage_failed_delta=1)
                return
            lines = text.splitlines()
            chunks = chunk_lines(lines, IMPORT_CHUNK_CHARS)
            set_progress(import_id, unit_key=unit_key, unit_total=len(chunks), unit_done=0)

            # Resume: bereits fertige Chunks überspringen
            existing = get_filemap_entry(import_id, path)
            old_chunks = existing.get("chunks") or []

            done = 0
            for i, (cs, ce) in enumerate(chunks):
                c = old_chunks[i] if i < len(old_chunks) else {}
                if c.get("summary") and not c.get("error"):
                    done += 1
                    continue
                async with llm_sem:
                    if not await gate(import_id, "filemap"):
                        return
                    numbered = "\n".join(f"{n:>6}| {lines[n - 1]}" for n in range(cs, ce + 1))
                    res = await llm_service.import_analyze_file(
                        path, f"{cs}-{ce} von {len(lines)}", numbered, config=llm_cfg
                    )
                if res["success"]:
                    data = res["data"] or {}
                    summary = str(data.get("summary") or "").strip()[:1000]
                    headings = verify_headings(data.get("headings"), lines, cs, ce)
                    set_filemap_chunk(import_id, path, i, {
                        "start_line": cs, "end_line": ce,
                        "summary": summary, "headings": headings,
                    })
                    done += 1
                else:
                    set_filemap_chunk(import_id, path, i, {
                        "start_line": cs, "end_line": ce,
                        "summary": "", "headings": [],
                        "error": str(res.get("error") or "LLM-Fehler")[:300],
                    })
                    done += 1  # als behandelt zählen (beim Resume wird erneut versucht)
                set_progress(import_id, unit_key=unit_key, unit_done=done)

            # Datei-Fazit (fertig = mind. ein Chunk mit Summary)
            cur = get_filemap_entry(import_id, path)
            cur_chunks = cur.get("chunks") or []
            ok = any(c.get("summary") and not c.get("error") for c in cur_chunks)
            set_filemap_entry(import_id, path, {
                "status": "done" if ok else "error",
                "chunks": cur_chunks,
                **({"error": "Alle Chunks fehlgeschlagen"} if not ok else {}),
            })
            set_progress(import_id, stage="filemap", stage_done_delta=1 if ok else 0,
                         stage_failed_delta=0 if ok else 1,
                         current=None if ok else f"Fehler: {path}")

    await run_units(import_id, "filemap", text_entries, process,
                    concurrency=IMPORT_FILE_CONCURRENCY, label=lambda u: u["path"])


def remove_staged_files(course_id: int, import_id: int, paths: list[str]) -> int:
    """Dateien aus der Stage entfernen (Staging + Manifest + Digests + Maps).

    Bereits importierte Medien/Quellen bleiben in der Kurs-Bibliothek (sie wurden
    kopiert); aus den Plan-Quellen werden die Pfade stillschweigend verwaist
    (Generierung meldet dann "Datei nicht gefunden" bzw. der Planner schlägt
    neue Quellen vor). Returns: Anzahl entfernter Dateien.
    """
    with Session(engine) as s:
        imp = s.get(CourseImport, import_id)
        if not imp or imp.course_id != course_id:
            return 0
        manifest = [dict(m) for m in (imp.manifest or [])]
    wanted = set(paths)
    by_path = {m["path"]: m for m in manifest}
    removed: list[str] = []
    staging = staging_dir(course_id, imp.job_id)

    def fn(imp2: CourseImport):
        nonlocal removed
        man = [dict(m) for m in (imp2.manifest or [])]
        kept = []
        for m in man:
            if m["path"] in wanted:
                removed.append(m["path"])
            else:
                kept.append(m)
        imp2.manifest = kept
        fm = {k: v for k, v in (imp2.file_map or {}).items() if k not in wanted}
        imp2.file_map = fm
        mm = {k: v for k, v in (imp2.media_map or {}).items() if k not in wanted}
        imp2.media_map = mm
        # Quellen ohne verbleibende Quelle entfernen
        rm = imp2.reference_map or {}
        new_rm = {}
        for key, e in rm.items():
            e2 = dict(e)
            srcs = [x for x in (e2.get("sources") or []) if x not in wanted]
            if srcs:
                e2["sources"] = srcs
                new_rm[key] = e2
            elif not e2.get("imported_ref_id"):
                pass  # nicht importiert + keine Quelle mehr → weg
            else:
                e2["sources"] = []
                new_rm[key] = e2
        imp2.reference_map = new_rm
        r = dict(imp2.report or {})
        dp = list(r.get("deleted_paths") or [])
        for pth in removed:
            if pth not in dp:
                dp.append(pth)
        imp2.report = {**r, "deleted_paths": dp}

    _mutate(import_id, fn)

    # Dateien vom Dateisystem entfernen (best effort)
    for pth in removed:
        for cand in (staging / "extracted" / pth,
                     staging / (by_path.get(pth) or {}).get("read_path", "")):
            try:
                if cand.is_file():
                    cand.unlink()
            except OSError:
                pass
    return len(removed)


# ═══════════════════════════════════════════════════════════════════
# STUFE 2: media (Medien-Import + LLM-Beschreibung)
# ═══════════════════════════════════════════════════════════════════

def _pixmap_to_white_png(pix) -> bytes:
    """PNG-Bytes aus einem gerenderten Pixmap; transparente Bereiche → weiß.

    PDF-Seiten mit transparentem Hintergrund würden ohne Alpha-Channel
    sonst auf schwarz gerendert.
    """
    import io
    from PIL import Image

    img = Image.open(io.BytesIO(pix.tobytes("png")))
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        alpha = img.getchannel("A")
        if alpha.getextrema()[0] < 255:  # nur bei echter Transparenz compositen
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=alpha)
            img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "png")
    return buf.getvalue()


def sanitize_svg(data: bytes) -> dict:
    """SVG für Medienbibliothek/Preview sanitisieren (XSS-Schutz).

    Entfernt Skript-/Embed-Elemente (script, foreignObject, iframe, object,
    embed), <style> mit Skriptinhalten, Event-Handler und skriptbare URL-Attribute.
    Liefert {"data": <sanitized bytes>, "hint": <Title-/Desc-Text>} oder {"error": ...}.
    """
    import io
    import xml.etree.ElementTree as ET

    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.S)  # DTD/Entities brechen ET
    raw = text.encode("utf-8")
    try:
        for _event, (prefix, uri) in ET.iterparse(io.BytesIO(raw), events=("start-ns",)):
            if re.fullmatch(r"ns\d+", prefix or ""):
                continue  # ns\d+ ist von ET reserviert — Prefix wird bei der Ausgabe automatisch neu vergeben
            ET.register_namespace(prefix, uri)  # ursprüngliche Präfixe für saubere Ausgabe
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return {"error": f"SVG nicht parsebar: {exc}"}

    def _local(name: Any) -> str:
        if not isinstance(name, str):
            return ""
        return name.rsplit("}", 1)[-1].lower()

    def _strip(el) -> None:
        for child in list(el):
            ln = _local(child.tag)
            if ln in ("script", "foreignobject", "iframe", "object", "embed"):
                el.remove(child)
                continue
            if ln == "style" and child.text and re.search(
                r"expression|javascript:|@import|url\(\s*['\"]?\s*https?:", child.text, re.I
            ):
                el.remove(child)
                continue
            _strip(child)
        for attr in list(el.attrib):
            if _local(attr).startswith("on"):
                del el.attrib[attr]
                continue
            if _local(attr) in ("href", "src", "srcset", "data") and re.match(
                r"\s*(javascript:|vbscript:|data:text/|https?://)", el.attrib[attr], re.I
            ):
                del el.attrib[attr]

    _strip(root)

    # <title>/<desc> als Beschreibungshinweis für die Medien-Bibliothek
    hint = ""
    for el in root.iter():
        if _local(el.tag) in ("title", "desc") and el.text and el.text.strip():
            hint = el.text.strip()
            break

    out = io.BytesIO()
    ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)
    return {"data": out.getvalue(), "hint": hint[:500]}


def _sniff_image_magic(path: Path) -> Optional[str]:
    """Raster-Dateiendung anhand der Magic Bytes erkennen (z.B. JPEG als .eps getarmt)."""
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"GIF8"):
        return ".gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    return None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _backfill_course_media_hashes(course_id: int) -> None:
    """Best-effort: content_hash für bereits existierende Kurs-Medien ohne Hash berechnen.

    Idempotent; ohne die Backfill kann nur gegen Medien-Dedup geprüft werden, die
    mit Hash importiert wurden (z. B. nach Import-Delete + Re-Upload derselben Zip).
    """
    with Session(engine) as s:
        rows = [
            (m.id, m.file_path)
            for m in s.exec(select(CourseMedia).where(CourseMedia.course_id == course_id)).all()
            if not m.content_hash
        ]
    for mid, file_path in rows:
        p = MEDIA_DIR / file_path
        if not p.is_file():
            continue
        try:
            h = _sha256_file(p)
        except OSError:
            continue
        with Session(engine) as s:
            row = s.get(CourseMedia, mid)
            if row and not row.content_hash:
                row.content_hash = h
                s.add(row)
                s.commit()


def _import_one_media(staging: Path, course_id: int, entry: dict, created_by: int) -> dict:
    """Eine Mediendatei (image/figure_pdf/svg/eps) in die Kurs-Medienbibliothek schreiben.

    Direkt nach MEDIA_DIR (nicht über save_upload — dessen Whitelist/Limits
    sind für normale Uploads gedacht). UUID-Namen → kein Kollisionsrisiko.
    Dedup: SHA256 der Quelldatei — bei bereits vorhandenem Medium werden die
    bestehenden CourseMedia-Rows zurückgegeben (duplicate=True, kein neuer Eintrag).
    """
    src = staging / "extracted" / entry["path"]
    if not src.is_file():
        return {"error": "Datei nicht gefunden"}
    try:
        src_hash = _sha256_file(src)
    except OSError:
        src_hash = None
    if src_hash:
        with Session(engine) as s:
            dup = [
                r for r in s.exec(select(CourseMedia).where(
                    CourseMedia.course_id == course_id,
                    CourseMedia.content_hash == src_hash,
                )).all()
            ]
            if dup:
                urls = [media_service.media_url(r) for r in dup]
                return {"duplicate": True, "media_ids": [r.id for r in dup], "urls": urls, "url": urls[0]}
    ext = src.suffix.lower()
    etype = entry["type"]
    # Altes Manifest (vor SVG/EPS-Support) hat diese als "other" klassifiziert
    if etype == "other" and ext in (".svg", ".eps"):
        etype = ext.lstrip(".")
    # .eps-Dateien sind häufig in Wirklichkeit Rastergrafiken (z.B. JPEG) — importieren wie Bild
    if etype == "eps":
        sniffed = _sniff_image_magic(src)
        if sniffed:
            etype, ext = "image", sniffed
    try:
        if etype == "image":
            if ext not in IMAGE_MIME:
                return {"error": f"Dateityp {ext} nicht unterstützt"}
            data = src.read_bytes()
            rel = f"course_{course_id}/{uuid.uuid4().hex}{ext}"
            (MEDIA_DIR / rel).parent.mkdir(parents=True, exist_ok=True)
            (MEDIA_DIR / rel).write_bytes(data)
            with Session(engine) as s:
                media = CourseMedia(
                    course_id=course_id, title=src.stem[:300], file_path=rel,
                    media_type="image", mime_type=IMAGE_MIME[ext],
                    file_size=len(data), content_hash=src_hash, created_by=created_by,
                )
                s.add(media)
                s.commit()
                s.refresh(media)
            return {
                "media_ids": [media.id], "urls": [media_service.media_url(media)],
                "url": media_service.media_url(media),
                "file": str(MEDIA_DIR / rel), "mime": IMAGE_MIME[ext],
            }
        elif etype == "svg":
            sanitized = sanitize_svg(src.read_bytes())
            if "error" in sanitized:
                return sanitized
            data = sanitized["data"]
            rel = f"course_{course_id}/{uuid.uuid4().hex}.svg"
            (MEDIA_DIR / rel).parent.mkdir(parents=True, exist_ok=True)
            (MEDIA_DIR / rel).write_bytes(data)
            with Session(engine) as s:
                media = CourseMedia(
                    course_id=course_id, title=src.stem[:300], file_path=rel,
                    media_type="image", mime_type="image/svg+xml",
                    file_size=len(data), content_hash=src_hash, created_by=created_by,
                )
                s.add(media)
                s.commit()
                s.refresh(media)
            return {
                "media_ids": [media.id], "urls": [media_service.media_url(media)],
                "url": media_service.media_url(media),
                "file": str(MEDIA_DIR / rel), "mime": "image/svg+xml",
                # Vektorgrafik: kein Vision-Call, Text-Hint aus <title>/<desc>
                "no_vision": True, "description_hint": sanitized["hint"],
            }
        elif etype == "eps":
            import subprocess
            import tempfile
            import io
            from PIL import Image

            def _gs_to_png_bytes(dpi: int) -> bytes:
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "out.png"
                    try:
                        proc = subprocess.run(
                            ["gs", "-dNOPAUSE", "-dBATCH", "-dQUIET", "-dSAFER",
                             "-sDEVICE=png16m", f"-r{dpi}",
                             f"-sOutputFile={out}", str(src)],
                            timeout=120, capture_output=True,
                        )
                    except FileNotFoundError:
                        raise RuntimeError("Ghostscript (gs) ist nicht installiert")
                    except subprocess.TimeoutExpired:
                        raise RuntimeError("EPS-Konvertierung: Zeitüberschreitung")
                    if proc.returncode != 0 or not out.is_file():
                        # gs kann binäres Müll in stderr schreiben → tolerant decodieren
                        err = proc.stderr.decode("utf-8", errors="replace").strip()
                        raise RuntimeError(f"EPS-Konvertierung fehlgeschlagen: {err[:200] or 'unbekannter Fehler'}")
                    return out.read_bytes()

            png = _gs_to_png_bytes(IMPORT_PNG_DPI)
            # Dimensions-Cap: bei Bedarf mit kleinerem DPI neu rendern
            with Image.open(io.BytesIO(png)) as im:
                if max(im.size) > IMPORT_PNG_MAX_DIM:
                    new_dpi = max(1, int(IMPORT_PNG_DPI * IMPORT_PNG_MAX_DIM / max(im.size)))
                    png = _gs_to_png_bytes(new_dpi)
            rel = f"course_{course_id}/{uuid.uuid4().hex}.png"
            (MEDIA_DIR / rel).parent.mkdir(parents=True, exist_ok=True)
            (MEDIA_DIR / rel).write_bytes(png)
            with Session(engine) as s:
                media = CourseMedia(
                    course_id=course_id, title=src.stem[:300], file_path=rel,
                    media_type="image", mime_type="image/png",
                    file_size=len(png), content_hash=src_hash, created_by=created_by,
                )
                s.add(media)
                s.commit()
                s.refresh(media)
            return {
                "media_ids": [media.id], "urls": [media_service.media_url(media)],
                "url": media_service.media_url(media),
                "file": str(MEDIA_DIR / rel), "mime": "image/png",
            }
        # figure_pdf → PNG (max. 5 Seiten, DPI/Dimensions-Cap)
        import fitz  # pymupdf

        doc = fitz.open(str(src))
        n_pages = min(len(doc), MAX_MEDIA_PAGES)
        if n_pages <= 0:
            doc.close()
            return {"error": "PDF hat keine Seiten"}
        media_list = []
        for pno in range(n_pages):
            page = doc[pno]
            zoom = IMPORT_PNG_DPI / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True)
            if max(pix.width, pix.height) > IMPORT_PNG_MAX_DIM:
                scale = IMPORT_PNG_MAX_DIM / max(pix.width, pix.height)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom * scale, zoom * scale), alpha=True)
            png = _pixmap_to_white_png(pix)
            rel = f"course_{course_id}/{uuid.uuid4().hex}.png"
            (MEDIA_DIR / rel).parent.mkdir(parents=True, exist_ok=True)
            (MEDIA_DIR / rel).write_bytes(png)
            with Session(engine) as s:
                title = src.stem + (f" ({pno + 1}/{n_pages})" if n_pages > 1 else "")
                media = CourseMedia(
                    course_id=course_id, title=title[:300], file_path=rel,
                    media_type="image", mime_type="image/png",
                    file_size=len(png), content_hash=src_hash, created_by=created_by,
                )
                s.add(media)
                s.commit()
                s.refresh(media)
                media_list.append({"id": media.id, "url": media_service.media_url(media), "file": str(MEDIA_DIR / rel)})
        doc.close()
        return {
            "media_ids": [m["id"] for m in media_list],
            "urls": [m["url"] for m in media_list],
            "url": media_list[0]["url"],
            "file": media_list[0]["file"],
            "mime": "image/png",
        }
    except Exception as exc:
        return {"error": str(exc)}


async def _backfill_descriptions(
    import_id: int,
    course_id: int,
    media_ids: list[int],
    llm_cfg: dict,
    llm_sem: asyncio.Semaphore,
) -> None:
    """Fehlende LLM-Beschreibungen für bereits importierte Medien nachrüsten (best effort).

    Medien, die bei fehlgeschlagenen Vision-Calls (z. B. Timeout auf der
    langsamen lokalen GPU) ohne Beschreibung importiert wurden, werden beim
    Resume hier nachbeschrieben. Pro Medium ein Vision-Call, begrenzt durch
    llm_sem; bei Stop (Pause/Cancel) bricht der Loop ab.
    """
    if not media_ids:
        return
    with Session(engine) as s:
        rows = []
        for mid in media_ids:
            m = s.get(CourseMedia, mid)
            if m and not m.llm_description and m.mime_type in IMAGE_MIME.values():
                rows.append((m.id, m.mime_type, m.file_path))
    for mid, mime_type, file_path in rows:
        async with llm_sem:
            if not await gate(import_id, "media"):
                return
            try:
                b64 = await asyncio.to_thread(
                    lambda p=MEDIA_DIR / file_path: base64.b64encode(p.read_bytes()).decode()
                )
                res = await llm_service.describe_media_image(
                    b64, mime_type=mime_type, config=llm_cfg
                )
                if not res["success"]:
                    logger.warning(
                        "Import %s: Backfill-Beschreibung fehlgeschlagen für %s: %s",
                        import_id, file_path, res.get("error"),
                    )
                    continue
                description = str((res["data"] or {}).get("description") or "").strip()[:2000]
                if description:
                    with Session(engine) as s:
                        m = s.get(CourseMedia, mid)
                        if m:
                            m.llm_description = description
                            s.add(m)
                            s.commit()
            except Exception as exc:
                logger.warning(
                    "Import %s: Backfill-Beschreibung fehlgeschlagen (%s): %s",
                    import_id, file_path, exc,
                )


async def run_media_job(
    course_id: int, import_id: int, selected_paths: Optional[list[str]] = None
) -> None:
    """Stufe 2: ausgewählte Medien importieren + LLM-Beschreibung.

    selected_paths=None (Resume): Pfade werden aus report[media_options] gelesen.
    """
    logger.info("Import %s (Kurs %s): media gestartet", import_id, course_id)
    try:
        set_stage_status(import_id, "media", "running")
        set_progress(import_id, stage="media", current="Medien werden importiert …")
        snap = job_snapshot(import_id)
        if snap is None:
            return
        if selected_paths is None:
            selected_paths = (snap["report"].get("media_options") or {}).get("paths") or []
        staging = staging_dir(course_id, snap["job_id"])
        # Best-effort-Backfill: alte Medien ohne Hash bekommen einen → Dedup funktioniert
        await asyncio.to_thread(_backfill_course_media_hashes, course_id)
        def _is_media(m: dict) -> bool:
            # Extension-Fallback: altes Manifest kennt svg/eps noch nicht
            return (m.get("type") in ("image", "figure_pdf", "svg", "eps")
                    or m.get("path", "").lower().endswith((".svg", ".eps")))

        units = [m for m in snap["manifest"] if m["path"] in selected_paths and _is_media(m)]
        # Counter pro Run neu (s. filemap) – sonst addieren sich Läufe auf.
        set_progress(import_id, stage="media", stage_total=len(units), stage_reset=True)
        llm_cfg = get_llm_config(course_id, IMPORT_TIMEOUT_MEDIA)
        llm_sem = asyncio.Semaphore(IMPORT_LLM_CONCURRENCY)

        async def process(entry: dict) -> None:
            path = entry["path"]
            unit_key = f"media:{path}"
            mm = get_media_map_entry(import_id, path)
            if mm:
                # idempotent: bereits importiert (Resume) — fehlende
                # Beschreibungen (früher fehlgeschlagene Vision-Calls) nachrüsten
                await _backfill_descriptions(
                    import_id, course_id, mm.get("media_ids") or [], llm_cfg, llm_sem
                )
                set_progress(import_id, unit_key=unit_key, unit_done=1, stage="media", stage_done_delta=1)
                return
            set_progress(import_id, unit_key=unit_key, unit_total=1, current=f"Medien: {path}")
            result = await asyncio.to_thread(_import_one_media, staging, course_id, entry, snap["created_by"])
            if "error" in result:
                append_report(import_id, warnings=[f"Medium nicht importiert ({path}): {result['error']}"])
                set_progress(import_id, stage="media", stage_failed_delta=1)
                return
            if result.get("duplicate"):
                # Bereits in der Kurs-Medienbibliothek vorhanden → nur verknüpfen
                set_media_map_entry(import_id, path, {
                    "url": result["url"], "urls": result["urls"],
                    "media_id": result["media_ids"][0], "media_ids": result["media_ids"],
                })
                append_report(import_id, warnings=[
                    f"Medium übersprungen ({path}): bereits in der Kurs-Medienbibliothek vorhanden."
                ])
                set_progress(import_id, unit_key=unit_key, unit_done=1, stage="media", stage_done_delta=1)
                return
            # Beschreibung: SVG liefert einen Text-Hint (kein Vision-Call),
            # Raster/PDF-Figuren per Vision-LLM (best effort)
            description = str(result.get("description_hint") or "")
            if not description and not result.get("no_vision"):
                async with llm_sem:
                    if await gate(import_id, "media"):
                        try:
                            b64 = await asyncio.to_thread(
                                lambda: base64.b64encode(Path(result["file"]).read_bytes()).decode()
                            )
                            res = await llm_service.describe_media_image(b64, mime_type=result["mime"], config=llm_cfg)
                            if res["success"]:
                                description = str((res["data"] or {}).get("description") or "").strip()[:2000]
                        except Exception as exc:
                            logger.warning("Import %s: Medien-Beschreibung fehlgeschlagen: %s", import_id, exc)
            if description:
                with Session(engine) as s:
                    for mid in result["media_ids"]:
                        m = s.get(CourseMedia, mid)
                        if m:
                            m.llm_description = description
                            s.add(m)
                    s.commit()
            set_media_map_entry(import_id, path, {
                "url": result["url"], "urls": result["urls"],
                "media_id": result["media_ids"][0], "media_ids": result["media_ids"],
            })
            set_progress(import_id, unit_key=unit_key, unit_done=1, stage="media", stage_done_delta=1)

        outcome = await run_units(import_id, "media", units, process,
                                  concurrency=IMPORT_FILE_CONCURRENCY, label=lambda u: u["path"])
        if outcome["stopped"]:
            return
        if outcome["crashed"]:
            append_report(import_id, errors=[f"Medien-Import: unerwartete Fehler bei: {', '.join(outcome['crashed'][:10])}"])
        # Referenz-Sync (Medien sind jetzt in der Bibliothek)
        with Session(engine) as s:
            media_service.sync_media_usages(s, course_id)
        set_stage_status(import_id, "media", "done")
        set_progress(import_id, current="Medien-Import abgeschlossen.")
        logger.info("Import %s: media abgeschlossen", import_id)
    except Exception as exc:
        logger.exception("Import %s: media Job fehlgeschlagen", import_id)
        append_report(import_id, errors=[f"media-Job intern fehlgeschlagen: {exc}"])
        if stage_status(import_id, "media") == "running":
            set_stage_status(import_id, "media", "error")


# ═══════════════════════════════════════════════════════════════════
# STUFE 3: references (Quellen-Bibliothek aus .bib/.bbl + \cite-Keys)
# ═══════════════════════════════════════════════════════════════════

# \cite{...}, \citep[...]{...}, \citet[...]{...}, \nocite{...} u. a. —
# Keys liegen in der letzten {...}-Gruppe (ohne geschachtelte Klammern).
_CITE_KEYS_RE = re.compile(
    r"\\(?:cite|citep|citet|nocite|citeauthor|citeyear|citetitle|parencite|textcite)"
    r"(?:\s*\[[^\]]*\]){0,2}\s*\{([^{}]*)\}"
)


def _empty_reference_fields() -> dict:
    return {
        "entry_type": "misc", "authors": [], "title": "", "year": "",
        "venue": "", "detail": "", "address": "", "doi": "", "url": "", "note": "",
    }


def detect_references(course_id: int, import_id: int) -> Optional[dict]:
    """Deterministische Quellen-Detektion (ohne LLM).

    .bib-Files → vollständige BibTeX-Entries; .bbl-Files und \\cite-Keys in TeX
    liefern Keys (ohne Bib-Daten = Stubs, im Quellen-Tab von Hand auffüllbar).
    Rückgabe: reference_map-Dict (None, wenn der Import nicht mehr existiert).
    """
    with Session(engine) as s:
        imp = s.get(CourseImport, import_id)
        if not imp or imp.course_id != course_id:
            return None
        manifest = [dict(m) for m in (imp.manifest or [])]
    staging = staging_dir(course_id, imp.job_id)
    refs: dict[str, dict] = {}

    def _add_source(key: str, src: str) -> None:
        e = refs.get(key)
        if e is None:
            return
        sources = e.setdefault("sources", [])
        if src not in sources:
            sources.append(src)

    def _bib_entries() -> list[dict]:
        # type "bib" (neue Manifeste); Pfad-Check als Fallback für ältere
        # Manifeste, in denen .bib/.bbl noch als "other" klassifiziert sind.
        out = []
        for m in manifest:
            if m.get("type") == "bib" or m["path"].lower().endswith((".bib", ".bbl")):
                out.append(m)
        return out

    def _read_bib(m: dict) -> Optional[str]:
        p = staging / "extracted" / m["path"]
        if not p.is_file():
            return None
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    # 1) .bib-Files → vollständige Entries (erster Fund eines Keys gewinnt) —
    #    vor .bbl verarbeiten, sonst würden bbl-Keys als Stubs "gewinnen"
    for m in _bib_entries():
        if not m["path"].lower().endswith(".bib"):
            continue
        text = _read_bib(m)
        if text is None:
            continue
        for e in bib.parse_bibtex(text):
            key = e["key"]
            if key in refs:
                _add_source(key, m["path"])
                continue
            data = bib.entry_to_reference(e)
            data["sources"] = [m["path"]]
            refs[key] = data

    # 2) .bbl-Files → nur Keys (kompilierte Bibliographie; Stub, falls der
    #    Key nicht in einer .bib vorhanden ist)
    for m in _bib_entries():
        if m["path"].lower().endswith(".bib"):
            continue
        text = _read_bib(m)
        if text is None:
            continue
        for key in bib.parse_bbl_keys(text):
            if key not in refs:
                refs[key] = {**_empty_reference_fields(), "stub": True, "sources": [m["path"]]}
            else:
                _add_source(key, m["path"])

    # 3) TeX-\cite-Keys (Stub, falls nicht in einer .bib vorhanden)
    for m in manifest:
        if m.get("type") != "tex" or not m.get("read_path"):
            continue
        p = staging / m["read_path"]
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for group in _CITE_KEYS_RE.findall(text):
            for key in (k.strip() for k in group.split(",")):
                if not key:
                    continue
                if key not in refs:
                    refs[key] = {**_empty_reference_fields(), "stub": True, "sources": [m["path"]]}
                else:
                    _add_source(key, m["path"])
    return refs


async def run_references_job(
    course_id: int, import_id: int, selected_keys: Optional[list[str]] = None
) -> None:
    """Stufe 3: ausgewählte Quellen in die Kurs-Quellenbibliothek importieren
    (+ LLM-Inhaltsbeschreibung für vollständige Entries).

    selected_keys=None (Resume): Keys werden aus report[references_options] gelesen;
    bereits importierte Quellen werden übersprungen (idempotent). Expliziter Start
    mit bereits importierten Keys = Re-Import: die bestehende Quelle wird
    überschrieben (Position bleibt erhalten).
    """
    logger.info("Import %s (Kurs %s): references gestartet", import_id, course_id)
    try:
        set_stage_status(import_id, "references", "running")
        set_progress(import_id, stage="references", current="Quellen werden importiert …")
        snap = job_snapshot(import_id)
        if snap is None:
            return
        reimport = selected_keys is not None  # expliziter Start vs. Resume
        if selected_keys is None:
            selected_keys = (snap["report"].get("references_options") or {}).get("keys") or []
        refmap = snap["reference_map"] or {}
        units = [{"key": k, "reimport": reimport} for k in selected_keys if k in refmap]
        set_progress(import_id, stage="references", stage_total=len(units), stage_reset=True)
        llm_cfg = get_llm_config(course_id, IMPORT_TIMEOUT_MEDIA)
        llm_sem = asyncio.Semaphore(IMPORT_LLM_CONCURRENCY)

        async def process(entry: dict) -> None:
            key = entry["key"]
            unit_key = f"references:{key}"
            data = refmap.get(key) or {}
            fields = {k: data[k] for k in (
                "entry_type", "authors", "title", "year", "venue",
                "detail", "address", "doi", "url", "note",
            ) if k in data}
            with Session(engine) as s:
                existing = s.exec(
                    select(CourseReference)
                    .where(CourseReference.course_id == course_id)
                    .where(CourseReference.key == key)
                ).first()
            overwrite = bool(existing) and entry.get("reimport")
            if existing and not overwrite:
                # idempotent: bereits importiert (Resume)
                set_progress(import_id, unit_key=unit_key, unit_done=1,
                             stage="references", stage_done_delta=1)
                return
            set_progress(import_id, unit_key=unit_key, unit_total=1,
                         current=f"Quellen: {key}" + (" (überschreiben)" if overwrite else ""))
            description = ""
            if not data.get("stub"):
                # Beschreibung: wann soll die Quelle zitiert werden? (Public-LLM, best effort)
                async with llm_sem:
                    if await gate(import_id, "references"):
                        try:
                            res = await llm_service.import_describe_reference(
                                bib.format_entry(
                                    data.get("authors") or [], data.get("title") or "",
                                    data.get("year") or "", data.get("venue") or "",
                                    data.get("detail") or "", data.get("doi") or "",
                                    data.get("url") or "",
                                ),
                                config=llm_cfg,
                            )
                            if res.get("success"):
                                description = strip_outer_fence(
                                    (res.get("data") or {}).get("model_solution", "")
                                ).strip()[:2000]
                        except Exception as exc:
                            logger.warning("Import %s: Quellen-Beschreibung fehlgeschlagen (%s): %s",
                                           import_id, key, exc)
            if overwrite and not description and existing is not None:
                # LLM fehlgeschlagen/Stub → bisherige Beschreibung behalten
                description = existing.description or ""
            with Session(engine) as s:
                ref = s.exec(
                    select(CourseReference)
                    .where(CourseReference.course_id == course_id)
                    .where(CourseReference.key == key)
                ).first()
                if ref is None:
                    order_rows = s.exec(
                        select(CourseReference.display_order)
                        .where(CourseReference.course_id == course_id)
                    ).all()
                    ref = CourseReference(
                        course_id=course_id,
                        created_by=snap["created_by"],
                        key=key,
                        display_order=(max(order_rows) + 1) if order_rows else 0,
                        description=description,
                        **fields,
                    )
                else:
                    # Re-Import → bestehende Quelle überschreiben (Position bleibt erhalten)
                    for k, v in fields.items():
                        setattr(ref, k, v)
                    ref.description = description
                s.add(ref)
                s.commit()
                s.refresh(ref)
                ref_id = ref.id
            set_reference_map_entry(import_id, key, {"imported_ref_id": ref_id})
            set_progress(import_id, unit_key=unit_key, unit_done=1,
                         stage="references", stage_done_delta=1)

        outcome = await run_units(
            import_id, "references", units, process,
            concurrency=IMPORT_FILE_CONCURRENCY, label=lambda u: u["key"],
        )
        if outcome["stopped"]:
            return
        if outcome["crashed"]:
            append_report(import_id, errors=[
                f"Quellen-Import: unerwartete Fehler bei: {', '.join(outcome['crashed'][:10])}"
            ])
        set_stage_status(import_id, "references", "done")
        set_progress(import_id, current="Quellen-Import abgeschlossen.")
        logger.info("Import %s: references abgeschlossen", import_id)
    except Exception as exc:
        logger.exception("Import %s: references Job fehlgeschlagen", import_id)
        append_report(import_id, errors=[f"references-Job intern fehlgeschlagen: {exc}"])
        if stage_status(import_id, "references") == "running":
            set_stage_status(import_id, "references", "error")


# ═══════════════════════════════════════════════════════════════════
# STUFE 3b: ref_extract (agentic LLM-Quellen-Extraktion + Dedup)
# ═══════════════════════════════════════════════════════════════════

_REF_FIELDS = ("entry_type", "authors", "title", "year", "venue",
               "detail", "address", "doi", "url", "note")


def set_reference_map(import_id: int, refmap: dict) -> None:
    def fn(imp: CourseImport):
        imp.reference_map = refmap

    _mutate(import_id, fn)


def _read_bib_text(staging: Path, manifest: list[dict], cap: int = 30000) -> str:
    """Inhalt aller .bib/.bbl-Dateien (concatenated, mit Cap)."""
    parts: list[str] = []
    total = 0
    for m in manifest:
        if not m["path"].lower().endswith((".bib", ".bbl")):
            continue
        p = staging / "extracted" / m["path"]
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if len(text) > 8000:
            text = text[:8000] + "\n… (gekürzt)"
        parts.append(f"### {m['path']}\n{text}")
        total += len(text)
        if total > cap:
            break
    return "\n\n".join(parts) or "(keine .bib/.bbl-Dateien)"


def _known_refs_text(reference_map: dict) -> str:
    if not reference_map:
        return "(keine)"
    lines = []
    for key, e in list(reference_map.items())[:200]:
        meta = []
        for f in _REF_FIELDS:
            v = e.get(f)
            if v:
                meta.append(f"{f}={v}")
        stub = " [stub]" if e.get("stub") else ""
        desc = f" [beschreibung: {str(e['description'])[:200]}]" if e.get("description") else ""
        lines.append(f"- {key}{stub}: " + "; ".join(str(x) for x in meta)[:400] + desc)
    return "\n".join(lines)


def _normalize_ref_entry(raw: dict, valid_paths: set[str]) -> Optional[dict]:
    """Ein LLM-Quellen-Eintrag validieren (untrusted → strikt whitelisten)."""
    key = re.sub(r"\s+", "", str(raw.get("key") or "").strip()).lower()
    if not key or len(key) > 100:
        return None
    out: dict[str, Any] = {"key": key, **_empty_reference_fields()}
    et = str(raw.get("entry_type") or "misc").strip().lower()
    if et in ("book", "article", "inproceedings", "incollection", "techreport", "phdthesis", "misc"):
        out["entry_type"] = et
    authors = raw.get("authors")
    if isinstance(authors, str):
        authors = [authors]
    if isinstance(authors, list):
        out["authors"] = [str(a).strip()[:300] for a in authors[:20] if str(a).strip()]
    for f in ("title", "year", "venue", "detail", "address", "doi", "url", "note"):
        v = str(raw.get(f) or "").strip()
        if v:
            out[f] = v[:500]
    out["description"] = str(raw.get("description") or "").strip()[:2000]
    aliases = raw.get("aliases")
    if isinstance(aliases, list):
        out["aliases"] = [re.sub(r"\s+", "", str(a)).strip().lower() for a in aliases[:20]
                          if re.sub(r"\s+", "", str(a)).strip().lower() and
                          re.sub(r"\s+", "", str(a)).strip().lower() != key]
    else:
        out["aliases"] = []
    srcs = raw.get("sources")
    if isinstance(srcs, list):
        out["sources"] = [str(s).strip() for s in srcs[:20] if str(s).strip() in valid_paths]
    else:
        out["sources"] = []
    return out


def _merge_reference_map(old: dict, raw_refs: list, valid_paths: set[str]) -> dict:
    """LLM-Ergebnis mit der deterministischen Map mergen (Bib-Daten gewinnen)."""
    entries = [e for e in (_normalize_ref_entry(r, valid_paths)
                           for r in raw_refs if isinstance(r, dict)) if e]
    new_map: dict[str, dict] = {}
    alias_to_key: dict[str, str] = {}
    for e in entries:
        key = e["key"]
        if key in new_map:
            continue  # Kollision: erster Eintrag gewinnt
        for a in e.get("aliases") or []:
            alias_to_key.setdefault(a, key)
        new_map[key] = e

    for old_key, old_e in old.items():
        tgt_key = old_key if old_key in new_map else alias_to_key.get(old_key)
        if tgt_key is None or tgt_key not in new_map:
            new_map[old_key] = old_e  # LLM kennt den Key nicht → unverändert behalten
            continue
        tgt = new_map[tgt_key]
        if old_key != tgt_key and old_key not in tgt.get("aliases", []):
            tgt.setdefault("aliases", []).append(old_key)
        # Feld-Merge: alte (deterministische) Bib-Daten gewinnen, neue ergänzen
        for f in _REF_FIELDS:
            if tgt.get(f) in (None, "", []):
                tgt[f] = old_e.get(f, tgt.get(f))
        tgt["description"] = tgt.get("description") or old_e.get("description") or ""
        srcs = list(tgt.get("sources") or [])
        for s in old_e.get("sources") or []:
            if s not in srcs:
                srcs.append(s)
        tgt["sources"] = srcs
        if old_e.get("imported_ref_id"):
            tgt["imported_ref_id"] = old_e["imported_ref_id"]
    return new_map


async def run_ref_extract_job(course_id: int, import_id: int) -> None:
    """Stufe 3b: agentic LLM-Extraktion aller Quellen (Text + Bib) inkl. Dedup."""
    logger.info("Import %s (Kurs %s): ref_extract gestartet", import_id, course_id)
    try:
        set_stage_status(import_id, "ref_extract", "running")
        set_progress(import_id, current="Quellen werden extrahiert …")
        snap = job_snapshot(import_id)
        if snap is None:
            return
        staging = staging_dir(course_id, snap["job_id"])
        llm_cfg = get_llm_config(course_id, IMPORT_TIMEOUT_PLAN)

        # Deterministische Basis sicherstellen (bib/bbl/cite-Keys)
        if not snap["reference_map"]:
            refs = detect_references(course_id, import_id)
            if refs:
                def fn(imp: CourseImport):
                    imp.reference_map = {k: dict(v) for k, v in refs.items()}
                _mutate(import_id, fn)
                snap["reference_map"] = refs

        manifest = snap["manifest"]
        valid_paths = {m["path"] for m in manifest}
        bib_text = await asyncio.to_thread(_read_bib_text, staging, manifest)
        digests = _build_digests_text(manifest, snap["file_map"])
        if len(digests) > 24000:
            digests = digests[:24000] + "\n… (Digests gekürzt)"

        steps = "(noch keine)"
        tool_budget = 0
        for i in range(IMPORT_PLAN_MAX_ITERATIONS):
            if not await gate(import_id, "ref_extract"):
                return
            set_progress(import_id, current=f"Quellen-Extraktion: Schritt {i + 1}/{IMPORT_PLAN_MAX_ITERATIONS} …")
            res = await llm_service.import_ref_extract_step(
                bib_text, _known_refs_text(snap["reference_map"]), digests, steps, config=llm_cfg
            )
            if not res["success"]:
                append_report(import_id, errors=[
                    f"Quellen-Extraktion: LLM-Fehler: {res.get('error')}"
                ])
                set_stage_status(import_id, "ref_extract", "error")
                return
            action = res["data"] or {}
            kind = str(action.get("action") or "")
            if kind == "read_file":
                tool_out = _planner_read(manifest, staging, action)
                tool_budget += len(tool_out)
                steps += (f"\n### Schritt {i + 1}\nLLM: {json.dumps(action, ensure_ascii=False)}\n"
                          f"Tool: {tool_out}\n")
                if tool_budget > IMPORT_PLAN_TOOL_BUDGET:
                    steps += "(Werkzeug-Budget erschöpft — gib JETZT final_refs aus.)\n"
                continue
            if kind == "final_refs":
                raw = action.get("references")
                new_map = _merge_reference_map(snap["reference_map"], raw if isinstance(raw, list) else [], valid_paths)
                set_reference_map(import_id, new_map)

                def fn2(imp: CourseImport):
                    r = dict(imp.report or {})
                    r["refs_extracted"] = True
                    imp.report = r
                _mutate(import_id, fn2)
                set_stage_status(import_id, "ref_extract", "done")
                set_progress(import_id, current="Quellen-Extraktion abgeschlossen.")
                logger.info("Import %s: ref_extract abgeschlossen (%d Quellen)", import_id, len(new_map))
                return
            steps += (f"\n### Schritt {i + 1}\nLLM: {json.dumps(action, ensure_ascii=False)}\n"
                      "Tool: (unbekannte Aktion — verwende read_file oder final_refs)\n")

        append_report(import_id, warnings=[
            "Quellen-Extraktion unvollständig (Iterations-Budget) — die deterministischen Quellen stehen weiterhin zur Verfügung."
        ])
        set_stage_status(import_id, "ref_extract", "error")
    except Exception as exc:
        logger.exception("Import %s: ref_extract Job fehlgeschlagen", import_id)
        append_report(import_id, errors=[f"ref_extract-Job intern fehlgeschlagen: {exc}"])
        if stage_status(import_id, "ref_extract") == "running":
            set_stage_status(import_id, "ref_extract", "error")


# ═══════════════════════════════════════════════════════════════════
# STUFE 4: plan (agentic Planner)
# ═══════════════════════════════════════════════════════════════════

def _build_file_tree_text(manifest: list[dict]) -> str:
    lines = []
    for m in manifest:
        extra = []
        if m.get("main_tex"):
            extra.append("main_tex")
        if m.get("line_count"):
            extra.append(f"{m['line_count']} Zeilen")
        if m.get("pages"):
            extra.append(f"{m['pages']} Seiten")
        suffix = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"- {m['path']} [{m['type']}]{suffix}")
    return "\n".join(lines) or "(leer)"


def _build_digests_text(manifest: list[dict], file_map: dict) -> str:
    out = []
    for m in manifest:
        if not m.get("read_path"):
            continue
        entry = file_map.get(m["path"]) or {}
        chunks = [c for c in (entry.get("chunks") or []) if c.get("summary")]
        head = f"### {m['path']} [{m['type']}]"
        if not chunks:
            err = entry.get("error")
            out.append(f"{head}\n  (keine Zusammenfassung{': ' + err if err else ''})")
            continue
        parts = []
        for c in chunks:
            parts.append(f"  - Zeilen {c['start_line']}-{c['end_line']}: {c['summary']}")
            for h in (c.get("headings") or [])[:30]:
                parts.append(f"    - [Z{h['line']} L{h['level']}] {h['text']}")
        out.append(head + "\n" + "\n".join(parts))
    return "\n".join(out) or "(keine Text-Dateien analysiert)"


def _pptx_slide_blocks(sidecar_text: str) -> list[tuple[int, str]]:
    parts = re.split(r"^%% Folie (\d+) %%\s*$", sidecar_text or "", flags=re.M)
    out = []
    for i in range(1, len(parts) - 1, 2):
        try:
            n = int(parts[i])
        except ValueError:
            continue
        body = parts[i + 1].strip()
        if body:
            out.append((n, body))
    return out


def _build_zip_decks_text(manifest: list[dict], staging: Path) -> str:
    out = []
    for m in manifest:
        if m.get("type") != "pptx" or not m.get("read_path"):
            continue
        try:
            text = (staging / m["read_path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        blocks = _pptx_slide_blocks(text)
        if not blocks:
            continue
        lines = [f"### {m['path']} ({len(blocks)} Folien)"]
        for n, body in blocks[:80]:
            first = next((l.strip() for l in body.splitlines() if l.strip()), "")
            lines.append(f"  - Folie {n}: {first[:120]}")
        out.append("\n".join(lines))
    return "\n".join(out) or "(keine pptx-Folien in der Zip)"


def _course_decks(course_id: int) -> list[dict]:
    """Bestehende Slide-Decks des Zielkurses (id, title, display_order, content)."""
    with Session(engine) as s:
        decks = s.exec(
            select(CourseMaterial)
            .where(CourseMaterial.course_id == course_id)
            .where(CourseMaterial.material_type == MaterialType.SLIDES)
            .order_by(CourseMaterial.display_order.asc(), CourseMaterial.id.asc())  # type: ignore[attr-defined]
        ).all()
        return [{"id": d.id, "title": d.title, "display_order": d.display_order, "content": d.content or ""} for d in decks]


def _course_sections(course_id: int) -> list[dict]:
    """Bestehende Skript-Kapitel des Zielkurses (id, title, display_order, summary)."""
    with Session(engine) as s:
        secs = s.exec(
            select(ScriptSection)
            .where(ScriptSection.course_id == course_id)
            .order_by(ScriptSection.display_order.asc(), ScriptSection.id.asc())  # type: ignore[attr-defined]
        ).all()
        return [{"id": sc.id, "title": sc.title, "display_order": sc.display_order,
                 "summary": (sc.summary or "").strip()} for sc in secs]



def _course_media_list(course_id: int) -> list[dict]:
    """Medienbibliothek des Zielkurses (title, url, description)."""
    with Session(engine) as s:
        rows = s.exec(
            select(CourseMedia).where(CourseMedia.course_id == course_id)
        ).all()
        return [{"title": m.title, "url": media_service.media_url(m),
                 "description": (m.llm_description or "").strip()[:200]} for m in rows[:80]]


def _build_media_list_text(course_id: int) -> str:
    media = _course_media_list(course_id)
    if not media:
        return "(keine Medien in der Kurs-Medienbibliothek)"
    lines = []
    for m in media:
        lines.append(f"- {m['title']} ({m['url']})" + (f": {m['description']}" if m["description"] else ""))
    return "\n".join(lines)


def _build_references_text(course_id: int) -> str:
    """Quellenverzeichnis des Zielkurses (Key, Autoren, Titel, Kernpunkte) als Text."""
    with Session(engine) as s:
        return build_references_text(s, course_id) or "(leeres Quellenverzeichnis)"


def _as_int(v: Any, default: int, lo: int = 1, hi: int = 10**9) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return max(lo, min(default, hi))
    return max(lo, min(n, hi))


def _normalize_plan(raw: Any) -> list[dict]:
    """Validiert/normalisiert den LLM-Plan (NUR Titel/Aktiv/Beschreibung).

    Quellen werden NICHT im Plan gehalten — sie ermittelt der Gather-Schritt
    vor der Generierung aus Beschreibung + Dateibaum.
    """
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for ch in raw[:40]:
        if not isinstance(ch, dict):
            continue
        title = str(ch.get("title") or "").strip()[:300]
        if not title:
            continue
        out.append({
            "title": title,
            "enabled": bool(ch.get("enabled", True)),
            "description": str(ch.get("description") or "").strip()[:2000],
            "section_id": None,
            "material_id": None,
            "script_status": "pending",
            "slides_status": "pending",
            "script_error": None,
            "slides_error": None,
        })
    return out


def normalize_plan(raw: Any) -> list[dict]:
    """Öffentliche Hülle für die API (manuelle Plan-Edits der UI validieren)."""
    return _normalize_plan(raw)


def _derive_slides_plan(chapter_plan: list[dict]) -> list[dict]:
    """Legacy-Ableitung: 1:1-Folien-Plan aus dem Skript-Plan (Titel/Aktiv/Status).

    Wird genutzt, solange ein Import noch kein eigenes slides_plan hat (vor der
    Pläne-Separierung erstellte Imports).
    """
    out: list[dict] = []
    for ch in chapter_plan or []:
        title = str(ch.get("title") or "").strip()[:300]
        if not title:
            continue
        out.append({
            "title": title,
            "enabled": bool(ch.get("enabled", True)),
            "description": str(ch.get("description") or "").strip()[:2000],
            "material_id": ch.get("material_id"),
            "slides_status": ch.get("slides_status") or "pending",
            "slides_error": ch.get("slides_error"),
        })
    return out


def _normalize_slides_plan(raw: Any) -> list[dict]:
    """Validiert/normalisiert den Folien-Plan (NUR Titel/Aktiv/Beschreibung).

    Analog zum Skript-Plan: Quellen (inkl. pptx-Folienbereiche) werden NICHT im
    Plan gehalten — der Gather-Schritt ermittelt sie vor der Generierung.
    """
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for d in raw[:40]:
        if not isinstance(d, dict):
            continue
        title = str(d.get("title") or "").strip()[:300]
        if not title:
            continue
        out.append({
            "title": title,
            "enabled": bool(d.get("enabled", True)),
            "description": str(d.get("description") or "").strip()[:2000],
            "material_id": None,
            "slides_status": "pending",
            "slides_error": None,
        })
    return out


def normalize_slides_plan(raw: Any) -> list[dict]:
    """Öffentliche Hülle für die API (manuelle Folien-Plan-Edits der UI)."""
    return _normalize_slides_plan(raw)


def _fallback_plan(manifest: list[dict], file_map: dict) -> list[dict]:
    """Fallback: je Text-Datei ein Kapitel (ganze Datei)."""
    out = []
    for m in manifest:
        if not m.get("read_path"):
            continue
        out.append({
            "title": Path(m["path"]).stem[:300],
            "enabled": True,
            "description": f"Quelldatei: {m['path']} (ganze Datei).",
            "section_id": None,
            "material_id": None,
            "script_status": "pending",
            "slides_status": "pending",
            "script_error": None,
            "slides_error": None,
        })
    return out


def _planner_read(manifest: list[dict], staging: Path, action: dict) -> str:
    """read_file-Tool des Planners (Pfad strikt gegen Manifest, Limits)."""
    path = str(action.get("path") or "")
    entry = next((m for m in manifest if m["path"] == path and m.get("read_path")), None)
    if not entry:
        return "FEHLER: Pfad nicht gefunden (nur Pfade aus dem Dateibaum mit read_path sind gültig)."
    p = staging / entry["read_path"]
    if not p.is_file():
        return "FEHLER: Datei nicht lesbar."
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "FEHLER: Datei nicht lesbar."
    n = len(lines)
    if n == 0:
        return "(Datei leer)"
    start = _as_int(action.get("start_line"), 1, 1, n)
    end = _as_int(action.get("end_line"), n, start, n)
    out_lines: list[str] = []
    chars = 0
    for i in range(start, end + 1):
        line = f"{i:>6}| {lines[i - 1]}\n"
        if chars + len(line) > IMPORT_READ_CHARS:
            break
        out_lines.append(line)
        chars += len(line)
    shown_end = start + len(out_lines) - 1 if out_lines else start
    note = f" (Zeilen {shown_end + 1}-{end} wegen Größenlimit nicht gezeigt)" if shown_end < end else ""
    return f"Zeilen {start}-{shown_end} von {n}{note}:\n" + "".join(out_lines)


def _previous_plan_text(entries: list[dict]) -> str:
    """Frühere Plan-Vorschläge (Titel + Beschreibung) als Text für den LLM-Planner."""
    lines: list[str] = []
    for e in entries or []:
        title = str(e.get("title") or "").strip()
        if not title:
            continue
        desc = str(e.get("description") or "").strip() or "(keine Beschreibung)"
        lines.append(f"- {title} — {desc}")
    return "\n".join(lines[:40]) if lines else "(keine vorherigen Vorschläge)"


async def run_planner_job(course_id: int, import_id: int) -> None:
    """Stufe 4: agentic Skript-Planner → chapter_plan.

    Der neue Plan ERSETZT die alten Vorschläge vollständig (die alten Vorschläge
    werden dem LLM mitgegeben, damit es sie übernehmen/ergänzen kann). Bestehende
    Kurs-Kapitel werden bei der Planung NICHT berücksichtigt.
    """
    logger.info("Import %s (Kurs %s): plan gestartet", import_id, course_id)
    try:
        set_stage_status(import_id, "plan", "running")
        set_progress(import_id, current="Kapitel-Planner analysiert die Materialien …")
        snap = job_snapshot(import_id)
        if snap is None:
            return
        staging = staging_dir(course_id, snap["job_id"])
        llm_cfg = get_llm_config(course_id, IMPORT_TIMEOUT_PLAN)

        file_tree = _build_file_tree_text(snap["manifest"])
        digests = _build_digests_text(snap["manifest"], snap["file_map"])
        main_tex_entries = [m["path"] for m in snap["manifest"] if m.get("main_tex")]
        main_tex = (
            f"HINWEIS: Main-TeX-Datei(en): {', '.join(main_tex_entries)} — darin steckt meist die Dokumentstruktur."
            if main_tex_entries else "(keine main.tex mit \\documentclass gefunden)"
        )
        previous_plan = _previous_plan_text(snap["chapter_plan"])

        steps = "(noch keine)"
        tool_budget = 0
        chapters: Optional[list[dict]] = None
        for i in range(IMPORT_PLAN_MAX_ITERATIONS):
            if not await gate(import_id, "plan"):
                return
            set_progress(import_id, current=f"Kapitel-Planner: Schritt {i + 1}/{IMPORT_PLAN_MAX_ITERATIONS} …")
            res = await llm_service.import_script_planner_step(
                file_tree, digests, main_tex, previous_plan, steps, config=llm_cfg
            )
            if not res["success"]:
                break
            action = res["data"] or {}
            kind = str(action.get("action") or "")
            if kind == "read_file":
                tool_out = _planner_read(snap["manifest"], staging, action)
                tool_budget += len(tool_out)
                steps += (
                    f"\n### Schritt {i + 1}\nLLM: {json.dumps(action, ensure_ascii=False)}\n"
                    f"Tool: {tool_out}\n"
                )
                if tool_budget > IMPORT_PLAN_TOOL_BUDGET:
                    steps += "(Werkzeug-Budget erschöpft — gib JETZT den final_plan aus.)\n"
                continue
            if kind == "final_plan":
                raw_chapters = action.get("script_plan")
                if raw_chapters is None:
                    raw_chapters = action.get("chapters")  # altes (einzelnes) Format
                chapters = _normalize_plan(raw_chapters)
                if action.get("notes"):
                    append_report(import_id, warnings=[f"Planner-Anmerkung: {str(action['notes'])[:300]}"])
                break
            steps += (
                f"\n### Schritt {i + 1}\nLLM: {json.dumps(action, ensure_ascii=False)}\n"
                "Tool: (unbekannte Aktion — verwende entweder read_file oder final_plan)\n"
            )

        if chapters is None or not chapters:
            chapters = _fallback_plan(snap["manifest"], snap["file_map"])
            append_report(import_id, warnings=[
                "Kapitel-Planner lief nicht erfolgreich zu Ende — Fallback-Plan erstellt (je Text-Datei ein Kapitel)."
            ])
        # Der neue Plan ersetzt die alten Vorschläge komplett.
        set_chapter_plan(import_id, chapters)
        set_stage_status(import_id, "plan", "done")
        set_progress(import_id, current="Kapitel-Vorschläge erstellt.")
        logger.info("Import %s: plan abgeschlossen (%d Vorschläge)", import_id, len(chapters))
    except Exception as exc:
        logger.exception("Import %s: plan Job fehlgeschlagen", import_id)
        append_report(import_id, errors=[f"plan-Job intern fehlgeschlagen: {exc}"])
        if stage_status(import_id, "plan") == "running":
            set_stage_status(import_id, "plan", "error")


async def run_slides_planner_job(course_id: int, import_id: int) -> None:
    """Stufe 4b: agentic Folien-Planner → slides_plan.

    Der neue Plan ERSETZT die alten Vorschläge vollständig (die alten Vorschläge
    werden dem LLM mitgegeben, damit es sie übernehmen/ergänzen kann). Bestehende
    Kurs-Decks/-Kapitel werden bei der Planung NICHT berücksichtigt.
    """
    logger.info("Import %s (Kurs %s): slides_plan gestartet", import_id, course_id)
    try:
        set_stage_status(import_id, "slides_plan", "running")
        set_progress(import_id, current="Folien-Planner analysiert die Materialien …")
        snap = job_snapshot(import_id)
        if snap is None:
            return
        staging = staging_dir(course_id, snap["job_id"])
        llm_cfg = get_llm_config(course_id, IMPORT_TIMEOUT_PLAN)

        file_tree = _build_file_tree_text(snap["manifest"])
        digests = _build_digests_text(snap["manifest"], snap["file_map"])
        zip_decks = _build_zip_decks_text(snap["manifest"], staging)
        previous_plan = _previous_plan_text(snap["slides_plan"])

        steps = "(noch keine)"
        tool_budget = 0
        decks: Optional[list[dict]] = None
        for i in range(IMPORT_PLAN_MAX_ITERATIONS):
            if not await gate(import_id, "slides_plan"):
                return
            set_progress(import_id, current=f"Folien-Planner: Schritt {i + 1}/{IMPORT_PLAN_MAX_ITERATIONS} …")
            res = await llm_service.import_slides_planner_step(
                file_tree, digests, zip_decks, previous_plan, steps, config=llm_cfg
            )
            if not res["success"]:
                break
            action = res["data"] or {}
            kind = str(action.get("action") or "")
            if kind == "read_file":
                tool_out = _planner_read(snap["manifest"], staging, action)
                tool_budget += len(tool_out)
                steps += (
                    f"\n### Schritt {i + 1}\nLLM: {json.dumps(action, ensure_ascii=False)}\n"
                    f"Tool: {tool_out}\n"
                )
                if tool_budget > IMPORT_PLAN_TOOL_BUDGET:
                    steps += "(Werkzeug-Budget erschöpft — gib JETZT den final_plan aus.)\n"
                continue
            if kind == "final_plan":
                raw = action.get("slides_plan")
                if raw is None:
                    raw = action.get("decks")  # Fallback-Format
                decks = _normalize_slides_plan(raw)
                if action.get("notes"):
                    append_report(import_id, warnings=[f"Folien-Planner-Anmerkung: {str(action['notes'])[:300]}"])
                break
            steps += (
                f"\n### Schritt {i + 1}\nLLM: {json.dumps(action, ensure_ascii=False)}\n"
                "Tool: (unbekannte Aktion — verwende entweder read_file oder final_plan)\n"
            )

        if decks is None or not decks:
            # Fallback: 1:1 aus den Skript-Kapiteln ableiten
            decks = _derive_slides_plan(snap["chapter_plan"])
            append_report(import_id, warnings=[
                "Folien-Planner lief nicht erfolgreich zu Ende — Folien-Plan aus dem Skript-Plan abgeleitet."
            ])
        # Der neue Plan ersetzt die alten Vorschläge komplett.
        set_slides_plan(import_id, decks)
        set_stage_status(import_id, "slides_plan", "done")
        set_progress(import_id, current="Deck-Vorschläge erstellt.")
        logger.info("Import %s: slides_plan abgeschlossen (%d Vorschläge)", import_id, len(decks))
    except Exception as exc:
        logger.exception("Import %s: slides_plan Job fehlgeschlagen", import_id)
        append_report(import_id, errors=[f"slides_plan-Job intern fehlgeschlagen: {exc}"])
        if stage_status(import_id, "slides_plan") == "running":
            set_stage_status(import_id, "slides_plan", "error")


# ═══════════════════════════════════════════════════════════════════
# Sanitizer
# ═══════════════════════════════════════════════════════════════════


def _resolve_image_path(ref: str, image_map: dict) -> Optional[str]:
    """LaTeX-Bildpfad → importierte /media-URL (exakt, mit Exts, per Basename)."""
    ref = (ref or "").strip()
    if not ref:
        return None
    for ext in ("", ".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg"):
        full = ref if ext == "" or ref.endswith(ext) else ref + ext
        if full in image_map:
            return image_map[full]
    base = ref.rsplit("/", 1)[-1]
    for ext in ("", ".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg"):
        b = base if ext == "" or base.endswith(ext) else base + ext
        for p, url in image_map.items():
            if p.rsplit("/", 1)[-1] == b:
                return url
    return None


# ─── Sanitizer (LLM-Output mit untrusted Input — Defense in Depth) ───

_DANGEROUS_TAG_RE = re.compile(
    r"</?\s*(?:script|style|iframe|object|embed|link|meta|base|form|input|button|select|"
    r"svg|canvas|audio|video|source|template|applet|frame|frameset|marquee|img)\b[^>]*>",
    re.IGNORECASE,
)
_ON_ATTR_RE = re.compile(r"""\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_JS_URL_RE = re.compile(r"javascript\s*:", re.IGNORECASE)
_EXT_URL_RE = re.compile(r"https?://[^\s)\]}>\"'`]+")


def sanitize_untrusted_markdown(text: str) -> tuple[str, list[str]]:
    """Fence-aware: gefährliche HTML-Elemente/Event-Handler aus LLM-Output entfernen.

    Returns: (gereinigter Text, Warnungen)
    """
    warnings: list[str] = []
    out: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    skip_tag = ""  # offener gefährlicher Block (script/style/iframe) über mehrere Zeilen
    n_dangerous = 0
    for line in (text or "").splitlines():
        stripped = line.strip()
        if in_fence:
            out.append(line)
            m = re.match(r"^(`{3,}|~{3,})\s*$", stripped)
            if m and m.group(1)[0] == fence_char and len(m.group(1)) >= fence_len:
                in_fence = False
            continue
        m = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if m:
            in_fence = True
            fence_char = m.group(1)[0]
            fence_len = len(m.group(1))
            out.append(line)
            continue
        if skip_tag:
            if f"</{skip_tag}" in line.lower():
                skip_tag = ""
            continue
        cleaned = line
        found = _DANGEROUS_TAG_RE.findall(cleaned)
        if found:
            n_dangerous += len(found)
            cleaned = _DANGEROUS_TAG_RE.sub("", cleaned)
        cleaned = _ON_ATTR_RE.sub("", cleaned)
        if _JS_URL_RE.search(cleaned):
            warnings.append("Import: 'javascript:-URL' aus generiertem Inhalt entfernt.")
            cleaned = _JS_URL_RE.sub("", cleaned)
        for tag in ("script", "style", "iframe"):
            if re.search(rf"<{tag}\b", cleaned, re.I) and not re.search(rf"</{tag}\s*>", cleaned, re.I):
                cleaned = re.sub(rf"<{tag}\b[^>]*>", "", cleaned, flags=re.I)
                skip_tag = tag
                break
        out.append(cleaned)
    if n_dangerous:
        warnings.append(f"Import: {n_dangerous} HTML-Tags (script/iframe/img/…) aus LLM-Antwort entfernt.")
    ext = sorted(set(_EXT_URL_RE.findall(text or "")))
    if ext:
        shown = ", ".join(ext[:10]) + ("…" if len(ext) > 10 else "")
        warnings.append(f"Import: externe URL(s) im generierten Inhalt gefunden: {shown}")
    return "\n".join(out), warnings


def strip_outer_fence(text: str) -> str:
    """Wrapping ```-Codeblock von der LLM-Antwort entfernen (falls vorhanden)."""
    t = (text or "").strip()
    m = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n```", t, re.S)
    if m:
        return m.group(1).strip()
    return t



# ═══════════════════════════════════════════════════════════════════
# STUFE 5: script (wortgetreue Kapitel-Konvertierung → ScriptSection)
# ═══════════════════════════════════════════════════════════════════

def _existing_section_labels(course_id: int) -> set[str]:
    labels: set[str] = set()
    with Session(engine) as s:
        for sec in s.exec(
            select(ScriptSection).where(ScriptSection.course_id == course_id)
        ).all():
            m = re.match(r"\s*\{#sec:([a-z0-9_]+)\}", sec.content or "")
            if m:
                labels.add(m.group(1))
    return labels


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (title or "").lower()).strip("_")[:40]


def _chapter_label(title: str, idx: int, used_labels: set[str]) -> str:
    base = _slugify(title) or f"kapitel_{idx + 1}"
    label, k = base, 2
    while label in used_labels:
        label = f"{base}_{k}"
        k += 1
    used_labels.add(label)
    return label


def _plan_digest(ch: dict, file_map: dict) -> str:
    """Grobe Zusammenfassung eines Plan-Kapitels aus den Chunk-Digests (Fallback-Kontext)."""
    parts = []
    desc = (ch.get("description") or "").strip()
    if desc:
        parts.append(desc)
    for src in ch.get("sources") or []:
        entry = file_map.get(src.get("file", "")) or {}
        for c in entry.get("chunks") or []:
            if not c.get("summary"):
                continue
            if c.get("start_line", 0) <= src.get("end_line", 10**9) and c.get("end_line", 0) >= src.get("start_line", 1):
                parts.append(c["summary"])
    return " ".join(parts)[:400] or "(keine Zusammenfassung)"


def _collect_sources(staging: Path, ch: dict, by_path: dict) -> tuple[str, set[str], bool]:
    """Liest alle Quell-Slices eines Kapitels.

    Returns: (concatenierter Text, referenzierte Bild-Pfade, enthalt_tex)
    """
    parts: list[str] = []
    img_refs: set[str] = set()
    has_tex = False
    for src in ch.get("sources") or []:
        m = by_path.get(str(src.get("file") or ""))
        if not m or not m.get("read_path"):
            continue
        p = staging / m["read_path"]
        if not p.is_file():
            continue
        sline = int(src.get("start_line") or 1)
        eline = int(src.get("end_line") or 10**9)
        try:
            text = _read_slice(p, sline, eline)
        except OSError:
            continue
        if not text.strip():
            continue
        if m.get("type") == "tex":
            has_tex = True
            img_refs.update(_INC_GRAPHICS_RE.findall(text))
        else:
            # Markdown-Bilder in md-Quellen
            for alt, url in re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text):
                if not url.startswith(("http", "/media/")):
                    img_refs.add(url)
        # Marker-Zeile: Datei + Zeilenbereich, damit das LLM die Herkunft jedes
        # Ausschnitts zuordnen kann (im Prompt als ignorierbare Marker erklärt).
        parts.append(f"%--- start {src.get('file') or m['path']}, Zeilen {sline}-{eline} ---\n{text}")
    return "\n\n".join(parts), img_refs, has_tex


def _image_table(
    img_refs: set[str], image_map: dict, descriptions: Optional[dict[str, str]] = None
) -> str:
    """ALLE importierten Medien (Zip-Pfad → Medien-URL, mit Kurzbeschreibung); in den
    Quellen referenzierte Einträge werden markiert. Nicht auflösbare Referenzen
    werden separat gemeldet."""
    lines = []
    entries = []
    descs = descriptions or {}
    for path in sorted(image_map):
        url = image_map[path]
        if not url:
            continue
        marked = any(_resolve_image_path(ref, image_map) == url for ref in img_refs)
        entry = f"{path} → {url}"
        d = (descs.get(path) or "").strip()
        if d:
            entry += f" — „{d}“"
        if marked:
            entry += "  [in den Quellen referenziert]"
        entries.append(entry)
    if entries:
        lines.append("ALLE importierten Medien (Original-Pfad → Medien-URL, mit Kurzbeschreibung; für Bilder NUR diese URLs verwenden):")
        lines.append("\n".join(entries[:300]))
    unresolved = [ref for ref in sorted(img_refs) if not _resolve_image_path(ref, image_map)]
    if unresolved:
        lines.append("NICHT importiert (Bild entfernen, Caption als Text behalten): " + ", ".join(unresolved[:50]))
    return "\n".join(lines) or "(keine Medien importiert)"


def _media_descriptions(snap: dict) -> dict[str, str]:
    """Zip-Pfad → Kurzbeschreibung (llm_description) der importierten Medien.

    Für Bild-Mappings in den Import-Prompts: Das LLM soll anhand der Beschreibung
    die passende Abbildung an der richtigen Stelle einsetzen.
    """
    ids: set[int] = set()
    for info in snap["media_map"].values():
        for mid in info.get("media_ids") or []:
            try:
                ids.add(int(mid))
            except (TypeError, ValueError):
                pass
    if not ids:
        return {}
    id_descs: dict[int, str] = {}
    with Session(engine) as s:
        for m in s.exec(select(CourseMedia).where(CourseMedia.id.in_(ids))).all():
            d = (m.llm_description or "").strip()
            if d:
                id_descs[m.id] = d[:300]
    out: dict[str, str] = {}
    for path, info in snap["media_map"].items():
        for mid in info.get("media_ids") or []:
            try:
                d = id_descs.get(int(mid))
            except (TypeError, ValueError):
                continue
            if d:
                out[path] = d
                break
    return out


def _deck_summary_lines(course_id: int, exclude_ids: Optional[set[int]] = None) -> list[str]:
    """Folien-Decks des Kurses (Titel + interne Zusammenfassung) für Cross-Ref-Kontexte."""
    exclude = exclude_ids or set()
    lines: list[str] = []
    with Session(engine) as s:
        mats = s.exec(
            select(CourseMaterial)
            .where(CourseMaterial.course_id == course_id)
            .where(CourseMaterial.material_type == MaterialType.SLIDES)
            .order_by(CourseMaterial.display_order.asc(), CourseMaterial.id.asc())  # type: ignore[attr-defined]
        ).all()
        for mat in mats:
            if mat.id in exclude:
                continue
            summ = (mat.summary or "").strip()
            if summ:
                lines.append(f"- {mat.title}: {summ}")
    return lines


def _deck_other_context(snap: dict, course_id: int, current_material_id: Optional[int]) -> str:
    """Kontext-Block für Deck-Prompts (generieren): andere Folien-Decks und
    Skript-Kapitel mit internen Zusammenfassungen (Medien kommen über das Bild-Mapping)."""
    deck_lines = _deck_summary_lines(course_id, exclude_ids={current_material_id} if current_material_id else set())
    chapter_lines: list[str] = []
    with Session(engine) as s:
        for c in snap["chapter_plan"]:
            if not c.get("section_id"):
                continue
            sec = s.get(ScriptSection, c["section_id"])
            summ = (sec.summary or "").strip() if sec else ""
            chapter_lines.append(f"- {c['title']}: {summ or _plan_digest(c, snap['file_map'])}")
    return (
        "FOLIEN-DECKS:\n" + ("\n".join(deck_lines) or "(keine)") + "\n\n"
        "SKRIPT-KAPITEL:\n" + ("\n".join(chapter_lines) or "(keine)")
    )


def _with_decks(others: str, deck_context: str) -> str:
    """Kapitel-Kontext + Folien-Deck-Zusammenfassungen unter einem Block vereinen."""
    if not deck_context:
        return others or "(keine)"
    if not others or others == "(keine)":
        return "FOLIEN-DECKS DES KURSES:\n" + deck_context
    return others + "\n\nFOLIEN-DECKS DES KURSES:\n" + deck_context


async def _generate_chapter(
    course_id: int,
    import_id: int,
    staging: Path,
    snap: dict,
    ch: dict,
    idx: int,
    by_path: dict,
    used_labels: set[str],
    chapter_summaries: dict[int, str],
    llm_cfg: dict,
    llm_sem: asyncio.Semaphore,
    deck_context: str = "",
    media_descs: Optional[dict[str, str]] = None,
) -> tuple[str, str, list[str]]:
    """Generiert das Markdown eines Kapitels. Returns: (content, sec_label, warnings).

    deck_context: Zusammenfassungen der Folien-Decks des Kurses (Cross-Ref-Kontext).
    media_descs: Zip-Pfad → Kurzbeschreibung der importierten Medien (Bild-Mapping).
    """
    warns: list[str] = []
    raw, img_refs, _has_tex = await asyncio.to_thread(_collect_sources, staging, ch, by_path)
    if not raw.strip():
        raise RuntimeError("Kein Quelltext für dieses Kapitel (Sources leer/ungültig).")

    # Bild-URLs (nur importierte Medien)
    url_map = {}
    for p, info in snap["media_map"].items():
        if info.get("url"):
            url_map[p] = info["url"]
    # Nicht auflösbare Bildreferenzen: explizit melden (sonst fallen die Bilder
    # still weg, wenn das LLM sie laut Prompt entfernt, und der User sieht es nicht)
    unresolved_imgs = [r for r in sorted(img_refs) if not _resolve_image_path(r, url_map)]
    if unresolved_imgs:
        warns.append(
            f"Skript „{ch['title']}“: {len(unresolved_imgs)} Bildreferenz(en) nicht in den importierten "
            f"Medien auffindbar — die Bilder fallen weg: {', '.join(unresolved_imgs[:10])}"
        )
    # Debug-Log: welche Quellen (Dateien/Zeilen) + Bildstatistik
    src_desc = ", ".join(
        f"{s.get('file')} (Z {s.get('start_line') or 1}–{s.get('end_line')})" for s in (ch.get("sources") or [])[:20]
    ) or "(keine)"
    log_bits = [f"Quellen: {src_desc}"]
    if img_refs:
        log_bits.append(f"Bilder: {len(img_refs) - len(unresolved_imgs)}/{len(img_refs)} aufgelöst")
    append_report(import_id, log=[f"Skript „{ch['title']}“: " + " | ".join(log_bits)])
    # Roh bleibt roh: Das LLM konvertiert LaTeX/Markdown selbst (Prompt erklärt
    # die Marker-Zeilen) — eine vor-konvertierte Fassung verwirrte es eher.
    # EIN LLM-Call pro Kapitel (wie bei Slides): der Quelltext wird NICHT
    # gekappt — bei sehr großen Inputs nur warnen (kleine Kontextfenster
    # könnten überlaufen).
    if len(raw) > CHAPTER_INPUT_WARN_CHARS:
        warns.append(
            f"Skript „{ch['title']}“: Achtung, sehr großer Input ({len(raw)} Zeichen) — "
            f"bei zu kleinen Kontextfenstern können Probleme auftauchen."
        )

    # Kontext: andere Kapitel (generierte Summaries, sonst Plan-Digests) — auch
    # bereits existierende Kapitel (section_id), selbst wenn sie in diesem Lauf
    # nicht (neu)generiert werden
    others = []
    for i, c in enumerate(snap["chapter_plan"]):
        if i == idx:
            continue
        if not c.get("enabled") and not c.get("section_id"):
            continue  # weder aktiv noch bereits generiert → kein Kontext
        s = chapter_summaries.get(i) or _plan_digest(c, snap["file_map"])
        others.append(f"- {c['title']}: {s}")
    other_text = _with_decks("\n".join(others), deck_context)
    references_text = await asyncio.to_thread(_build_references_text, course_id)

    async with llm_sem:
        if not await gate(import_id, "script"):
            raise RuntimeError("gestoppt (Pause/Cancel)")
        res = await llm_service.import_convert_chapter(
            chapter_title=ch["title"],
            other_chapters=other_text,
            image_map=_image_table(img_refs, url_map, media_descs or {}),
            references=references_text,
            source_text=raw,
            config=llm_cfg,
            # Output-Budget: das konvertierte Kapitel ist grob so lang wie die
            # Quelle (≈1 Token pro 4 Zeichen) + Puffer. (Provider clamps ggf. auf
            # das Modell-Output-Limit; abgeschnittene Antworten werden in
            # _call_plain automatisch fortgesetzt.)
            max_tokens=min(65536, len(raw) // 4 + 8192),
        )
    if not res["success"]:
        raise RuntimeError(f"LLM-Konvertierung fehlgeschlagen: {res.get('error')}")
    content = strip_outer_fence(res["data"]["model_solution"]).strip()
    if not content:
        raise RuntimeError("LLM hat leeren Inhalt geliefert.")
    # Das LLM-Kapitel-Label (aus dem Quell-\label abgeleitet, s. Label-Regeln im
    # Prompt) wird übernommen — es ist Single Source of Truth für Cross-Refs
    # (Summaries, andere Kapitel, Folien). Fehlt es, wird ein deterministischer
    # Title-Slug-Label vorgeworfen (damit @sec:-Referenzen aufs Kapitel
    # funktionieren); used_labels hält den Slug kollisionsfrei.
    m = re.match(r"^\s*\{#sec:([a-z0-9_]+)\}\s*", content)
    if m:
        sec_label = m.group(1)
        used_labels.add(sec_label)
    else:
        sec_label = _chapter_label(ch["title"], idx, used_labels)
        content = f"{{#sec:{sec_label}}}\n\n" + content

    # Sanitizer (untrusted input!)
    content, sanitize_warns = sanitize_untrusted_markdown(content)
    warns.extend(sanitize_warns)

    # Post-Check: erwartete Bild-URLs vorhanden?
    expected = [_resolve_image_path(r, url_map) for r in img_refs]
    for url in {u for u in expected if u}:
        if url not in content:
            warns.append(f"Skript „{ch['title']}“: erwartetes Bild {url} fehlt im generierten Inhalt.")
    return content, sec_label, warns


def _save_section(
    course_id: int, created_by: int, title: str, content: str, order: int,
    section_id: Optional[int], summary: str = "",
) -> int:
    with Session(engine) as s:
        sec: Optional[ScriptSection] = None
        if section_id:
            sec = s.get(ScriptSection, section_id)
        if sec:
            sec.title = title[:300]
            sec.content = content
            sec.display_order = order
            if summary:
                sec.summary = summary
        else:
            sec = ScriptSection(
                course_id=course_id, title=title[:300], content=content,
                is_visible=False, display_order=order, created_by=created_by,
                summary=summary,
            )
            s.add(sec)
        s.commit()
        s.refresh(sec)
        assert sec.id is not None
        return sec.id


GATHER_KIND_RULES = {
    "script": (
        "- Für das Skript-Kapitel muss der Inhalt möglichst WORTGETREU generiert werden:"
        " nimm in 'sources' ALLE Dateien/Bereiche auf, die den Lehrinhalt dieses Kapitels enthalten"
        " (nichts Wichtiges auslassen, in Lehrstoff-Reihenfolge)."
    ),
    "slides": (
        "- Für Slide-Decks genügen kompakte Quellen (die Folien fassen den Inhalt zusammen)."
    ),
}

async def _gather_sources(
    course_id: int,
    import_id: int,
    snap: dict,
    staging: Path,
    title: str,
    description: str,
    kind: str,
    stage: str,
    llm_cfg: dict,
    llm_sem: asyncio.Semaphore,
) -> Optional[tuple[list[dict], str]]:
    """Agentic Quellen-Sammlung für ein Kapitel/Deck (LLM-Action-Loop).

    Das LLM ermittelt aus BESCHREIBUNG (Quelldateien mit Pfad + Zeilenbereich,
    vom Plan) + Dateibaum + Digests die konkreten Quellen in der Zip.
    Tools: read_file (gestagte Dateien), finish (sources + notes).
    Returns: (sources, notes) oder None = LLM-Aufruf fehlgeschlagen.
    LLM-Output ist untrusted: Pfade werden gegen das Manifest gewhitelisted,
    Zeilen gegen line_count geclamped.
    """
    manifest = snap["manifest"]
    by_path = {m["path"]: m for m in manifest}
    file_tree = _build_file_tree_text(manifest)
    digests = _build_digests_text(manifest, snap["file_map"])
    if len(digests) > 24000:
        digests = digests[:24000] + "\n… (Digests gekürzt)"
    media_text = _build_media_list_text(course_id)
    references_text = _build_references_text(course_id)

    steps = "(noch keine)"
    tool_budget = 0
    for i in range(IMPORT_GATHER_MAX_ITERATIONS):
        if not await gate(import_id, stage):
            raise RuntimeError("gestoppt (Pause/Cancel)")
        set_progress(
            import_id,
            current=f'Quellen-Sammlung „{title}“: Schritt {i + 1}/{IMPORT_GATHER_MAX_ITERATIONS} …',
        )
        async with llm_sem:
            res = await llm_service.import_gather_step(
                kind, GATHER_KIND_RULES.get(kind, ""), title, description or "(keine)",
                file_tree, digests, media_text, references_text, steps, config=llm_cfg,
            )
        if not res["success"]:
            logger.warning(
                "Import %s: Quellen-Sammlung für „%s“ fehlgeschlagen: %s", import_id, title, res.get("error")
            )
            return None
        action = res["data"] or {}
        act = str(action.get("action") or "")
        if act == "read_file":
            tool_out = _planner_read(manifest, staging, action)
            tool_budget += len(tool_out)
            steps += (
                f"\n### Schritt {i + 1}\nLLM: {json.dumps(action, ensure_ascii=False)}\n"
                f"Tool: {tool_out}\n"
            )
            if tool_budget > IMPORT_PLAN_TOOL_BUDGET:
                steps += "(Werkzeug-Budget erschöpft — gib JETZT finish aus.)\n"
            continue
        if act == "finish":
            out: list[dict] = []
            raw_srcs = action.get("sources")
            for src in (raw_srcs if isinstance(raw_srcs, list) else [])[:20]:
                if not isinstance(src, dict):
                    continue
                f = str(src.get("file") or "")
                m = by_path.get(f)
                if not m or not m.get("read_path"):
                    continue
                lc = max(1, int(m.get("line_count") or 1))
                s = _as_int(src.get("start_line"), 1, 1, lc)
                e = _as_int(src.get("end_line"), lc, s, lc)
                out.append({"file": f, "start_line": s, "end_line": e})
            notes = str(action.get("notes") or "").strip()[:500]
            return out, notes
        steps += (
            f"\n### Schritt {i + 1}\nLLM: {json.dumps(action, ensure_ascii=False)}\n"
            "Tool: (unbekannte Aktion — verwende read_file oder finish)\n"
        )

    # Iterations-Budget erschöpft → keine Quellen ermittelt
    append_report(import_id, warnings=[
        f'Quellen-Sammlung „{title}“ unvollständig (Iterations-Budget) — keine Quellen ermittelt.'
    ])
    return [], ""


async def run_script_job(course_id: int, import_id: int) -> None:
    """Stufe 5: enabled Kapitel (Status != done) sequentiell konvertieren."""
    logger.info("Import %s (Kurs %s): script gestartet", import_id, course_id)
    try:
        set_stage_status(import_id, "script", "running")
        snap = job_snapshot(import_id)
        if snap is None:
            return
        staging = staging_dir(course_id, snap["job_id"])
        by_path = {m["path"]: m for m in snap["manifest"]}
        enabled = [c for c in snap["chapter_plan"] if c.get("enabled")]
        todo = [c for c in snap["chapter_plan"] if c.get("enabled") and c.get("script_status") != "done"]
        if not todo:
            set_stage_status(import_id, "script", "done")
            return

        used_labels = _existing_section_labels(course_id)
        # Labels bereits generierter Kapitel dieses Imports mitzählen
        for i, c in enumerate(snap["chapter_plan"]):
            if c.get("section_id"):
                with Session(engine) as s:
                    sec = s.get(ScriptSection, c["section_id"])
                    if sec:
                        m = re.match(r"\s*\{#sec:([a-z0-9_]+)\}", sec.content or "")
                        if m:
                            used_labels.add(m.group(1))
        chapter_summaries: dict[int, str] = {}
        for i, c in enumerate(snap["chapter_plan"]):
            # Bestehende Kapitel mit Summary gehören immer in den Kontext —
            # unabhängig davon, ob sie in diesem Job (neu)generiert werden.
            if c.get("section_id"):
                with Session(engine) as s:
                    sec = s.get(ScriptSection, c["section_id"])
                    if sec and sec.summary:
                        chapter_summaries[i] = sec.summary
        # Folien-Decks des Kurses (Summary) + Medien-Kurzbeschreibungen für den Kontext
        deck_context = "\n".join(_deck_summary_lines(course_id))
        media_descs = await asyncio.to_thread(_media_descriptions, snap)

        llm_cfg = get_llm_config(course_id, IMPORT_TIMEOUT_CONVERT)
        llm_sem = asyncio.Semaphore(IMPORT_LLM_CONCURRENCY)

        # Plan-Reihenfolge = Ziel-Reihenfolge im Kurs: bestehende Kapitel werden zu
        # Job-Start durchnummeriert; neue Kapitel erhalten beim Speichern dieselbe
        # Position (order=idx); Kurs-Kapitel AUSSERHALB des Plans rücken ans Ende
        # (in ihrer bisherigen relativen Reihenfolge).
        plan_section_ids = [ch["section_id"] for ch in snap["chapter_plan"] if ch.get("section_id")]
        with Session(engine) as s:
            order = 0
            for ch in snap["chapter_plan"]:
                if ch.get("section_id"):
                    sec = s.get(ScriptSection, ch["section_id"])
                    if sec:
                        sec.display_order = order
                order += 1
            if plan_section_ids:
                tail = s.exec(
                    select(ScriptSection)
                    .where(ScriptSection.course_id == course_id)
                    .where(~ScriptSection.id.in_(plan_section_ids))
                    .order_by(ScriptSection.display_order.asc(), ScriptSection.id.asc())  # type: ignore[attr-defined]
                ).all()
                for sc in tail:
                    sc.display_order = order
                    order += 1
            s.commit()

        for idx, ch in enumerate(snap["chapter_plan"]):
            if not ch.get("enabled") or ch.get("script_status") == "done":
                continue
            unit_key = f"script:{idx}"
            if not await gate(import_id, "script"):
                return
            set_progress(import_id, current=f"Skript: {ch['title']}", unit_key=unit_key, unit_total=1)
            try:
                # Kapitel ohne Quellen → LLM ermittelt sie aus Beschreibung + Dateibaum.
                # Auch bereits generierte Kapitel (section_id, erneute Generierung)
                # werden „gathered“, solange sie keine Quellen haben; das Kapitel wird
                # danach komplett neu aus den Import-Materialien generiert.
                # (Sobald sources gesetzt sind → Gather wird übersprungen, resume-safe.)
                if not ch.get("sources"):
                    set_progress(import_id, current=f"Skript: {ch['title']} — Quellen werden aus den Materialien ermittelt …")
                    gathered = await _gather_sources(
                        course_id, import_id, snap, staging, ch["title"],
                        ch.get("description") or "",
                        "script", "script", llm_cfg, llm_sem,
                    )
                    if gathered is None:
                        update_plan_chapter(import_id, idx, {
                            "script_status": "error",
                            "script_error": "Quellen-Sammlung fehlgeschlagen (LLM-Aufruf).",
                        })
                        append_report(import_id, errors=[
                            f"Skript-Kapitel „{ch['title']}“: Quellen-Sammlung fehlgeschlagen (LLM-Aufruf)."
                        ])
                        continue
                    sources, _notes = gathered
                    patch: dict[str, Any] = {}
                    if sources:
                        patch["sources"] = sources
                    if patch:
                        update_plan_chapter(import_id, idx, patch)
                        ch.update(patch)
                        append_report(import_id, warnings=[
                            f"Skript-Kapitel „{ch['title']}“: Quellen automatisch ermittelt "
                            f"({len(sources)} Datei(n))."
                        ])
                        gather_log = ", ".join(
                            f"{s.get('file')} (Z {s.get('start_line') or 1}–{s.get('end_line')})" for s in sources[:20]
                        ) or "(keine)"
                        append_report(import_id, log=[f"Skript „{ch['title']}“: Quellen automatisch ermittelt: {gather_log}"])
                    if not sources:
                        update_plan_chapter(import_id, idx, {
                            "script_status": "error",
                            "script_error": "Keine passenden Quellen in den importierten Materialien gefunden.",
                        })
                        append_report(import_id, errors=[
                            f"Skript-Kapitel „{ch['title']}“: Keine passenden Quellen in den importierten Materialien gefunden."
                        ])
                        continue
                content, _sec_label, warns = await _generate_chapter(
                    course_id, import_id, staging, snap, ch, idx,
                    by_path, used_labels, chapter_summaries, llm_cfg, llm_sem,
                    deck_context=deck_context, media_descs=media_descs,
                )
                for w in warns:
                    append_report(import_id, warnings=[w])
                # Summary zuerst (für Folgekapitel-Kontext), dann speichern
                summary = ""
                async with llm_sem:
                    if await gate(import_id, "script"):
                        res = await llm_service.import_chapter_summary(
                            ch["title"], content, config=llm_cfg
                        )
                    else:
                        res = {"success": False}
                if res.get("success"):
                    summary = strip_outer_fence(res["data"]["model_solution"])
                section_id = _save_section(
                    course_id, snap["created_by"], ch["title"], content, idx,
                    ch.get("section_id"), summary,
                )
                chapter_summaries[idx] = summary or _plan_digest(ch, snap["file_map"])
                update_plan_chapter(import_id, idx, {
                    "script_status": "done", "script_error": None, "section_id": section_id,
                    # Generiert → Checkbox deaktivieren (erneutes Anhaken = Neu-Generierung)
                    "enabled": False,
                })
                set_progress(import_id, unit_key=unit_key, unit_done=1)
            except RuntimeError as e:
                if "gestoppt" in str(e):
                    return
                logger.warning("Import %s: Kapitel %s: %s", import_id, ch["title"], e)
                update_plan_chapter(import_id, idx, {"script_status": "error", "script_error": str(e)[:500]})
                append_report(import_id, errors=[f"Skript-Kapitel „{ch['title']}“: {e}"])
            except Exception as e:
                logger.exception("Import %s: Kapitel %s fehlgeschlagen", import_id, ch["title"])
                update_plan_chapter(import_id, idx, {"script_status": "error", "script_error": str(e)[:500]})
                append_report(import_id, errors=[f"Skript-Kapitel „{ch['title']}“: {e}"])

        set_stage_status(import_id, "script", "done")
        set_progress(import_id, current="Skript-Generierung abgeschlossen.")
        logger.info("Import %s: script abgeschlossen", import_id)
    except Exception as exc:
        logger.exception("Import %s: script Job fehlgeschlagen", import_id)
        append_report(import_id, errors=[f"script-Job intern fehlgeschlagen: {exc}"])
        if stage_status(import_id, "script") == "running":
            set_stage_status(import_id, "script", "error")


# ═══════════════════════════════════════════════════════════════════
# STUFE 6: slides (Slide-Decks pro Kapitel → CourseMaterial SLIDES)
# ═══════════════════════════════════════════════════════════════════

def _extract_labels(content: str) -> set[str]:
    labels: set[str] = set()
    for kind in ("fig", "eq", "code", "box"):
        labels.update(re.findall(rf"\{{#{kind}:([a-z0-9_]+)\}}", content or ""))
    return labels


def cap_slides(content: str, max_slides: int) -> str:
    """Deck (fence-aware) auf max_slides Folien kürzen (rest wird abgeschnitten)."""
    out: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    seps = 0
    for line in (content or "").splitlines():
        stripped = line.strip()
        if in_fence:
            out.append(line)
            m = re.match(r"^(`{3,}|~{3,})\s*$", stripped)
            if m and m.group(1)[0] == fence_char and len(m.group(1)) >= fence_len:
                in_fence = False
            continue
        m = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if m:
            in_fence = True
            fence_char = m.group(1)[0]
            fence_len = len(m.group(1))
            out.append(line)
            continue
        if not out and not stripped:
            continue
        if stripped == "---":
            seps += 1
            if seps >= max_slides:  # max_slides-1 Trenner = max_slides Folien
                break
        out.append(line)
    return "\n".join(out)


def _deck_output_tokens(n_src_slides: int, source_chars: int) -> int:
    """Output-Budget für die Deck-Generierung.

    1:1-Modus: skaliert mit der Anzahl der Quell-Folien (~600 Token/Folie).
    Sonst: skaliert grob mit der Quelltext-Größe (≈ 1 Token pro 4 Zeichen)
    mit 64k-Cap — ohne Foliengrenze kann ein Deck deutlich über 25 Folien
    werden, die Standard-SLIDES_MAX_TOKENS reichen dann nicht.
    """
    if n_src_slides:
        return max(SLIDES_MAX_TOKENS, 600 * n_src_slides)
    return max(SLIDES_MAX_TOKENS, min(65536, source_chars // 4 + 4096))


def _deck_source_sections(snap: dict, dk: dict) -> list[tuple[str, str]]:
    """(Kapitel-Titel, Skript-Inhalt) der Quell-Kapitel eines Decks (in Reihenfolge).

    Bevorzugt: "sections" (ScriptSection-ids, aus Planner/Gather).
    Fallback: "script_chapters" (0-basierte Plan-Indizes, Legacy-Pläne).
    """
    out: list[tuple[str, str]] = []
    seen: set[int] = set()
    for sid in (dk.get("sections") or [])[:5]:
        try:
            n = int(sid)
        except (TypeError, ValueError):
            continue
        if n in seen:
            continue
        seen.add(n)
        with Session(engine) as s:
            sec = s.get(ScriptSection, n)
            if sec and (sec.content or "").strip():
                out.append((str(sec.title or ""), sec.content or ""))
    if not out:
        plan = snap["chapter_plan"]
        for i in dk.get("script_chapters") or []:
            if not (0 <= i < len(plan)):
                continue
            ch = plan[i]
            if not ch.get("section_id"):
                continue
            with Session(engine) as s:
                sec = s.get(ScriptSection, ch["section_id"])
                if sec and (sec.content or "").strip():
                    out.append((str(ch.get("title") or ""), sec.content or ""))
    return out


def _resolve_source_slides(import_id: int, dk: dict, staging: Path, by_path: dict) -> tuple[str, int]:
    """(Prompt-Text, Anzahl) der Quell-Folien aus den pptx-Quellen des Decks.

    Die Quellen des Decks zeigen auf pptx-Dateien mit Zeilenbereich (in den
    Sidecar-.md, die „%% Folie N %%“-Marker enthalten); alle Folien, deren
    Marker im Zeilenbereich liegt, werden 1:1 konvertiert.
    Returns: ("(keine Quell-Folien …)", 0), falls das Deck keine auflösbaren
    Quell-Folien hat.
    """
    src_slides = "(keine Quell-Folien für dieses Deck — aus der QUELLE generieren)"
    collected: list[tuple[int, str]] = []
    for src in dk.get("sources") or []:
        m = by_path.get(str(src.get("file") or ""))
        if not m or m.get("type") != "pptx" or not m.get("read_path"):
            continue
        p = staging / m["read_path"]
        if not p.is_file():
            continue
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        lo = max(1, int(src.get("start_line") or 1))
        hi = min(len(lines), int(src.get("end_line") or len(lines)))
        markers = [
            (i + 1, int(nm.group(1)))
            for i, ln in enumerate(lines)
            if (nm := re.match(r"^%% Folie (\d+) %%\s*$", ln.strip()))
        ]
        for k, (line_no, slide_no) in enumerate(markers):
            if not (lo <= line_no <= hi):
                continue
            end_no = markers[k + 1][0] - 1 if k + 1 < len(markers) else len(lines)
            body = "\n".join(lines[line_no:end_no]).strip()
            if body:
                collected.append((slide_no, body))
    if not collected:
        return src_slides, 0
    n_total = len(collected)
    if n_total > IMPORT_SLIDE_DECK_MAX_SLIDES_1TO1:
        collected = collected[:IMPORT_SLIDE_DECK_MAX_SLIDES_1TO1]
        append_report(import_id, warnings=[
            f"Deck „{dk['title']}“: {n_total} Quell-Folien, nur die ersten "
            f"{IMPORT_SLIDE_DECK_MAX_SLIDES_1TO1} konvertiert (Sicherheits-Cap)."
        ])
    return (
        f"QUELLE FOLIEN (1:1 konvertieren, Reihenfolge + Inhalt beibehalten; "
        f"Ziel: EXAKT {len(collected)} Folien — eine Quellfolie = eine Ziel-Folie):\n"
        + "\n\n".join(f"%% Folie {n} %%\n{b}" for n, b in collected)
    ), len(collected)


async def _generate_deck(
    course_id: int,
    import_id: int,
    staging: Path,
    snap: dict,
    dk: dict,
    idx: int,
    by_path: dict,
    llm_cfg: dict,
    llm_sem: asyncio.Semaphore,
    media_descs: Optional[dict[str, str]] = None,
) -> str:
    """Generiert + validiert ein Slide-Deck für einen Folien-Plan-Eintrag (1 Retry bei Fehler).

    media_descs: Zip-Pfad → Kurzbeschreibung der importierten Medien (Bild-Mapping).
    """
    # Quelle: Skript-Kapitel des Decks (sections bzw. legacy script_chapters), sonst rohe Sources
    section_parts = _deck_source_sections(snap, dk)
    section_content = "\n\n".join(
        f'=== Skript-Kapitel „{t}“ ===\n{c}' for t, c in section_parts if c.strip()
    )
    # Quell-Folien früh auflösen: bei vorhandenen Quell-Folien sind sie die 1:1-Referenz
    # → der Text-Quellen-Fallback (Gather/Rohtext) wird übersprungen
    src_slides, n_src_slides = _resolve_source_slides(import_id, dk, staging, by_path)
    url_map = {p: i["url"] for p, i in snap["media_map"].items() if i.get("url")}
    img_refs: set[str] = set()
    used_raw_sources = False
    if not section_content.strip() and n_src_slides == 0:
        if not dk.get("sources"):
            # Deck ohne Quellen → LLM ermittelt sie aus Beschreibung + Dateibaum
            set_progress(import_id, current=f"Folien: {dk['title']} — Quellen werden aus den Materialien ermittelt …")
            gathered = await _gather_sources(
                course_id, import_id, snap, staging, dk["title"],
                dk.get("description") or "",
                "slides", "slides", llm_cfg, llm_sem,
            )
            if gathered is None:
                raise RuntimeError("Quellen-Sammlung fehlgeschlagen (LLM-Aufruf).")
            sources, _notes = gathered
            patch: dict[str, Any] = {}
            if sources:
                patch["sources"] = sources
            if patch:
                update_slides_deck(import_id, idx, patch)
                dk.update(patch)
                append_report(import_id, warnings=[
                    f"Deck „{dk['title']}“: Quellen automatisch ermittelt ({len(sources)} Datei(n))."
                ])
                gather_log = ", ".join(
                    f"{s.get('file')} (Z {s.get('start_line') or 1}–{s.get('end_line')})" for s in sources[:20]
                ) or "(keine)"
                append_report(import_id, log=[f"Deck „{dk['title']}“: Quellen automatisch ermittelt: {gather_log}"])
            section_parts = _deck_source_sections(snap, dk)
            section_content = "\n\n".join(
                f'=== Skript-Kapitel „{t}“ ===\n{c}' for t, c in section_parts if c.strip()
            )
        raw, img_refs, _has_tex = await asyncio.to_thread(_collect_sources, staging, dk, by_path)
        # Roh bleibt roh: Das LLM konvertiert LaTeX/Markdown selbst (der Prompt erklärt
        # die Marker-Zeilen) — analog zum Skript-Import.
        if raw.strip():
            section_content = (
                section_content + "\n\n" + raw if section_content.strip() else raw
            )
            used_raw_sources = True
        # Nach dem Gather können pptx-Quellen vorliegen → 1:1-Quell-Folien neu
        # auflösen (die Quellen kamen erst jetzt von der LLM-Sammlung).
        src_slides, n_src_slides = _resolve_source_slides(import_id, dk, staging, by_path)
    # Debug-Log: welche Quellen (Skript-Kapitel, Dateien, 1:1-Folien) dienen diesem Deck
    src_bits: list[str] = []
    if section_parts:
        src_bits.append(
            "Skript-Kapitel: " + ", ".join(f"“{t}“" for t, _ in section_parts)
            + f" ({len(section_content)} Zeichen)"
        )
    if used_raw_sources and dk.get("sources"):
        src_bits.append("Dateien: " + ", ".join(
            f"{s.get('file')} (Z {s.get('start_line') or 1}–{s.get('end_line')})" for s in dk["sources"][:20]
        ))
    if n_src_slides:
        src_bits.append(f"1:1 aus {n_src_slides} Quell-Folien (pptx)")
    append_report(import_id, log=[f"Deck „{dk['title']}“: Quelle: " + (" | ".join(src_bits) or "(keine)")])

    # Kontext: andere Decks + Skript-Kapitel (interne Summaries) — Medien kommen
    # über das Bild-Mapping (inkl. Kurzbeschreibungen)
    other_context = _deck_other_context(snap, course_id, dk.get("material_id"))
    script_labels = _extract_labels(section_content)
    ref_text = await asyncio.to_thread(_build_references_text, course_id)
    image_map_text = _image_table(img_refs, url_map, media_descs or {})

    if not section_content.strip() and n_src_slides == 0:
        raise RuntimeError("Kein Quelltext für dieses Deck (Skript-Kapitel und Sources leer).")

    # 1:1-Modus (QUELLE FOLIEN vorhanden) — für Runaway-Cap und Fallback-Text.
    is_1to1 = n_src_slides > 0

    source = section_content or (
        "(keine Text-Quelle — die QUELLE FOLIEN oben sind die Grundlage)" if is_1to1 else ""
    )
    max_tokens = _deck_output_tokens(n_src_slides, len(source))
    if len(section_content) > IMPORT_SLIDE_DECK_SOURCE_WARN_CHARS:
        append_report(import_id, warnings=[
            f"Deck „{dk['title']}“: Achtung, sehr großer Input ({len(section_content)} Zeichen) — "
            f"bei zu kleinen Kontextfenstern können Probleme auftauchen."
        ])
    ctx = other_context
    last_err: Optional[str] = None
    content: Optional[str] = None
    for attempt in range(2):
        async with llm_sem:
            if not await gate(import_id, "slides"):
                raise RuntimeError("gestoppt (Pause/Cancel)")
            res = await llm_service.import_generate_slide_deck(
                dk["title"], ctx,
                ", ".join(sorted(script_labels)) or "(keine)",
                image_map_text, ref_text, src_slides, source,
                config=llm_cfg, max_tokens=max_tokens,
            )
        if not res["success"]:
            last_err = str(res.get("error") or "LLM-Fehler")
            ctx = other_context + f"\n\nFEHLER DES VORIGEN VERSUCHS: {last_err} — korrigiere das Deck-Format strikt."
            continue
        draft = strip_outer_fence(res["data"]["model_solution"]).strip()
        if not draft:
            last_err = "LLM hat leeren Inhalt geliefert."
            ctx = other_context + f"\n\nFEHLER DES VORIGEN VERSUCHS: {last_err}"
            continue
        try:
            n = slide_count(draft)
            if is_1to1 and n > n_src_slides + 2:
                # Runaway-Schutz im 1:1-Modus (sonst: kein Cap — Foliengenzahl
                # orientiert sich am Inhalt der QUELLE)
                draft = cap_slides(draft, n_src_slides + 2)
                append_report(import_id, warnings=[
                    f"Deck „{dk['title']}“: {n} Folien, auf {n_src_slides + 2} gekappt "
                    f"(Sicherheits-Cap im 1:1-Modus)."
                ])
            parse_slides(draft)  # strikte Validierung (wirft SlideError)
            draft, warns = sanitize_untrusted_markdown(draft)
            for w in warns:
                append_report(import_id, warnings=[w])
            content = draft
            break
        except SlideError as e:
            last_err = str(e)
            ctx = other_context + f"\n\nFEHLER DES VORIGEN VERSUCHS: {last_err} — nutze das Format EXAKT wie beschrieben."
    if content is None:
        raise RuntimeError(f"Deck konnte nicht generiert/validiert werden: {last_err}")

    # Post-Check: erwartete Bild-URLs vorhanden?
    for url in {u for u in (_resolve_image_path(r, url_map) for r in img_refs) if u}:
        if url not in content:
            append_report(import_id, warnings=[
                f"Deck „{dk['title']}“: erwartetes Bild {url} fehlt im generierten Deck."
            ])
    return content


async def run_slides_job(course_id: int, import_id: int) -> None:
    """Stufe 6: Slide-Decks für den Folien-Plan (Status != done)."""
    logger.info("Import %s (Kurs %s): slides gestartet", import_id, course_id)
    try:
        set_stage_status(import_id, "slides", "running")
        # Legacy: noch kein eigenes slides_plan → 1:1 aus dem Skript-Plan ableiten
        def _ensure_slides_plan(imp: CourseImport):
            if not (imp.slides_plan or []) and (imp.chapter_plan or []):
                imp.slides_plan = _derive_slides_plan(imp.chapter_plan)

        _mutate(import_id, _ensure_slides_plan)
        snap = job_snapshot(import_id)
        if snap is None:
            return
        staging = staging_dir(course_id, snap["job_id"])
        by_path = {m["path"]: m for m in snap["manifest"]}
        plan = snap["slides_plan"]
        todo = [d for d in plan if d.get("enabled") and d.get("slides_status") != "done"]
        if not todo:
            set_stage_status(import_id, "slides", "done")
            return

        decks = _course_decks(course_id)
        mode = "A" if not decks else ("B" if len(decks) == 1 else "C")
        # Plan-Reihenfolge = Ziel-Reihenfolge im Kurs (analog Skript): Decks im Plan
        # (mit material_id) werden zu Job-Start durchnummeriert, neue Decks erhalten
        # beim Speichern dieselbe Position; Decks außerhalb des Plans rücken ans Ende
        # (bisherige relative Reihenfolge).
        plan_order: dict[int, int] = {}
        plan_deck_ids = [d["material_id"] for d in plan if d.get("material_id")]
        with Session(engine) as s:
            order = 0
            for i, d in enumerate(plan):
                plan_order[i] = order
                if d.get("material_id"):
                    mat = s.get(CourseMaterial, d["material_id"])
                    if mat:
                        mat.display_order = order
                order += 1
            if plan_deck_ids:
                tail = s.exec(
                    select(CourseMaterial)
                    .where(CourseMaterial.course_id == course_id)
                    .where(CourseMaterial.material_type == MaterialType.SLIDES)
                    .where(~CourseMaterial.id.in_(plan_deck_ids))
                    .order_by(CourseMaterial.display_order.asc(), CourseMaterial.id.asc())  # type: ignore[attr-defined]
                ).all()
                for m in tail:
                    m.display_order = order
                    order += 1
            s.commit()
        delete_original = bool((snap["report"].get("slides_options") or {}).get("delete_original"))

        media_descs = await asyncio.to_thread(_media_descriptions, snap)
        llm_cfg = get_llm_config(course_id, IMPORT_TIMEOUT_CONVERT)
        llm_sem = asyncio.Semaphore(IMPORT_LLM_CONCURRENCY)

        success = 0
        for idx, dk in enumerate(plan):
            if not dk.get("enabled") or dk.get("slides_status") == "done":
                continue
            unit_key = f"slides:{idx}"
            if not await gate(import_id, "slides"):
                return
            set_progress(import_id, current=f"Folien: {dk['title']}", unit_key=unit_key, unit_total=1)
            try:
                content = await _generate_deck(
                    course_id, import_id, staging, snap, dk, idx,
                    by_path, llm_cfg, llm_sem, media_descs=media_descs,
                )
                with Session(engine) as s:
                    mat = s.get(CourseMaterial, dk["material_id"]) if dk.get("material_id") else None
                    if mat:
                        # Retry eines bereits generierten Decks → Inhalt aktualisieren
                        mat.title = dk["title"][:300]
                        mat.content = content
                        mat.display_order = plan_order[idx]
                    else:
                        mat = CourseMaterial(
                            course_id=course_id, title=dk["title"][:300],
                            material_type=MaterialType.SLIDES, content=content,
                            is_visible=False, display_order=plan_order[idx],
                            created_by=snap["created_by"],
                        )
                        s.add(mat)
                    s.commit()
                    s.refresh(mat)
                    mat_id = mat.id
                # Interne Zusammenfassung für Cross-Deck-Kontexte (best effort):
                # dient anderen Decks/Kapiteln als Kontext (Konsistenz, Querverweise)
                async with llm_sem:
                    if await gate(import_id, "slides"):
                        res = await llm_service.import_deck_summary(
                            dk["title"], content, config=llm_cfg
                        )
                    else:
                        res = {"success": False}
                if res.get("success"):
                    deck_summary = strip_outer_fence(res["data"]["model_solution"])
                    with Session(engine) as s:
                        mat = s.get(CourseMaterial, mat_id)
                        if mat and mat.summary != deck_summary:
                            mat.summary = deck_summary
                            s.add(mat)
                            s.commit()
                update_slides_deck(import_id, idx, {
                    "slides_status": "done", "slides_error": None, "material_id": mat_id,
                    # Generiert → Checkbox deaktivieren (erneutes Anhaken = Neu-Generierung)
                    "enabled": False,
                })
                set_progress(import_id, unit_key=unit_key, unit_done=1)
                success += 1
            except RuntimeError as e:
                if "gestoppt" in str(e):
                    return
                logger.warning("Import %s: Deck %s: %s", import_id, dk["title"], e)
                update_slides_deck(import_id, idx, {"slides_status": "error", "slides_error": str(e)[:500]})
                append_report(import_id, errors=[f"Slide-Deck „{dk['title']}“: {e}"])
            except Exception as e:
                logger.exception("Import %s: Deck %s fehlgeschlagen", import_id, dk["title"])
                update_slides_deck(import_id, idx, {"slides_status": "error", "slides_error": str(e)[:500]})
                append_report(import_id, errors=[f"Slide-Deck „{dk['title']}“: {e}"])

        # Modus B: Original-Deck löschen (nur bei Erfolg + Backup)
        if mode == "B" and delete_original and success > 0:
            try:
                backup = staging / "sidecars" / f"original_deck_{decks[0]['id']}.md"
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_text(decks[0]["content"], encoding="utf-8")
                with Session(engine) as s:
                    d = s.get(CourseMaterial, decks[0]["id"])
                    if d:
                        s.delete(d)
                    s.commit()
                append_report(import_id, warnings=[
                    f"Original-Deck „{decks[0]['title']}“ wurde gelöscht (Backup im Staging-Verzeichnis)."
                ])
            except Exception as e:
                append_report(import_id, errors=[f"Original-Deck konnte nicht gelöscht werden: {e}"])

        set_stage_status(import_id, "slides", "done")
        set_progress(import_id, current="Folien-Generierung abgeschlossen.")
        logger.info("Import %s: slides abgeschlossen (%d Decks)", import_id, success)
    except Exception as exc:
        logger.exception("Import %s: slides Job fehlgeschlagen", import_id)
        append_report(import_id, errors=[f"slides-Job intern fehlgeschlagen: {exc}"])
        if stage_status(import_id, "slides") == "running":
            set_stage_status(import_id, "slides", "error")


# ═══════════════════════════════════════════════════════════════
# Serialize + Lifecycle
# ═══════════════════════════════════════════════════════════════

def serialize_import(imp: CourseImport) -> dict:
    """Kompletter Import-State für die UI (GET-Endpoint, Polling)."""
    decks = [
        {"id": d["id"], "title": d["title"],
         "display_order": d["display_order"], "slide_count": slide_count(d["content"])}
        for d in _course_decks(imp.course_id)
    ]
    statuses = (imp.filemap_status, imp.media_status, imp.references_status,
                imp.ref_extract_status, imp.plan_status, imp.slides_plan_status,
                imp.script_status, imp.slides_status)
    return {
        "id": imp.id,
        "course_id": imp.course_id,
        "job_id": imp.job_id,
        "zip_name": imp.zip_name,
        "created_at": imp.created_at.isoformat() if imp.created_at else None,
        "filemap_status": imp.filemap_status,
        "media_status": imp.media_status,
        "references_status": imp.references_status,
        "ref_extract_status": imp.ref_extract_status,
        "plan_status": imp.plan_status,
        "slides_plan_status": imp.slides_plan_status,
        "script_status": imp.script_status,
        "slides_status": imp.slides_status,
        "manifest": imp.manifest or [],
        "file_map": imp.file_map or {},
        "media_map": imp.media_map or {},
        "reference_map": imp.reference_map or {},
        "chapter_plan": imp.chapter_plan or [],
        "slides_plan": get_slides_plan(imp),
        "progress": imp.progress or {},
        "report": imp.report or {},
        "decks": decks,
        "has_active": any(st in ("running", "paused") for st in statuses),
    }


def mark_interrupted_on_startup() -> None:
    """App-Start: alle aktiven Jobs → 'interrupted' (Runner startet sie stateless neu)."""
    with Session(engine) as s:
        for imp in s.exec(select(CourseImport)).all():
            changed = False
            for stage in IMPORT_STAGES:
                if getattr(imp, f"{stage}_status") in ("running", "paused"):
                    setattr(imp, f"{stage}_status", "interrupted")
                    changed = True
            if changed:
                imp.updated_at = datetime.now()
                s.add(imp)
        s.commit()


def resolve_preview_path(course_id: int, import_id: int, path: str) -> Optional[Path]:
    """Sichere Auflösung eines Preview-Pfads (strikt gegen Manifest, nur Medien)."""
    with Session(engine) as s:
        imp = s.get(CourseImport, import_id)
        if not imp or imp.course_id != course_id:
            return None
        staging = staging_dir(course_id, imp.job_id)
        m = next((x for x in (imp.manifest or []) if x.get("path") == path), None)
        if not m or m.get("type") not in ("image", "figure_pdf", "svg"):
            return None
    p = (staging / "extracted" / path).resolve()
    if not p.is_relative_to(staging.resolve()) or not p.is_file():
        return None
    return p


def delete_import(course_id: int, import_id: Optional[int] = None) -> None:
    """Import-Zeile(n) + Staging-Verzeichnis löschen (best effort)."""
    with Session(engine) as s:
        q = select(CourseImport).where(CourseImport.course_id == course_id)
        if import_id is not None:
            q = q.where(CourseImport.id == import_id)
        for imp in s.exec(q).all():
            s.delete(imp)
        s.commit()
    root = course_staging_root(course_id)
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)
