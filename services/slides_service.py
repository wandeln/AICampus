"""
Slide-Decks & Slide-Theme.

Format „Markdown plus":
- Markdown ist die Single Source of Truth.
- Folientrenner = eine eigene Zeile ``---`` (fence-aware: Codeblöcke werden
  nicht gespalten).
- Unterfolien-Trenner = eine eigene Zeile ``--`` (fence-aware), NUR innerhalb
  einer Folie: teilt sie in vertikal gestapelte Unterfolien (Reveal-Nest
  <section><section>…</section></section>). Jeder Segment wird wie eine
  normale Folie geparsed (eigene Direktiven/Notiz/Hintergrund).
- Pro Folie dürfen am Anfang (aufeinanderfolgenden Zeilen) Richtlinien stehen:
    layout: center | topleft | default   (Default: default)
    transition: fade | slide | zoom | none | autoanimate   (Default: autoanimate)
    class: [A-Za-z0-9_-]+
    notes: <eine Zeile>
    background: ![Titel](/media/…/datei.png) oder ![Titel](/media/…/datei.html)
      (Bild bzw. .html-Applet als vollflächiger Folienhintergrund)
      optionales {zoom=X} (Punkt optional, X > 0) — nur bei .html-Applets,
      skaliert das Applet um den Faktor X (Iframe selbst wird vergrößert,
      Layout-Box wird um 1/X verkleinert, damit der Applet-Inhalt scharf
      vergrößert wird statt gecropped)
- Spalten: Renderer-Seite (markdown-renderer.js) — ``@startcolumn[:gewicht]`` …
  ``@nextcolumn[:gewicht]`` … ``@endcolumn``, nicht Teil des Folien-Parsers.

Das Theme (Design) pro Kurs ist ein JSON-Objekt (eine DB-Zeile pro Kurs):
    {
      "template": "light" | "dark" | "serif" | "simple",
      "colors": {"primary", "accent", "background", "text"},  # je #rrggbb
      "font_scale": 0.8–1.5,
      "footer": true/false,
      "logo_media_id": int | null,
      "aspect_ratio": "4:3" | "3:2" | "sqrt2" | "5:3" | "16:9" | "2:1" | "golden"
    }
"""

import math
import re
from dataclasses import dataclass, field
from typing import Optional

# "default" (Standard): Titel oben, übriger Inhalt vertikal zentriert;
# "topleft": Inhalt direkt unter dem Titel (Folien mit viel Text).
LAYOUTS = ("center", "topleft", "default")
# "autoanimate" ist die Standard-Transition (Reveal-Auto-Animate: Inhalte
# animieren zwischen den Folien ineinander); fade/slide/zoom/none sind die
# klassischen Reveal-Transitions.
TRANSITIONS = ("fade", "slide", "zoom", "none", "autoanimate")
DEFAULT_TRANSITION = "autoanimate"
COLOR_KEYS = ("primary", "accent", "background", "text")

# Feste Seitenverhältnisse (Key → numerischer Wert Breite/Höhe).
# Der Key wird im Theme gespeichert; Default ist "16:9".
ASPECT_RATIOS: dict[str, float] = {
    "4:3": 4 / 3,
    "3:2": 3 / 2,
    "sqrt2": math.sqrt(2),
    "5:3": 5 / 3,
    "16:9": 16 / 9,
    "2:1": 2.0,
    "golden": (1 + math.sqrt(5)) / 2,
}
ASPECT_LABELS: dict[str, str] = {
    "4:3": "4:3 (klassisch)",
    "3:2": "3:2",
    "sqrt2": "√2:1 (DIN A)",
    "5:3": "5:3",
    "16:9": "16:9 (Widescreen)",
    "2:1": "2:1",
    "golden": "Goldener Schnitt",
}
DEFAULT_ASPECT = "16:9"

# Logo: Ecke der Folie (Key) + Skalierung (1.0 = Default-Größe 2.6em).
LOGO_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right")
DEFAULT_LOGO_POSITION = "top-right"
LOGO_SCALE_MIN = 0.5
LOGO_SCALE_MAX = 2.0

