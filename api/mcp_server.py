"""
AICampus MCP-Server (Streamable HTTP, stateless) — Read-Tools.

Wird unter /mcp gemountet (main.py). Auth: Bearer-MCP-Token pro Request
(s. services/mcp_token_service.py); bei jedem Tool-Call werden Token,
Kurs-Allowlist, Kurs-Mitgliedschaft und Rolle live geprüft.

Design: flach, ohne „entered course"-State — alle Kurs-Tools nehmen
`course_id` als Parameter (s. docs/LLM-plans/mcp-integration.md).

Datenschutz (bewusste Tool-Whitelist, keine generischen Read-Tools):
- Rolle STUDENT: nur `is_visible=True`-Content; Aufgaben ohne
  Musterlösung und ohne MC-Richtigkeitsflags (mc_service.student_view)
- Nie erreichbar: Einreichungen, Feedback, Punkte, Mitglieder, Forum,
  Hint-Dialoge, interne LLM-Summaries, Settings
"""

import json
import logging
from typing import Optional, Union

from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlmodel import Session, func, select

from database.base import engine
from database.models import (
    Course,
    CourseMaterial,
    CourseReference,
    CourseRole,
    GlobalUserRole,
    MaterialType,
    McpTokenCourse,
    ScriptSection,
    Task,
    User,
    UserCourse,
)
from services import mcp_token_service
from services.auth_service import set_current_user_id
from services.mc_service import McValidationError, parse_mc_data, student_view, tutor_view

logger = logging.getLogger(__name__)

ROLE_LABELS = {
    CourseRole.PROF: "Professor",
    CourseRole.TUTOR: "Tutor",
    CourseRole.STUDENT: "Student",
}


class McpToolError(ToolError):
    """Tool-Fehler mit Meldung für den LLM-Client (statt generischem 500).

    Subclass von ToolError, damit die Meldung ans Modell durchgeht
    (andere Exceptions erreichen den Client nur als "Error executing tool …").
    """


# ─── Auth / Hilfsfunktionen ───────────────────────────────────────

def _bearer_token(ctx: Context) -> Optional[str]:
    headers = ctx.headers or {}
    value = headers.get("authorization") or headers.get("Authorization")
    return mcp_token_service.parse_bearer(value)


def _auth(
    ctx: Context,
    session: Session,
    course_id: Optional[int],
    write: bool = False,
    tool_name: Optional[str] = None,
):
    """Token + (falls course_id) Allowlist + Mitgliedschaft + Rolle prüfen.

    Liefert (user, token, role). Bei course_id=None (list_courses) ist die
    Rolle egal (STUDENT) und wird nicht geprüft. Global-Admin zählt wie
    PROF (analog require_course_access) — die Token-Allowlist gilt trotzdem.
    """
    raw = _bearer_token(ctx)
    if not raw:
        raise McpToolError(
            "Nicht authentifiziert. Erwartet wird ein Header "
            "'Authorization: Bearer aic_…' (Token unter Einstellungen → MCP anlegen)."
        )
    token = mcp_token_service.verify_token(session, raw)
    if not token:
        raise McpToolError("Token ist ungültig, unbekannt oder abgelaufen.")
    user = session.get(User, token.user_id)
    if not user:
        raise McpToolError("Der Token-Besitzer existiert nicht mehr.")
    set_current_user_id(user.id)
    logger.info("[MCP] tool=%s user=%s course=%s write=%s",
                tool_name or "?", user.username, course_id, write)

    if course_id is None:
        return user, token, CourseRole.STUDENT

    if not session.exec(
        select(McpTokenCourse)
        .where(McpTokenCourse.token_id == token.id)
        .where(McpTokenCourse.course_id == course_id)
    ).first():
        raise McpToolError(
            f"Kurs {course_id} ist für dieses Token nicht freigegeben. "
            "Prüfe list_courses für deine Kurse und gib den Kurs ggf. unter "
            "Einstellungen → MCP frei."
        )

    if user.role == GlobalUserRole.ADMIN:
        return user, token, CourseRole.PROF

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()
    if not membership:
        raise McpToolError("Du bist nicht mehr Mitglied dieses Kurses.")
    role = membership.role_in_course
    if write and role not in (CourseRole.PROF, CourseRole.TUTOR):
        raise McpToolError(
            "Dieses Tool erfordert die Rolle Tutor oder Professor "
            "(Studenten haben nur Lesezugriff)."
        )
    return user, token, role


def _is_tutor(role: CourseRole) -> bool:
    return role in (CourseRole.PROF, CourseRole.TUTOR)


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


