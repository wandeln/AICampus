"""
Skript-Kapitel: Vorlesungsskript aus mehreren Markdown-Dateien.

Regeln (analog zu den Übungsaufgaben):
- Reihenfolge per display_order (Drag-and-Drop → PATCH .../reorder).
- is_visible = für Studenten freigeschaltet (Default: aus).
- Anlegen/Bearbeiten/Reihenfolge/Sichtbarkeit/Löschen: PROF/TUTOR.
- LLM-Generierung: POST .../ai-generate (Titel/Inhalt).
- Nach jeder Content-Änderung: sync_media_usages() (Medien-Einbindung).
"""

import re
from datetime import datetime, timezone
from typing import Optional, TypedDict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session, delete, select

from database.base import get_session
from database.models import (
    ContentVersion,
    Course,
    CourseReference,
    CourseRole,
    FeedbackSource,
    GlobalUserRole,
    ScriptQuestion,
    ScriptSection,
    Submission,
    Task,
    User,
    UserCourse,
)
from services import bibtex as bib
from services import content_edits
from services import fig_labels
from services.auth_service import get_current_user, require_course_access
from services.content_edits import ContentEditError, mask_code_blocks
from services.llm_service import LLMService
from services import media_service
from services.media_service import sync_media_usages
from services.references_service import build_references_text
from services.settings_resolver import get_effective_llm_config

router = APIRouter(prefix="/api", tags=["Skript"])
llm_service = LLMService()

# Figuren-/Label-Scan: Single Source of Truth in services/fig_labels.py
# (Parität zum Client-Renderer s. dort) — underscore-Alias, damit die
# Imports in api/slides.py unverändert bleiben.
_scan_fig_labels = fig_labels.scan_fig_labels
_scan_figures = fig_labels.scan_figures

# Regexen identisch zu static/js/markdown-renderer.js:
# Labels werden nur außerhalb von Code-Blöcken gezählt (fenced + inline
# Code werden vorher entfernt). [\w-] ≈ JS [\p{L}0-9_-] (Unicode-Buchstaben,
# Ziffern, Unterstrich, Bindestrich).
_CODE_FENCED_RE = re.compile(r"```[\s\S]*?```")
_CODE_INLINE_RE = re.compile(r"`[^`]+`")
_EQ_LABEL_RE = re.compile(r"\$\$[\s\S]*?\$\$\s*\{#eq:([\w-]+)\}")


def _scan_labels(content: str) -> tuple[list[str], list[str]]:
    """{#fig:…}/{#eq:…}-Labels aus Markdown in Reihenfolge des Vorkommens (Code-Blöcke ignoriert)."""
    figs = _scan_fig_labels(content)
    eqs = _EQ_LABEL_RE.findall(fig_labels.strip_code(content))
    return figs, eqs
# Beschriftete Code-Blöcke: {#code:label} auf der ÖFFNENDEN Zeile eines
# Fenced-Blocks (``` …), optional mit Caption [text] auf derselben Zeile
# (Syntax {#code:label}[caption], s. parseFenceHead in markdown-renderer.js).
# Schließende Fences haben eine leere Kopfzeile → werden nicht erkannt.
# Caption-Zeichenkette [^\]]* ≈ JS-Token (bis zum ersten ]).
_CODE_FENCE_OPEN_RE = re.compile(r"^```([^\n]*)$", re.MULTILINE)
_CODE_LABEL_IN_HEAD_RE = re.compile(r"\{#code:([\w-]+)\}")
_CODE_CAPTION_IN_HEAD_RE = re.compile(r"\[([^\]]*)\]")
# Sektions-Label am Ende einer Heading-Zeile: ## Titel {#sec:label}
_SEC_LABEL_TAIL_RE = re.compile(r"\s*\{#sec:([\w-]+)\}\s*$")
# Kapitel-Label: {#sec:label} als EIGENE ZEILE = erste nicht-leere Zeile des Inhalts
_CHAPTER_LABEL_LINE_RE = re.compile(r"\{#sec:([\w-]+)\}")
_HEADING_NUM_LINE_RE = re.compile(r"^(#{2,6})\s+(.*\S)\s*$", re.MULTILINE)



def _scan_code_labels(content: str) -> list[tuple[str, str]]:
    """(Label, Caption) beschrifteter Code-Blöcke in Reihenfolge des Vorkommens.

    Im Gegensatz zu Figuren/Gleichungen wird hier genau in den Code-Blöcken
    gesucht: {#code:label} steht auf der öffnenden ```-Zeile (Tokens in
    beliebiger Reihenfolge, nahtlos oder mit Leerzeichen, s. parseFenceHead).
    """
    out: list[tuple[str, str]] = []
    for m in _CODE_FENCE_OPEN_RE.finditer(content or ""):
        head = m.group(1)
        lm = _CODE_LABEL_IN_HEAD_RE.search(head)
        if not lm:
            continue
        cm = _CODE_CAPTION_IN_HEAD_RE.search(head)
        caption = cm.group(1).strip() if cm else ""
        out.append((lm.group(1), caption))
    return out


# Beschriftete Boxen: {#box:label} und [Caption] je max. einmal auf der
# @startbox:{typ}-Zeile (oder legacy @box:{typ}), in beliebiger Reihenfolge,
# ansonsten nur Leerraum —
# Semantik identisch zum Box-Tokenizer in markdown-renderer.js. WICHTIG:
# JEDER der beiden Token-Typen ist einzeln zulässig (Label OHNE Caption =
# nummerierte Box ohne eigene Überschrift — der Normalfall!).
# Ungültige Schreibweise (weitere Zeilentexte) → Box bleibt literal dort,
# daher zählt sie hier auch nicht.
_BOX_LABEL_HEAD = (
    r"(?:"
    r"[ \t]*\{#box:(?P<box_lbl1>[\w-]+)\}"
    r"|[ \t]*\{#box:(?P<box_lbl2>[\w-]+)\}[ \t]*\[(?P<box_cap2>[^\]]*)\]"
    r"|[ \t]*\[(?P<box_cap3>[^\]]*)\][ \t]*\{#box:(?P<box_lbl3>[\w-]+)\}"
    r")"
)
_BOX_LABEL_RE = re.compile(
    r"^@(?:start)?box:(?P<box_type>[\w-]+)" + _BOX_LABEL_HEAD + r"[ \t]*(?:\r\n|\n|$)", re.MULTILINE
)