# Template-Defaults (Farben; font: Serifen-Fallback für das "serif"-Template)
THEME_TEMPLATES: dict[str, dict] = {
    "light": {"primary": "#1d4ed8", "accent": "#2563eb", "background": "#ffffff", "text": "#1f2937", "font": ""},
    "dark": {"primary": "#60a5fa", "accent": "#fbbf24", "background": "#0f172a", "text": "#e5e7eb", "font": ""},
    "serif": {"primary": "#7c2d12", "accent": "#b45309", "background": "#fffbf5", "text": "#292524", "font": "serif"},
    "simple": {"primary": "#111827", "accent": "#6b7280", "background": "#fafafa", "text": "#1f2937", "font": ""},
}

FONT_SCALE_MIN = 0.8
FONT_SCALE_MAX = 1.5


class SlideError(ValueError):
    """Fehler beim Parsen/Validieren von Slides oder Themes (deutsche Meldung)."""


@dataclass
class Slide:
    """Eingeparste Folie: Direktiven + Folientext.

    Ein Block mit ≥2 ``--``-Segmenten wird zu einem Stack: ``children`` ist
    dann die Liste der (vertikalen) Unterfolien, der Stack selbst ist ein
    leerer Container (``columns == [""]``, keine Direktiven)."""

    layout: str = "default"  # Default: Titel oben, Rest vertikal zentriert
    transition: Optional[str] = None
    css_class: Optional[str] = None
    notes: Optional[str] = None
    background: Optional[str] = None  # Rohwert der background:-Richtlinie (![Titel](…))
    # Immer genau 1 Eintrag: der Folientext (Markdown).
    # Spalten (@startcolumn … @nextcolumn … @endcolumn) sind Renderer-Seite
    # (markdown-renderer.js) und Teil des Texts, kein eigener Parser-Begriff.
    columns: list[str] = field(default_factory=lambda: [""])
    children: list["Slide"] = field(default_factory=list)  # nur bei Stacks (`--`)


# ═══════════════════════════════════════════════════════════════════
# Parser
# ═══════════════════════════════════════════════════════════════════

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_DIRECTIVE = re.compile(r"^(layout|transition|class|notes|background):\s*(\S.*)$")
_CLASS = re.compile(r"^[A-Za-z0-9_-]+$")
# background: Markdown-Bild-/Applet-Snippet ![Titel](/media/…) ohne Zusätze
# (kein Label/Attribute) — der Pfad darf kein Whitespace enthalten.
# Optionales {zoom=X} am Ende (Punkt optional) — wird serverseitig nur auf
# valide Syntax geprüft; die Auswirkung (Iframe skalieren) hat nur .html-
# Applets (Klient-Seite, s. slides.js/wireBgZoom).
# { im Pfad ausgeschlossen: sonst würde ein falsch platziertes {zoom=X}
# (innerhalb der Klammer) stillschweigend als Pfadteil verschluckt.
_BG_IMAGE = re.compile(r"^!\[[^\]]*\]\([^)\s{]+\)(?:[ \t]*\{\.?zoom=[\d.]+\})?$")


def _split_slides(content: str) -> list[str]:
    """Teilt den Inhalt an Zeilen mit exakt ``---`` (fence-aware)."""
    blocks: list[str] = []
    current: list[str] = []
    fence_char = ""
    fence_len = 0

    for line in content.splitlines():
        if fence_char:
            current.append(line)
            lead = len(line) - len(line.lstrip(" "))
            rest = line.strip()
            if lead <= 3 and len(rest) >= fence_len and set(rest) == {fence_char}:
                fence_char = ""
        else:
            if line.strip() == "---":
                blocks.append("\n".join(current))
                current = []
                continue
            m = _FENCE_OPEN.match(line)
            if m:
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
            current.append(line)

    blocks.append("\n".join(current))
    return blocks


