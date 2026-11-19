"""
Folien-Index + Notizen-Persistenz (Stift/Whiteboard).

Notizen hängen an einer stabilen ``slide_uid`` statt am Folien-Index. Die
Zuordnung folge→uid führt der Server pro Deck (``SlideIndex``) und gleicht sie
über Inhalts-Hashes der Blatt-Folien ab (kein ``id:`` im Markdown):

  verschoben → Notizen wandern mit · kopiert → Notizen werden geklont ·
  eingefügt/gelöscht → andere unberührt · editiert → Notizen entfallen
  (bleiben aber ≤ 30 Tage als Orphan und kommen zurück, wenn derselbe
  Inhalt wieder auftaucht: Edit-Undo / Version-Restore).

Der Abgleich läuft lazy (``get_slide_uids``) bei jedem Zugriff, egal über
welchen Speicherpfad (Autosave, MCP, LLM, Import, Restore) der Inhalt kam.

Siehe docs/LLM-plans/slide-annotations-live-share.md.
"""

import difflib
import hashlib
import json
import re
import secrets
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, delete, select

from database.models import (
    CourseMaterial,
    SlideAnnotation,
    SlideAnnotationShare,
    SlideIndex,
)
from services.slides_service import _DIRECTIVE, _split_slides, _split_vertical

ORPHAN_DAYS = 30
WB_UID = "_wb"  # Pseudo-Folie des globalen Whiteboards: mode 1, f = Seitennummer (0-basiert)
MAX_EVENTS_PER_OP = 600
MAX_EVENTS_PER_ROW = 20000
EVENT_TYPES = {"draw", "erase", "strokeend", "clear"}
UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)


# ─── Hashing ────────────────────────────────────────────────────────

def leaf_hashes(content: str) -> list[str]:
    """Inhalts-Hash je Blatt-Folie in Reveal-Reihenfolge (``--`` einzeln).

    Fehlertolerant (lenient Splitter, kein striktes parse_slides). Ohne
    ``notes:``/``transition:`` und whitespace-normalisiert — beides ändert nicht,
    was gezeichnet wurde."""
    if not content or not content.strip():
        return []
    out: list[str] = []
    for block in _split_slides(content):
        for seg in _split_vertical(block):
            lines = seg.splitlines()
            n = len(lines)
            i = 0
            while i < n and not lines[i].strip():
                i += 1
            kept: list[str] = []
            while i < n:
                m = _DIRECTIVE.match(lines[i])
                if not m:
                    break
                if m.group(1) not in ("notes", "transition"):
                    kept.append(f"{m.group(1)}: {m.group(2).strip()}")
                i += 1
            kept.extend(l.rstrip() for l in lines[i:])
            text = "\n".join(kept).strip()
            out.append(hashlib.sha1(text.encode("utf-8")).hexdigest())
    return out


def _new_uid() -> str:
    return secrets.token_urlsafe(6).replace("_", "x").replace("-", "y")[:8]


# ─── Abgleich (rein, ohne DB — testbar) ─────────────────────────────