def _box_label_of(m: re.Match) -> str:
    """Label aus einem _BOX_LABEL_RE/_BOX_HEAD_RE-Match (eine der drei Gruppen)."""
    return m["box_lbl1"] or m["box_lbl2"] or m["box_lbl3"] or ""


def _scan_box_labels(content: str) -> list[tuple[str, str]]:
    """(Label, Typ) beschrifteter Boxen in Reihenfolge des Vorkommens.

    {#box:label} steht auf der @startbox:{typ}-Zeile (optional mit [Caption]).
    Code-Blöcke werden ignoriert (@startbox:… in Code bleibt literal).
    """
    text = _CODE_FENCED_RE.sub("", content or "")
    text = _CODE_INLINE_RE.sub("", text)
    return [(_box_label_of(m), m["box_type"]) for m in _BOX_LABEL_RE.finditer(text)]


# Beschriftete Tabellen: Pipe-Tabellen-Block (Zeilen beginnend mit |) +
# Label-Zeile {#tab:label} direkt darunter, optional mit Caption [text] und
# Zoom {zoom=X} (Syntax {#tab:label}[caption]{zoom=X}, s. markdown-renderer.js;
# Zoom = Tabellenschrift ×X, clientseitig via --tab-zoom). Leerzeilen zwischen
# Tabelle und Label sind erlaubt (wie \s* bei eq/fig). Caption-Zeichenkette
# [^\]]* ≈ JS-Token (bis zum ersten ]). Text nach der Caption/Zoom → kein Match
# (Tippfehler bleiben literal, wie bei den Box-Labels).
_TAB_CAPTION_RE = re.compile(
    r"(?:^[ \t]*\|[^\n]*\r?\n)+[ \t]*(?:\r?\n[ \t]*)*\{#tab:([\w-]+)\}(?:[ \t]*\[([^\]]*)\])?(?:[ \t]*\{\.?zoom=[\d.]+\})?[ \t]*(?:\r?\n|$)",
    re.MULTILINE,
)


def _scan_tables(content: str) -> list[tuple[str, str]]:
    """(Caption, Label) beschrifteter Tabellen in Reihenfolge des Vorkommens (Code-Blöcke ignoriert)."""
    text = _CODE_FENCED_RE.sub("", content or "")
    text = _CODE_INLINE_RE.sub("", text)
    # Gruppen-Reihenfolge im Regex ist (Label, Caption) → hier auf (Caption, Label) drehen
    return [(m.group(2) or "", m.group(1)) for m in _TAB_CAPTION_RE.finditer(text)]


# ─── Hover-Previews für Referenz-Tooltips ─────────────────────────────────
# Label → gekürzter Objektinhalt (eq: LaTeX-Quelltext, code: Code, box:
# Box-Inhalt als Plain-Text, fig: Caption). Das Frontend rendert den Wert
# im Hover-Tooltip aufgelöster @fig:/@eq:/@code:/@box:-Referenzen (eq per
# KaTeX, code in <pre>, Rest als Text).
_PREVIEW_LIMIT = 320
# $$…$$-Block inkl. Label (wie _EQ_LABEL_RE, zusätzlich mit LaTeX-Gruppe).
# Die LaTeX-Gruppe darF keine $$-Grenze überschreiten (Tempered Token):
# sonst zwingt ein UNbeschrifteter Display-Block vor dem beschrifteten die
# Gruppe zu einem Span über beide Blöcke, und die Preview zeigt die falsche
# Gleichung. Nur der Block, dem das Label direkt (nur Whitespace) folgt,
# liefert die Preview — dieselbe Semantik wie der JS-Renderer (dort ist die
# Label-Gruppe optional, daher spannt es nie).
_EQ_PREVIEW_RE = re.compile(r"\$\$((?:(?!\$\$)[\s\S])*?)\$\$\s*\{#eq:([\w-]+)\}")
# Fenced-Block inkl. Inhalt (Label auf der ÖFFNENDEN Zeile, s. _scan_code_labels).
_CODE_BLOCK_RE = re.compile(r"^```([^\n]*)\n?([\s\S]*?)^```[ \t]*$", re.MULTILINE)
# Box-Öffnung inkl. Head-Zeile (Label auf der @startbox:-Zeile, s. _BOX_LABEL_RE;
# Gruppen: box_type, box_lbl1/2/3, box_cap2/3). NICHT verankert (wie früher):
# eine mittelzeilige Öffnung (z. B. Bullet) zählt mit. Der Inhalt wird
# tiefenbasiert per _find_box_close ermittelt — Boxen sind NESTBAR
# (s. markdown-renderer.js); der alte Nicht-greedy-Regex brach beim innersten
# @endbox ab und verlor den Rest der äußeren Box.
_BOX_HEAD_RE = re.compile(r"@(?:start)?box:(?P<box_type>[\w-]+)" + _BOX_LABEL_HEAD + r"[ \t]*\r?\n")
# Start-/End-Events für die Tiefenzählung (Parität zu BOX_EVENT_RE in JS).
_BOX_EVENT_RE = re.compile(r"@startbox:[\w-]+|@endbox")


def _find_box_close(text: str, start: int) -> re.Match | None:
    """Zugehöriges @endbox (Zeilenanfang) zur bei start geöffneten Box —
    verschachtelte Boxen werden per Tiefenzählung gepaart; None = ungeschlossen."""
    depth = 1
    for m in _BOX_EVENT_RE.finditer(text, start):
        if m.group(0).startswith("@startbox"):
            depth += 1
            continue
        if m.start() != 0 and text[m.start() - 1] != "\n":
            continue  # nicht Zeilenanfang → literal (wie in JS)
        depth -= 1
        if depth == 0:
            return m
    return None