def _split_vertical(block: str) -> list[str]:
    """Teilt einen Folien-Block an Zeilen mit exakt ``--`` (fence-aware) in
    Segmente: 1 Segment = normale Folie, ≥2 = vertikal gestapelte
    Unterfolien. Exakter Zeilenabgleich, damit ``--`` nie die ersten beiden
    Zeichen von ``---`` matcht (dafür ist ``_split_slides`` zuständig)."""
    segments: list[str] = []
    current: list[str] = []
    fence_char = ""
    fence_len = 0

    for line in block.splitlines():
        if fence_char:
            current.append(line)
            lead = len(line) - len(line.lstrip(" "))
            rest = line.strip()
            if lead <= 3 and len(rest) >= fence_len and set(rest) == {fence_char}:
                fence_char = ""
        else:
            if line.strip() == "--":
                segments.append("\n".join(current))
                current = []
                continue
            m = _FENCE_OPEN.match(line)
            if m:
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
            current.append(line)

    segments.append("\n".join(current))
    return segments


def _parse_block(block: str, index: int) -> Slide:
    """Parsert ein Folien-Block (Richtlinien + Folientext) streng."""
    lines = block.splitlines()
    slide = Slide()
    seen: set[str] = set()
    i = 0
    n = len(lines)

    while i < n and not lines[i].strip():
        i += 1

    while i < n:
        m = _DIRECTIVE.match(lines[i])
        if not m:
            break
        key, value = m.group(1), m.group(2).strip()
        if key in seen:
            raise SlideError(f"Folie {index}: Richtlinie '{key}' ist doppelt angegeben.")
        seen.add(key)

        if key == "layout":
            if value not in LAYOUTS:
                raise SlideError(
                    f"Folie {index}: Unbekanntes Layout '{value}' (erlaubt: {', '.join(LAYOUTS)})."
                )
            slide.layout = value
        elif key == "transition":
            if value not in TRANSITIONS:
                raise SlideError(
                    f"Folie {index}: Unbekannte Transition '{value}' (erlaubt: {', '.join(TRANSITIONS)})."
                )
            slide.transition = value
        elif key == "class":
            if not _CLASS.match(value):
                raise SlideError(
                    f"Folie {index}: Ungültige Klassen-Angabe '{value}' "
                    "(nur Buchstaben, Ziffern, '-' und '_' erlaubt)."
                )
            slide.css_class = value
        elif key == "background":
            if not _BG_IMAGE.match(value):
                raise SlideError(
                    f"Folie {index}: Ungültige Hintergrund-Angabe '{value}' "
                    "(erwartet: „![Titel](/media/…/datei.png)“ oder „![Titel](/media/…/datei.html)“; "
                    "bei .html-Applets optional angehängt: {zoom=2})."
                )
            slide.background = value
        else:  # notes
            slide.notes = value
        i += 1

    # Spalten (@startcolumn … @nextcolumn … @endcolumn) sind Renderer-Seite
    # (markdown-renderer.js) — der Folientext bleibt ein einziger Part.
    slide.columns = ["\n".join(lines[i:]).strip()]
    return slide


def parse_slides(content: str) -> list[Slide]:
    """Parst ein Slide-Deck (strikt). Leerer Inhalt → leere Liste.

    Ein Block mit ≥2 ``--``-Segmenten wird zu einem Stack: leerer
    Eltern-Slide mit ``children`` (jedes Segment = eine Unterfolie; die
    Fehlernummer bezieht sich auf die umgebende Folie, d.h. den
    ``---``-Block)."""
    if not content or not content.strip():
        return []
    slides: list[Slide] = []
    for i, block in enumerate(_split_slides(content), start=1):
        segments = _split_vertical(block)
        if len(segments) == 1:
            slides.append(_parse_block(segments[0], i))
        else:
            stack = _parse_block("", i)
            stack.children = [_parse_block(seg, i) for seg in segments]
            slides.append(stack)
    return slides


def slide_count(content: str) -> int:
    """Anzahl anzeigbarer (Blatt-)Folien, fehlerverzeihend — für
    Kachel-Badges & Listen. Ein Stack zählt jede ``--``-Unterfolie
    (stimmt mit Reveal's Folienzähler überein)."""
    if not content or not content.strip():
        return 0
    return sum(len(_split_vertical(b)) for b in _split_slides(content))


