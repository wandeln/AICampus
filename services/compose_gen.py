"""
Download-Pakete für Workspace-Aufgaben.

Erzeugt selbstlaufende tar.gz-Pakete mit docker-compose.yml, sodass
Studenten und Tutoren Aufgaben auch LOKAL (auf dem eigenen Rechner)
ausführen können — ohne Zugriff auf die AICampus-Server.

Prinzip:
  - Basis ist IMMER ein öffentliches Standard-Image (FROM python:3.11-slim,
    FROM debian:bookworm-slim, …) — das wird beim lokalen Build von den
    öffentlichen CDNs registriert, NIE von unseren Servern.
  - images/ = die Image-Spec-Dockerfile der Aufgabe 1:1 ins Paket (Compose
    baut daraus). Tasks ohne auflösbare
    Image-Spec sind nicht paketierbar (ValueError).

Zugriffsklassen (statt Pfad-Zonen):
  - ``workspace/`` enthält ALLE öffentlichen Dateien (✏️ edit + 🔒 read-only)
    AM REALEN PFAD (data/ bleibt data/ in workspace/).
  - Je 🔒-Top-Level-Pfad ein ro-Bind-Mount
    (``./workspace/<p>:/workspace/<p>:ro``) — im init-Service rw, damit
    Datasets dort landen.
  - 👤-Dateien (versteckt) kommen NIE ins Studenten-Paket. Im Tutor-Paket
    liegen sie AM REALEN PFAD top-level (Namen frei, z. B. .solution/ oder
    beliebig); ``init-private`` (rw) und ``verify`` (ro) mounten sie.
  - /assets ist verschwunden: Datasets liegen in workspace/data/ (typisch
    🔒) und werden von .init.sh nachgeladen.

Alle Commands kommen aus den Skripten der Aufgabe (skriptbasiertes Modell):
  - workspace/run.sh               → Aufgabe ausführen
  - workspace/.init.sh             → einmalige Initialisierung (public;
    (👤 in AICampus, aber IMMER im Paket) 🔒 rw, Datasets nach
    workspace/data/)
  - .init_hidden.sh (👤, Wurzel)  → einmalige private Initialisierung
                                     (NUR Tutor-Paket, NACH .init.sh; 👤 rw)
  - workspace/test.sh            → öffentliche Self-Check-Tests
  - .test_private.sh (👤, Wurzel) → private Tests/Judge (NUR Tutor-Paket, ro)

Paket-Varianten:
  student + Task        → workspace/ (public) + compose + images/ + README
  tutor   + Task        → workspace/ (public) + 👤-Dateien (top-level)
                          + compose (workspace + init + init-private
                          + verify) + images/
  student + Submission  → Student-Workspace (Snapshot) + compose + images/
  tutor   + Submission  → Student-Workspace (Snapshot) + 👤-Dateien
                          + compose (wie Tutor) + images/
  student + Live        → aktueller Workspace-Snapshot des Studenten
                          (eigene Lösung) + compose + images/ + README

🔒-Dateien werden ab MAX_DATA_IN_PACKAGE aus dem Paket gestrichen; hat die
Aufgabe ein .init.sh, lädt die Initialisierung die Daten lokal nach
(README verweist darauf).
"""

import hashlib
import io
import json
import re
import shutil
import tarfile
import tempfile
import threading
import time
import zlib
from datetime import date
from pathlib import Path
from typing import Optional

from database.models import Submission, Task
from services.workspace_service import (
    effective_file_access,
    file_disk_path,
    hidden_mount_paths,
    readonly_mount_paths,
    workspace_service,
)

# 🔒-Dateien im Paket: mehr als so viel wird ausgelassen (README vermerkt das)
MAX_DATA_IN_PACKAGE = 100 * 1024 * 1024

# Kurzlebiger Cache: {(kind, task_id, submission_id): (fingerprint, filename, bytes, ts)}
_PKG_CACHE: dict[tuple, tuple[str, str, bytes, float]] = {}
_PKG_CACHE_LOCK = threading.Lock()
_PKG_CACHE_TTL = 600          # 10 min
_PKG_CACHE_MAX = 8