# Tabelle inkl. Block (Label-Zeile nach dem Block, s. _TAB_CAPTION_RE;
# {zoom=X} nicht erfasst — die Preview braucht nur Tabellen-Block + Label).
_TAB_CONTENT_RE = re.compile(
    r"((?:^[ \t]*\|[^\n]*\r?\n)+)[ \t]*(?:\r?\n[ \t]*)*\{#tab:([\w-]+)\}(?:[ \t]*\[([^\]]*)\])?(?:[ \t]*\{\.?zoom=[\d.]+\})?[ \t]*(?:\r?\n|$)",
    re.MULTILINE,
)
# Formel-Span ($$…$$-Block oder $…$ inline) — für math-bewusste Preview-Bearbeitung.
_MATH_SPAN_RE = re.compile(r"\$\$[\s\S]*?\$\$|\$[^$\n]+?\$")


def _truncate_preview(text: str, limit: int = _PREVIEW_LIMIT) -> str:
    """Kürzt auf limit Zeichen — aber nie mitten in einer Formel (sonst
    bliebe ein offenes $ stehen, das das Frontend nicht rendern kann)."""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    pos = limit
    for m in _MATH_SPAN_RE.finditer(t):
        if m.start() >= pos:
            break
        if m.end() > pos:  # Schnitt liegt im Formel-Span → vor ihn schieben
            pos = m.start()
    return t[:pos].rstrip() + " …"