def strip_slide_notes(content: str) -> str:
    """Entfernt die ``notes:``-Richtlinien aus einem Slide-Deck (fence-aware).

    Sprechernotizen dürfen Nicht-Tutoren nicht erreichen (API-Response →
    nicht im DOM). Die Richtlinie wird nur in ihrer gültigen Position
    (Richtlinienkette am Folienanfang, aufeinanderfolgende Zeilen) entfernt;
    alles andere bleibt unverändert — auch ``notes: …`` im Fließtext oder
    in Codeblöcken. ``--``-Zeilen sind ebenfalls Blockgrenzen (die
    Richtlinienkette einer Unterfolie wird so korrekt erkannt).
    """
    if not content:
        return ""

    out_lines: list[str] = []
    block_lines: list[str] = []
    fence_char = ""
    fence_len = 0

    def flush_block() -> None:
        # Richtlinienkette am Blockanfang (wie _parse_block): führende
        # Leerzeilen überspringen, dann aufeinanderfolgende "key: value"-Zeilen.
        n = len(block_lines)
        i = 0
        while i < n and not block_lines[i].strip():
            i += 1
        j = i
        while j < n and _DIRECTIVE.match(block_lines[j]):
            j += 1
        for k in range(i, j):
            m = _DIRECTIVE.match(block_lines[k])
            if m and m.group(1) == "notes":
                continue  # Sprechernotiz filtern
            out_lines.append(block_lines[k])
        out_lines.extend(block_lines[j:])
        block_lines.clear()

    for line in content.splitlines():
        if fence_char:
            block_lines.append(line)
            lead = len(line) - len(line.lstrip(" "))
            rest = line.strip()
            if lead <= 3 and len(rest) >= fence_len and set(rest) == {fence_char}:
                fence_char = ""
        else:
            if line.strip() == "---":
                flush_block()
                out_lines.append("---")
                continue
            if line.strip() == "--":
                flush_block()
                out_lines.append("--")
                continue
            m = _FENCE_OPEN.match(line)
            if m:
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
            block_lines.append(line)
    flush_block()

    return "\n".join(out_lines)


# ═══════════════════════════════════════════════════════════════════
# LLM-Edits: stellenweise Änderungen am bestehenden Deck
# ═══════════════════════════════════════════════════════════════════

def numbered_slide_content(content: str) -> str:
    """Deck mit expliziten Foliennummern („%% Folie N %%“-Marker) — für den
    LLM-Prompt, damit das LLM in „content_edits“ fehlerfrei auf Folien
    referenzieren kann. Die Marker sind KEIN Teil des Deck-Inhalts.
    """
    if not content or not content.strip():
        return ""
    blocks = [b.strip() for b in _split_slides(content)]
    return "\n\n---\n\n".join(f"%% Folie {i} %%\n{b}" for i, b in enumerate(blocks, start=1))


def _validate_single_slide(block: str, edit_no: int, op: str) -> str:
    """Validiert Folieninhalt für replace_slide/insert_slide_after:
    nicht leer, ohne Folientrenner (fence-aware), strikt parsbar."""
    b = (block or "").strip()
    if not b:
        raise SlideError(f"Edit {edit_no} („{op}“): Folieninhalt ist leer.")
    if len(_split_slides(b)) != 1:
        raise SlideError(
            f"Edit {edit_no} („{op}“): Folieninhalt darf keinen Folientrenner („---“) enthalten."
        )
    parse_slides(b)
    return b


def _normalize_ws(text: str) -> tuple[str, list[int]]:
    """Komprimiert Whitespace-Runs auf ein einzelnes Leerzeichen.
    Liefert (normalisierte Zeichenkette, Originalindizes der normalisierten Zeichen)."""
    chars: list[str] = []
    pos: list[int] = []
    in_ws = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not in_ws:
                chars.append(" ")
                pos.append(i)
            in_ws = True
        else:
            chars.append(ch)
            pos.append(i)
            in_ws = False
    return "".join(chars), pos


