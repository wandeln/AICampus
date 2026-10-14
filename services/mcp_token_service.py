"""
MCP-Token: Erzeugung, Speicherung (nur SHA-256-Hash), Verifikation.

Ein Token pro User, mit Kurs-Allowlist (McpTokenCourse). Das Klartext-Token
(Format `aic_<64 hex>`) wird einmalig beim Anzeigen zurückgegeben und nie
persistiert — in der DB liegt nur der SHA-256-Hash (Lookup-Index), analog
GitHub-PATs. Hohe Entropie macht Brute-Force irrelevant, ein zusätzlicher
Salt wäre für den Hash-Lookup kontraproduktiv.
"""

import hashlib
import secrets
from datetime import datetime
from typing import Optional, Tuple

from sqlmodel import Session, select

from database.models import McpToken


def generate_token() -> Tuple[str, str]:
    """Erzeugt (klartext_token, sha256_hex). Klartext nur einmal zurückgeben!"""
    raw = "aic_" + secrets.token_hex(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_bearer(authorization: Optional[str]) -> Optional[str]:
    """'Bearer aic_...' → Token, sonst None."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    raw = parts[1].strip()
    return raw if raw.startswith("aic_") else None


def verify_token(session: Session, raw: str) -> Optional[McpToken]:
    """Token-Hash → gültiges McpToken (existiert, nicht abgelaufen), sonst None.

    Nebeneffekt: last_used_at aktualisieren (max. 1×/Min, KISS-Guard).
    """
    token = session.exec(
        select(McpToken).where(McpToken.token_hash == hash_token(raw))
    ).first()
    if not token or not token.user_id:
        return None
    if token.expires_at and token.expires_at < datetime.now():
        return None
    if token.last_used_at is None or (datetime.now() - token.last_used_at).total_seconds() > 60:
        token.last_used_at = datetime.now()
        session.add(token)
        session.commit()
    return token