def _preview_plain(text: str, limit: int = _PREVIEW_LIMIT) -> str:
    """Markdown → grober Plain-Text (für Box-Previews im Tooltip).

    Formeln ($…$ / $$…$$) bleiben ERKÄNNBAR erhalten (das Frontend rendert
    sie im Tooltip per KaTeX); Emphasis-Marker werden nur AUSSERHALB von
    Formeln entfernt, damit z. B. $x_i^2$ nicht zu $xi^2$ wird.
    """
    t = _CODE_FENCED_RE.sub(" ", text or "")
    t = _CODE_INLINE_RE.sub(lambda m: m.group(0).strip("`"), t)
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r" [\1] ", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s+", "", t, flags=re.MULTILINE)
    parts: list[str] = []
    last = 0
    for m in _MATH_SPAN_RE.finditer(t):
        parts.append(re.sub(r"[*_~]+", "", t[last : m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(re.sub(r"[*_~]+", "", t[last:]))
    t = "".join(parts)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return _truncate_preview(t, limit)


def _scan_previews(content: str) -> dict[str, str]:
    """Kind-prefixed Label → Preview-Text (gekürzt) der beschrifteten Objekte.

    Ergänzt die _scan_*-Funktionen um den INHALT (für Hover-Tooltips).
    Pro „kind:label“ gewinnt das erste Vorkommen. fig/eq/box/tab werden wie
    die jeweiligen _scan_* außerhalb von Code-Blöcken gesucht, code IN den
    Code-Blöcken (Label auf der öffnenden ```-Zeile). tab = Tabellen-Zeilen
    als Plain-Text (Formeln bleiben erkennbar, s. _preview_plain).
    """
    out: dict[str, str] = {}
    text = _CODE_FENCED_RE.sub("", content or "")
    text = _CODE_INLINE_RE.sub("", text)
    for cap, label, inners in _scan_figures(text):
        if label is not None:
            out.setdefault(f"fig:{label}", _truncate_preview(cap))
        for sub_label, inner_cap in inners or []:
            if sub_label is not None:
                out.setdefault(f"fig:{sub_label}", _truncate_preview(inner_cap))
    for m in _EQ_PREVIEW_RE.finditer(text):
        out.setdefault(f"eq:{m.group(2)}", _truncate_preview(m.group(1)))
    for m in _BOX_HEAD_RE.finditer(text):
        close = _find_box_close(text, m.end())
        if close is None:
            continue  # ungeschlossene Box bleibt literal
        body = text[m.end() : close.start()]
        if body.endswith("\r\n"):
            body = body[:-2]
        elif body.endswith("\n"):
            body = body[:-1]
        out.setdefault(f"box:{_box_label_of(m)}", _preview_plain(body))
    for m in _TAB_CONTENT_RE.finditer(text):
        out.setdefault(f"tab:{m.group(2)}", _preview_plain(m.group(1)))
    for m in _CODE_BLOCK_RE.finditer(content or ""):
        lm = _CODE_LABEL_IN_HEAD_RE.search(m.group(1))
        if lm:
            out.setdefault(f"code:{lm.group(1)}", _truncate_preview(m.group(2)))
    return out


def _clean_heading_title(title: str) -> str:
    """Inline-Markdown (Code, Links, Betonung) aus einem Heading-Titel entfernen —
    für die Anzeige im Inhaltsverzeichnis. LaTeX ($…$) und Zitationen
    (@cite:/@citet:/@citep:) bleiben erhalten, damit der Renderer
    (renderCaptionRef) sie als KaTeX bzw. Zitations-Link rendern kann."""
    keep_parts: list[str] = []

    def _stash(m: re.Match) -> str:
        keep_parts.append(m.group(0))
        return f"\x00{len(keep_parts) - 1}\x00"

    t = re.sub(r"\$[^$\n]*\$|@(?:citep|citet|cite):[\w-]+", _stash, title)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[*_~]+", "", t)
    t = " ".join(t.split())
    return re.sub(r"\x00(\d+)\x00", lambda m: keep_parts[int(m.group(1))], t)


class _ScannedHeading(TypedDict):
    """Eine erkannte Markdown-Section (h2–h6) mit optionalem {#sec:label}."""

    level: int
    title: str
    label: Optional[str]


def _scan_headings(content: str) -> list[_ScannedHeading]:
    """Markdown-Sections h2–h6 (Code-Blöcke ignoriert): level, title, optionales {#sec:label}."""
    masked = mask_code_blocks(content or "")
    out: list[_ScannedHeading] = []
    for m in _HEADING_NUM_LINE_RE.finditer(masked):
        title = m.group(2).strip()
        label: Optional[str] = None
        lm = _SEC_LABEL_TAIL_RE.search(title)
        if lm:
            label = lm.group(1)
            title = title[: lm.start()].rstrip()
        out.append({"level": len(m.group(1)), "title": title, "label": label})
    return out


def _local_section_numbers(headings: list[_ScannedHeading]) -> list[str]:
    """Kapitellokale Nummerierung: h2 → N, h3 → N.M, h4 → N.M.K, h5 → N.M.K.L, h6 → N.M.K.L.M."""
    n2 = n3 = n4 = n5 = n6 = 0
    nums = []
    for h in headings:
        lvl = h["level"]
        if lvl == 2:
            n2 += 1
            n3 = n4 = n5 = n6 = 0
            nums.append(str(n2))
        elif lvl == 3:
            n3 += 1
            n4 = n5 = n6 = 0
            nums.append(f"{n2}.{n3}")
        elif lvl == 4:
            n4 += 1
            n5 = n6 = 0
            nums.append(f"{n2}.{n3}.{n4}")
        elif lvl == 5:
            n5 += 1
            n6 = 0
            nums.append(f"{n2}.{n3}.{n4}.{n5}")
        else:
            n6 += 1
            nums.append(f"{n2}.{n3}.{n4}.{n5}.{n6}")
    return nums


def _chapter_label(content: str) -> str:
    """Kapitel-Label = {#sec:label} als eigene Zeile = erste nicht-leere Zeile
    des Inhalts (sonst "").

    Die Label-Zeile selbst wird nicht gerendert (Renderer entfernt sie).
    @sec:label verweist aus anderen Kapiteln auf das Kapitel („Kap. N“).
    """
    for line in (content or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _CHAPTER_LABEL_LINE_RE.fullmatch(line)
        return m.group(1) if m else ""
    return ""


# ─── LLM-Edits: stellenweise Änderungen am bestehenden Inhalt ────────────
# Das LLM darf für lokale Änderungen statt des Volltexts eine Liste von
# Edit-Objekten liefern („content_edits“). Diese werden serverseitig
# (services.content_edits) auf den bestehenden Inhalt angewendet — der Anker
# (Heading bzw. kurzes Snippet) muss dabei eindeutig sein. Nicht anwendbare
# Edits werden übersprungen und als Warnung gemeldet, die übrigen werden
# trotzdem angewendet; erst wenn gar keines anwendbar ist, bleibt der Inhalt
# unverändert (HTTP 400).

def _apply_content_edits(content: str, edits: object) -> tuple[str, int, list[str]]:
    """HTTP-Wrapper um content_edits.apply_content_edits (ContentEditError →
    HTTP 400; der Inhalt bleibt unverändert). Gibt
    (neuer_content, applied_count, warnings)."""
    try:
        return content_edits.apply_content_edits(content, edits)
    except ContentEditError as e:
        raise HTTPException(400, str(e))




def _section_to_dict(s: ScriptSection, include_content: bool = False, include_summary: bool = False) -> dict:
    d = {
        "id": s.id,
        "title": s.title,
        "is_visible": s.is_visible,
        "display_order": s.display_order,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }
    if include_content:
        d["content"] = s.content
    if include_summary:  # nur für Tutor/PROF/Admin — Studenten sehen sie nicht
        d["summary"] = s.summary or ""
    return d


def _get_membership(session: Session, user: User, course_id: int) -> Optional[UserCourse]:
    if user.role == GlobalUserRole.ADMIN:
        return None  # Admin braucht keine Membership
    return session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()


def _check_member(user: User, session: Session, course_id: int) -> None:
    """Jedes Kurs-Mitglied (oder Admin) darf lesen."""
    if user.role == GlobalUserRole.ADMIN:
        return
    if not _get_membership(session, user, course_id):
        raise HTTPException(403, "Du bist kein Mitglied dieses Kurses.")



def _next_order(session: Session, course_id: int) -> int:
    rows = session.exec(
        select(ScriptSection)
        .where(ScriptSection.course_id == course_id)
        .order_by(ScriptSection.display_order.desc())  # type: ignore[attr-defined]
    ).all()
    return rows[0].display_order + 1 if rows else 0


def _check_course_tutor(user: User, course_id: int, session: Session) -> None:
    """PROF/TUTOR-Check für Routen ohne course_id im Path. Admin darf immer."""
    if user.role == GlobalUserRole.ADMIN:
        return
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()
    if not membership or membership.role_in_course not in (CourseRole.PROF, CourseRole.TUTOR):
        raise HTTPException(403, "Keine Berechtigung, dieses Kapitel zu bearbeiten.")


# ─── Lesen ────────────────────────────────────────────────────────

@router.get("/courses/{course_id}/script-sections")
async def list_sections(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kapitel des Skripts (Studenten nur freigeschaltene, optional mit Content)."""
    _check_member(user, session, course_id)
    is_tutor = user.role == GlobalUserRole.ADMIN or (
        (m := _get_membership(session, user, course_id))
        is not None
        and m.role_in_course in (CourseRole.PROF, CourseRole.TUTOR)
    )

    q = select(ScriptSection).where(ScriptSection.course_id == course_id)
    if not is_tutor:
        q = q.where(ScriptSection.is_visible == True)  # noqa: E712
    sections = session.exec(q.order_by(ScriptSection.display_order.asc())).all()  # type: ignore[attr-defined]

    include_content = request.query_params.get("include_content") in ("1", "true")
    return [
        _section_to_dict(s, include_content=include_content, include_summary=is_tutor)
        for s in sections
    ]


@router.get("/script-sections/{section_id}")
async def get_section(
    section_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Einzelnes Kapitel inkl. Markdown-Content."""
    section = session.get(ScriptSection, section_id)
    if not section:
        raise HTTPException(404, "Kapitel nicht gefunden.")
    _check_member(user, session, section.course_id)

    is_tutor = user.role == GlobalUserRole.ADMIN or (
        (m := _get_membership(session, user, section.course_id))
        is not None
        and m.role_in_course in (CourseRole.PROF, CourseRole.TUTOR)
    )
    if not is_tutor and not section.is_visible:
        raise HTTPException(404, "Kapitel nicht gefunden oder noch nicht freigeschaltet.")

    return _section_to_dict(section, include_content=True, include_summary=is_tutor)


@router.get("/courses/{course_id}/script-refmap")
async def script_refmap(
    course_id: int,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Live-berechnete Nummerierungs-Map für Querverweise (ohne DB-Speicherung).

    Die Nummerierung ist ein abgeleiteter Wert (wie LaTeX zur Kompilierzeit):
    Jedes beschriftete Objekt erhält eine durchlaufende Nummer über das
    gesamte Skript. Studenten sehen nur freigeschaltete Kapitel → saubere
    „veröffentlichte“ Nummerierung.

    Payload:
        mode:     "reading" (Student) bzw. "edit" (PROF/TUTOR/Admin) — beeinflusst nur das
                  Layout der @task:-Boxen (Querverweise verlinken alle auf die Skript-Seite)
        courseId: Kurs-ID
        chapters: {id: {title, label, num, visible, maxFig, maxEq, maxCode, maxBox, maxTab,
                        sections: [{num, title, label, anchor}]}}
                  # num = Kapitelnummer (rollenabhängig), sections = h2–h6-Liste fürs TOC
                  # maxFig/maxEq/maxCode/maxBox/maxTab = Preview-Fallback (neue, ungespeicherte Labels)
        labels:   {"<kind>:<label>": {kind, sectionId, num, chapter?, type?, sub?, preview?}}  # globale Nummer, bei Duplikaten: erstes Vorkommen gewinnt (pro Kind)
                  # keys sind kind-prefixed ("eq:test" ≠ "box:test"), kinds: fig / eq / sec / code / box / tab (sec: num = "N.M…"-String, box: type = Box-Typ,
                  # z. B. "satz" → Referenz-Text „Satz N"; fig: sub = Letter a/b/… bei gelabelten
                  # Subfigure-Innern, num = Nummer des Parent-Komplexes → Referenz „Abb. N a)")
                  # als erste nicht-leere Zeile im Inhalt → sec-Entry mit chapter: true und num = Kapitelnummer
                  # preview: gekürzter Objektinhalt für Hover-Tooltips (eq: LaTeX, code: Code,
                  # box: Text, fig: Caption, sec: Titel)
        tocVisible: Inhaltsverzeichnis für Studenten sichtbar (Tutor/Prof sehen es immer)
        chapterOrder: Kapitel-IDs in Display-Reihenfolge (JSON-Keys werden bei
                  numerischen IDs vom Parser numerisch sortiert → Reihenfolge nur hier)
        figures:  [{num, caption, label, sectionId}]  # Abbildungsverzeichnis fürs TOC
        codes:    [{num, caption, label, sectionId}]  # Codes-Tabelle fürs TOC
                  # ({#code:label} in der öffnenden Zeile eines Fenced-Blocks)
        tables:   [{num, caption, label, sectionId}]  # Tabellenverzeichnis fürs TOC
                  # ({#tab:label} in der Zeile direkt unter der Pipe-Tabelle)
        tasks:    {id: {id, title, taskType, maxPoints, myPoints, attemptsUsed, maxAttempts, deadline}}
                  # für @task:{id}-Referenzen. Student: nur freigeschaltete Aufgaben +
                  # eigene Punkte (analog Aufgabenübersicht); PROF/TUTOR/Admin: alle.
        references: {key: {key, num, authors, year, title, venue, detail, url, entry}}
                  # für @cite:{key} / @citet:{key} / @citep:{key}. num = kursweite
                  # stabile Zitations-Nummer (display_order-Reihenfolge) — alle Rollen.
    """
    _check_member(user, session, course_id)
    # Live-berechneter abgeleiteter Wert → niemals cachen (Browser/Proxy)
    response.headers["Cache-Control"] = "no-store"
    course = session.get(Course, course_id)
    is_tutor = user.role == GlobalUserRole.ADMIN or (
        (m := _get_membership(session, user, course_id))
        is not None
        and m.role_in_course in (CourseRole.PROF, CourseRole.TUTOR)
    )

    q = select(ScriptSection).where(ScriptSection.course_id == course_id)
    if not is_tutor:
        q = q.where(ScriptSection.is_visible == True)  # noqa: E712
    sections = session.exec(q.order_by(ScriptSection.display_order.asc())).all()  # type: ignore[attr-defined]

    chapters: dict[str, dict[str, object]] = {}
    labels: dict[str, dict[str, object]] = {}
    figures: list[dict[str, object]] = []
    codes: list[dict[str, object]] = []
    tables: list[dict[str, object]] = []
    fig_running = 0
    eq_running = 0
    code_running = 0
    box_running = 0
    tab_running = 0
    for ch_num, s in enumerate(sections, start=1):
        fig_pairs = _scan_figures(s.content)  # (Caption, Label, Inners) in Vorkommensreihenfolge
        eqs = _scan_labels(s.content)[1]
        code_items = _scan_code_labels(s.content)  # (Label, Caption) in Vorkommensreihenfolge
        box_items = _scan_box_labels(s.content)  # (Label, Typ) in Vorkommensreihenfolge
        tab_items = _scan_tables(s.content)  # (Caption, Label) in Vorkommensreihenfolge
        previews = _scan_previews(s.content)  # "kind:label" → Hover-Preview (Tooltip)
        max_fig = 0
        max_eq = 0
        max_code = 0
        max_box = 0
        max_tab = 0
        for caption, label, inners in fig_pairs:
            fig_running += 1
            if label and f"fig:{label}" not in labels:
                e: dict[str, object] = {"kind": "fig", "sectionId": s.id, "num": fig_running}
                if p := previews.get(f"fig:{label}"):
                    e["preview"] = p
                labels[f"fig:{label}"] = e
            max_fig = max(max_fig, fig_running)
            figures.append({"num": fig_running, "caption": caption, "label": label, "sectionId": s.id})
            # Gelabelte Subfigure-Inners: eigene Refmap-Entries (ebenfalls
            # kind "fig" — @fig:label), num = Nummer des Parent-Komplexes
            # (beim ersten Claim), sub = Letter (a, b, …) → „Abb. N a)".
            if inners:
                for i, (sub_label, _inner_cap) in enumerate(inners):
                    if not sub_label or f"fig:{sub_label}" in labels:
                        continue
                    e2: dict[str, object] = {
                        "kind": "fig", "sectionId": s.id,
                        "num": fig_running, "sub": chr(97 + i),
                    }
                    if p := previews.get(f"fig:{sub_label}"):
                        e2["preview"] = p
                    labels[f"fig:{sub_label}"] = e2
        for label in eqs:
            eq_running += 1
            if f"eq:{label}" not in labels:
                e: dict[str, object] = {"kind": "eq", "sectionId": s.id, "num": eq_running}
                if p := previews.get(f"eq:{label}"):
                    e["preview"] = p
                labels[f"eq:{label}"] = e
            max_eq = max(max_eq, eq_running)
        for label, caption in code_items:
            code_running += 1
            if f"code:{label}" not in labels:
                e: dict[str, object] = {"kind": "code", "sectionId": s.id, "num": code_running}
                if p := previews.get(f"code:{label}"):
                    e["preview"] = p
                labels[f"code:{label}"] = e
            max_code = max(max_code, code_running)
            codes.append({"num": code_running, "caption": caption, "label": label, "sectionId": s.id})
        for label, box_type in box_items:
            box_running += 1
            if f"box:{label}" not in labels:
                e: dict[str, object] = {"kind": "box", "type": box_type, "sectionId": s.id, "num": box_running}
                if p := previews.get(f"box:{label}"):
                    e["preview"] = p
                labels[f"box:{label}"] = e
            max_box = max(max_box, box_running)
        for caption, label in tab_items:
            tab_running += 1
            if f"tab:{label}" not in labels:
                e: dict[str, object] = {"kind": "tab", "sectionId": s.id, "num": tab_running}
                if p := previews.get(f"tab:{label}"):
                    e["preview"] = p
                labels[f"tab:{label}"] = e
            max_tab = max(max_tab, tab_running)
            tables.append({"num": tab_running, "caption": caption, "label": label, "sectionId": s.id})
        # Sections (h2–h6): kapitellokale Nummerierung, Labels, Anker fürs TOC
        headings = _scan_headings(s.content)
        ch_label = _chapter_label(s.content)
        # Kapitel-Label ({#sec:label} als eigene Zeile) wird VOR den Section-Labels
        # registriert, damit @sec:label auf das Kapitel („Kap. N“) zeigt, falls eine
        # Section denselben Namen trägt.
        if ch_label and f"sec:{ch_label}" not in labels:
            labels[f"sec:{ch_label}"] = {
                "kind": "sec", "sectionId": s.id, "num": ch_num,
                "chapter": True, "preview": s.title,
            }
        sec_entries = []
        for h, local in zip(headings, _local_section_numbers(headings)):
            full_num = f"{ch_num}.{local}"
            sec_entries.append({
                "num": full_num,
                "level": h["level"],
                "title": _clean_heading_title(h["title"]),
                "label": h["label"],
                "anchor": f"sec:{h['label']}" if h["label"] else f"sec:{s.id}-{full_num}",
            })
            if h["label"] and f"sec:{h['label']}" not in labels:
                labels[f"sec:{h['label']}"] = {
                    "kind": "sec", "sectionId": s.id, "num": full_num,
                    "preview": _clean_heading_title(h["title"]),
                }
        chapters[str(s.id)] = {
            "title": s.title,
            "label": ch_label,
            "num": ch_num,
            "visible": s.is_visible,
            "maxFig": max_fig,
            "maxEq": max_eq,
            "maxCode": max_code,
            "maxBox": max_box,
            "maxTab": max_tab,
            "sections": sec_entries,
        }

    # Aufgaben für @task:{id}-Referenzen (Datenquelle der Aufgaben-Box).
    tasks_map: dict[str, dict] = {}
    tq = select(Task).where(Task.course_id == course_id)
    if not is_tutor:
        tq = tq.where(Task.is_visible == True)  # noqa: E712
    for task in session.exec(tq.order_by(Task.display_order.asc())).all():  # type: ignore[attr-defined]
        my_points = 0.0
        attempts_used = 0
        if not is_tutor and task.id is not None:
            # Eigene Punkte: wie in der Aufgabenübersicht (my-points-Endpoint) —
            # letzte Abgabe, menschliche Bewertung schlägt LLM-Bewertung.
            subs = session.exec(
                select(Submission)
                .where(Submission.task_id == task.id)
                .where(Submission.student_id == user.id)
                .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
            ).all()
            attempts_used = len(subs)
            if subs:
                human_points = 0.0
                llm_points = 0.0
                override_exists = False
                for fb in subs[0].feedback_list:
                    if fb.source == FeedbackSource.HUMAN:
                        human_points = max(human_points, fb.points_earned)
                        override_exists = True
                    else:
                        llm_points = max(llm_points, fb.points_earned)
                my_points = human_points if override_exists else llm_points
        tasks_map[str(task.id)] = {
            "id": task.id,
            "title": task.title,
            "taskType": task.task_type.value,
            "maxPoints": task.max_points,
            "myPoints": round(my_points, 1),
            "attemptsUsed": attempts_used,
            "maxAttempts": task.max_attempts,
            "deadline": task.deadline,
        }

    # Quellen für @cite:/@citet:/@citep: (alle Rollen; Nummer = display_order)
    references: dict[str, dict] = {}
    for num, ref in enumerate(
        session.exec(
            select(CourseReference)
            .where(CourseReference.course_id == course_id)
            .order_by(CourseReference.display_order.asc())  # type: ignore[attr-defined]
            .order_by(CourseReference.id.asc())  # type: ignore[attr-defined]
        ).all(),
        start=1,
    ):
        refs = ref.authors or []
        references[ref.key] = {
            "key": ref.key,
            "num": num,
            "authors": refs,
            "year": ref.year or "",
            "title": ref.title or "",
            "venue": ref.venue or "",
            "detail": ref.detail or "",
            "doi": ref.doi or "",
            "url": ref.url or "",
            "entry": bib.format_entry(
                refs, ref.title or "", ref.year or "",
                ref.venue or "", ref.detail or "", ref.doi or "", ref.url or "",
            ),
        }

    return {
        "mode": "edit" if is_tutor else "reading",
        "courseId": course_id,
        "tocVisible": course.toc_visible,
        "chapterOrder": [str(s.id) for s in sections],
        "chapters": chapters,
        "labels": labels,
        "figures": figures,
        "codes": codes,
        "tables": tables,
        "tasks": tasks_map,
        "references": references,
    }


# ─── Schreiben (PROF/TUTOR) ───────────────────────────────────────

@router.post("/courses/{course_id}/script-sections")
async def create_section(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Neues Skript-Kapitel anlegen (am Ende der Reihenfolge)."""
    user, _ = user_and_course
    body = await request.json()

    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Titel darf nicht leer sein.")

    section = ScriptSection(
        course_id=course_id,
        title=title,
        content=body.get("content") or "",
        is_visible=bool(body.get("is_visible", False)),
        display_order=_next_order(session, course_id),
        summary=body.get("summary") or "",
        created_by=user.id,  # type: ignore[arg-type]
    )
    session.add(section)
    session.commit()
    session.refresh(section)
    sync_media_usages(session, course_id)

    return {
        "message": f"Kapitel '{section.title}' erstellt.",
        "section": _section_to_dict(section),
    }


@router.put("/script-sections/{section_id}")
async def update_section(
    section_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kapitel aktualisieren (Titel, Content, Sichtbarkeit)."""
    section = session.get(ScriptSection, section_id)
    if not section:
        raise HTTPException(404, "Kapitel nicht gefunden.")
    _check_course_tutor(user, section.course_id, session)
    body = await request.json()

    if "title" in body:
        title = (body.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "Titel darf nicht leer sein.")
        section.title = title
    if "content" in body:
        section.content = body.get("content") or ""
    if "is_visible" in body:
        section.is_visible = bool(body.get("is_visible"))
    if "summary" in body:
        section.summary = body.get("summary") or ""

    section.updated_at = datetime.now(timezone.utc)
    session.add(section)
    session.commit()
    session.refresh(section)
    sync_media_usages(session, section.course_id)

    return {"message": "Kapitel aktualisiert.", "section": _section_to_dict(section)}


@router.patch("/script-sections/{section_id}/visibility")
async def toggle_section_visibility(
    section_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kapitel für Studenten freischalten/verstecken."""
    section = session.get(ScriptSection, section_id)
    if not section:
        raise HTTPException(404, "Kapitel nicht gefunden.")
    _check_course_tutor(user, section.course_id, session)
    body = await request.json()

    section.is_visible = bool(body.get("is_visible", not section.is_visible))
    section.updated_at = datetime.now(timezone.utc)
    session.add(section)
    session.commit()
    session.refresh(section)

    return {"message": "Sichtbarkeit aktualisiert.", "is_visible": section.is_visible}


@router.patch("/courses/{course_id}/script-toc-visibility")
async def toggle_toc_visibility(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Inhaltsverzeichnis für Studenten ein-/ausblenden."""
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")
    _check_course_tutor(user, course_id, session)
    body = await request.json()

    course.toc_visible = bool(body.get("is_visible", not course.toc_visible))
    session.add(course)
    session.commit()
    session.refresh(course)

    return {"message": "Inhaltsverzeichnis aktualisiert.", "is_visible": course.toc_visible}


@router.delete("/script-sections/{section_id}")
async def delete_section(
    section_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kapitel löschen (nur PROF/TUTOR)."""
    section = session.get(ScriptSection, section_id)
    if not section:
        raise HTTPException(404, "Kapitel nicht gefunden.")
    _check_course_tutor(user, section.course_id, session)

    section_course_id = section.course_id
    section_title = section.title

    # Fragen auf dieses Kapitel bleiben als „Allgemein“ (sonst FK-Verstoß)
    for q in session.exec(
        select(ScriptQuestion).where(ScriptQuestion.section_id == section_id)
    ).all():
        q.section_id = None
        session.add(q)

    # Versions-History des Kapitels mit löschen
    session.exec(
        delete(ContentVersion)
        .where(ContentVersion.entity_type == "script_section")
        .where(ContentVersion.entity_id == section_id)
    )

    session.delete(section)
    session.commit()
    sync_media_usages(session, section_course_id)

    return {"message": f"Kapitel '{section_title}' gelöscht."}


@router.patch("/courses/{course_id}/script-sections/reorder")
async def reorder_sections(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Reihenfolge der Kapitel ändern (nur PROF/TUTOR)."""
    body = await request.json()
    section_ids = body.get("section_ids")
    if not isinstance(section_ids, list) or not section_ids:
        raise HTTPException(400, "section_ids muss eine nicht-leere Liste sein.")

    sections = session.exec(
        select(ScriptSection).where(ScriptSection.course_id == course_id)
    ).all()
    by_id = {s.id: s for s in sections}

    for idx, sid in enumerate(section_ids):
        s = by_id.get(sid)
        if not s:
            raise HTTPException(404, f"Kapitel {sid} nicht gefunden.")
        s.display_order = idx
        session.add(s)
    session.commit()

    return {"message": "Reihenfolge aktualisiert."}


@router.post("/courses/{course_id}/script-sections/ai-generate")
async def ai_generate_section(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """
    LLM generiert/ändert die angeforderten Felder eines Skript-Kapitels.

    Request:
        {
            "topic": "Rekursion / Füge ein Beispiel mit Baumdiagramm hinzu",
            "section_id": 3,            // optional: aktuell bearbeitetes Kapitel
            "current_title": "...",
            "current_content": "...",
            "generate_fields": {"title": true, "content": true, "summary": true}
        }

    Response:
        {
            // Leeres Feld = LLM hat das Feld nicht geändert (bestehender Wert bleibt).
            "title": "...", "content": "...", "summary": "...",
            "edits_applied": 0,   // Anzahl tatsächlich angewendeter „content_edits“
            "warnings": [],       // z.B. fehlgeschlagene Edits, entfernte/doppelte fig/eq-Labels
            "latency_ms": 123
        }
    """
    body = await request.json()
    gen = body.get("generate_fields", {}) or {}
    generate_fields = [f for f in ("title", "content", "summary") if gen.get(f)]
    if not generate_fields:
        raise HTTPException(400, "Keine Felder angefordert.")

    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    # Andere Kapitel (Titel + interne Zusammenfassung + vorhandene Labels) —
    # für Notations- und Label-Konsistenz. Das aktuell bearbeitete
    # Kapitel wird ausgeschlossen.
    section_id = body.get("section_id")
    other_chapters = []
    for s in session.exec(
        select(ScriptSection)
        .where(ScriptSection.course_id == course_id)
        .order_by(ScriptSection.display_order.asc())  # type: ignore[attr-defined]
    ).all():
        if s.id == section_id:
            continue
        figs, eqs = _scan_labels(s.content)
        code_labels = [l for l, _cap in _scan_code_labels(s.content)]
        box_labels = [f"@box:{l} ({t})" for l, t in _scan_box_labels(s.content)]
        tab_labels = [l for _cap, l in _scan_tables(s.content)]
        # Section-Labels fürs @sec:-Referenzieren; das Kapitel-Label
        # ({#sec:label} als eigene Zeile) wird gekennzeichnet, damit das LLM
        # es als Kapitel-Referenz erkennt und nicht doppelt belegt.
        sec_labels = [h["label"] for h in _scan_headings(s.content) if h["label"]]
        ch_label = _chapter_label(s.content)
        if ch_label and ch_label in sec_labels:
            sec_labels.remove(ch_label)
            sec_labels.insert(0, ch_label + " (Kapitel-Label)")
        other_chapters.append(
            {
                "title": s.title,
                "summary": (s.summary or "").strip(),
                "labels":
                    [f"@fig:{l}" for l in figs]
                    + [f"@eq:{l}" for l in eqs]
                    + [f"@code:{l}" for l in code_labels]
                    + box_labels
                    + [f"@tab:{l}" for l in tab_labels]
                    + [f"@sec:{l}" for l in sec_labels],
            }
        )
    other_chapters = other_chapters[:20]

    # Noch nicht im Skript verwendete (sichtbare) Medien — ggf. einbindbar
    # (vollständig, ohne Limit — analog zum Folien-Flow).
    unused_media = media_service.unused_media_for_script(session, course_id)

    # Übungsaufgaben des Kurses (ID + Titel) — das LLM kann passende Aufgaben
    # per @task:{id} im Kapitel einbinden (Aufgaben-Box für Studenten).
    course_tasks = [
        {"id": t.id, "title": t.title}
        for t in session.exec(
            select(Task)
            .where(Task.course_id == course_id)
            .order_by(Task.display_order.asc())  # type: ignore[attr-defined]
        ).all()
    ]

    current_title = (body.get("current_title") or "").strip()
    current_content = (body.get("current_content") or "").strip()
    llm_cfg = get_effective_llm_config(session, course_id)
    result = await llm_service.generate_script_section(
        course_name=course.name,
        topic=(body.get("topic") or "").strip(),
        generate_fields=generate_fields,
        current_title=current_title,
        current_content=current_content,
        other_chapters=other_chapters,
        unused_media=unused_media,
        course_tasks=course_tasks,
        references=build_references_text(session, course_id),
        config=llm_cfg,
    )
    if not result.get("success"):
        raise HTTPException(502, f"LLM-Fehler: {result.get('error', 'Unbekannter Fehler')}")

    data = result.get("data") or {}
    response = {"title": "", "content": "", "summary": "", "edits_applied": 0, "warnings": []}
    if "title" in generate_fields:
        response["title"] = (data.get("title") or "").strip()
    if "content" in generate_fields:
        new_content = (data.get("content") or "").strip()
        content_edits = data.get("content_edits")
        if new_content:
            response["content"] = new_content
        elif content_edits:
            # Stellenweise Bearbeitung: Edits serverseitig auf den bestehenden
            # Inhalt anwenden (Anker müssen eindeutig sein, sonst HTTP 400).
            if not current_content:
                raise HTTPException(400, "LLM lieferte stellenweise Edits („content_edits“), aber es existiert kein Inhalt zum Editieren. Bitte erneut versuchen.")
            merged, applied, warnings = _apply_content_edits(current_content, content_edits)
            response["content"] = merged
            response["edits_applied"] = applied
            response["warnings"] = warnings
        # sonst: LLM hat weder „content“ noch „content_edits“ geliefert → ""
    if "summary" in generate_fields:
        response["summary"] = (data.get("summary") or "").strip()
    response["latency_ms"] = result.get("latency_ms", 0)
    return response
