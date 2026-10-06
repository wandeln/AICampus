"""
Web-Push-Subscriptions (M3): Browser-Subscriptionen (PushManager) speichern,
damit In-App-Benachrichtigungen auch als Browser-Push zugestellt werden
können, wenn kein AICampus-Tab offen ist (Versand: services/notifications.py
via pywebpush/FCM, VAPID-Keys aus .env).

- GET  /api/push/status        → Feature aktiv? + öffentlicher VAPID-Key
- POST /api/push/subscribe     → Subscription speichern/aktualisieren
                                 (Dedupe-Key: Endpoint, ein pro Gerät)
- POST /api/push/unsubscribe   → Subscription löschen (Permission entzogen)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from config import VAPID_PUBLIC_KEY, push_enabled
from database.base import get_session
from database.models import PushSubscription, User
from services.auth_service import get_current_user

router = APIRouter(prefix="/api/push", tags=["Web Push"])

logger = logging.getLogger(__name__)


class SubscribeRequest(BaseModel):
    """PushSubscription-JSON aus dem Browser (PushManager.subscribe).

    expirationTime & Co. bleiben bewusst unmodelliert (Pydantic ignoriert
    unbekannte Felder) — relevant sind nur Endpoint + Keys.
    """

    endpoint: str
    keys: dict


class UnsubscribeRequest(BaseModel):
    endpoint: str


@router.get("/status")
async def push_status(user: User = Depends(get_current_user)):
    """Feature-Status + öffentlicher VAPID-Key für PushManager.subscribe."""
    enabled = push_enabled()
    return {"enabled": enabled, "public_key": VAPID_PUBLIC_KEY if enabled else ""}


@router.post("/subscribe")
async def push_subscribe(
    data: SubscribeRequest,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Subscription speichern oder aktualisieren (Dedupe via Endpoint).

    Wird vom Service Worker (subscribe + pushsubscriptionchange,
    d. h. Key-Rotation) und vom „Push aktivieren“-Button im
    Glocken-Menü aufgerufen.
    """
    p256dh = (data.keys or {}).get("p256dh", "")
    auth = (data.keys or {}).get("auth", "")
    if not data.endpoint or not p256dh:
        raise HTTPException(400, "Ungültige Push-Subscription.")

    user_agent = (request.headers.get("user-agent") or "")[:300]
    row = session.exec(
        select(PushSubscription).where(PushSubscription.endpoint == data.endpoint)
    ).first()
    if row:
        # Gleicher Browser, (ggf. anderer Account): Endpoint gehört jetzt
        # dem aktuellen User — die alte Zuordnung wäre totes Gewicht.
        row.user_id = user.id  # type: ignore[arg-type]
        row.p256dh = p256dh
        row.auth = auth
        row.user_agent = user_agent
        session.add(row)
    else:
        session.add(
            PushSubscription(
                user_id=user.id,  # type: ignore[arg-type]
                endpoint=data.endpoint,
                p256dh=p256dh,
                auth=auth,
                user_agent=user_agent,
            )
        )
    session.commit()
    return {"ok": True}


@router.post("/unsubscribe")
async def push_unsubscribe(
    data: UnsubscribeRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Subscription des Users löschen („Push abschalten“ im Glocken-Menü)."""
    row = session.exec(
        select(PushSubscription).where(PushSubscription.endpoint == data.endpoint)
    ).first()
    if row and row.user_id == user.id:
        session.delete(row)
        session.commit()
    return {"ok": True}