# ── Inhaltssammler ─────────────────────────────────────────────

def _task_access_map(task: Task) -> dict:
    """Zugriffs-Karten der Aufgabe aus der DB (eigene Session — wird via
    to_thread aufgerufen und bekommt nicht die Request-Session mit).

    Liefert:
      files:     {relpath: {"disk": Path, "access": "edit"|"readonly"|"hidden"}}
      ro_paths:  Top-Level-🔒-Pfade (readonly_mount_paths)
      hid_paths: Top-Level-👤-Pfade (hidden_mount_paths)
      judge_path: Pfad der 👤-Judge-Datei (.test_private.sh) oder None
    """
    from sqlmodel import Session
    from database.base import engine
    with Session(engine) as s:
        files = workspace_service.task_files(s, task)
        fm = workspace_service.folder_map(s, task)
        fmap: dict[str, dict] = {
            f.path: {
                "disk": file_disk_path(task.id, f.path),
                "access": effective_file_access(f.path, f.access, fm),
            }
            for f in files
        }
        ro = readonly_mount_paths(files, fm)
        hid = hidden_mount_paths(files, fm)
        judge = workspace_service.judge_script_path(s, task)
    return {"files": fmap, "ro_paths": ro, "hid_paths": hid,
            "judge_path": judge}


def _slug(title: str, task_id: int) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:40] or f"task{task_id}"


def _source_fingerprint(task: Task, kind: str,
                        submission: Optional[Submission],
                        img_spec: Optional[dict] = None,
                        fmap: Optional[dict] = None) -> str:
    """Billiger Fingerprint aus den QUELLEN (ohne Paket zu schreiben):
    Task-Felder + mtime/Größe aller Task-Dateien + Zugriffsklassen
    (effektiv, aus der DB — ändern sich ohne Disk-Mtime!) + Snapshot bei
    Submission. Änderung → neuer Fingerprint → Cache-Miss."""
    h = hashlib.sha256()
    h.update(f"{kind}|{task.id}|{task.title}|"
             f"{task.workspace_image or ''}|"
             f"{task.workspace_timeout}|{task.workspace_cpu}|"
             f"{task.workspace_memory}|{task.workspace_internet}|"
             f"{task.workspace_main_file or ''}|"
             f"{task.updated_at.isoformat()}".encode())
    if fmap is not None:
        # Zugriffsklassen sind DB-Metadaten (kein Disk-Mtime) — ohne sie
        # würde eine reine Access-Änderung ein veraltetes Paket liefern.
        acc = {p: v["access"] for p, v in fmap["files"].items()}
        h.update(("acc:" + json.dumps(acc, sort_keys=True,
                                      ensure_ascii=False)).encode() + b"\n")
        h.update(("ro:" + "|".join(fmap["ro_paths"])).encode() + b"\n")
        h.update(("hid:" + "|".join(fmap["hid_paths"])).encode() + b"\n")
        h.update(("judge:" + (fmap["judge_path"] or "")).encode() + b"\n")
    if img_spec is not None:
        # Spec-INHALT zählt mit (gleicher Name, geänderter Build → Cache-Miss)
        from compute_agent.image_spec import content_hash
        h.update(f"imgspec:{content_hash(img_spec['dockerfile'])}\n".encode())
    base = file_disk_path(task.id, "")
    if base.is_dir():
        for p in sorted(base.rglob("*")):
            if p.is_file():
                st = p.stat()
                h.update(f"{p.relative_to(base).as_posix()}:{st.st_mtime_ns}:{st.st_size}\n".encode())
    if submission is not None and submission.workspace_snapshot:
        try:
            from services.workspace_service import snapshot_abs_path
            snap = snapshot_abs_path(submission.workspace_snapshot)
            if snap.is_file():
                st = snap.stat()
                h.update(f"snap:{st.st_mtime_ns}:{st.st_size}".encode())
        except OSError:
            pass
    return h.hexdigest()


