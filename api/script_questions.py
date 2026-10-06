"""
Skript-Fragen: alle Kurs-Mitglieder (Studierende/Tutoren/PROFs) stellen Fragen
zum Skript — die KI antwortet sofort, zusätzlich dürfen alle Kurs-Mitglieder
menschliche Antworten geben, sodass pro Frage ein Dialog entsteht.

Die Fragen werden persistiert (inkl. optionaler Text-Auswahl „quote“), damit
Tutoren/PROFs schwierige Stellen im Skript identifizieren und es verbessern
können. Der Status ist von Tutoren/PROFs auf „addressed“ umschaltbar.

Sichtbarkeit: Fragesteller und Antwortende werden immer mit Klarnamen, Rolle
und Avatar angezeigt (siehe load_questions_payload).

Endpoints:
- GET    /courses/{course_id}/script-questions
- POST   /courses/{course_id}/script-questions   (alle Kurs-Mitglieder)
- POST   /script-questions/{question_id}/responses
- PATCH  /script-questions/{question_id}         (Autor oder Staff: Status)
- DELETE /script-questions/{question_id}         (Autor oder Staff)
"""

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Field, Session, SQLModel, select

from database.base import get_session
from database.models import (
    Course,
    CourseRole,
    GlobalUserRole,
    ScriptQuestion,
    ScriptQuestionResponse,
    ScriptSection,
    User,
    UserCourse,
)
from services import notifications
from services.auth_service import get_current_user, require_course_access
from services.llm_service import LLMService
from services.settings_resolver import get_effective_llm_config
from api.script import _check_member

router = APIRouter(prefix="/api", tags=["Skript-Fragen"])
llm_service = LLMService()

# Forum ist für alle Kurs-Mitglieder (Student/Tutor/PROF) + Admins offen.
_ALL_COURSE_ROLES = (CourseRole.PROF, CourseRole.TUTOR, CourseRole.STUDENT)

# Kapitel-Inhalt im LLM-Prompt begrenzen (Token-Budget)
_SECTION_CONTENT_LIMIT = 16000
# Eigene letzte Fragen als Kontext für das LLM
_HISTORY_LIMIT = 5


class ScriptQuestionCreate(SQLModel):
    section_id: Optional[int] = None
    question: str = Field(min_length=1, max_length=2000)
    quote: str = Field(default="", max_length=1000)
    # Kontext um die Quote (eindeutige Ortung im Skript) + Startoffset
    # der Quote innerhalb des Kontexts; wird verworfen, wenn inkonsistent.
    quote_ctx: Optional[str] = Field(default=None, max_length=2000)
    quote_off: int = Field(default=0, ge=0)


class ScriptQuestionStatusUpdate(SQLModel):
    status: str = Field(min_length=1, max_length=20)


class ScriptQuestionResponseCreate(SQLModel):
    content: str = Field(min_length=1, max_length=8000)


# ─── Helpers ───────────────────────────────────────────────────────


def _role_in_course(session: Session, user: User, course_id: int) -> Optional[str]:
    """Rolle des Users im Kurs (als String) oder None bei keiner Membership."""
    uc = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()
    if uc:
        return uc.role_in_course.value
    return "ADMIN" if user.role == GlobalUserRole.ADMIN else None


def _is_staff(session: Session, user: User, course_id: int) -> bool:
    """Admin (global) oder Tutor/PROF im Kurs."""
    if user.role == GlobalUserRole.ADMIN:
        return True
    return _role_in_course(session, user, course_id) in (
        CourseRole.PROF.value,
        CourseRole.TUTOR.value,
    )


def _avatar_url(user: Optional[User]) -> Optional[str]:
    return f"/avatars/{user.avatar.rsplit('/', 1)[-1]}" if user and user.avatar else None


