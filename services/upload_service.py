"""Upload-Aufgaben: Storage für Template-/Musterlösungs-Dateien und
Student-Einreichungen.

Jeder Zweck hat genau EINE Datei, immer original wie hochgeladen:
- data/upload_tasks/{task_id}/<name>        (Template + Musterlösung)
- data/submissions/{submission_id}/<name>   (Einreichung; Zips bekommen
  nebenher upload_extracted/ = Lazy-Extraktions-Cache)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Optional

from config import SUBMISSION_DIR, UPLOAD_TASK_DIR, VERSION_SNAPSHOT_DIR
from services.workspace_service import (
    MAX_LLM_FILE_CHARS,
    MAX_LLM_TOTAL_CHARS,
    WorkspaceService,
)

EXTRACTED_DIRNAME = "upload_extracted"


# ─── Pfade ───────────────────────────────────────────────────────────

def task_upload_dir(task_id: int) -> Path:
    return UPLOAD_TASK_DIR / str(task_id)


def task_upload_file(task_id: int, name: Optional[str]) -> Optional[Path]:
    """Pfad der Template-/Musterlösungs-Datei (oder None)."""
    if not name:
        return None
    p = task_upload_dir(task_id) / name
    return p if p.is_file() else None


def submission_upload_dir(submission_id: int) -> Path:
    return SUBMISSION_DIR / str(submission_id)


def submission_upload_file(submission_id: int, name: Optional[str]) -> Optional[Path]:
    """Pfad der eingereichten Originaldatei (oder None)."""
    if not name:
        return None
    p = submission_upload_dir(submission_id) / name
    return p if p.is_file() else None


def safe_filename(name: str) -> str:
    """Client-Dateiname → sicheres Basename (keine Pfadbestandteile)."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in (".", "..") or len(name) > 200:
        raise ValueError("Ungültiger Dateiname.")
    return name


# ─── Task-Dateien (Template / Musterlösung) ──────────────────────────

def save_task_file(task_id: int, name: str, data: bytes) -> None:
    d = task_upload_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)


def remove_task_file(task_id: int, name: Optional[str]) -> None:
    if not name:
        return
    p = task_upload_dir(task_id) / name
    if p.is_file():
        p.unlink(missing_ok=True)


def copy_task_files(src_id: int, dst_id: int) -> None:
    """Upload-Dateien einer Task kopieren (Task-Duplizieren)."""
    src = task_upload_dir(src_id)
    if src.is_dir():
        shutil.copytree(src, task_upload_dir(dst_id), dirs_exist_ok=True)


def cleanup_task(task_id: int) -> None:
    shutil.rmtree(task_upload_dir(task_id), ignore_errors=True)


def cleanup_submission(submission_id: int, filename: Optional[str]) -> None:
    """Einreichungsdatei + Extraktions-Cache entfernen (restliches
    SUBMISSION_DIR bleibt für Workspace-Snapshots etc. unangetastet)."""
    d = submission_upload_dir(submission_id)
    p = d / EXTRACTED_DIRNAME
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    if filename:
        (d / filename).unlink(missing_ok=True)


# ─── Version-Snapshots (Task-Dateien in Task-Versionen) ────────────
# Schwester-Mechanismus zu workspace_service.capture_version_snapshot:
# Je Version max. 2 Dateien (Template + Musterlösung), einfach als
# Dateikopien unter version_snapshots/{id}/upload_files/.

def _task_upload_entries(task) -> list:
    entries = []
    for field in (task.upload_template, task.upload_solution_file):
        if not field:
            continue
        p = task_upload_dir(task.id) / field
        if p.is_file():
            st = p.stat()
            entries.append([field, int(st.st_size), int(st.st_mtime_ns)])
    return entries