def _resolve_span(content: str, old: str, edit_no: int) -> tuple[int, int]:
    """Span eines kurzen Snippets, das EXAKT EINMAL im Inhalt vorkommt und
    innerhalb EINER (Unter-)Folie bleibt (kein Folien- oder Unterfolien-
    trenner, fence-aware). Zuerst exakte Suche; als Fallback
    whitespace-insensitive Suche (Zeilenumbrüche/mehrere Leerzeichen
    normalisiert)."""
    if not (old or "").strip():
        raise SlideError(f"Edit {edit_no} („replace_span“): „old“ ist leer.")
    old_blocks = _split_slides(old)
    if len(old_blocks) > 1 or any(len(_split_vertical(b)) > 1 for b in old_blocks):
        raise SlideError(
            f"Edit {edit_no} („replace_span“): „old“ darf keinen Trenner („---“/„--“) enthalten."
        )
    idx = content.find(old)
    if idx >= 0:
        if content.find(old, idx + 1) >= 0:
            raise SlideError(f"Edit {edit_no}: Snippet „{old[:60]}…“ ist mehrdeutig (mehrere Treffer).")
        return idx, idx + len(old)
    n_content, pos = _normalize_ws(content)
    n_old, _ = _normalize_ws(old)
    if n_old:
        n_idx = n_content.find(n_old)
        if n_idx >= 0 and n_content.find(n_old, n_idx + 1) < 0:
            start = pos[n_idx]
            end = pos[n_idx + len(n_old) - 1] + 1
            if content[end - 1].isspace():
                while end < len(content) and content[end].isspace():
                    end += 1
            return start, end
    raise SlideError(
        f"Edit {edit_no}: Snippet „{old[:60]}…“ wurde im aktuellen Inhalt nicht (eindeutig) gefunden."
    )