def _chapter_index_text(session: Session, course_id: int) -> str:
    """Titel (+ Zusammenfassung) der sichtbaren Kapitel — für Querverweise."""
    sections = session.exec(
        select(ScriptSection)
        .where(ScriptSection.course_id == course_id)
        .where(ScriptSection.is_visible == True)  # noqa: E712
        .order_by(ScriptSection.display_order.asc())  # type: ignore[union-attr]
    ).all()[:20]
    if not sections:
        return "(Keine sichtbaren Kapitel.)"
    lines = [
        f"- {s.title}" + (f" — {s.summary.strip()}" if (s.summary or "").strip() else "")
        for s in sections
    ]
    return "\n".join(lines)


def load_questions_payload(
    session: Session,
    course_id: int,
    viewer: User,
) -> list[dict[str, Any]]:
    """Alle Skript-Fragen des Kurses (neueste zuerst) inkl. Antworten + Rechten.

    Fragesteller und Antwortende werden immer mit Klarnamen/Rolle/Avatar
    angezeigt. Bulk-Load gegen N+1.
    """
    questions = session.exec(
        select(ScriptQuestion)
        .where(ScriptQuestion.course_id == course_id)
        .order_by(ScriptQuestion.created_at.desc())  # type: ignore[attr-defined]
    ).all()
    if not questions:
        return []

    staff = _is_staff(session, viewer, course_id)

    # Alle Antworten der Fragen in einem Zug laden (chronologisch)
    question_ids = [q.id for q in questions]
    responses = session.exec(
        select(ScriptQuestionResponse)
        .where(ScriptQuestionResponse.question_id.in_(question_ids))  # type: ignore[union-attr]
        .order_by(ScriptQuestionResponse.created_at.asc())  # type: ignore[union-attr]
    ).all()
    responses_by_question: dict[int, list[ScriptQuestionResponse]] = {}
    for r in responses:
        responses_by_question.setdefault(r.question_id, []).append(r)

    # Nutzer (Fragesteller + Antwortende) + Rollen + Kapitel-Titel
    user_ids = {q.student_id for q in questions}
    for r in responses:
        if r.user_id is not None:
            user_ids.add(r.user_id)
    users = {
        u.id: u
        for u in session.exec(select(User).where(User.id.in_(user_ids))).all()  # type: ignore[attr-defined]
    }
    roles = {
        uc.user_id: uc.role_in_course.value
        for uc in session.exec(
            select(UserCourse)
            .where(UserCourse.course_id == course_id)
            .where(UserCourse.user_id.in_(user_ids))  # type: ignore[attr-defined]
        ).all()
    }
    section_ids = {q.section_id for q in questions if q.section_id is not None}
    section_titles: dict[int, str] = {
        s.id: s.title
        for s in session.exec(
            select(ScriptSection).where(ScriptSection.id.in_(section_ids))  # type: ignore[attr-defined]
        ).all()
        if s.id is not None
    }

    payload: list[dict[str, Any]] = []
    for q in questions:
        if q.id is None:
            continue
        is_mine = q.student_id == viewer.id
        student = users.get(q.student_id)
        student_info = {
            "name": student.name if student else "unknown",
            "role": roles.get(q.student_id) or "STUDENT",
            "avatar": _avatar_url(student),
        }

        q_responses = []
        for r in responses_by_question.get(q.id, []):
            if r.source == "llm" or r.user_id is None:
                r_name, r_role, r_avatar = "AICampus", "LLM", None
            else:
                u = users.get(r.user_id)
                r_name = u.name if u else "unknown"
                r_role = roles.get(r.user_id) or "STUDENT"
                r_avatar = _avatar_url(u)
            q_responses.append(
                {
                    "id": r.id,
                    "source": r.source,
                    "content": r.content,
                    "created_at": r.created_at.isoformat(),
                    "name": r_name,
                    "role": r_role,
                    "avatar": r_avatar,
                }
            )

        payload.append(
            {
                "id": q.id,
                "section_id": q.section_id,
                "section_title": section_titles.get(q.section_id) if q.section_id is not None else None,
                "quote": q.quote,
                "quote_ctx": q.quote_ctx,
                "quote_off": q.quote_off,
                "question": q.question,
                "status": q.status,
                "created_at": q.created_at.isoformat(),
                "updated_at": q.updated_at.isoformat() if q.updated_at else None,
                "is_mine": is_mine,
                "student": student_info,
                "can_delete": is_mine or staff,
                "can_set_status": staff or is_mine,
                "responses": q_responses,
            }
        )
    return payload