def _image_spec_for_task(task: Task) -> dict:
    """Image-Spec der Aufgabe als {name, dockerfile} (via workspace_image).

    Wirft ValueError, wenn die Aufgabe kein auflösbares Image hat
    (Task-Image fehlt oder Spec ungültig) — das Paket ist dann nicht
    möglich; die UI zeigt den Fehler graceful (graceful 400).
    Eigne Session — wird via to_thread aufgerufen und bekommt deshalb
    nicht die Request-Session mit."""
    from sqlmodel import Session
    from database.base import engine
    from services.image_spec_service import resolve_spec
    from compute_agent.image_spec import validate
    if not task.workspace_image:
        raise ValueError(
            "Download-Paket nicht möglich: Aufgabe hat kein Image (Image-Spec).")
    with Session(engine) as s:
        row = resolve_spec(s, task.course_id, task.workspace_image)
    if row is None:
        raise ValueError(
            f"Download-Paket nicht möglich: Image-Spec „{task.workspace_image}“ "
            "existiert nicht (mehr).")
    try:
        validate(row.dockerfile)
    except Exception as e:  # noqa: BLE001 — ungültige Dockerfile
        raise ValueError(
            f"Download-Paket nicht möglich: Image-Spec „{task.workspace_image}“ "
            f"ist ungültig: {e}")
    return {"name": row.name, "dockerfile": row.dockerfile}


# ── Generatoren ────────────────────────────────────────────────────

def _image_build_files(task: Task, img_spec: dict) -> tuple[str, dict[str, bytes]]:
    """(Dockerfile, {filename: bytes}) für images/ — lokal lauffähig.
    Die Spec-Dockerfile 1:1 ins Paket (Compose baut daraus)."""
    if img_spec is None:
        raise ValueError("Download-Paket nicht möglich: Aufgabe hat kein Image.")
    return img_spec["dockerfile"], {}


def _mount(src: str, dst: str, ro: bool) -> str:
    """Eine Volume-Zeile des Compose (6er-Einrücken)."""
    return f"      - {src}:{dst}" + (":ro" if ro else "")


def _has_content(p: Path) -> bool:
    """Pfad ist Datei oder (nicht-leerer) Ordner."""
    if p.is_file():
        return True
    if p.is_dir():
        return any(x.is_file() for x in p.rglob("*"))
    return False


def _remove_path(p: Path) -> None:
    """Datei oder Ordnerbaum löschen (defensive Säuberung, nie fehler)."""
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.is_file():
            p.unlink()
    except OSError:
        pass


