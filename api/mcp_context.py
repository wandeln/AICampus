"""
MCP-Client-Kontext: Der Browser des Users meldet seinen aktuellen
UI-Zustand (offene Seite, markierter Text, aktuelles Kapitel/Folie) —
das MCP-Tool `get_user_focus` (api/mcp_server.py) nutzt ihn.

- POST /api/mcp/client-context → Upsert (Session-Auth, eigene Zustände)

Last-write-wins: Bei mehreren Tabs zählt der zuletzt aktive
(versteckte Tabs melden nicht — s. static/js/mcp-context.js).
Nur nicht-sensible UI-Zustände; kein Logging des Inhalts
(s. docs/LLM-plans/mcp-client-context.md).
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session

from database.base import get_session
from database.models import McpClientContext, User
from services.auth_service import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["MCP"])


class ClientContextIn(BaseModel):
    url: str = Field(max_length=500)
    selection: str | None = Field(default=None, max_length=1000)
    selection_context: str | None = Field(default=None, max_length=2000)
    selection_offset: int | None = Field(default=None, ge=0, le=2000)
    section_id: int | None = Field(default=None, ge=1)
    slide: dict | None = None  # {"index": int, "count": int, "text": str}


def _clean(text: str | None, limit: int) -> str | None:
    return (text or "").strip()[:limit] or None


@router.post("/client-context")
async def report_client_context(
    data: ClientContextIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Aktuellen UI-Zustand des Users updaten (für MCP get_user_focus)."""
    slide_index: int | None = None
    slide_count: int | None = None
    slide_text: str | None = None
    if isinstance(data.slide, dict):
        raw_index, raw_count, raw_text = data.slide.get("index"), data.slide.get("count"), data.slide.get("text")
        if isinstance(raw_index, int) and not isinstance(raw_index, bool) and raw_index >= 0:
            slide_index = raw_index
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count > 0:
            slide_count = raw_count
        slide_text = _clean(raw_text, 2000)

    row = session.get(McpClientContext, user.id)
    if not row:
        row = McpClientContext(user_id=user.id)  # type: ignore[arg-type]
        session.add(row)
    row.url = (data.url or "").strip()[:500] or "/"
    row.selection = _clean(data.selection, 1000)
    row.selection_context = _clean(data.selection_context, 2000)
    row.selection_offset = data.selection_offset
    row.section_id = data.section_id
    row.slide_index = slide_index
    row.slide_count = slide_count
    row.slide_text = slide_text
    row.updated_at = datetime.now()
    session.commit()
    return {"ok": True}