# ─── Endpoints ─────────────────────────────────────────────────────


@router.get("/courses/{course_id}/script-questions")
async def list_script_questions(
    course_id: int,
    session: Session = Depends(get_session),
    viewer_and_course: tuple[User, int] = Depends(require_course_access(*_ALL_COURSE_ROLES)),
):
    """Alle Skript-Fragen des Kurses (neueste zuerst, inkl. Antworten)."""
    viewer, _ = viewer_and_course
    return load_questions_payload(session, course_id, viewer)


@router.post("/courses/{course_id}/script-questions", status_code=201)
async def create_script_question(
    course_id: int,
    data: ScriptQuestionCreate,
    session: Session = Depends(get_session),
    viewer_and_course: tuple[User, int] = Depends(require_course_access(*_ALL_COURSE_ROLES)),
):
    """Neue Skript-Frage (alle Kurs-Mitglieder) — die KI antwortet synchron."""
    viewer, _ = viewer_and_course

    question_text = data.question.strip()
    if not question_text:
        raise HTTPException(400, "Die Frage darf nicht leer sein.")
    quote = (data.quote or "").strip()[:1000]

    # Kontext nur übernehmen, wenn die Quote exakt darin enthalten ist
    # (sonst wäre die Markierung im Skript unzuverlässig)
    quote_ctx: Optional[str] = None
    quote_off = 0
    if data.quote_ctx and quote:
        ctx = data.quote_ctx[:2000]
        off = max(0, min(int(data.quote_off or 0), len(ctx)))
        if off + len(quote) <= len(ctx) and ctx[off:off + len(quote)] == quote:
            quote_ctx = ctx
            quote_off = off

    section = None
    if data.section_id is not None:
        section = session.get(ScriptSection, data.section_id)
        if not section or section.course_id != course_id or not section.is_visible:
            raise HTTPException(404, "Kapitel nicht gefunden oder noch nicht freigeschaltet.")

    q = ScriptQuestion(
        course_id=course_id,
        section_id=section.id if section else None,
        student_id=viewer.id,  # type: ignore[arg-type]
        question=question_text,
        quote=quote,
        quote_ctx=quote_ctx,
        quote_off=quote_off,
    )
    session.add(q)
    session.commit()
    session.refresh(q)

    # ── LLM-Antwort (synchron, wie beim Hinweis-System) ──────────
    course = session.get(Course, course_id)
    course_name = course.name if course else "Kurs"
    config = get_effective_llm_config(session, course_id)

    if section:
        content = section.content or ""
        section_context = content[:_SECTION_CONTENT_LIMIT]
        if len(content) > _SECTION_CONTENT_LIMIT:
            section_context += "\n…(Ausschnitt — das Kapitel ist länger)"
    else:
        section_context = "(Kein Kapitel ausgewählt — die Frage ist allgemein zum Skript.)"

    quote_context = f"„{quote}“" if quote else "(Keine Textauswahl — die Frage ist keiner konkreten Stelle zugeordnet.)"

    history = session.exec(
        select(ScriptQuestion)
        .where(ScriptQuestion.course_id == course_id)
        .where(ScriptQuestion.student_id == viewer.id)
        .where(ScriptQuestion.id != q.id)
        .order_by(ScriptQuestion.created_at.desc())  # type: ignore[attr-defined]
    ).all()[:_HISTORY_LIMIT]
    history_text = "(Dies ist die erste Frage.)"
    if history:
        history_text = "\n".join(f"Q{i}: {h.question}" for i, h in enumerate(history, 1))

    result = await llm_service.answer_script_question(
        course_name=course_name,
        section_context=section_context,
        quote_context=quote_context,
        chapter_index=_chapter_index_text(session, course_id),
        question_history=history_text,
        student_question=question_text,
        config=config,
    )

    if result.get("success"):
        llm_data = result.get("data")
        answer = (llm_data.get("text") or "").strip() if isinstance(llm_data, dict) else ""
        answer = answer or "(Die KI konnte keine Antwort geben.)"
    else:
        # LLM-Fehler nicht verschleiern, aber die Frage trotzdem speichern
        answer = "⚠️ Die KI konnte gerade keine Antwort geben. Bitte versuche es später erneut oder wende dich im Forum an die Tutoren."
    session.add(
        ScriptQuestionResponse(
            question_id=q.id,  # type: ignore[arg-type]
            user_id=None,
            source="llm",
            content=answer,
        )
    )
    session.commit()

    # Glocke: KI-Antwort wie eine normale Antwort benachrichtigen
    # (konsistent mit menschlichen Antworten; der Fehlerfall ist nur im
    # Dialog sichtbar und wird nicht benachrichtigt). Auto-Read läuft über
    # den Fragen-Dialog (offener Dialog → read-type, templates/course/script.html).
    if result.get("success"):
        notifications.notify_script_question_answer(
            session, course_id, q.student_id, q.question, "AICampus",
            question_id=q.id,
        )

    payload = load_questions_payload(session, course_id, viewer)
    return next((item for item in payload if item["id"] == q.id), payload[0])


