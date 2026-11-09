"""
Emoji-Reaktionen auf Nachrichten (Forum + DM, gemeinsame Tabelle
message_reactions, s. database/models.py).

Eine Reaktion = (kind, message_id, user_id, emoji), eindeutig. Erneut
auf dasselbe Emoji reagieren entfernt die eigene Reaktion (Toggle).
Serialisiert wird pro Nachricht eine Liste (nach Häufigkeit sortiert):

    [{"emoji", "count", "mine", "users": [{"id", "name"}, …]}, …]

„mine" ist aus Sicht des anfragenden Users; „users" für den Tooltip
(wer reagiert hat).
"""

from typing import Any

from sqlmodel import Session, select

from database.models import MessageReaction, User

KIND_FORUM = "forum"
KIND_DM = "dm"


def load_reactions(session: Session, kind: str, message_ids: list[int], viewer_id: int) -> dict[int, list[dict[str, Any]]]:
    """Reaktionen mehrerer Nachrichten in einem Query (ohne N+1).

    Rückgabe: message_id → serialisierte Liste (fehlende IDs nicht enthalten).
    """
    if not message_ids:
        return {}
    rows = session.exec(
        select(MessageReaction.message_id, MessageReaction.emoji, MessageReaction.user_id, User.name)
        .join(User, MessageReaction.user_id == User.id)
        .where(MessageReaction.kind == kind)
        .where(MessageReaction.message_id.in_(message_ids))  # type: ignore[attr-defined]
    ).all()

    grouped: dict[int, dict[str, list[tuple[int, str]]]] = {}
    for message_id, emoji, user_id, name in rows:
        grouped.setdefault(message_id, {}).setdefault(emoji, []).append((user_id, name))

    out: dict[int, list[dict[str, Any]]] = {}
    for message_id, emojis in grouped.items():
        items = [
            {
                "emoji": emoji,
                "count": len(users),
                "mine": any(uid == viewer_id for uid, _ in users),
                "users": [{"id": uid, "name": name} for uid, name in users],
            }
            for emoji, users in emojis.items()
        ]
        items.sort(key=lambda r: (-r["count"], r["emoji"]))
        out[message_id] = items
    return out


def toggle_reaction(
    session: Session, kind: str, message_id: int, user_id: int, emoji: str
) -> list[dict[str, Any]]:
    """Eigene Reaktion auf ein Emoji setzen oder entfernen (je nach Vorhandensein).

    Der Commit bleibt beim Aufrufer; zurückgegeben wird die neue
    Reaktionsliste der Nachricht (aus Sicht des reagierenden Users).
    """
    existing = session.exec(
        select(MessageReaction)
        .where(MessageReaction.kind == kind)
        .where(MessageReaction.message_id == message_id)
        .where(MessageReaction.user_id == user_id)
        .where(MessageReaction.emoji == emoji)
    ).first()
    if existing:
        session.delete(existing)
    else:
        session.add(
            MessageReaction(kind=kind, message_id=message_id, user_id=user_id, emoji=emoji)  # type: ignore[arg-type]
        )
    session.flush()
    return load_reactions(session, kind, [message_id], user_id).get(message_id, [])


def delete_reactions_for_messages(session: Session, kind: str, message_ids: list[int]) -> None:
    """Reaktionen löschen, wenn ihre Nachrichten wegfallen (Commit beim Aufrufer)."""
    if not message_ids:
        return
    for r in session.exec(
        select(MessageReaction)
        .where(MessageReaction.kind == kind)
        .where(MessageReaction.message_id.in_(message_ids))  # type: ignore[attr-defined]
    ).all():
        session.delete(r)
