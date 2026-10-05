"""
Notification-Service: Anlage der in-App-Benachrichtigungen (Glocke) +
Live-Push an offene Tabs des Empfängers (In-Memory-Queues).

Die Queues sind der Fan-out-Mechanismus für den SSE-Stream (M2). M1
nutzt sie noch nicht — das Frontend pollt /api/notifications.

Die notify_*-Funktionen sind defensiv: Ein Fehler bei der Benachrichtigung
darf den Hauptpfad (Forum-Post, Antwort, Grading) niemals brechen.
"""

import asyncio
import logging
from typing import Any, Iterable, Optional

from sqlmodel import Session, select

from database.models import Course, Notification, User, UserCourse

logger = logging.getLogger(__name__)

# user_id → aktive Queues (je offene Verbindung; M2: SSE-Stream)
_queues: dict[int, set[asyncio.Queue]] = {}


# ─── Live-Registry (für M2: SSE) ────────────────────────────────────


def subscribe(user_id: int) -> asyncio.Queue:
    """Registriert eine Queue für Live-Pushes (Rückgabe = Queue abonnieren)."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _queues.setdefault(user_id, set()).add(q)
    return q


def unsubscribe(user_id: int, q: asyncio.Queue) -> None:
    """Entfernt eine Queue (Verbindung geschlossen)."""
    qs = _queues.get(user_id)
    if qs:
        qs.discard(q)
        if not qs:
            _queues.pop(user_id, None)


def _push(user_id: int, payload: dict[str, Any]) -> None:
    for q in list(_queues.get(user_id, ())):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("[Notifications] Queue voll (User %s) — Event verworfen.", user_id)


# ─── Helpers ────────────────────────────────────────────────────────


def _payload(n: Notification) -> dict[str, Any]:
    """API-/SSE-Format einer Benachrichtigung."""
    return {
        "id": n.id,
        "type": n.type,
        "title": n.title,
        "body": n.body,
        "link": n.link,
        "course_id": n.course_id,
        "read": n.read_at is not None,
        "created_at": n.created_at.isoformat(),
    }


def _truncate(text: str, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _course_name(session: Session, course_id: Optional[int]) -> str:
    course = session.get(Course, course_id) if course_id else None
    return course.name if course else "Kurs"


def _create_many(
    session: Session,
    user_ids: Iterable[int],
    type_: str,
    title: str,
    body: str,
    link: str,
    course_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    actor_id: Optional[int] = None,
) -> None:
    """Legt Benachrichtigungen für mehrere Empfänger an + Live-Push."""
    ids = sorted({int(uid) for uid in user_ids if uid})
    if not ids:
        return
    rows = [
        Notification(
            user_id=uid,
            course_id=course_id,
            channel_id=channel_id,
            actor_id=actor_id,
            type=type_,
            title=title[:200],
            body=body[:500],
            link=link[:300],
        )
        for uid in ids
    ]
    session.add_all(rows)
    session.commit()
    for n in rows:
        _push(n.user_id, _payload(n))


def course_member_ids(session: Session, course_id: int) -> list[int]:
    """Alle Kurs-Mitglieder (UserCourse-Zeilen) eines Kurses."""
    return list(
        session.exec(
            select(UserCourse.user_id).where(UserCourse.course_id == course_id)
        ).all()
    )


# ─── Trigger (werden aus den jeweiligen API-Endpoints aufgerufen) ──


def notify_forum_message(
    session: Session,
    course_id: int,
    channel_id: int,
    channel_name: str,
    sender: User,
    content: str,
) -> None:
    """Neue Forum-Nachricht → alle Kurs-Mitglieder außer dem Absender.

    Alle erhalten die Benachrichtigung — auch User, die den Kanal gerade
    in einem (Hintergrund-)Tab offen haben. Wer den Kanal tatsächlich
    aufruft, hat sie ja im Chat: Das Forum-Polling markiert die eigenen
    Benachrichtigungen des Kanals direkt als gelesen (api/forum.py,
    list_forum_messages). Präsenz-basiertes Unterdrücken hat sich als
    unzuverlässig erwiesen (gedrosselte Hintergrund-Tabs halten die
    Präsenz am Leben → gar keine Benachrichtigungen mehr).
    """
    try:
        members = [uid for uid in course_member_ids(session, course_id) if uid != sender.id]
        if not members:
            return
        course_name = _course_name(session, course_id)
        _create_many(
            session,
            members,
            type_="forum_message",
            title=f"{sender.name} hat im Forum geschrieben",
            body=f"{course_name} · {channel_name}: „{_truncate(content)}“",
            link=f"/courses/{course_id}/forum?channel={channel_id}",
            course_id=course_id,
            channel_id=channel_id,
            actor_id=sender.id,
        )
    except Exception:
        session.rollback()
        logger.exception("[Notifications] Forum-Nachricht (Kurs %s) fehlgeschlagen.", course_id)


def notify_script_question_answer(
    session: Session,
    course_id: int,
    student_id: int,
    question: str,
    responder: User,
    content: str,
) -> None:
    """Neue menschliche Antwort auf eine Skript-Frage → der Fragesteller."""
    try:
        if responder.id == student_id:
            return  # Eigene Antwort: keine Benachrichtigung
        course_name = _course_name(session, course_id)
        _create_many(
            session,
            [student_id],
            type_="script_question_answer",
            title=f"{responder.name} hat deine Frage beantwortet",
            body=f"{course_name} — „{_truncate(question, 120)}“",
            link=f"/courses/{course_id}/script",
            course_id=course_id,
            actor_id=responder.id,
        )
    except Exception:
        session.rollback()
        logger.exception("[Notifications] Skript-Frage-Antwort (Kurs %s) fehlgeschlagen.", course_id)


def notify_llm_feedback(
    session: Session,
    course_id: int,
    student_id: int,
    task_id: int,
    task_title: str,
    points: float,
    max_points: float,
    success: bool = True,
    error: str = "",
) -> None:
    """LLM-Feedback nach Abgabe → der Student (Erfolg oder Fehler)."""
    try:
        course_name = _course_name(session, course_id)
        if success:
            title = f"Feedback bereit: {task_title}"
            body = f"{course_name}: {points:g} / {max_points:g} Punkte."
        else:
            title = f"Feedback-Fehler: {task_title}"
            body = f"{course_name}: {error or 'Die Korrektur ist fehlgeschlagen.'}"
        _create_many(
            session,
            [student_id],
            type_="llm_feedback",
            title=title,
            body=body[:500],
            link=f"/courses/{course_id}/tasks/{task_id}",
            course_id=course_id,
            actor_id=None,
        )
    except Exception:
        session.rollback()
        logger.exception("[Notifications] LLM-Feedback (Kurs %s, Aufgabe %s) fehlgeschlagen.", course_id, task_id)
