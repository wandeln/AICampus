"""
Medienbibliothek: Datei-Speicherung + MediaUsage-Reconciliation.

Speicherort:  data/media/course_{id}/<uuid>.<ext>  (siehe config.MEDIA_DIR)
Versand:      authentifizierte Route GET /media/{course_id}/{filename} (main.py)
              — kein Static-Mount, damit Medien versteckter Aufgaben nicht
              ohne Kurs-Membership abrufbar sind.

Single Source of Truth für Einbindungen ist das Markdown selbst
(task.description / material.content, Referenz: /media/{course_id}/<datei>).
sync_media_usages() baut die MediaUsage-Tabelle daraus neu auf.
"""

import re
import uuid
from pathlib import Path
from typing import Any, Optional, TypedDict

from fastapi import HTTPException
from sqlmodel import Session, select

from config import AVATAR_DIR, MEDIA_DIR
from database.models import (
    CourseMaterial,
    CourseMedia,
    MaterialType,
    MediaUsage,
    ScriptSection,
    Task,
)

# ─── Upload-Limits & Whitelist ────────────────────────────────────
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_APPLET_BYTES = 200 * 1024  # 200 KB (LLM-generiertes HTML, inkl. inline JS/CSS)
MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB (Profilbilder)
ALLOWED_MEDIA: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
# SVG ist kein Bild, sondern ein Applet (save_svg_upload, sanitized) —
# Raster bleibt die einzige erlaubte Bild-Form.

_FILENAME_RE = re.compile(r"[A-Za-z0-9._\-]+\Z")


class RefEntry(TypedDict):
    """Eine Quellen-Referenz auf ein Medium (pro Aufgabe/Material dedupliziert)."""
    task_id: Optional[int]
    material_id: Optional[int]
    location: str
    count: int


class RefInfo(TypedDict):
    """Aggregierte Referenz-Infos pro Datei für die Medien-Übersicht."""
    total: int
    duplicates: list[str]


def media_url(media: CourseMedia) -> str:
    """Öffentliche (authentifizierte) URL eines Mediums."""
    return f"/media/{media.course_id}/{media.file_path.rsplit('/', 1)[-1]}"


def media_dir_for_course(course_id: int) -> Path:
    d = MEDIA_DIR / f"course_{course_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_upload(course_id: int, filename: str, data: bytes) -> tuple[str, str, int]:
    """Validiert + speichert einen Upload.

    Returns: (relativer file_path, mime_type, size)
    Raises:  400/413 bei ungültigem Upload.
    """
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_MEDIA:
        allowed = ", ".join(sorted(ALLOWED_MEDIA))
        raise HTTPException(400, f"Dateityp nicht erlaubt. Erlaubt: {allowed}")
    if not data:
        raise HTTPException(400, "Datei ist leer.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Datei zu groß (max. {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")

    course_dir = media_dir_for_course(course_id)
    stored_name = f"{uuid.uuid4().hex}{ext}"
    (course_dir / stored_name).write_bytes(data)
    return f"course_{course_id}/{stored_name}", ALLOWED_MEDIA[ext], len(data)


def replace_file(rel_path: str, filename: str, data: bytes) -> tuple[str, str, int]:
    """Überschreibt eine bestehende Medium-Datei am selben Pfad.

    Der Dateiname (UUID + Extension) bleibt unverändert, damit Markdown-Referenzen
    und gespeicherte URLs weiter funktionieren — nur Inhalt, MIME-Type und Größe
    werden aktualisiert.

    Returns: (relativer file_path, mime_type, size)
    Raises:  400/404/413 bei ungültigem Upload.
    """
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_MEDIA:
        allowed = ", ".join(sorted(ALLOWED_MEDIA))
        raise HTTPException(400, f"Dateityp nicht erlaubt. Erlaubt: {allowed}")
    if not data:
        raise HTTPException(400, "Datei ist leer.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Datei zu groß (max. {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")

    p = (MEDIA_DIR / rel_path).resolve()
    if not p.is_relative_to(MEDIA_DIR.resolve()) or not p.is_file():
        raise HTTPException(404, "Bestehende Datei nicht gefunden.")
    p.write_bytes(data)
    return rel_path, ALLOWED_MEDIA[ext], len(data)