@router.post("/script-questions/{question_id}/responses", status_code=201)
async def add_script_question_response(
    question_id: int,
    data: ScriptQuestionResponseCreate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Menschliche Antwort auf eine Skript-Frage (alle Kurs-Mitglieder)."""
    q = session.get(ScriptQuestion, question_id)
    if not q:
        raise HTTPException(404, "Frage nicht gefunden.")
    _check_member(user, session, q.course_id)

    content = data.content.strip()
    if not content:
        raise HTTPException(400, "Die Antwort darf nicht leer sein.")

    r = ScriptQuestionResponse(
        question_id=q.id,  # type: ignore[arg-type]
        user_id=user.id,
        source="human",
        content=content,
    )
    session.add(r)
    q.updated_at = datetime.now(timezone.utc)
    session.add(q)
    session.commit()
    session.refresh(r)

    # Glocke: Fragesteller über die neue Antwort informieren
    notifications.notify_script_question_answer(
        session, q.course_id, q.student_id, q.question, user.name,
        question_id=q.id, responder_id=user.id,
    )

    return {
        "id": r.id,
        "source": r.source,
        "content": content,
        "created_at": r.created_at.isoformat(),
        "name": user.name,
        "role": _role_in_course(session, user, q.course_id) or "STUDENT",
        "avatar": _avatar_url(user),
    }


@router.patch("/script-questions/{question_id}")
async def update_script_question_status(
    question_id: int,
    data: ScriptQuestionStatusUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Frage als „behandelt“ markieren / wieder „offen“ (Autor oder Staff)."""
    q = session.get(ScriptQuestion, question_id)
    if not q:
        raise HTTPException(404, "Frage nicht gefunden.")
    if not _is_staff(session, user, q.course_id) and q.student_id != user.id:
        raise HTTPException(403, "Nur der Fragesteller (oder ein Tutor/ein PROF) kann den Status ändern.")
    if data.status not in ("open", "addressed"):
        raise HTTPException(400, "Status muss 'open' oder 'addressed' sein.")

    q.status = data.status
    q.updated_at = datetime.now(timezone.utc)
    session.add(q)
    session.commit()
    session.refresh(q)

    return {"message": "Status aktualisiert.", "status": q.status}


@router.delete("/script-questions/{question_id}")
async def delete_script_question(
    question_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Skript-Frage inkl. aller Antworten löschen (Autor oder Staff)."""
    q = session.get(ScriptQuestion, question_id)
    if not q:
        raise HTTPException(404, "Frage nicht gefunden.")
    if not _is_staff(session, user, q.course_id) and q.student_id != user.id:
        raise HTTPException(403, "Nur der Fragesteller (oder ein Tutor/ein PROF) kann diese Frage löschen.")

    for r in session.exec(
        select(ScriptQuestionResponse).where(ScriptQuestionResponse.question_id == q.id)
    ).all():
        session.delete(r)
    session.delete(q)
    session.commit()

    return {"message": "Frage gelöscht."}