def reconcile(old_uids: list[str], old_hashes: list[str], new_hashes: list[str],
              orphans: list[dict]) -> tuple[list[str], list[tuple[str, str]], list[tuple[str, str]], list[str]]:
    """Ordnet neuen Folien uids zu.

    Rückgabe: (new_uids, clones[(src_uid, new_uid)], dropped[(uid, hash)], revived[uid])
    ``dropped`` = alte Folien ohne Gegenstück (werden ggf. Orphans); ``revived`` =
    Orphan-uids, die wieder zugeordnet wurden."""
    n = len(new_hashes)
    new_uids: list[Optional[str]] = [None] * n
    used_old: set[int] = set()

    # 1) gleiche Blöcke (deckt Einfügen/Löschen; identische Folien paaren sich positionstreu)
    sm = difflib.SequenceMatcher(None, old_hashes, new_hashes, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                new_uids[j1 + k] = old_uids[i1 + k]
                used_old.add(i1 + k)

    # 2) verschoben: übrige neue Folie == übrige alte Folie
    pool: dict[str, deque] = defaultdict(deque)
    for i, h in enumerate(old_hashes):
        if i not in used_old:
            pool[h].append(i)
    for j in range(n):
        if new_uids[j] is None and pool.get(new_hashes[j]):
            i = pool[new_hashes[j]].popleft()
            new_uids[j] = old_uids[i]
            used_old.add(i)

    # 3) kopiert / wiederbelebt / neu (in Reihenfolge, damit Kopien von Kopien klappen)
    orphan_pool: dict[str, deque] = defaultdict(deque)
    for o in orphans:
        orphan_pool[o["hash"]].append(o["uid"])
    clones: list[tuple[str, str]] = []
    revived: list[str] = []
    for j in range(n):
        if new_uids[j] is not None:
            continue
        h = new_hashes[j]
        src = next((new_uids[k] for k in range(j - 1, -1, -1) if new_hashes[k] == h and new_uids[k]), None)
        if src is None:
            src = next((new_uids[k] for k in range(j + 1, n) if new_hashes[k] == h and new_uids[k]), None)
        if src is not None:
            nu = _new_uid()
            clones.append((src, nu))
            new_uids[j] = nu
        elif orphan_pool.get(h):
            nu = orphan_pool[h].popleft()
            revived.append(nu)
            new_uids[j] = nu
        else:
            new_uids[j] = _new_uid()

    dropped = [(old_uids[i], old_hashes[i]) for i in range(len(old_uids)) if i not in used_old]
    return [u for u in new_uids if u is not None], clones, dropped, revived


# ─── Index (DB) ─────────────────────────────────────────────────────

def get_slide_uids(session: Session, deck: CourseMaterial) -> list[str]:
    """uids der Blatt-Folien des Decks (gleiche Reihenfolge wie Reveal);
    gleicht den Index bei geändertem Inhalt zuerst ab."""
    new_hashes = leaf_hashes(deck.content)
    idx = session.get(SlideIndex, deck.id)
    if idx is not None and idx.hashes == new_hashes:
        return list(idx.uids)

    with _locks[deck.id]:
        idx = session.get(SlideIndex, deck.id, populate_existing=True)
        if idx is not None and idx.hashes == new_hashes:
            return list(idx.uids)
        now = datetime.now()
        if idx is None:
            idx = SlideIndex(deck_id=deck.id, uids=[], hashes=[], orphans=[])
        new_uids, clones, dropped, revived = reconcile(idx.uids, idx.hashes, new_hashes, idx.orphans)

        # Kopien: Zeilen der Quelle klonen
        for src, dst in clones:
            for row in session.exec(select(SlideAnnotation).where(
                    SlideAnnotation.deck_id == deck.id, SlideAnnotation.slide_uid == src)).all():
                session.add(SlideAnnotation(
                    deck_id=deck.id, mode=row.mode, slide_uid=dst, f=row.f,
                    events=json.loads(json.dumps(row.events)), rev=1, updated_at=now))

        # Orphans: bestehende (ohne wiederbelebte, ohne abgelaufene) + neu verwaiste
        cutoff = now - timedelta(days=ORPHAN_DAYS)
        keep: list[dict] = []
        expired: list[str] = []
        for o in idx.orphans:
            if o["uid"] in revived:
                continue
            if datetime.fromisoformat(o["at"]) < cutoff:
                expired.append(o["uid"])
            else:
                keep.append(o)
        live = set(new_uids)
        for uid, h in dropped:
            if uid in live:
                continue
            has_rows = session.exec(select(SlideAnnotation.id).where(
                SlideAnnotation.deck_id == deck.id, SlideAnnotation.slide_uid == uid)).first()
            if has_rows is not None:
                keep.append({"uid": uid, "hash": h, "at": now.isoformat()})
        if expired:
            session.exec(delete(SlideAnnotation).where(
                SlideAnnotation.deck_id == deck.id, SlideAnnotation.slide_uid.in_(expired)))  # type: ignore[attr-defined]

        idx.uids = new_uids
        idx.hashes = new_hashes
        idx.orphans = keep
        session.add(idx)
        session.commit()
        return list(new_uids)


# ─── Freigabe ───────────────────────────────────────────────────────

def is_shared(session: Session, deck_id: int) -> bool:
    row = session.get(SlideAnnotationShare, deck_id)
    return bool(row and row.shared)


def set_shared(session: Session, deck_id: int, shared: bool, user_id: Optional[int]) -> None:
    row = session.get(SlideAnnotationShare, deck_id) or SlideAnnotationShare(deck_id=deck_id)
    row.shared = shared
    row.updated_at = datetime.now()
    row.updated_by = user_id
    session.add(row)
    session.commit()


# ─── Notizen laden / ändern ─────────────────────────────────────────

def row_dict(r: SlideAnnotation) -> dict:
    return {"uid": r.slide_uid, "mode": r.mode, "f": r.f, "rev": r.rev, "events": r.events}


def load_all(session: Session, deck: CourseMaterial) -> list[dict]:
    """Alle Zeilen der aktuell vorhandenen Folien + des globalen Whiteboards (Orphans ausgenommen)."""
    uids = get_slide_uids(session, deck) + [WB_UID]
    rows = session.exec(select(SlideAnnotation).where(
        SlideAnnotation.deck_id == deck.id, SlideAnnotation.slide_uid.in_(uids))).all()  # type: ignore[attr-defined]
    return [row_dict(r) for r in rows]


def load_slide(session: Session, deck_id: int, uid: str) -> list[dict]:
    rows = session.exec(select(SlideAnnotation).where(
        SlideAnnotation.deck_id == deck_id, SlideAnnotation.slide_uid == uid)).all()
    return [row_dict(r) for r in rows]


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < 1e7


def clean_event(e) -> Optional[dict]:
    """Whitelist + Typprüfung eines Chalkboard-Events (Server vertraut dem Client nicht)."""
    if not isinstance(e, dict) or e.get("type") not in EVENT_TYPES:
        return None
    t = e["type"]
    out: dict = {"type": t}
    if t == "draw":
        if not all(_num(e.get(k)) for k in ("x1", "y1", "x2", "y2")) or not isinstance(e.get("color"), int):
            return None
        out.update({k: e[k] for k in ("x1", "y1", "x2", "y2", "color")})
    elif t == "erase":
        if not (_num(e.get("x")) and _num(e.get("y"))):
            return None
        out.update({"x": e["x"], "y": e["y"]})
    if isinstance(e.get("board"), int) and 0 <= e["board"] < 1000:
        out["board"] = e["board"]
    sid = e.get("sid")
    if isinstance(sid, str) and len(sid) <= 40:
        out["sid"] = sid
    if _num(e.get("time")):
        out["time"] = e["time"]
    return out


def _last_group(events: list) -> Optional[str]:
    """sid der zuletzt ABGESCHLOSSENEN Gruppe (strokeend/clear) — ein gerade laufender
    Strich eines anderen Dozenten wird so nicht mitten im Zeichnen entfernt."""
    for e in reversed(events):
        if e.get("type") in ("strokeend", "clear") and e.get("sid"):
            return e["sid"]
    return events[-1].get("sid") if events else None


def _what(group: list) -> str:
    """Art einer Gruppe für den Hinweis „… von X rückgängig“: Zeichnung | Radierung | Löschen."""
    types = {e.get("type") for e in group}
    return "Zeichnung" if "draw" in types else "Radierung" if "erase" in types else "Löschen"


def apply_op(session: Session, deck_id: int, op: dict, redo: dict) -> Optional[dict]:
    """Wendet einen Client-Op an und persistiert ihn.

    kind: append (events) | undo | redo | reset_all. Undo/Redo bilden EINE gemeinsame
    History je Zeile (Folie/Fragment/Whiteboard-Seite) für alle Dozenten; ``redo`` ist
    der flüchtige Redo-Puffer des Raums ({(uid, mode, f): [Gruppen]}), jedes append leert ihn.
    Rückgabe: bereinigter Op inkl. ``rev`` (bei reset_all ohne rev; undo → kind "remove",
    redo → kind "append") oder None bei ungültigem Op / nichts zu tun.
    Aufrufer serialisiert pro Deck (asyncio.Lock)."""
    kind = op.get("kind")
    if kind == "reset_all":
        session.exec(delete(SlideAnnotation).where(SlideAnnotation.deck_id == deck_id))
        session.commit()
        redo.clear()
        return {"kind": "reset_all"}

    uid, mode, f = op.get("slide"), op.get("mode"), op.get("f", -1)
    if not (isinstance(uid, str) and UID_RE.match(uid) and mode in (0, 1)
            and isinstance(f, int) and -1 <= f < 1000):
        return None
    row = session.exec(select(SlideAnnotation).where(
        SlideAnnotation.deck_id == deck_id, SlideAnnotation.mode == mode,
        SlideAnnotation.slide_uid == uid, SlideAnnotation.f == f)).first()
    key = (uid, mode, f)

    if kind == "append":
        raw = op.get("events")
        if not isinstance(raw, list) or not raw or len(raw) > MAX_EVENTS_PER_OP:
            return None
        events = [ev for ev in (clean_event(e) for e in raw) if ev]
        if not events:
            return None
        if row is None:
            row = SlideAnnotation(deck_id=deck_id, mode=mode, slide_uid=uid, f=f, events=[], rev=0)
        if len(row.events) + len(events) > MAX_EVENTS_PER_ROW:
            return None
        row.events = list(row.events) + events
        redo.pop(key, None)                       # neue Striche → Redo verfällt
        out_kind, out_extra = "append", {"events": events}
    elif kind == "undo":
        sid = _last_group(row.events) if row else None
        if sid is None:
            return None
        gone = [e for e in row.events if e.get("sid") == sid]
        row.events = [e for e in row.events if e.get("sid") != sid]
        stack = redo.setdefault(key, [])
        stack.append(gone)
        del stack[:-100]
        out_kind, out_extra = "remove", {"sids": [sid], "what": _what(gone)}
    elif kind == "redo":
        stack = redo.get(key)
        if row is None or not stack:
            return None
        gone = stack.pop()
        row.events = list(row.events) + gone
        out_kind, out_extra = "append", {"events": gone, "what": _what(gone)}
    else:
        return None

    row.rev += 1
    row.updated_at = datetime.now()
    session.add(row)
    session.commit()
    return {"kind": out_kind, "slide": uid, "mode": mode, "f": f, "rev": row.rev, **out_extra}