def _snippet(text: str, query: str, width: int = 200) -> str:
    """~400 Zeichen um die erste Trefferstelle (für Search-Results)."""
    text = text or ""
    idx = text.lower().find(query.lower())
    if idx < 0:
        return text[: width * 2]
    start = max(0, idx - width)
    end = min(len(text), idx + len(query) + width)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def _resolve(session: Session, model, course_id: int, ref: Union[int, str], label: str):
    """ref = ID (int) oder exakter Titel (case-insensitive)."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        obj = session.get(model, int(ref))
        if not obj or obj.course_id != course_id:
            raise McpToolError(f"{label} mit ID {ref} nicht in diesem Kurs gefunden.")
        return obj
    rows = session.exec(
        select(model)
        .where(model.course_id == course_id)
        .where(func.lower(model.title) == ref.strip().lower())
    ).all()
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise McpToolError(
            f"{label} '{ref}' nicht gefunden. Nutze course_overview für die ID-Liste."
        )
    names = "; ".join(f"{r.title} (ID {r.id})" for r in rows)
    raise McpToolError(f"Titel '{ref}' ist mehrdeutig: {names}. Bitte per ID aufrufen.")


def _ref_match(text: str, query: str) -> bool:
    return query.lower() in (text or "").lower()


# ─── Server + Tools ───────────────────────────────────────────────

server = MCPServer(
    "AICampus",
    instructions=(
        "AICampus-Kursinhalte: Vorlesungsskripte, Folien (Slide-Decks), "
        "Übungsaufgaben und Quellen. Alle Kurs-Tools brauchen ein course_id: "
        "rufe zuerst list_courses() auf, um deine Kurse und deine Rolle zu "
        "ermitteln. Orientiere dich dann mit course_overview(course_id) oder "
        "search_course(course_id, query) und lies Details mit "
        "read_script_section / read_material / read_task / search_references."
    ),
)


@server.tool()
async def list_courses(ctx: Context) -> str:
    """Alle für dieses Token freigegebenen Kurse (ID, Name, Semester, deine Rolle).

    Erst aufrufen, um das course_id für alle anderen Tools zu ermitteln."""
    with Session(engine) as session:
        user, token, _ = _auth(ctx, session, None, tool_name="list_courses")
        rows = session.exec(
            select(McpTokenCourse).where(McpTokenCourse.token_id == token.id)
        ).all()
        courses = []
        for tc in sorted(rows, key=lambda r: r.course_id):
            course = session.get(Course, tc.course_id)
            if not course:
                continue
            if user.role == GlobalUserRole.ADMIN:
                role = CourseRole.PROF
            else:
                m = session.exec(
                    select(UserCourse)
                    .where(UserCourse.user_id == user.id)
                    .where(UserCourse.course_id == course.id)
                ).first()
                if not m:
                    continue
                role = m.role_in_course
            courses.append({
                "course_id": course.id,
                "name": course.name,
                "semester": course.semester,
                "role": ROLE_LABELS[role],
            })
        return _json({
            "courses": courses,
            "hint": "Nutze das course_id aus dieser Liste für alle anderen Tools.",
        })


@server.tool()
async def course_overview(course_id: int, ctx: Context) -> str:
    """Kursübersicht ohne Inhalt: Skript-Kapitel, Slide-Decks, Aufgaben, Quellen-Anzahl.

    Für Studenten nur freigeschaltete Inhalte; für Tutor/Prof zusätzlich ein
    visible-Flag je Element."""
    with Session(engine) as session:
        _user, _token, role = _auth(ctx, session, course_id, tool_name="course_overview")
        course = session.get(Course, course_id)
        if not course:
            raise McpToolError(f"Kurs {course_id} nicht gefunden (siehe list_courses).")
        tutor = _is_tutor(role)

        sections = session.exec(
            select(ScriptSection)
            .where(ScriptSection.course_id == course_id)
            .order_by(ScriptSection.display_order.asc())  # type: ignore[attr-defined]
        ).all()
        section_list = []
        for i, s in enumerate(sections, start=1):
            if not tutor and not s.is_visible:
                continue
            entry = {"id": s.id, "title": s.title}
            if tutor:
                entry["visible"] = s.is_visible
            section_list.append(entry)

        materials = session.exec(
            select(CourseMaterial)
            .where(CourseMaterial.course_id == course_id)
            .order_by(CourseMaterial.display_order.asc())  # type: ignore[attr-defined]
        ).all()
        deck_list = []
        for m in materials:
            if not tutor and not m.is_visible:
                continue
            entry = {
                "id": m.id,
                "title": m.title,
                "type": "slides" if m.material_type == MaterialType.SLIDES else "script (legacy)",
            }
            if tutor:
                entry["visible"] = m.is_visible
            deck_list.append(entry)

        tasks = session.exec(
            select(Task)
            .where(Task.course_id == course_id)
            .order_by(Task.display_order.asc())  # type: ignore[attr-defined]
        ).all()
        task_list = []
        for t in tasks:
            if not tutor and not t.is_visible:
                continue
            entry = {"id": t.id, "title": t.title, "type": t.task_type.value}
            if tutor:
                entry["visible"] = t.is_visible
            task_list.append(entry)

        ref_count = session.exec(
            select(func.count(CourseReference.id)).where(
                CourseReference.course_id == course_id
            )
        ).one()

        return _json({
            "course": {"course_id": course.id, "name": course.name, "semester": course.semester},
            "script_sections": section_list,
            "slide_decks_and_scripts": deck_list,
            "tasks": task_list,
            "reference_count": ref_count,
            "hint": (
                "Details: read_script_section / read_material / read_task; "
                "Volltextsuche: search_course; Quellen: search_references."
            ),
        })


@server.tool()
async def search_course(ctx: Context, course_id: int, query: str, limit: int = 10) -> str:
    """Volltextsuche (Keyword) über Skript-Kapitel, Folien/Skripte, Aufgabenstellungen
    und Quellen. Liefert je Treffer Ort + Snippet um die Trefferstelle.

    Für Studenten nur freigeschaltete Inhalte; Musterlösungen werden NICHT durchsucht."""
    with Session(engine) as session:
        _user, _token, role = _auth(ctx, session, course_id, tool_name="search_course")
        if not (query or "").strip():
            raise McpToolError("Leere Suchanfrage.")
        course = session.get(Course, course_id)
        if not course:
            raise McpToolError(f"Kurs {course_id} nicht gefunden (siehe list_courses).")
        tutor = _is_tutor(role)
        lim = max(1, min(int(limit), 25))

        hits = []

        sections = session.exec(
            select(ScriptSection).where(ScriptSection.course_id == course_id)
        ).all()
        for s in sorted(sections, key=lambda x: x.display_order):
            if not tutor and not s.is_visible:
                continue
            text = f"{s.title}\n{s.content}"
            if _ref_match(text, query):
                hits.append({
                    "location": "script_section",
                    "id": s.id,
                    "title": s.title,
                    "snippet": _snippet(text, query),
                    "read_with": "read_script_section",
                })

        materials = session.exec(
            select(CourseMaterial).where(CourseMaterial.course_id == course_id)
        ).all()
        for m in sorted(materials, key=lambda x: x.display_order):
            if not tutor and not m.is_visible:
                continue
            text = f"{m.title}\n{m.content}"
            if _ref_match(text, query):
                hits.append({
                    "location": "material",
                    "id": m.id,
                    "title": m.title,
                    "type": "slides" if m.material_type == MaterialType.SLIDES else "script (legacy)",
                    "snippet": _snippet(text, query),
                    "read_with": "read_material",
                })

        tasks = session.exec(select(Task).where(Task.course_id == course_id)).all()
        for t in sorted(tasks, key=lambda x: x.display_order):
            if not tutor and not t.is_visible:
                continue
            text = f"{t.title}\n{t.description}"
            if _ref_match(text, query):
                hits.append({
                    "location": "task",
                    "id": t.id,
                    "title": t.title,
                    "type": t.task_type.value,
                    "snippet": _snippet(text, query),
                    "read_with": "read_task",
                })

        refs = session.exec(
            select(CourseReference).where(CourseReference.course_id == course_id)
        ).all()
        for r in sorted(refs, key=lambda x: x.display_order):
            authors = ", ".join(r.authors or [])
            text = f"{r.key} {authors} {r.title} {r.venue} {r.description}"
            if _ref_match(text, query):
                hits.append({
                    "location": "reference",
                    "key": r.key,
                    "title": r.title,
                    "snippet": _snippet(r.description or r.title, query),
                    "read_with": "search_references",
                })

        total = len(hits)
        return _json({
            "course": course.name,
            "query": query,
            "total_hits": total,
            "hits": hits[:lim],
            "hint": "Ggf. limit erhöhen oder Suchbegriff verfeinern." if total > lim else None,
        })


@server.tool()
async def read_script_section(course_id: int, ref: Union[int, str], ctx: Context) -> str:
    """Komplettes Skript-Kapitel (Markdown). ref = Kapitel-ID oder exakter Titel."""
    with Session(engine) as session:
        _user, _token, role = _auth(ctx, session, course_id, tool_name="read_script_section")
        section = _resolve(session, ScriptSection, course_id, ref, "Skript-Kapitel")
        if not _is_tutor(role) and not section.is_visible:
            raise McpToolError("Kapitel nicht gefunden oder noch nicht freigeschaltet.")
        return _json({
            "id": section.id,
            "course_id": course_id,
            "title": section.title,
            "content": section.content,
        })


@server.tool()
async def read_material(course_id: int, ref: Union[int, str], ctx: Context) -> str:
    """Slide-Deck oder (Legacy-)Skript als Markdown. ref = ID oder exakter Titel.

    Slides-Folien sind mit `---` getrennt."""
    with Session(engine) as session:
        _user, _token, role = _auth(ctx, session, course_id, tool_name="read_material")
        material = _resolve(session, CourseMaterial, course_id, ref, "Material")
        if not _is_tutor(role) and not material.is_visible:
            raise McpToolError("Material nicht gefunden oder noch nicht freigeschaltet.")
        return _json({
            "id": material.id,
            "course_id": course_id,
            "title": material.title,
            "type": "slides" if material.material_type == MaterialType.SLIDES else "script (legacy)",
            "content": material.content,
        })


@server.tool()
async def read_task(course_id: int, ref: Union[int, str], ctx: Context) -> str:
    """Übungsaufgabe inkl. Aufgabenstellung. ref = Aufgaben-ID oder exakter Titel.

    Für Studenten nur freigeschaltete Aufgaben OHNE Musterlösung und ohne
    MC-Richtigkeitszeichen; für Tutor/Prof vollständig."""
    with Session(engine) as session:
        user, _token, role = _auth(ctx, session, course_id, tool_name="read_task")
        task = _resolve(session, Task, course_id, ref, "Aufgabe")
        tutor = _is_tutor(role)
        if not tutor and not task.is_visible:
            raise McpToolError("Aufgabe nicht gefunden oder noch nicht freigeschaltet.")
        out = {
            "id": task.id,
            "course_id": course_id,
            "title": task.title,
            "type": task.task_type.value,
            "max_points": task.max_points,
            "deadline": task.deadline,
            "description": task.description,
        }
        if task.text_template:
            out["text_template"] = task.text_template
        if tutor and task.model_solution:
            out["model_solution"] = task.model_solution
        if task.task_type.value == "mc" and task.mc_data:
            try:
                data = parse_mc_data(task.mc_data)
                out["mc_questions"] = (
                    tutor_view(data)
                    if tutor
                    else student_view(data, task_id=task.id, user_id=user.id)
                )
            except McValidationError:
                pass
        return _json(out)


@server.tool()
async def search_references(course_id: int, query: str, ctx: Context) -> str:
    """Quellen-Bibliothek durchsuchen (Key, Titel, Autoren, Venue, Beschreibung).

    Liefert Zitat + Kurzbeschreibung je Treffer."""
    with Session(engine) as session:
        _user, _token, _role = _auth(ctx, session, course_id, tool_name="search_references")
        if not (query or "").strip():
            raise McpToolError("Leere Suchanfrage.")
        course = session.get(Course, course_id)
        if not course:
            raise McpToolError(f"Kurs {course_id} nicht gefunden (siehe list_courses).")
        refs = session.exec(
            select(CourseReference).where(CourseReference.course_id == course_id)
        ).all()
        hits = []
        for r in sorted(refs, key=lambda x: x.display_order):
            authors = ", ".join(r.authors or [])
            haystack = f"{r.key} {authors} {r.title} {r.venue} {r.detail} {r.description}"
            if not _ref_match(haystack, query):
                continue
            citation = f"{authors} ({r.year}): {r.title}."
            if r.venue:
                citation += f" {r.venue}."
            hits.append({
                "key": r.key,
                "citation": citation,
                "description": r.description,
                "url": r.url or None,
                "doi": r.doi or None,
            })
        return _json({
            "course": course.name,
            "query": query,
            "hits": hits,
            "hint": "Zitieren im Kurs-Markdown mit @cite:{key}." if hits else None,
        })


# ─── Mount-App (stateless Streamable HTTP) ─────────────────────────
#
# stateless_http: neue Session pro Request, kein serverseitiger State —
# passt zum flachen Design (Auth kommt ohnehin pro Request per Header).
# DNS-Rebinding-Schutz wird deaktiviert: die eigentliche Sicherheitsebene
# ist TLS (nginx) + hochentropiges Bearer-Token; Browser-Clients sind
# nicht Zielgruppe dieses Endpunkts.

mcp_app = server.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