def _slide_number(raw: object, edit_no: int, op: str) -> int:
    """Foliennummer aus einem Edit („slide“) — int, ganzwertiges float oder
    Ziffern-String; alle anderen Werte → SlideError."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise SlideError(f"Edit {edit_no} („{op}“): „slide“ muss eine Zahl sein.")
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw.lstrip("-").isdigit():
            raise SlideError(f"Edit {edit_no} („{op}“): „slide“ muss eine Zahl sein.")
        return int(raw)
    if isinstance(raw, float) and not raw.is_integer():
        raise SlideError(f"Edit {edit_no} („{op}“): „slide“ muss eine ganze Zahl sein.")
    return int(raw)


def apply_slide_edits(content: str, edits: object) -> str:
    """Wendet die LLM-Edit-Liste („content_edits“) auf ein bestehendes
    Slide-Deck an.

    Ops:
    - {"op": "replace_slide", "slide": N, "content": "..."}   Folie N ersetzen
    - {"op": "insert_slide_after", "slide": N, "content": "..."}
      neue Folie nach N (0 = als erste Folie)
    - {"op": "delete_slide", "slide": N}                       Folie N löschen
    - {"op": "replace_span", "old": "...", "new": "..."}
      kurzes, eindeutig vorkommendes Snippet innerhalb EINER (Unter-)Folie
      ersetzen (darf weder „---“ noch „--“ überspannen)

    Die Foliennummern beziehen sich auf das Deck VOR der Bearbeitung. Eine
    Folie kann „--“-Unterfolien enthalten: replace_slide/delete_slide wirken
    auf die ganze Folie (inkl. aller Unterfolien); der „content“ von
    replace_slide/insert_slide_after darf selbst „--“-Unterfolien enthalten.
    Gibt den neuen Inhalt zurück; wirft SlideError, wenn ein Edit ungültig
    oder mehrdeutig ist (unbekanntes op, Foliennummer außerhalb des Bereichs,
    „old“ nicht eindeutig) — dann wird nichts angewendet.
    """
    if not isinstance(edits, list) or not edits:
        raise SlideError("„content_edits“ ist keine (nicht-leere) Liste von Edit-Objekten.")
    if not content or not content.strip():
        raise SlideError("Es existiert kein Inhalt, auf den die Edits angewendet werden könnten.")

    blocks = [b.strip() for b in _split_slides(content)]
    n = len(blocks)

    replaced: dict[int, str] = {}
    deleted: set[int] = set()
    inserts: dict[int, list[str]] = {}
    span_edits: list[tuple[int, str, str]] = []
    touched: dict[int, str] = {}  # Folie → op (Konflikt-Erkennung replace/delete)

    for i, edit in enumerate(edits):
        edit_no = i + 1
        if not isinstance(edit, dict):
            raise SlideError(f"Edit {edit_no} ist kein Objekt.")
        op = str(edit.get("op") or "").strip()
        if op in ("replace_slide", "insert_slide_after", "delete_slide"):
            slide_no = _slide_number(edit.get("slide"), edit_no, op)
            if op == "insert_slide_after":
                if not 0 <= slide_no <= n:
                    raise SlideError(
                        f"Edit {edit_no} („insert_slide_after“): Foliennummer {slide_no} "
                        f"außerhalb des Bereichs (0 bis {n} erlaubt)."
                    )
            elif not 1 <= slide_no <= n:
                raise SlideError(
                    f"Edit {edit_no} („{op}“): Foliennummer {slide_no} "
                    f"außerhalb des Bereichs (1 bis {n} erlaubt)."
                )
            if op == "replace_slide":
                if slide_no in touched:
                    raise SlideError(
                        f"Edit {edit_no}: Folie {slide_no} wird bereits von einem anderen "
                        f"Edit („{touched[slide_no]}“) bearbeitet."
                    )
                touched[slide_no] = op
                replaced[slide_no] = _validate_single_slide(str(edit.get("content") or ""), edit_no, op)
            elif op == "delete_slide":
                if slide_no in touched:
                    raise SlideError(
                        f"Edit {edit_no}: Folie {slide_no} wird bereits von einem anderen "
                        f"Edit („{touched[slide_no]}“) bearbeitet."
                    )
                touched[slide_no] = op
                deleted.add(slide_no)
            else:  # insert_slide_after
                inserts.setdefault(slide_no, []).append(
                    _validate_single_slide(str(edit.get("content") or ""), edit_no, op)
                )
        elif op == "replace_span":
            span_edits.append((edit_no, str(edit.get("old") or ""), str(edit.get("new") or "")))
        else:
            raise SlideError(f"Edit {edit_no}: unbekanntes „op“ {op!r}.")

    # Folien-Ops in einem Pass auf die Original-Blöcke (Einfüge-Reihenfolge
    # der Edits erhalten); dann Snippet-Ops sequenziell auf den zusammen-
    # gefügten Inhalt (frischer Eindeutigs-Check pro Edit).
    out_blocks: list[str] = list(inserts.get(0, []))
    for k in range(1, n + 1):
        if k not in deleted:
            out_blocks.append(replaced.get(k, blocks[k - 1]))
        out_blocks.extend(inserts.get(k, []))

    new_content = "\n\n---\n\n".join(out_blocks)

    for edit_no, old, new in span_edits:
        s, e = _resolve_span(new_content, old, edit_no)
        new_content = new_content[:s] + new + new_content[e:]

    # Mehrere Leerzeilen, die durch Edits entstehen können, zusammenziehen.
    new_content = re.sub(r"\n{3,}", "\n\n", new_content)
    # Finale strenge Validierung (leeres Deck nach Delete-all ist gültig).
    parse_slides(new_content)
    return new_content


# ═══════════════════════════════════════════════════════════════════
# Theme
# ═══════════════════════════════════════════════════════════════════

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _clamp_font_scale_lenient(value) -> float:
    """Clamping für die Anzeige (unzulässige Werte → 1.0, kein Fehler)."""
    try:
        return min(FONT_SCALE_MAX, max(FONT_SCALE_MIN, float(value)))
    except (TypeError, ValueError):
        return 1.0


def _clamp_font_scale(value) -> float:
    """Clamping für die Validierung (PUT); unzulässige Werte → SlideError."""
    try:
        fs = float(value)
    except (TypeError, ValueError):
        raise SlideError("'font_scale' muss eine Zahl sein.")
    return min(FONT_SCALE_MAX, max(FONT_SCALE_MIN, fs))


def _clamp_logo_scale_lenient(value) -> float:
    """Clamping für die Anzeige (unzulässige Werte → 1.0, kein Fehler)."""
    try:
        return min(LOGO_SCALE_MAX, max(LOGO_SCALE_MIN, float(value)))
    except (TypeError, ValueError):
        return 1.0


def _clamp_logo_scale(value) -> float:
    """Clamping für die Validierung (PUT); unzulässige Werte → SlideError."""
    try:
        ls = float(value)
    except (TypeError, ValueError):
        raise SlideError("'logo_scale' muss eine Zahl sein.")
    return min(LOGO_SCALE_MAX, max(LOGO_SCALE_MIN, ls))


def resolve_theme(raw: Optional[dict]) -> dict:
    """Füllt ein (ggf. unvollständiges) Theme mit Template-Defaults auf (lenient)."""
    raw = raw or {}
    template = raw.get("template") if raw.get("template") in THEME_TEMPLATES else "light"
    base = THEME_TEMPLATES[template]

    colors_in = raw.get("colors") or {}
    if not isinstance(colors_in, dict):
        colors_in = {}

    aspect = raw.get("aspect_ratio") if raw.get("aspect_ratio") in ASPECT_RATIOS else DEFAULT_ASPECT

    return {
        "template": template,
        "font": base["font"],
        "colors": {key: (colors_in.get(key) or base[key]) for key in COLOR_KEYS},
        "font_scale": _clamp_font_scale_lenient(raw.get("font_scale", 1.0)),
        "footer": bool(raw.get("footer", True)),
        "logo_media_id": raw.get("logo_media_id") or None,
        "logo_position": (
            raw.get("logo_position")
            if raw.get("logo_position") in LOGO_POSITIONS
            else DEFAULT_LOGO_POSITION
        ),
        "logo_scale": _clamp_logo_scale_lenient(raw.get("logo_scale", 1.0)),
        "aspect_ratio": aspect,
        "aspect_value": ASPECT_RATIOS[aspect],
    }


def validate_theme(raw) -> dict:
    """Validiert ein Theme streng (PUT) und liefert die normalisierte Form."""
    if not isinstance(raw, dict):
        raise SlideError("Das Theme muss ein JSON-Objekt sein.")

    theme: dict = {
        "template": "light",
        "colors": {},
        "font_scale": 1.0,
        "footer": True,
        "logo_media_id": None,
        "logo_position": DEFAULT_LOGO_POSITION,
        "logo_scale": 1.0,
        "aspect_ratio": DEFAULT_ASPECT,
    }

    if "template" in raw and raw["template"] is not None:
        t = str(raw["template"])
        if t not in THEME_TEMPLATES:
            raise SlideError(f"Unbekanntes Template '{t}' (erlaubt: {', '.join(THEME_TEMPLATES)}).")
        theme["template"] = t

    if "colors" in raw and raw["colors"] is not None:
        colors = raw["colors"]
        if not isinstance(colors, dict):
            raise SlideError("'colors' muss ein Objekt sein.")
        for key, value in colors.items():
            if key not in COLOR_KEYS:
                raise SlideError(f"Unbekannte Farbe '{key}' (erlaubt: {', '.join(COLOR_KEYS)}).")
            if not isinstance(value, str) or not _HEX.match(value):
                raise SlideError(f"Farbe '{key}' muss im Hex-Format #rrggbb sein.")
            theme["colors"][key] = value.lower()

    if "font_scale" in raw and raw["font_scale"] is not None:
        theme["font_scale"] = _clamp_font_scale(raw["font_scale"])

    if "footer" in raw and raw["footer"] is not None:
        theme["footer"] = bool(raw["footer"])

    if "logo_media_id" in raw:
        value = raw["logo_media_id"]
        if value is None:
            theme["logo_media_id"] = None
        elif isinstance(value, int) and value > 0:
            theme["logo_media_id"] = value
        else:
            raise SlideError("'logo_media_id' muss eine Zahl oder null sein.")

    if "logo_position" in raw and raw["logo_position"] is not None:
        pos = str(raw["logo_position"])
        if pos not in LOGO_POSITIONS:
            raise SlideError(
                f"Unbekannte Logo-Position '{pos}' (erlaubt: {', '.join(LOGO_POSITIONS)})."
            )
        theme["logo_position"] = pos

    if "logo_scale" in raw and raw["logo_scale"] is not None:
        theme["logo_scale"] = _clamp_logo_scale(raw["logo_scale"])

    if "aspect_ratio" in raw and raw["aspect_ratio"] is not None:
        a = str(raw["aspect_ratio"])
        if a not in ASPECT_RATIOS:
            raise SlideError(
                f"Unbekanntes Seitenverhältnis '{a}' (erlaubt: {', '.join(ASPECT_RATIOS)})."
            )
        theme["aspect_ratio"] = a

    return theme
