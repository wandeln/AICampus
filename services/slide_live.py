"""
Deck-Raum für Live-Notizen (WebSocket): Verbindungen, Presence, Broadcast.

Pro Deck ein In-Memory-Raum (single-process Uvicorn → zulässig, wie die
SSE-Queues in notifications.py; NIE mit mehreren Workern betreiben).
Jede Verbindung hat eine eigene Sendequeue + Writer-Task, damit ein langsamer
Client weder andere blockiert noch die Reihenfolge der Nachrichten ändert.

Studierende erhalten Notizen (ops) und Laser erst, wenn das Deck mit Notizen
freigegeben ist (``room.shared``). Presence (Folgen) bekommen alle — aber nur
von Dozenten, die „live“ sind (``Conn.live``, vom Dozenten per Popup/Taste V
gesetzt, flüchtig).

Siehe docs/LLM-plans/slide-annotations-live-share.md.
"""

import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)

QUEUE_MAX = 1000
LASER_DROP_ABOVE = 50     # Laser-Nachrichten verwerfen, wenn die Queue so voll ist
PING_SECONDS = 25         # hält Proxy-Idle-Timeouts offen


class Conn:
    _next_id = 0

    def __init__(self, ws: WebSocket, user_id: int, name: str, tutor: bool):
        Conn._next_id += 1
        self.id = Conn._next_id
        self.ws = ws
        self.user_id = user_id
        self.name = name
        self.tutor = tutor
        self.q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self.pres: Optional[dict] = None     # {slide, f, mode, board, active, ts}
        self.live = False                    # Dozent ist für Folgen/Laser freigegeben
        self.dead = False
        self.writer: Optional[asyncio.Task] = None
        self._win = {}                       # Rate-Limit: kind → (Fensterstart, Anzahl)

    def allow(self, kind: str, per_sec: int) -> bool:
        now = time.monotonic()
        start, n = self._win.get(kind, (now, 0))
        if now - start >= 1:
            start, n = now, 0
        self._win[kind] = (start, n + 1)
        return n < per_sec


class Room:
    def __init__(self, deck_id: int, shared: bool):
        self.deck_id = deck_id
        self.shared = shared
        self.conns: set[Conn] = set()
        self.lock = asyncio.Lock()           # serialisiert Ops pro Deck (Reihenfolge + rev)
        self.seqs: dict[str, int] = {}       # cs → letzte verarbeitete seq (Dedup bei Reconnect)

    def visible_to(self, c: Conn, force: bool = False) -> bool:
        return force or c.tutor or self.shared


_rooms: dict[int, Room] = {}


def get_room(deck_id: int, shared: bool) -> Room:
    room = _rooms.get(deck_id)
    if room is None:
        room = _rooms[deck_id] = Room(deck_id, shared)
    return room


def room_if_exists(deck_id: int) -> Optional[Room]:
    return _rooms.get(deck_id)


# ─── Senden ─────────────────────────────────────────────────────────

def enqueue(c: Conn, msg: dict) -> None:
    """Nicht-blockierend in die Queue der Verbindung. Voll → Verbindung beenden
    (Client reconnectet und lädt die Folie neu)."""
    if c.dead:
        return
    if msg.get("type") == "laser" and c.q.qsize() > LASER_DROP_ABOVE:
        return
    try:
        c.q.put_nowait(json.dumps(msg, separators=(",", ":")))
    except asyncio.QueueFull:
        logger.warning("[SlideLive] Queue voll (User %s) — Verbindung wird beendet.", c.user_id)
        c.dead = True
        asyncio.create_task(_close(c))


async def _close(c: Conn, code: int = 1013) -> None:
    c.dead = True
    try:
        await c.ws.close(code=code)
    except Exception:
        pass


async def writer(c: Conn) -> None:
    """Sendet die Queue der Verbindung der Reihe nach; Ping bei Leerlauf."""
    try:
        while True:
            try:
                data = await asyncio.wait_for(c.q.get(), timeout=PING_SECONDS)
            except asyncio.TimeoutError:
                data = '{"type":"ping"}'
            await asyncio.wait_for(c.ws.send_text(data), timeout=5)
    except Exception:
        c.dead = True


def broadcast(room: Room, msg: dict, exclude: Optional[Conn] = None, force: bool = False) -> None:
    for c in list(room.conns):
        if c is exclude or not room.visible_to(c, force):
            continue
        enqueue(c, msg)


# ─── Presence ───────────────────────────────────────────────────────

def presence_list(room: Room) -> list[dict]:
    """Aktive, live-freigegebene Tutoren (sichtbarer Tab) mit aktueller Position; je User
    der jüngste Eintrag (mehrere Tabs desselben Users → ein Dropdown-Eintrag)."""
    best: dict[int, Conn] = {}
    for c in room.conns:
        if not c.tutor or not c.live or not c.pres or not c.pres.get("active"):
            continue
        cur = best.get(c.user_id)
        if cur is None or c.pres["ts"] > cur.pres["ts"]:
            best[c.user_id] = c
    out = []
    for c in best.values():
        p = c.pres
        out.append({"cid": c.id, "uid": c.user_id, "name": c.name, "slide": p["slide"], "f": p["f"],
                    "mode": p["mode"], "board": p["board"], "ts": p["ts"]})
    out.sort(key=lambda e: -e["ts"])
    return out


def push_presence(room: Room) -> None:
    broadcast(room, {"type": "presence", "tutors": presence_list(room)}, force=True)


# ─── Join / Leave ───────────────────────────────────────────────────

def join(room: Room, c: Conn) -> None:
    room.conns.add(c)
    c.writer = asyncio.create_task(writer(c))
    enqueue(c, {"type": "hello", "shared": room.shared, "tutor": c.tutor, "uid": c.user_id, "cid": c.id})
    enqueue(c, {"type": "presence", "tutors": presence_list(room)})


def leave(room: Room, c: Conn) -> None:
    room.conns.discard(c)
    c.dead = True
    if c.writer:
        c.writer.cancel()
    if c.tutor and c.live:
        push_presence(room)
    if not room.conns:
        _rooms.pop(room.deck_id, None)