# ─── Applets (LLM-generierte self-contained Dateien: HTML oder SVG) ────
#
# Applets gibt es in zwei Formaten (Unterscheidung: Datei-Extension —
# kein separates media_type):
# - .html: self-contained HTML-Dokument, läuft in einem sandboxed Iframe
#   (allow-scripts, ohne allow-same-origin). validate_applet_content() prüft
#   serverseitig, dass keine externen Ressourcen geladen werden
#   (Offline-Design + Sicherheit).
# - .svg: rohe SVG (statisch oder mit SMIL/CSS-Animationen), gerendert
#   via <img>. sanitize_svg() entfernt Skripte/Embeds VOR dem Speichern
#   (XSS-Schutz).
# Das 200-KB-Limit (MAX_APPLET_BYTES) gilt nur für LLM-Output (api/media.py).

_EXTERNAL_SRC_RE = re.compile(r"""(?:src|href)\s*=\s*["']?\s*(?:https?:)?//""")


def sanitize_svg(data: bytes) -> dict:
    """SVG für Medienbibliothek/Preview sanitisieren (XSS-Schutz).

    Entfernt Skript-/Embed-Elemente (script, foreignObject, iframe, object,
    embed), <style> mit Skriptinhalten, Event-Handler und skriptbare URL-Attribute.
    SMIL- und CSS-@keyframes-Animationen bleiben erhalten.
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

    if _local(root.tag) != "svg":
        return {"error": f"Kein SVG: Wurzelelement '<{_local(root.tag) or '?'}>' ist nicht <svg>."}

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


def validate_applet_content(content: str, fmt: str = "html") -> Optional[bytes]:
    """Validiert Applet-Content. Returns für SVG die sanitized Bytes (die
    gespeichert werden), für HTML None. Raises 400/413 bei Verstößen.

    Das 200-KB-Limit für LLM-Output wird vom Caller (api/media.py) geprüft.
    """
    if not content or not content.strip():
        raise HTTPException(400, "Der Applet-Code ist leer.")
    if fmt == "svg":
        data = content.encode("utf-8")
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Applet-SVG zu groß (max. {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
        result = sanitize_svg(data)
        if "error" in result:
            raise HTTPException(400, f"Ungültige SVG: {result['error']}")
        return result["data"]
    if fmt != "html":
        raise HTTPException(400, "Applet-Format muss „html“ oder „svg“ sein.")
    if len(content.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Applet-HTML zu groß (max. {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    low = content.lower()
    if "<html" not in low or "</html>" not in low:
        raise HTTPException(400, "Ungültiges Applet-HTML (es muss ein vollständiges HTML-Dokument sein).")
    if _EXTERNAL_SRC_RE.search(content):
        raise HTTPException(400, "Applet darf keine externen Ressourcen laden (keine CDN-Links/URLs in src/href).")
    return None


def applet_format_of(file_path: str) -> str:
    """Applet-Format (html|svg) aus dem Dateipfad ableiten."""
    return "svg" if file_path.lower().endswith(".svg") else "html"


def save_applet(course_id: int, content: str, fmt: str = "html") -> tuple[str, str, int]:
    """Validiert + speichert ein neues Applet (HTML oder SVG).

    Returns: (relativer file_path, mime_type, size)
    """
    sanitized = validate_applet_content(content, fmt)
    data = sanitized if sanitized is not None else content.encode("utf-8")
    course_dir = media_dir_for_course(course_id)
    ext = ".svg" if fmt == "svg" else ".html"
    stored_name = f"{uuid.uuid4().hex}{ext}"
    (course_dir / stored_name).write_bytes(data)
    return f"course_{course_id}/{stored_name}", "image/svg+xml" if fmt == "svg" else "text/html", len(data)


def replace_applet(rel_path: str, content: str, fmt: Optional[str] = None) -> tuple[str, str, int]:
    """Überschreibt eine Applet-Datei am selben Pfad.

    Der Dateiname bleibt unverändert, damit Markdown-Referenzen und gespeicherte
    URLs weiter funktionieren. fmt defaultet auf die Datei-Extension.

    Returns: (relativer file_path, mime_type, size)
    Raises:  400/404/413 bei ungültigem Content bzw. fehlender Datei.
    """
    if fmt is None:
        fmt = applet_format_of(rel_path)
    if applet_format_of(rel_path) != fmt:
        raise HTTPException(404, "Bestehende Applet-Datei nicht gefunden (Format passt nicht).")
    sanitized = validate_applet_content(content, fmt)
    data = sanitized if sanitized is not None else content.encode("utf-8")
    p = (MEDIA_DIR / rel_path).resolve()
    if not p.is_relative_to(MEDIA_DIR.resolve()) or not p.is_file():
        raise HTTPException(404, "Bestehende Applet-Datei nicht gefunden.")
    p.write_bytes(data)
    return rel_path, "image/svg+xml" if fmt == "svg" else "text/html", len(data)


def save_svg_upload(course_id: int, data: bytes) -> tuple[str, str, int]:
    """Sanitisiert + speichert eine hochgeladene SVG-Datei als Applet.

    Returns: (relativer file_path, mime_type, size)
    """
    if not data:
        raise HTTPException(400, "Datei ist leer.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Datei zu groß (max. {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    result = sanitize_svg(data)
    if "error" in result:
        raise HTTPException(400, f"Ungültige SVG: {result['error']}")
    course_dir = media_dir_for_course(course_id)
    stored_name = f"{uuid.uuid4().hex}.svg"
    (course_dir / stored_name).write_bytes(result["data"])
    return f"course_{course_id}/{stored_name}", "image/svg+xml", len(result["data"])


def svg_to_png(data: bytes, output_width: int = 1200) -> bytes:
    """Rasterisiert eine SVG zu PNG (für die Vision-basierte Metadaten-Generierung)."""
    try:
        import cairosvg
    except ImportError as e:
        raise HTTPException(400, "SVG kann nicht rasterisiert werden (cairosvg nicht installiert).") from e
    try:
        return cairosvg.svg2png(bytestring=data, output_width=output_width)
    except Exception as e:  # noqa: BLE001 — cairosvg wirft diverse Exceps bei defekten SVGs
        raise HTTPException(400, f"SVG kann nicht rasterisiert werden: {e}") from e


# ─── Avatare (Profilbilder, user-spezifisch) ──────────────────────
#
# Eigenes Verzeichnis (AVATAR_DIR), da Avatare keinem Kurs zugeordnet sind
# und nicht in die Kurs-Medien-Bibliothek (CourseMedia) gehören.

def save_avatar(filename: str, data: bytes) -> tuple[str, str]:
    """Validiert + speichert ein Profilbild-Upload.

    Returns: (relativer file_path, mime_type)
    Raises:  400/413 bei ungültigem Upload.
    """
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_MEDIA:
        allowed = ", ".join(sorted(ALLOWED_MEDIA))
        raise HTTPException(400, f"Dateityp nicht erlaubt. Erlaubt: {allowed}")
    if not data:
        raise HTTPException(400, "Datei ist leer.")
    if len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(413, f"Profilbild zu groß (max. {MAX_AVATAR_BYTES // (1024 * 1024)} MB).")

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{ext}"
    (AVATAR_DIR / stored_name).write_bytes(data)
    return f"avatars/{stored_name}", ALLOWED_MEDIA[ext]


def resolve_avatar_path(filename: str) -> Path | None:
    """Löst einen Dateinamen auf eine sichere Datei unter AVATAR_DIR.

    Returns: Pfad (wenn Datei existiert) oder None.
    """
    if not _FILENAME_RE.match(filename):
        return None
    p = (AVATAR_DIR / filename).resolve()
    if not p.is_relative_to(AVATAR_DIR.resolve()):
        return None
    return p if p.is_file() else None


def delete_avatar_file(rel_path: str) -> None:
    """Löscht ein Profilbild auf der Festplatte (best effort)."""
    try:
        p = (AVATAR_DIR / rel_path.rsplit("/", 1)[-1]).resolve()
        if p.is_relative_to(AVATAR_DIR.resolve()) and p.is_file():
            p.unlink()
    except OSError:
        pass  # Datei fehlt schon oder FS-Problem — DB-Eintrag wird trotzdem gelöscht


def resolve_media_path(course_id: int, filename: str) -> Path | None:
    """Löst einen Dateinamen auf eine sichere Datei unter MEDIA_DIR/course_{id}.

    Returns: Pfad (wenn Datei existiert) oder None.
    """
    if not _FILENAME_RE.match(filename):
        return None
    p = (MEDIA_DIR / f"course_{course_id}" / filename).resolve()
    if not p.is_relative_to(MEDIA_DIR.resolve()):
        return None
    return p if p.is_file() else None


def delete_file(file_path: str) -> None:
    """Löscht die Datei auf der Festplatte (best effort)."""
    try:
        p = (MEDIA_DIR / file_path).resolve()
        if p.is_relative_to(MEDIA_DIR.resolve()) and p.is_file():
            p.unlink()
    except OSError:
        pass  # Datei fehlt schon oder FS-Problem — DB-Eintrag wird trotzdem gelöscht


# ─── Referenz-Scan + Reconciliation ───────────────────────────────

def _reference_pattern(course_id: int) -> re.Pattern[str]:
    return re.compile(rf"/media/{course_id}/([A-Za-z0-9._\-]+)")


def _scan_references(session: Session, course_id: int) -> dict[str, list[RefEntry]]:
    """Scannt alle Aufgaben & Materialien des Kurses auf /media/{id}/<datei>-Referenzen.

    Returns: {filename: [RefEntry, ...]} (pro Quelle dedupliziert;
    count = wie oft in derselben Quelle referenziert)
    """
    pattern = _reference_pattern(course_id)
    refs: dict[str, dict[str, RefEntry]] = {}

    def add(fname: str, task_id: Optional[int], material_id: Optional[int], location: str) -> None:
        bucket = refs.setdefault(fname, {})
        # Skript-Kapitel haben kein material_id — der Location-String
        # ("Skript: <Titel>") unterscheidet sie voneinander.
        key = f"task:{task_id}" if task_id else f"material:{material_id}|{location}"
        if key in bucket:
            bucket[key]["count"] += 1
        else:
            bucket[key] = {
                "task_id": task_id,
                "material_id": material_id,
                "location": location,
                "count": 1,
            }

    for t in session.exec(select(Task).where(Task.course_id == course_id)).all():
        for fname in pattern.findall(t.description or ""):
            add(fname, t.id, None, f"Aufgabe: {t.title}")

    for mat in session.exec(
        select(CourseMaterial).where(CourseMaterial.course_id == course_id)
    ).all():
        kind = "Skript" if mat.material_type == MaterialType.SCRIPT else "Slides"
        for fname in pattern.findall(mat.content or ""):
            add(fname, None, mat.id, f"{kind}: {mat.title}")

    for s in session.exec(
        select(ScriptSection).where(ScriptSection.course_id == course_id)
    ).all():
        for fname in pattern.findall(s.content or ""):
            add(fname, None, None, f"Skript: {s.title}")

    return {fname: list(bucket.values()) for fname, bucket in refs.items()}


def sync_media_usages(session: Session, course_id: int) -> None:
    """Rebaut die MediaUsage-Einträge eines Kurses aus dem Markdown-Inhalt.

    Aufrufen nach: Task/Material create/update/delete/duplicate + Media delete.
    """
    media_list = session.exec(
        select(CourseMedia).where(CourseMedia.course_id == course_id)
    ).all()
    if media_list:
        for u in session.exec(
            select(MediaUsage).where(MediaUsage.media_id.in_([m.id for m in media_list]))  # type: ignore[attr-defined]
        ).all():
            session.delete(u)

    by_filename = {m.file_path.rsplit("/", 1)[-1]: m for m in media_list}
    for fname, entries in _scan_references(session, course_id).items():
        media = by_filename.get(fname)
        if not media:
            continue  # Referenz auf unbekanntes Medium (z.B. gelöscht)
        for e in entries:
            session.add(
                MediaUsage(
                    media_id=media.id,  # type: ignore[arg-type]
                    task_id=e["task_id"],
                    material_id=e["material_id"],
                    location=e["location"],
                )
            )
    session.commit()


def _replace_references(session: Session, course_id: int, old_fname: str, new_fname: str) -> None:
    """Ersetzt Markdown-Referenzen auf eine umbenannte Medium-Datei in allen
    Kurs-Inhalten (dieselben Felder wie der Referenz-Scan)."""
    for t in session.exec(select(Task).where(Task.course_id == course_id)).all():
        if t.description and old_fname in t.description:
            t.description = t.description.replace(old_fname, new_fname)
            session.add(t)
    for mat in session.exec(select(CourseMaterial).where(CourseMaterial.course_id == course_id)).all():
        if mat.content and old_fname in mat.content:
            mat.content = mat.content.replace(old_fname, new_fname)
            session.add(mat)
    for s in session.exec(select(ScriptSection).where(ScriptSection.course_id == course_id)).all():
        if s.content and old_fname in s.content:
            s.content = s.content.replace(old_fname, new_fname)
            session.add(s)


def switch_applet_format(session: Session, course_id: int, media: CourseMedia, new_format: str) -> None:
    """Formatwechsel eines Applets (html↔svg): Datei-Umbenennung (gleiche
    UUID), MIME-Update + Reparatur der Markdown-Referenzen.

    Der Content bleibt unverändert — die eigentliche Umwandlung macht das
    LLM (Applet-Studio / MCP). Wirft 400/404 bei ungültigem Format bzw.
    fehlender Datei.
    """
    if new_format not in ("html", "svg"):
        raise HTTPException(400, "Format muss „html“ oder „svg“ sein.")
    cur = applet_format_of(media.file_path)
    if cur == new_format:
        raise HTTPException(400, f"Das Applet hat bereits das Format „{cur}“.")
    p = (MEDIA_DIR / media.file_path).resolve()
    if not p.is_relative_to(MEDIA_DIR.resolve()) or not p.is_file():
        raise HTTPException(404, "Applet-Datei fehlt.")
    old_fname = p.name
    new_fname = f"{p.stem}.{new_format}"
    p.rename(p.with_name(new_fname))
    _replace_references(session, course_id, old_fname, new_fname)
    media.file_path = f"course_{course_id}/{new_fname}"
    media.mime_type = "image/svg+xml" if new_format == "svg" else "text/html"
    session.add(media)
    session.commit()
    session.refresh(media)
    sync_media_usages(session, course_id)


def reference_counts(session: Session, course_id: int) -> dict[str, RefInfo]:
    """Zusätzliche Referenz-Infos pro Datei für die Medien-Übersicht.

    Returns: {filename: {"total": n, "duplicates": [location, ...]}}
    (duplicates = Quellen, in denen das Medium mehrfach vorkommt)
    """
    result: dict[str, RefInfo] = {}
    for fname, entries in _scan_references(session, course_id).items():
        total = sum(e["count"] for e in entries)
        duplicates = [e["location"] for e in entries if e["count"] > 1]
        result[fname] = {"total": total, "duplicates": duplicates}
    return result


def all_media_for_course(session: Session, course_id: int) -> list[dict]:
    """Alle Medien des Kurses für LLM-Prompts (z.B. Aufgabenstellung).

    Returns: [{"title", "description", "url"}]
    """
    return [
        {
            "title": m.title,
            "description": (m.llm_description or "").strip(),
            "url": media_url(m),
        }
        for m in session.exec(
            select(CourseMedia).where(CourseMedia.course_id == course_id)
        ).all()
    ]


def unused_media_for_script(session: Session, course_id: int) -> list[dict]:
    """Sichtbare Medien, die in keinem Skript-Kapitel referenziert sind.

    Für den LLM-Prompt, damit das LLM passende Medien einbinden kann.
    Returns: [{"title", "description", "url"}]
    """
    pattern = _reference_pattern(course_id)
    used: set[str] = set()
    for s in session.exec(
        select(ScriptSection).where(ScriptSection.course_id == course_id)
    ).all():
        used.update(pattern.findall(s.content or ""))

    result = []
    for m in session.exec(
        select(CourseMedia).where(CourseMedia.course_id == course_id)
    ).all():
        fname = m.file_path.rsplit("/", 1)[-1]
        if fname in used:
            continue
        result.append({
            "title": m.title,
            "description": (m.llm_description or "").strip(),
            "url": media_url(m),
        })
    return result
