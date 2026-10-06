"""
Notification-Service: Anlage der in-App-Benachrichtigungen (Glocke) +
Live-Push an offene SSE-Streams des Empfängers (In-Memory-Queues).

Jeder offene Stream-Connector (GET /api/notifications/stream, M2)
besitzt eine Queue; _push() speist bei jeder neuen Benachrichtigung
alle Queues des Empfängers. In-Memory: Queues überleben keinen
Reload/Neustart — das Frontend-Polling ist der Fallback.

Die notify_*-Funktionen sind defensiv: Ein Fehler bei der Benachrichtigung
darf den Hauptpfad (Forum-Post, Antwort, Grading) niemals brechen.
"""

import asyncio
import json
import logging
import threading
from typing import Any, Iterable, Optional

from sqlmodel import Session, select

from config import PUSH_APP_URL, VAPID_PRIVATE_KEY, push_enabled
from database.base import engine
from database.models import Course, Notification, PushSubscription, User, UserCourse

logger = logging.getLogger(__name__)

# user_id → aktive Queues (je offene SSE-Verbindung)
_queues: dict[int, set[asyncio.Queue]] = {}


# ─── Live-Registry (SSE-Stream /api/notifications/stream) ───────────


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


def notify_read(user_id: int) -> None:
    """Live-Event „gelesen" → Glocke (Badge/Liste) sofort neu laden.

    Wird nach Auto-Read (Forum-Polling) und manuellen Read-Endpoints
    aufgerufen, damit alle offenen Tabs des Users sofort updaten statt
    bis zum nächsten Poll zu warten. Das Frontend wertet den Payload
    nicht aus — er dient nur als Trigger für fetchNotifications().
    """
    try:
        _push(user_id, {"event": "read"})
    except Exception:
        logger.exception("[Notifications] Read-Event (User %s) fehlgeschlagen.", user_id)


def push_forum_message(session: Session, course_id: int, channel_id: int, message_id: int) -> None:
    """Live-Event (SSE): neue Forum-Nachricht → offene Chat-Seiten updaten.

    Ergänzt die Glocke-Benachrichtigung: alle Kurs-Mitglieder (inkl.
    Absender, dessen Fetch idempotent ist) bekommen das Event in ihren
    Streams. Die Forum-Seite lädt bei passendem channel_id sofort die
    neuen Nachrichten nach, statt auf den 4-s-Poll zu warten (der
    bleibt als Fallback). Payload ohne Inhalt — der Client holt sich
    die Nachricht über den normalen Poll-Fetch.
    """
    try:
        payload = {
            "event": "forum_message",
            "course_id": course_id,
            "channel_id": channel_id,
            "message_id": message_id,
        }
        for uid in course_member_ids(session, course_id):
            _push(uid, payload)
    except Exception:
        logger.exception("[Notifications] Forum-Live-Event (Kurs %s, Kanal %s) fehlgeschlagen.", course_id, channel_id)


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
        _schedule_webpush(n.user_id, n)


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
    responder_name: str,
    question_id: Optional[int] = None,
    responder_id: Optional[int] = None,
) -> None:
    """Neue Antwort auf eine Skript-Frage (menschlich oder LLM) → der Fragesteller.

    ``responder_id=None`` = die KI („AICampus“). Der Deep-Link ``#sq:{id}``
    öffnet die Frage im Fragen-Dialog der Skript-Seite.
    """
    try:
        if responder_id is not None and responder_id == student_id:
            return  # Eigene Antwort: keine Benachrichtigung
        course_name = _course_name(session, course_id)
        link = (
            f"/courses/{course_id}/script#sq:{question_id}" if question_id
            else f"/courses/{course_id}/script"
        )
        _create_many(
            session,
            [student_id],
            type_="script_question_answer",
            title=f"{responder_name} hat deine Frage beantwortet",
            body=f"{course_name} — „{_truncate(question, 120)}“",
            link=link,
            course_id=course_id,
            actor_id=responder_id,
        )
    except Exception:
        session.rollback()
        logger.exception("[Notifications] Skript-Frage-Antwort (Kurs %s) fehlgeschlagen.", course_id)


# ─── Web Push (M3): Browser-Push via FCM (pywebpush) ─────────


def _send_webpush_sync(user_id: int, payload: dict[str, Any]) -> None:
    """Benachrichtigung an alle gespeicherten Push-Subscriptions eines
    Users senden (blockiert → läuft immer im Thread-Pool).

    Defensiv wie der Rest des Moduls: Fehler dürfen den Hauptpfad nicht
    berühren. 404/410 vom Push-Service = Endpoint tot → Subscription
    wird entfernt (sonst würde jede Nachricht an sie verloren gehen).
    """
    try:
        from pywebpush import WebPushException, webpush

        with Session(engine) as session:
            subs: list[PushSubscription] = list(
                session.exec(select(PushSubscription).where(PushSubscription.user_id == user_id)).all()
            )
        for sub in subs:
            try:
                webpush(
                    subscription_info={
                        "endpoint": sub.endpoint,
                        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                    },
                    data=json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    # sub: kanonische URL OHNE trailing slash (py-vapid-Regex)
                    vapid_claims={"sub": PUSH_APP_URL},
                    ttl=300,  # 5 min: kurz offline darf nichts verloren gehen, mehr nicht
                )
            except WebPushException as e:
                status = e.response.status_code if e.response is not None else None
                if status in (404, 410):
                    with Session(engine) as session:
                        row = session.get(PushSubscription, sub.id)
                        if row:
                            session.delete(row)
                            session.commit()
                    logger.info("[Push] Subscription %s entfernt (Endpoint tot, HTTP %s).", sub.id, status)
                else:
                    logger.warning("[Push] Zustellung an Subscription %s fehlgeschlagen (HTTP %s).", sub.id, status)
    except Exception:
        logger.exception("[Push] Versand (User %s) fehlgeschlagen.", user_id)


def _schedule_webpush(user_id: int, n: Notification) -> None:
    """Browser-Push für eine neue Benachrichtigung anlegen (nie blockierend).

    Im Event-Loop → Thread-Pool; ohne Loop (z. B. aus einem
    Hintergrund-Thread des Grading-Flows) → eigener Daemon-Thread.
    """
    if not push_enabled():
        return
    payload = {
        "title": n.title,
        "body": n.body,
        "url": n.link,
        "id": n.id,
        "tag": str(n.id),
    }
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        threading.Thread(target=_send_webpush_sync, args=(user_id, payload), daemon=True).start()
        return
    loop.run_in_executor(None, _send_webpush_sync, user_id, payload)


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
