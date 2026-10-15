"""
MCP-Token verwalten (User-Settings).

- GET    /api/mcp-tokens        → Eigene Tokens (inkl. erlaubter Kurse)
- POST   /api/mcp-tokens        → Token anlegen (Name + Kurs-Allowlist)
- PUT    /api/mcp-tokens/{id}   → Name / Kurs-Allowlist ändern
- DELETE /api/mcp-tokens/{id}   → Token widerrufen

Nur der jeweilige Token-Besitzer kann seine Tokens sehen/ändern.
Das Klartext-Token wird NUR bei der Erzeugung zurückgegeben.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from database.base import get_session
from database.models import Course, McpToken, McpTokenCourse, User, UserCourse
from services.auth_service import get_current_user
from services import mcp_token_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp-tokens", tags=["MCP"])


# ─── Schemas ──────────────────────────────────────────────────────

class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    course_ids: list[int] = Field(min_length=1)
    allow_write: bool = True  # Write-Tools erlauben (Rolle Tutor/Prof begrenzt zusätzlich)


class TokenUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    course_ids: list[int] | None = Field(default=None, min_length=1)
    allow_write: bool | None = None


def _my_course_ids(session: Session, user: User) -> set[int]:
    return {
        m.course_id
        for m in session.exec(
            select(UserCourse).where(UserCourse.user_id == user.id)
        ).all()
    }


def _get_own_token(session: Session, user: User, token_id: int) -> McpToken:
    token = session.get(McpToken, token_id)
    if not token or token.user_id != user.id:
        raise HTTPException(404, "Token nicht gefunden.")
    return token


def _validate_courses(session: Session, user: User, course_ids: list[int]) -> list[int]:
    """course_ids müssen eigene Kurse sein (Dedupe + Reihenfolge erhalten)."""
    mine = _my_course_ids(session, user)
    seen: list[int] = []
    for cid in course_ids:
        if cid not in seen and cid in mine:
            seen.append(cid)
    if len(seen) != len(set(course_ids)):
        raise HTTPException(
            400, "Alle ausgewählten Kurse müssen deine eigenen Kurse sein."
        )
    return seen


def _token_dict(session: Session, token: McpToken) -> dict:
    course_ids = sorted(tc.course_id for tc in token.courses)
    courses = []
    for cid in course_ids:
        course = session.get(Course, cid)
        if course:
            courses.append({"id": course.id, "name": course.name, "semester": course.semester})
    return {
        "id": token.id,
        "name": token.name,
        "courses": courses,
        "allow_write": token.allow_write is not False,
        "created_at": token.created_at.isoformat() if token.created_at else None,
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
    }


# ─── Endpoints ────────────────────────────────────────────────────

@router.get("/")
async def list_tokens(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Eigene MCP-Tokens (ohne Klartext — der ist nie wieder lesbar)."""
    tokens = session.exec(
        select(McpToken).where(McpToken.user_id == user.id).order_by(McpToken.created_at.desc())  # type: ignore[attr-defined]
    ).all()
    return {"tokens": [_token_dict(session, t) for t in tokens]}


@router.post("/")
async def create_token(
    data: TokenCreate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Neues Token anlegen. Gibt das Klartext-Token EINMALS zurück."""
    course_ids = _validate_courses(session, user, data.course_ids)

    raw, token_hash = mcp_token_service.generate_token()
    token = McpToken(
        user_id=user.id,  # type: ignore[arg-type]
        name=data.name.strip(),
        token_hash=token_hash,
        allow_write=data.allow_write,
    )
    session.add(token)
    session.flush()  # token.id nötig
    for cid in course_ids:
        session.add(McpTokenCourse(token_id=token.id, course_id=cid))  # type: ignore[arg-type]
    session.commit()
    session.refresh(token)

    logger.info("[MCP] User '%s' hat Token '%s' angelegen (Kurse: %s).",
                user.username, token.name, course_ids)
    return {
        "message": f"Token '{token.name}' wurde erstellt.",
        "token": raw,
        "mcp_token": _token_dict(session, token),
    }


@router.put("/{token_id}")
async def update_token(
    token_id: int,
    data: TokenUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Name und/oder Kurs-Allowlist eines eigenen Tokens ändern."""
    token = _get_own_token(session, user, token_id)

    if data.name is not None:
        token.name = data.name.strip()
    if data.allow_write is not None:
        token.allow_write = data.allow_write
    if data.course_ids is not None:
        course_ids = _validate_courses(session, user, data.course_ids)
        for tc in list(token.courses):
            session.delete(tc)
        # Collection bereinigen: die deleteden Instanzen durften nicht darin
        # verbleiben, sonst crash der save-update-Cascade beim session.add(token)
        # (InvalidRequestError) — s. PUT /api/mcp-tokens/{id} 500er.
        token.courses.clear()
        session.flush()
        for cid in course_ids:
            session.add(McpTokenCourse(token_id=token.id, course_id=cid))  # type: ignore[arg-type]

    session.add(token)
    session.commit()
    session.refresh(token)
    return {"message": "Token wurde aktualisiert.", "mcp_token": _token_dict(session, token)}


@router.delete("/{token_id}")
async def revoke_token(
    token_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Eigenes Token widerrufen (Allowlist-Zeilen mit-cascaden)."""
    token = _get_own_token(session, user, token_id)
    name = token.name
    for tc in list(token.courses):
        session.delete(tc)
    session.delete(token)
    session.commit()
    logger.info("[MCP] User '%s' hat Token '%s' widerrufen.", user.username, name)
    return {"message": f"Token '{name}' wurde widerrufen."}