def capture_version_snapshot(task, version_id: int) -> Optional[str]:
    """Upload-Dateien der Aufgabe als Version-Snapshot sichern.

    Liefert den relativen Pfad (version_snapshots/{id}/upload_files)
    oder None (Aufgabentyp ≠ Upload bzw. keine Dateien). Digest-Check:
    Auto-Save feuert bei jeder Tipp-Pause — Rewrite nur bei Änderung."""
    if task.task_type.value != "upload":
        return None
    entries = _task_upload_entries(task)
    if not entries:
        return None
    digest = hashlib.sha256(
        json.dumps(entries, sort_keys=True).encode()).hexdigest()
    vdir = VERSION_SNAPSHOT_DIR / str(version_id)
    hash_path = vdir / "upload_manifest.sha256"
    if hash_path.exists() and hash_path.read_text().strip() == digest:
        return f"version_snapshots/{version_id}/upload_files"
    fdir = vdir / "upload_files"
    fdir.mkdir(parents=True, exist_ok=True)
    for p in fdir.iterdir():  # Stale-Dateien aus früherem Capture entfernen
        if p.is_file():
            p.unlink()
    for field in (task.upload_template, task.upload_solution_file):
        if not field:
            continue
        p = task_upload_dir(task.id) / field
        if p.is_file():
            shutil.copy2(p, fdir / field)
    hash_path.write_text(digest, encoding="utf-8")
    return f"version_snapshots/{version_id}/upload_files"


def restore_version_snapshot(task_id: int, snapshot_rel: str) -> Optional[Path]:
    """Version-Snapshot auf den Task-Upload-Dir anwenden: aktuelle Dateien
    durch die Snapshot-Dateien ersetzen. Liefert den Backup-Pfad des alten
    Stands (None = war leer); Semantik wie Workspace-Restore (Commit macht
    der Caller). Bei Fehler wird der alte Stand automatisch zurückgeführt."""
    base = task_upload_dir(task_id)
    backup: Optional[Path] = None
    if base.exists() and any(base.iterdir()):
        backup = base.with_name(base.name + ".restore-bak")
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        shutil.move(str(base), str(backup))
    src = VERSION_SNAPSHOT_DIR / str(snapshot_rel.split("/")[-2]) / "upload_files"
    try:
        base.mkdir(parents=True, exist_ok=True)
        for p in src.iterdir():
            if p.is_file():
                shutil.copy2(p, base / p.name)
    except Exception:
        rollback_restore_backup(task_id, backup)
        raise
    return backup


def rollback_restore_backup(task_id: int, backup: Optional[Path]) -> None:
    """Backup nach FEHLGESCHLAGENEM Restore zurückführen."""
    if not backup:
        return
    base = task_upload_dir(task_id)
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    try:
        shutil.move(str(backup), str(base))
    except OSError:
        shutil.rmtree(backup, ignore_errors=True)


def discard_restore_backup(backup: Optional[Path]) -> None:
    """Backup nach ERFOLGREICHEM Restore entfernen."""
    if backup and Path(backup).exists():
        shutil.rmtree(backup, ignore_errors=True)


def copy_version_snapshot(src_version_id: int, dst_version_id: int) -> str:
    """Upload-Snapshot kopieren (Restore erzeugt eine neue Version mit dem
    selben Datei-Stand — eigene Kopie = einfache Delete-Semantik)."""
    src = VERSION_SNAPSHOT_DIR / str(src_version_id) / "upload_files"
    dst_dir = VERSION_SNAPSHOT_DIR / str(dst_version_id)
    dst_dir.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst_dir / "upload_files", dirs_exist_ok=True)
    manifest = VERSION_SNAPSHOT_DIR / str(src_version_id) / "upload_manifest.sha256"
    if manifest.exists():
        shutil.copy2(manifest, dst_dir / "upload_manifest.sha256")
    return f"version_snapshots/{dst_version_id}/upload_files"


# ─── Einreichung ─────────────────────────────────────────────────────

def save_submission_file(submission_id: int, name: str, data: bytes) -> None:
    d = submission_upload_dir(submission_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)


# ─── Lazy-Zip-Extraktion ─────────────────────────────────────────────