def _extract_tar_bytes(tar_bytes: bytes, dest: Path) -> None:
    """In-Memory-Snapshot sicher entpacken (gleiche Guards wie
    workspace_service.extract_snapshot: keine Links, kein Entweichen)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if not target.is_relative_to(base):
                raise ValueError(f"Ungültiger Snapshot-Eintrag: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"Links nicht erlaubt: {member.name}")
        tar.extractall(dest)


def _compose_content(task: Task, slug: str, has_init: bool,
                     has_init_private: bool,
                     has_verify: bool, ro_mounts: list[str],
                     hid_mounts: list[str], judge_path: str) -> str:
    """docker-compose.yml — rein skriptbasiert:

    workspace     → laufende Arbeitsumgebung (hält mit sleep infinity);
                    Skripte (run.sh, test.sh, …) laufen im Terminal des
                    Containers (Anleitung in der README)
    init          → einmalige Initialisierung (.init.sh; 🔒 rw, Internet IMMER)
    init-private  → private Initialisierung (👤 rw; nur Tutor-Paket)
    verify        → .test_private.sh (nur Tutor-Paket)
    """
    internet = bool(task.workspace_internet)
    L = [
        f"# AICampus Workspace-Paket: {task.title}",
        f"# Generiert am {date.today().isoformat()} — Anleitung in README.md",
        "services:",
        "  workspace:",
        "    build:",
        "      context: ./images",
        "    image: aicampus-ws-local:1",
        f"    container_name: aicampus-ws-{slug}",
        "    working_dir: /workspace",
        "    volumes:",
        _mount("./workspace", "/workspace", ro=False),
    ]
    L += [_mount(f"./workspace/{p}", f"/workspace/{p}", ro=True) for p in ro_mounts]
    if internet:
        L.append("    # Internet-Zugriff laut Aufgaben-Konfiguration aktiviert")
    else:
        L += [
            "    # Kein Internet (Standard). Zeile entfernen, falls die",
            "    # Aufgabe es doch benötigt.",
            "    network_mode: none",
        ]
    L.append("    command: sleep infinity")

    if has_init:
        L += [
            "",
            "  # Einmalige Initialisierung (public; benötigt Internet).",
            "  # Vor dem ersten 'docker compose up' ausführen:",
            "  #   docker compose run --rm init",
            "  init:",
            "    build:",
            "      context: ./images",
            "    image: aicampus-ws-local:1",
            "    working_dir: /workspace",
            "    volumes:",
            _mount("./workspace", "/workspace", ro=False),
        ]
        # 🔒 rw: .init.sh darf Datasets/Dateien in read-only-Bereiche legen
        L += [_mount(f"./workspace/{p}", f"/workspace/{p}", ro=False) for p in ro_mounts]
        L.append("    command: bash /workspace/.init.sh")

    if has_init_private:
        L += [
            "",
            "  # Einmalige PRIVATE Initialisierung (nur dieses Paket).",
            "  # NACH 'init' ausführen — sie teilt das Volume:",
            "  #   docker compose run --rm init-private",
            "  init-private:",
            "    build:",
            "      context: ./images",
            "    image: aicampus-ws-local:1",
            "    working_dir: /workspace",
            "    volumes:",
            _mount("./workspace", "/workspace", ro=False),
        ]
        # 👤 rw: .init_hidden.sh darf in versteckte Bereiche schreiben
        L += [_mount(f"./{p}", f"/workspace/{p}", ro=False) for p in hid_mounts]
        # Das Skript selbst liegt top-level im Paket (👤-Dateien)
        L.append(_mount("./.init_hidden.sh", "/.init_hidden.sh", ro=True))
        L.append("    command: bash /.init_hidden.sh")

    if has_verify:
        L += [
            "",
            "  # Private Tests ausführen (tutorenseitig — nur mit diesem Paket):",
            "  #   docker compose run --rm verify",
            "  verify:",
            "    build:",
            "      context: ./images",
            "    image: aicampus-ws-local:1",
            "    working_dir: /workspace",
            "    volumes:",
            _mount("./workspace", "/workspace", ro=False),
        ]
        L += [_mount(f"./workspace/{p}", f"/workspace/{p}", ro=True) for p in ro_mounts]
        L += [_mount(f"./{p}", f"/workspace/{p}", ro=True) for p in hid_mounts]
        if not internet:
            L.append("    network_mode: none")
        L.append(f"    command: bash /workspace/{judge_path}")
    return "\n".join(L) + "\n"


def _readme_content(task: Task, kind: str, has_run: bool,
                    has_init: bool, has_init_private: bool,
                    data_omitted: bool, has_verify: bool,
                    submission: Optional[Submission],
                    live: bool = False) -> str:
    is_sub = submission is not None
    if live:
        ws_desc = "deine aktuelle Lösung (Stand: jetzt, inkl. generierter " \
                  "Dateien)"
    elif is_sub and kind == "student":
        ws_desc = "deine Lösung (Stand der Einreichung)"
    elif is_sub:
        ws_desc = "die Student-Dateien dieser Einreichung"
    else:
        ws_desc = "die Aufgaben-Vorlage (Starter-Dateien)"
    L = [
        f"# AICampus Workspace-Paket: {task.title}",
        "",
        f"Generiert am {date.today().isoformat()} von AICampus."
        + (" (deine aktuelle Lösung)" if live
           else " (Version dieser Einreichung)" if is_sub
           else " (Aufgaben-Vorlage)"),
        "",
        "## Inhalt",
        "",
        "| Ordner / Datei | Inhalt |",
        "|---|---|",
        f"| `workspace/` | {ws_desc} — public Dateien am realen Pfad; "
        "🔒-Bereiche (z. B. `data/`) sind read-only |",
    ]
    if has_run:
        L.append("| `workspace/run.sh` | Ausführen-Skript der Aufgabe (nicht editieren) |")
    if has_init:
        L.append("| `workspace/.init.sh` | einmalige Initialisierung (nicht editieren, "
                 "im System für Studenten versteckt) |")
    if kind == "tutor":
        if has_init_private:
            L.append("| `.init_hidden.sh` | einmalige private Initialisierung (privat!) |")
        L.append("| 👤-Dateien (top-level) | Musterlösung / private Tests am realen "
                 "Pfad (privat!) |")
    L += [
        "| `images/` | Dockerfile der Laufumgebung (öffentliches Basis-Image + dünne Schicht) |",
        "",
        "## Voraussetzungen",
        "",
        "- Docker + Docker Compose (v2)",
    ]
    if has_init:
        L.append("- Internetzugang für die einmalige Initialisierung (`init`)")
    L += [
        "",
        "## Starten",
        "",
        "```bash",
        "docker compose build          # einmalig; zieht das öffentliche Basis-Image",
    ]
    if has_init:
        L.append("docker compose run --rm init      # einmalig: Pakete + Daten → workspace/")
    if has_init_private:
        L.append("docker compose run --rm init-private  # einmalig (NACH init): private Umgebung")
    L += [
        "docker compose up -d workspace         # Arbeitsumgebung im Hintergrund starten",
        "docker compose exec -it workspace bash  # Terminal in den Container öffnen",
        "```",
        "",
        "Im Terminal stehst du bereits in `/workspace`; dort laufen die",
        "Skripte der Aufgabe direkt, z. B. `bash run.sh` (Aufgabe ausführen)",
        "oder `bash test.sh` (Self-Check). `exit` schließt das Terminal,",
        "der Container läuft weiter — du kannst dich jederzeit erneut mit",
        "`docker compose exec -it workspace bash` verbinden. Den Container",
        "stoppst du mit `docker compose stop`.",
        "",
    ]
    if data_omitted:
        size_mb = MAX_DATA_IN_PACKAGE // (1024 * 1024)
        if has_init:
            L += [
                f"**Hinweis:** Die read-only-Bereiche (🔒) sind größer als {size_mb} MB",
                "und wurden NICHT ins Paket aufgenommen — die Initialisierung",
                "(`docker compose run --rm init`) lädt die Daten nach `workspace/`.",
                "",
            ]
        else:
            L += [
                f"**Hinweis:** Die read-only-Bereiche (🔒) sind größer als {size_mb} MB und "
                "wurden NICHT ins Paket aufgenommen — sie sind weiterhin im "
                "AICampus-Workspace verfügbar.",
                "",
            ]
    if has_verify:
        L += [
            "## Private Tests (nur mit diesem Paket möglich)",
            "",
            "```bash",
            "docker compose run --rm verify",
            "```",
            "",
            "führt den privaten Judge (👤-Datei `.test_private.sh`) aus.",
            "",
        ]
    if kind == "student":
        L += [
            "**Wichtig:** Dieses Paket enthält bewusst KEINE Musterlösung und keine",
            "privaten Tests. Die Bewertung erfolgt serverseitig nach der Abgabe.",
            "Die Skripte der Aufgabe (`run.sh`, `.init.sh`, `test.sh`) gehören zur",
            "Aufgabe — bitte nicht editieren oder ersetzen.",
            "",
        ]
    return "\n".join(L)


# ── Hauptfunktion ──────────────────────────────────────────────────

def build_package(task: Task, kind: str,
                  submission: Optional[Submission] = None,
                  snapshot_tar: Optional[bytes] = None) -> tuple[str, bytes]:
    """Paket bauen (oder aus dem Cache). Liefert (filename, tar_bytes).

    kind: "student" | "tutor"
    submission: None → Aufgaben-Vorlage, sonst Snapshot dieser Einreichung.
    snapshot_tar: Live-Snapshot (tar.gz) des aktuellen Student-Workspaces
    → Paket der „eigenen Lösung“ (Vorrang vor submission; wird NICHT
    gecacht, da der Inhalt bei jedem Download neu ist).
    """
    if kind not in ("student", "tutor"):
        raise ValueError("kind muss 'student' oder 'tutor' sein")
    if task.task_type.value != "workspace":
        raise ValueError("Keine Workspace-Aufgabe.")

    live = submission is None and snapshot_tar is not None
    cache_key = (kind, task.id, submission.id if submission else 0)
    img_spec = _image_spec_for_task(task)
    fmap = _task_access_map(task)
    fingerprint = None if live else _source_fingerprint(
        task, kind, submission, img_spec, fmap)

    with _PKG_CACHE_LOCK:
        if fingerprint is not None:
            hit = _PKG_CACHE.get(cache_key)
            if (hit and hit[0] == fingerprint
                    and time.time() - hit[3] < _PKG_CACHE_TTL):
                return hit[1], hit[2]

    slug = _slug(task.title, task.id)
    top_name = (f"submission-{submission.id}" if submission
                else f"task-{task.id}-{slug}" + ("-solution" if live else ""))

    # Public (✏️+🔒) vs. hidden (👤), nur Dateien, die auf der Disk existieren
    public = {p: v for p, v in fmap["files"].items()
              if v["access"] != "hidden" and v["disk"].is_file()}
    hidden = {p: v for p, v in fmap["files"].items()
              if v["access"] == "hidden" and v["disk"].is_file()}
    ro_files = {p for p, v in public.items() if v["access"] == "readonly"}
    judge_path = fmap["judge_path"]

    tmpdir = Path(tempfile.mkdtemp(prefix="aicampus-pkg-"))
    try:
        top = tmpdir / top_name
        (top / "workspace").mkdir(parents=True)

        # 1) workspace/ — public Dateien am realen Pfad
        #    (Studenten-Dateien: Live-Snapshot ODER Snapshot der Einreichung;
        #    sonst Aufgaben-Vorlage)
        data_omitted = False

        def _ro_capped_write() -> dict:
            """🔒-Volumen-Cap: read-only-Bereiche > Limit → weglassen
            (.init.sh lädt sie lokal nach; README vermerkt das)."""
            nonlocal data_omitted
            ro_total = sum(v["disk"].stat().st_size for p, v in public.items()
                           if p in ro_files)
            if ro_total > MAX_DATA_IN_PACKAGE:
                data_omitted = True
                return {p: v for p, v in public.items() if p not in ro_files}
            return public

        if live:
            # Live-Snapshot: enthält auch die 🔒-Bind-Mounts (tar liest
            # durch die Mounts) → 👤/🔒 entfernen, 🔒 aus der Task-Disk
            # nachlegen (mit 🔒-Cap wie die Vorlage). Die ✏️-Dateien
            # bleiben in der Studenten-Version (kein Überschreiben!).
            assert snapshot_tar is not None  # live ⇒ snapshot_tar gesetzt
            _extract_tar_bytes(snapshot_tar, top / "workspace")
            for p in fmap["hid_paths"] + fmap["ro_paths"]:
                _remove_path(top / "workspace" / p)
            for rel, v in ((p, v) for p, v in _ro_capped_write().items()
                           if p in ro_files):
                dst = top / "workspace" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(v["disk"].read_bytes())
        elif submission is not None and submission.workspace_snapshot:
            try:
                workspace_service.extract_snapshot(
                    submission.workspace_snapshot, top / "workspace")
            except (ValueError, tarfile.TarError, OSError,
                    EOFError, zlib.error):
                # z. B. alte, still abgeschnittene Snapshots — klarer
                # Fehler statt 500 (Endpunkt wandelt ValueError in 400 um).
                raise ValueError(
                    "Paket nicht möglich: Der Snapshot der Einreichung ist "
                    "beschädigt oder unvollständig.")
            # 🔒-Bind-Mount-Inhalte (z. B. Datasets aus .init.sh) stecken
            # in älteren Snapshots (tar liest durch die Mounts) → wie im
            # Live-Zweig entfernen, damit nicht das ganze Dataset mit
            # heruntergeladen wird; neue Snapshots enthalten sie gar nicht.
            for p in fmap["hid_paths"] + fmap["ro_paths"]:
                _remove_path(top / "workspace" / p)
            # 🔒-Dateien aus der Task-Disk nachliefern (mit 🔒-Cap), sofern
            # sie fehlen — Standard-Verhalten für alle ro-Dateien, nicht
            # nur die System-Skripte.
            for rel, v in ((p, v) for p, v in _ro_capped_write().items()
                           if p in ro_files):
                dst = top / "workspace" / rel
                if not dst.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(v["disk"].read_bytes())
        else:
            for rel, v in _ro_capped_write().items():
                dst = top / "workspace" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(v["disk"].read_bytes())

        # .init.sh (👤) steckt in KEINEM Snapshot (hidden → nicht im
        # Container), aber der lokale init-Service braucht es IMMER →
        # aus der Task-Disk explizit nachlegen (alle Paket-Varianten).
        init_v = hidden.get(".init.sh")
        if init_v is not None:
            (top / "workspace" / ".init.sh").write_bytes(
                init_v["disk"].read_bytes())

        # 2) 👤-Dateien (NUR Tutor) am realen Pfad top-level
        if kind == "tutor":
            for rel, v in hidden.items():
                dst = top / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(v["disk"].read_bytes())

        # 3) images/ — Spec-Dockerfile 1:1
        (top / "images").mkdir()
        dockerfile, img_files = _image_build_files(task, img_spec)
        (top / "images" / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        for fname, fdata in img_files.items():
            (top / "images" / fname).write_bytes(fdata)

        # 4) Flags + Mount-Pfade (aus dem tatsächlichen Paket-Inhalt)
        has_run = (top / "workspace" / "run.sh").is_file()
        has_init = (top / "workspace" / ".init.sh").is_file()
        has_init_private = (kind == "tutor"
                            and (top / ".init_hidden.sh").is_file())
        has_verify = (kind == "tutor" and judge_path is not None
                      and (top / judge_path).is_file())
        ro_mounts = [p for p in fmap["ro_paths"]
                     if _has_content(top / "workspace" / p)]
        hid_mounts = [p for p in fmap["hid_paths"] if _has_content(top / p)]

        # 5) compose.yml — Services rein per Skript-Konvention
        #    (GPU: Nutzer ergänzt lokal ggf. den deploy-Block)
        (top / "compose.yml").write_text(
            _compose_content(task, slug, has_init=has_init,
                             has_init_private=has_init_private,
                             has_verify=has_verify,
                             ro_mounts=ro_mounts, hid_mounts=hid_mounts,
                             judge_path=judge_path or ""),
            encoding="utf-8")

        # 6) README.md
        (top / "README.md").write_text(
            _readme_content(task, kind, has_run=has_run,
                            has_init=has_init, has_init_private=has_init_private,
                            data_omitted=data_omitted, has_verify=has_verify,
                            submission=submission, live=live),
            encoding="utf-8")

        # 7) tar.gz (Fingerprint aus den Quellen wurde vor dem Bau geprüft;
        #    Live-Pakete werden nicht gecacht)
        buf = _targz(top)
        filename = f"{top_name}.tar.gz"
        if fingerprint is not None:
            with _PKG_CACHE_LOCK:
                if len(_PKG_CACHE) >= _PKG_CACHE_MAX:
                    _PKG_CACHE.pop(next(iter(_PKG_CACHE)))
                _PKG_CACHE[cache_key] = (fingerprint, filename, buf, time.time())
        return filename, buf
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _targz(top: Path) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        tar.add(top, arcname=top.name)
    return out.getvalue()
