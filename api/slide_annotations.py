"""
Live-Notizen für Slide-Decks (Stift, Whiteboard, Laser).

- WS  /ws/courses/{c}/slides/{d}/live          Deck-Raum (Tutoren senden, Studierende lesen)
- GET /api/courses/{c}/slides/{d}/annotations        alle Notizen (Tutor; Init)
- GET /api/courses/{c}/slides/{d}/annotations/slide  eine Folie (Tutor; Student nur wenn freigegeben)
- GET /api/courses/{c}/slides/{d}/annotations/state  {shared}
- PUT /api/courses/{c}/slides/{d}/access             Zugriffsstufe hidden|visible|notes (Tutor)

Persistenz/Abgleich: services/slide_index_service.py, Raum: services/slide_live.py.
Siehe docs/LLM-plans/slide-annotations-live-share.md.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select

from database.base import engine, get_session
from database.models import (
    CourseMaterial, CourseRole, GlobalUserRole, MaterialType, User, UserCourse,
)
from services import slide_index_service as svc
from services import slide_live as live
from services.auth_service import decode_access_token, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Slide-Notizen"])

MAX_MSG = 300_000


def _access(session: Session, user: User, course_id: int, deck_id: int) -> tuple[CourseMaterial, bool]:
    """(Deck, is_tutor) — 403/404 wie slides_present_page."""
    deck = session.get(CourseMaterial, deck_id)
    if (not deck or deck.course_id != course_id or deck.material_type != MaterialType.SLIDES
            or deck.deleted_at is not None):
        raise HTTPException(404, "Slide-Deck nicht gefunden.")
    m = session.exec(select(UserCourse).where(
        UserCourse.user_id == user.id, UserCourse.course_id == course_id)).first()
    is_admin = user.role == GlobalUserRole.ADMIN
    if not m and not is_admin:
        raise HTTPException(403, "Du bist kein Mitglied dieses Kurses.")
    is_tutor = is_admin or (m is not None and m.role_in_course in (CourseRole.PROF, CourseRole.TUTOR))
    if not deck.is_visible and not is_tutor:
        raise HTTPException(404, "Slide-Deck nicht gefunden.")
    return deck, is_tutor


# ─── REST ───────────────────────────────────────────────────────────

@router.get("/api/courses/{course_id}/slides/{deck_id}/annotations")
def get_annotations(course_id: int, deck_id: int, session: Session = Depends(get_session),
                    user: User = Depends(get_current_user)):
    deck, is_tutor = _access(session, user, course_id, deck_id)
    shared = svc.is_shared(session, deck_id)
    if not is_tutor and not shared:     # Studierende (PDF-Export): nur freigegebene Notizen
        raise HTTPException(403, "Notizen sind nicht freigegeben.")
    return {"shared": shared, "rows": svc.load_all(session, deck)}


@router.get("/api/courses/{course_id}/slides/{deck_id}/annotations/slide")
def get_annotations_slide(course_id: int, deck_id: int, uid: str, session: Session = Depends(get_session),
                          user: User = Depends(get_current_user)):
    _deck, is_tutor = _access(session, user, course_id, deck_id)
    if not is_tutor and not svc.is_shared(session, deck_id):
        raise HTTPException(403, "Notizen sind nicht freigegeben.")
    if not svc.UID_RE.match(uid):
        raise HTTPException(400, "Ungültige Folien-ID.")
    return {"rows": svc.load_slide(session, deck_id, uid)}


@router.get("/api/courses/{course_id}/slides/{deck_id}/annotations/state")
def get_annotation_state(course_id: int, deck_id: int, session: Session = Depends(get_session),
                         user: User = Depends(get_current_user)):
    _access(session, user, course_id, deck_id)
    return {"shared": svc.is_shared(session, deck_id)}


@router.put("/api/courses/{course_id}/slides/{deck_id}/access")
async def put_access(course_id: int, deck_id: int, body: dict, session: Session = Depends(get_session),
                     user: User = Depends(get_current_user)):
    """Zugriffsstufe des Decks: hidden (versteckt) | visible (sichtbar ohne Notizen) | notes (mit Notizen)."""
    deck, is_tutor = _access(session, user, course_id, deck_id)
    if not is_tutor:
        raise HTTPException(403, "Nur für Tutoren.")
    level = body.get("level")
    if level not in ("hidden", "visible", "notes"):
        raise HTTPException(400, "Ungültige Stufe.")
    deck.is_visible = level != "hidden"
    deck.updated_at = datetime.now(timezone.utc)
    session.add(deck)
    session.commit()
    shared = level == "notes"
    svc.set_shared(session, deck_id, shared, user.id)
    _apply_share(deck_id, shared)
    return {"level": level}


def _apply_share(deck_id: int, shared: bool) -> None:
    room = live.room_if_exists(deck_id)
    if room is None:
        return
    room.shared = shared
    live.broadcast(room, {"type": "share", "shared": shared}, force=True)


# ─── WebSocket ──────────────────────────────────────────────────────

def _apply_sync(deck_id: int, op: dict):
    with Session(engine) as s:
        return svc.apply_op(s, deck_id, op)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < 1e7


@router.websocket("/ws/courses/{course_id}/slides/{deck_id}/live")
async def live_ws(ws: WebSocket, course_id: int, deck_id: int) -> None:
    token = ws.cookies.get("access_token", "")
    payload = decode_access_token(token) if token else None
    user_id = payload.get("sub") if payload else None
    if not user_id:
        await ws.close(code=4401)
        return
    try:
        with Session(engine) as session:
            user = session.get(User, user_id)
            if user is None:
                await ws.close(code=4401)
                return
            _deck, is_tutor = _access(session, user, course_id, deck_id)
            shared = svc.is_shared(session, deck_id)
            name = user.name
    except HTTPException:
        await ws.close(code=4403)
        return

    await ws.accept()
    room = live.get_room(deck_id, shared)
    conn = live.Conn(ws, user_id, name, is_tutor)
    live.join(room, conn)
    try:
        while not conn.dead:
            text = await ws.receive_text()
            if len(text) > MAX_MSG:
                await ws.close(code=1009)
                break
            if not is_tutor:
                continue  # Studierende sind read-only
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if isinstance(msg, dict):
                await _handle(room, conn, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.warning("[SlideLive] Verbindung (Deck %s) beendet", deck_id, exc_info=True)
    finally:
        live.leave(room, conn)


async def _handle(room: live.Room, conn: live.Conn, msg: dict) -> None:
    t = msg.get("type")

    if t == "op":
        if not conn.allow("op", 60):
            return
        cs, seq = msg.get("cs"), msg.get("seq")
        if not (isinstance(cs, str) and len(cs) <= 40 and isinstance(seq, int)):
            return
        async with room.lock:
            if seq <= room.seqs.get(cs, 0):           # Wiederholung nach Reconnect
                live.enqueue(conn, {"type": "ack", "seq": seq, "dup": True})
                return
            result = await asyncio.to_thread(_apply_sync, room.deck_id, msg)
            room.seqs[cs] = seq
            if len(room.seqs) > 500:
                room.seqs.pop(next(iter(room.seqs)))
            if result is None:
                live.enqueue(conn, {"type": "ack", "seq": seq, "err": True})
                return
            live.enqueue(conn, {"type": "ack", "seq": seq, "rev": result.get("rev")})
            live.broadcast(room, {"type": "op", **result}, exclude=conn)

    elif t == "laser":
        if not conn.live or not conn.allow("laser", 60):
            return
        slide = msg.get("slide")
        if not (isinstance(slide, str) and svc.UID_RE.match(slide)):
            return
        out = {"type": "laser", "cid": conn.id, "slide": slide, "on": bool(msg.get("on", True)),
               "pressed": bool(msg.get("pressed"))}
        if out["on"]:
            if not (_num(msg.get("x")) and _num(msg.get("y"))):
                return
            out["x"], out["y"] = msg["x"], msg["y"]
            pts = msg.get("pts")
            if isinstance(pts, list):
                out["pts"] = [[p[0], p[1]] for p in pts[:40]
                              if isinstance(p, list) and len(p) == 2 and _num(p[0]) and _num(p[1])]
        live.broadcast(room, out, exclude=conn)

    elif t == "view":
        if not conn.allow("view", 20):
            return
        slide, f, mode, board = msg.get("slide"), msg.get("f", -1), msg.get("mode", 0), msg.get("board", 0)
        if not (isinstance(slide, str) and svc.UID_RE.match(slide) and isinstance(f, int)
                and mode in (0, 1) and isinstance(board, int) and 0 <= board < 1000):
            return
        conn.pres = {"slide": slide, "f": f, "mode": mode, "board": board,
                     "active": bool(msg.get("active", True)), "ts": time.time()}
        if conn.live:
            live.push_presence(room)

    elif t == "live":
        if not conn.allow("live", 5):
            return
        # Dozent macht sich für Studierende (und andere Dozenten) live verfolgbar / nicht mehr
        conn.live = bool(msg.get("on"))
        live.push_presence(room)
