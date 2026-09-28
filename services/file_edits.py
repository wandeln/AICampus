"""
Serverseitiges Anwenden der LLM-„file_edits“ (stellenweise Edits) auf den
Inhalt einer Workspace-Task-Datei (beliebige Text-/Code-Datei).

Im Unterschied zu services/content_edits.py (Markdown-Kapitel) gibt es hier
KEINE Heading-Logik (replace_section/insert_after/delete_section) — für
Code-/Shell-Dateien wären #-Zeilen (Kommentare, Shebangs) falsche Anker.
Einzige Operation ist die generische Snippet-Ersetzung:
- {"op": "replace_span", "old": "...", "new": "..."}
  kurzes, eindeutig vorkommendes Snippet ersetzen (mehrere Zeilen erlaubt;
  "new" = "" löscht das Snippet)

Verwendet vom AI-Flow (api/tutor.py). Anwendung ist partiell: Jedes Edit
wird unabhängig auf den VORHANDENEN Dateiinhalt aufgelöst; fehlgeschlagene
Edits (Anker nicht gefunden/mehrdeutig, Überlappung, unbekanntes op) werden
übersprungen und als Warnung gemeldet, die übrigen werden trotzdem
angewendet. Bei Überlappung gewinnt das in der Liste zuerst genannte Edit.
Wirft FileEditError, wenn gar kein Edit anwendbar ist — dann bleibt die
Datei unverändert.

Die Snippet-Auflösung (exakt + whitespace-insensitiver Fallback) wird aus
services/content_edits.py wiederverwendet — exakt dieselbe Semantik wie
bei den Skript-/Folien-Edits.
"""

from services.content_edits import (
    ContentEditError,
    _resolve_span,
    _spans_overlap,
)


class FileEditError(ValueError):
    """Edit ungültig oder unklar (deutsche Meldung)."""


def _resolve_single_edit(content: str, edit: object) -> tuple[int, int, str]:
    """Löst ein einzelnes Datei-Edit zu (start, end, Ersetzung) auf; wirft
    FileEditError/ContentEditError, wenn es nicht anwendbar ist."""
    if not isinstance(edit, dict):
        raise FileEditError("LLM-Datei-Edit nicht anwendbar: Edit ist kein Objekt.")
    op = str(edit.get("op") or "").strip()
    if op != "replace_span":
        raise FileEditError(
            f"LLM-Datei-Edit nicht anwendbar: unbekanntes „op“ {op!r} "
            "(erlaubt: „replace_span“).")
    old = str(edit.get("old") or "")
    new = str(edit.get("new") or "")
    s, e = _resolve_span(content, old)
    return s, e, new


def apply_file_edits(content: str, edits: object) -> tuple[str, int, list[str]]:
    """Wendet die LLM-Edit-Liste („file_edits“) auf den Dateiinhalt an.

    Partielle Anwendung: Jedes Edit wird unabhängig aufgelöst; fehlgeschlagene
    Edits (Anker nicht gefunden/mehrdeutig, Überlappung, unbekanntes op) werden
    übersprungen und als Warnung gemeldet, die übrigen werden trotzdem
    angewendet. Bei Überlappung gewinnt das in der Liste zuerst genannte Edit.

    Gibt (neuer_content, applied_count, warnings) zurück. Wirft FileEditError,
    wenn gar kein Edit anwendbar ist — dann bleibt die Datei unverändert."""
    if not isinstance(edits, list) or not edits:
        raise FileEditError(
            "LLM-Antwort ungültig: „edits“ ist keine (nicht-leere) Liste von "
            "Edit-Objekten.")
    accepted: list[tuple[int, int, str]] = []  # (start, end, Ersetzung)
    failed: list[str] = []  # je „Edit N: <Meldung>“
    for i, edit in enumerate(edits):
        try:
            span = _resolve_single_edit(content, edit)
        except (ContentEditError, FileEditError) as e:
            failed.append(f"Edit {i + 1}: {e}")
            continue
        if any(_spans_overlap(span, acc) for acc in accepted):
            failed.append(
                f"Edit {i + 1}: überschneidet sich mit einem vorherigen "
                "Edit — übersprungen.")
            continue
        accepted.append(span)
    if not accepted:
        raise FileEditError("LLM-Datei-Edits nicht anwendbar: " + "; ".join(failed))
    accepted.sort(key=lambda sp: (sp[0], sp[1]))
    parts: list[str] = []
    pos = 0
    for s, e, repl in accepted:
        parts.append(content[pos:s])
        parts.append(repl)
        pos = e
    parts.append(content[pos:])
    warnings: list[str] = []
    if len(accepted) < len(edits):
        warnings.append(
            f"{len(accepted)} von {len(edits)} Datei-Edits umgesetzt — "
            f"{len(edits) - len(accepted)} fehlgeschlagen: " + "; ".join(failed))
    return "".join(parts), len(accepted), warnings