def extracted_dir_of(file_path: Path) -> Path:
    return file_path.parent / EXTRACTED_DIRNAME


def is_zip_file(file_path: Path) -> bool:
    """Zip-Prüfung per Magic Bytes (vertraut nicht der Extension)."""
    try:
        with open(file_path, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def ensure_extracted(zip_path: Path) -> Path:
    """Zip nach nebenliegendes upload_extracted/ entpacken (safe, idempotent).

    Entpacken in Temp-Dir + Atom-Rename → kein halbes Verzeichnis bei
    parallelen Aufrufen. Wirft ValueError (Limits) oder
    zipfile.BadZipFile (kein gültiges Zip)."""
    from services.import_service import safe_extract_zip
    out = extracted_dir_of(zip_path)
    if out.is_dir():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.parent / f"{EXTRACTED_DIRNAME}.tmp-{os.getpid()}"
    try:
        tmp.mkdir(parents=True)
        safe_extract_zip(zip_path, tmp)
        if out.exists():
            shutil.rmtree(out, ignore_errors=True)
        os.replace(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return out


# ─── LLM-Kontext ─────────────────────────────────────────────────────

def _single_file_text(p: Path, max_file: int) -> Optional[str]:
    try:
        data = p.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:1024]:
        return None  # Binary — nicht in den LLM
    text = data.decode(errors="replace")
    if p.suffix == ".ipynb":
        text = WorkspaceService._strip_notebook_outputs(text)
    if len(text) > max_file:
        text = text[:max_file] + "\n… (gekürzt)"
    return text


def collect_upload_texts(
    file_path: Path,
    max_file: int = MAX_LLM_FILE_CHARS,
    max_total: int = MAX_LLM_TOTAL_CHARS,
) -> dict[str, str]:
    """Textdateien einer Upload-Datei für den LLM-Prompt (Zip → entpackte
    Dateien, sonst die Einzeldatei) — dieselben Regeln wie das
    Workspace-Grading (Binary-Skip, Notebook-Output-Strip, Truncation)."""
    if not file_path.is_file():
        return {}
    if is_zip_file(file_path):
        try:
            root = ensure_extracted(file_path)
        except (ValueError, zipfile.BadZipFile, OSError):
            return {}
        return WorkspaceService._collect_texts(root, max_file, max_total)
    text = _single_file_text(file_path, max_file)
    return {file_path.name: text} if text is not None else {}


def list_upload_files(file_path: Path) -> list[dict]:
    """Dateiliste für den Read-only-Viewer (Zip → entpackter Baum)."""
    if not file_path.is_file():
        return []
    if is_zip_file(file_path):
        try:
            root = ensure_extracted(file_path)
        except (ValueError, zipfile.BadZipFile, OSError):
            return []
        out = []
        for p in sorted(root.rglob("*")):
            if p.is_file():
                out.append({
                    "path": p.relative_to(root).as_posix(),
                    "size": p.stat().st_size,
                })
        return out
    return [{"path": file_path.name, "size": file_path.stat().st_size}]


def read_upload_file(file_path: Path, rel_path: str, max_bytes: int = 2_000_000) -> bytes:
    """Inhalt einer Datei aus einer Upload-Datei (Viewer). Wirft
    FileNotFoundError (Datei unbekannt) oder ValueError (Pfadvorgabe)."""
    if ".." in rel_path.replace("\\", "/").split("/") or rel_path.startswith(("/", "\\")):
        raise ValueError("Ungültiger Dateipfad.")
    if is_zip_file(file_path):
        root = ensure_extracted(file_path)
        p = (root / rel_path).resolve()
        if not p.is_relative_to(root.resolve()):
            raise ValueError("Ungültiger Dateipfad.")
    else:
        if rel_path != file_path.name:
            raise FileNotFoundError(rel_path)
        p = file_path
    if not p.is_file():
        raise FileNotFoundError(rel_path)
    return p.read_bytes()[:max_bytes]
