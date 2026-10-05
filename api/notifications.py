"""
Benachrichtigungen (in-App-Glocke in der Top-Bar):

- GET  /api/notifications           → ungelesene Benachrichtigungen + Ungelesen-Zähler
- POST /api/notifications/read-all  → alle als gelesen markieren
- POST /api/notifications/{id}/read → einzelne als gelesen markieren

„Gelesen“ ist soft: read_at wird gesetzt und die Einträge bleiben in der
DB (Historie/Statistik), aber die API-Liste zeigt nur noch Ungelesenes.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import update
from sqlmodel import Session, func, select

from database.base import get_session
from database.models import Notification, User
from services.auth_service import get_current_user

router = APIRouter(prefix="/api/notifications", tags=["Benachrichtigungen"])


def _item_dict(n: Notification) -> dict:
    return {
        "id": n.id,
        "type": n.type,
        "title": n.title,
        "body": n.body,
        "link": n.link,
        "course_id": n.course_id,
        "created_at": n.created_at.isoformat(),
    }


def _get_owned(session: Session, user: User, notification_id: int) -> Notification:
    """Benachrichtigung laden und sicherstellen, dass sie zum User gehört."""
    n = session.get(Notification, notification_id)
    if not n or n.user_id != user.id:
        raise HTTPException(404, "Benachrichtigung nicht gefunden.")
    return n


@router.get("")
async def list_notifications(
    limit: int = Query(30, ge=1, le=100, description="Anzahl der letzten Einträge"),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Ungelesene Benachrichtigungen des Users (neueste zuerst) + Zähler.

    Gelesene Einträge (read_at gesetzt) bleiben in der DB, werden aber
    nicht mehr aufgelistet.
    """
    items = session.exec(
        select(Notification)
        .where(Notification.user_id == user.id)
        .where(Notification.read_at.is_(None))
        .order_by(Notification.id.desc())  # type: ignore[union-attr]
        .limit(limit)
    ).all()
    unread = session.exec(
        select(func.count())  # type: ignore[call-overload]
        .select_from(Notification)
        .where(Notification.user_id == user.id)
        .where(Notification.read_at.is_(None))
    ).one()
    return {"unread_count": unread, "items": [_item_dict(n) for n in items]}


@router.post("/read-all")
async def mark_all_notifications_read(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Alle ungelesenen Benachrichtigungen des Users als gelesen markieren."""
    session.exec(
        update(Notification)
        .where(Notification.user_id == user.id)
        .where(Notification.read_at.is_(None))
        .values(read_at=datetime.now())
    )
    session.commit()
    return {"ok": True}


@router.post("/{notification_id}/read")
async def mark_notification_read(
    notification_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Eine Benachrichtigung als gelesen markieren."""
    n = _get_owned(session, user, notification_id)
    if n.read_at is None:
        n.read_at = datetime.now()
        session.add(n)
        session.commit()
    return {"ok": True}
