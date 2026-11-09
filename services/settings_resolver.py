"""
Settings-Resolver: Bestimmt die effektive Konfiguration fuer LLM und LDAP.

Prioritaet ( hoechste → tiefste ):
    1. course_settings   (Kurs-Override)
    2. global_settings    (Admin-Dashboard Systemeinstellungen)
    3. .env / Umgebungsvariablen
    4. config.py Default  (hardcoded Fallback)
"""

import json
from typing import Optional
from sqlmodel import Session, select

from config import (
    LLM_API_URL, LLM_API_KEY, LLM_MODEL,
    LLM_API_URL_PUBLIC, LLM_API_KEY_PUBLIC, LLM_MODEL_PUBLIC,
    LLM_IMAGE_API_URL, LLM_IMAGE_API_KEY, LLM_IMAGE_MODEL,
    LDAP_ENABLED, LDAP_SERVER, LDAP_BASE_DN,
    LDAP_BIND_DN, LDAP_BIND_PW, LDAP_USER_SEARCH,
    COMPUTE_AGENT_URL, COMPUTE_AGENT_KEY,
)
from database.models import GlobalSettings, CourseSettings


# ─── LLM Config ──────────────────────────────────────────────────

def get_effective_llm_config(
    session: Session,
    course_id: Optional[int] = None,
) -> dict:
    """
    Gibt die effektive LLM-Konfiguration zurueck.

    Returns:
        {
            "api_url": str,
            "api_key": str,
            "model": str,
            "grading_prompt": str | None,
            "source": "course" | "global" | "env",
            # Public Endpoint (nicht-sensitive Aufgaben)
            "api_url_public": str,
            "api_key_public": str,
            "model_public": str,
            # Image-Endpoint (Bilder erstellen/editieren; nur global, kein Kurs-Override)
            "api_url_image": str,
            "api_key_image": str,
            "model_image": str,
        }
    """
    # Start: config.py Defaults (.env + hardcoded)
    api_url = LLM_API_URL
    api_key = LLM_API_KEY
    model = LLM_MODEL
    api_url_public = LLM_API_URL_PUBLIC
    api_key_public = LLM_API_KEY_PUBLIC
    model_public = LLM_MODEL_PUBLIC
    api_url_image = LLM_IMAGE_API_URL
    api_key_image = LLM_IMAGE_API_KEY
    model_image = LLM_IMAGE_MODEL
    grading_prompt = None
    source = "env"

    # Ebene 2: global_settings
    gs = session.exec(select(GlobalSettings)).first()
    if gs:
        if gs.llm_api_url:
            api_url = gs.llm_api_url
            source = "global"
        if gs.llm_api_key:
            api_key = gs.llm_api_key
        if gs.llm_model:
            model = gs.llm_model
        if gs.grading_prompt:
            grading_prompt = gs.grading_prompt
        if gs.llm_api_url_public:
            api_url_public = gs.llm_api_url_public
        if gs.llm_api_key_public:
            api_key_public = gs.llm_api_key_public
        if gs.llm_model_public:
            model_public = gs.llm_model_public
        if gs.llm_image_api_url:
            api_url_image = gs.llm_image_api_url
        if gs.llm_image_api_key:
            api_key_image = gs.llm_image_api_key
        if gs.llm_image_model:
            model_image = gs.llm_image_model

    # Ebene 1: course_settings (Override)
    if course_id:
        cs = session.exec(
            select(CourseSettings).where(CourseSettings.course_id == course_id)
        ).first()
        if cs:
            if cs.llm_api_url:
                api_url = cs.llm_api_url
                source = "course"
            if cs.llm_model:
                model = cs.llm_model
            if cs.grading_prompt:
                grading_prompt = cs.grading_prompt

    return {
        "api_url": api_url,
        "api_key": api_key,
        "model": model,
        "grading_prompt": grading_prompt,
        "source": source,
        # Public Endpoint (leer = Fallback auf Private Endpoint)
        "api_url_public": api_url_public,
        "api_key_public": api_key_public,
        "model_public": model_public,
        # Image-Endpoint (Bilder erstellen/editieren)
        "api_url_image": api_url_image,
        "api_key_image": api_key_image,
        "model_image": model_image,
    }


def _parse_agent_list(raw) -> list[dict]:
    """JSON-Agent-Liste parsen (ungültig/leer → [])."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [a for a in parsed if isinstance(a, dict) and a.get("url")]


def _merge_agents(base: list[dict], override: list[dict]) -> list[dict]:
    """Engine-Listen vereinigen (Merge statt Replace): Override-Engines
    ersetzen bei Namensgleichheit die Base-Engine an derselben Position,
    neue Namen werden angehängt. Basis: global/.env, Override: Kurs —
    so bleiben globale Engines auch für Kurse verfügbar."""
    def _name(a: dict) -> str:
        return str(a.get("name") or a.get("url") or "")

    merged = list(base)
    for o in override:
        target = next((b for b in merged if _name(b) == _name(o)), None)
        if target is not None:
            merged[merged.index(target)] = o
        else:
            merged.append(o)
    return merged


# ─── Compute Config (Workspace-Aufgaben) ─────────────────────────

def get_effective_compute_config(
    session: Session,
    course_id: Optional[int] = None,
) -> dict:
    """
    Effektive Compute-Konfiguration (per-Student-Docker-Container).

    Engines werden gemergt: globale Engines (Admin) sind immer verfügbar,
    Kurs-Engines ergänzen sie; bei Namenskonflikt hat der Kurs Vorrang.
    .env-Local-Engine gilt nur, solange keine globalen Engines gesetzt sind.
    Jeder Agent trägt sein Herkunftsfeld `scope` ("global" | "course" | "env")
    für die UI (read-only-Anzeige).

    Workspace-Aufgaben sind immer verfügbar (kein Feature-Flag mehr);
    ohne erreichbare Engine degradieren die Views sauber.

    Returns:
        {
            "enabled": bool,        # immer True (kompatibel für alte Aufrufer)
            "gpu_enabled": bool,    # Legacy (UI/Routing ignoriert es)
            "agents": list[dict],   # roh (name,url,key,gpus,scope) — Normalisierung in workspace_service
            "source": "course" | "global" | "env",   # höchste beteiligte Schicht
        }
    """
    enabled = True
    gpu_enabled = False
    agents: list[dict] = []
    source = "env"

    gs = session.exec(select(GlobalSettings)).first()
    if gs:
        gpu_enabled = bool(getattr(gs, "workspace_gpu_enabled", False))
        agents = _parse_agent_list(getattr(gs, "compute_agents", None))
        for a in agents:
            a["scope"] = "global"
        if agents:
            source = "global"

    if not agents:
        agents = [{"name": "local", "url": COMPUTE_AGENT_URL,
                   "key": COMPUTE_AGENT_KEY, "gpu": False, "scope": "env"}]

    if course_id:
        cs = session.exec(
            select(CourseSettings).where(CourseSettings.course_id == course_id)
        ).first()
        if cs:
            if cs.compute_gpu_enabled is not None:
                gpu_enabled = bool(cs.compute_gpu_enabled)
            course_agents = _parse_agent_list(cs.compute_agents)
            if course_agents:
                for a in course_agents:
                    a["scope"] = "course"
                agents = _merge_agents(agents, course_agents)
                source = "course"

    return {
        "enabled": enabled,
        "gpu_enabled": gpu_enabled,
        "agents": agents,
        "source": source,
    }


# ─── LDAP Config ─────────────────────────────────────────────────

def get_effective_ldap_config(
    session: Session,
    course_id: Optional[int] = None,
) -> dict:
    """
    Gibt die effektive LDAP-Konfiguration zurueck.

    Returns:
        {
            "use_ldap": bool,
            "ldap_server": str | None,
            "ldap_base_dn": str | None,
            "ldap_bind_dn": str | None,
            "ldap_bind_pw": str | None,
            "ldap_user_search": str | None,
        }
    """
    # Start: config.py Defaults (.env + hardcoded)
    use_ldap = LDAP_ENABLED
    server = LDAP_SERVER if LDAP_ENABLED else None
    base_dn = LDAP_BASE_DN if LDAP_ENABLED else None
    bind_dn = LDAP_BIND_DN if LDAP_ENABLED else None
    bind_pw = LDAP_BIND_PW if LDAP_ENABLED else None
    user_search = LDAP_USER_SEARCH if LDAP_ENABLED else None

    # Ebene 2: global_settings
    gs = session.exec(select(GlobalSettings)).first()
    if gs:
        if gs.use_ldap:
            use_ldap = True
            if gs.ldap_server:
                server = gs.ldap_server
            if gs.ldap_base_dn:
                base_dn = gs.ldap_base_dn
            if gs.ldap_bind_dn:
                bind_dn = gs.ldap_bind_dn
            if gs.ldap_bind_pw is not None:
                bind_pw = gs.ldap_bind_pw
            if gs.ldap_user_search:
                user_search = gs.ldap_user_search

    # Ebene 1: course_settings (Override)
    if course_id:
        cs = session.exec(
            select(CourseSettings).where(CourseSettings.course_id == course_id)
        ).first()
        if cs:
            if cs.use_ldap:
                use_ldap = True
            if cs.ldap_server:
                server = cs.ldap_server
            if cs.ldap_base_dn:
                base_dn = cs.ldap_base_dn
            if cs.ldap_bind_dn:
                bind_dn = cs.ldap_bind_dn
            if cs.ldap_bind_pw is not None:
                bind_pw = cs.ldap_bind_pw
            if cs.ldap_user_search:
                user_search = cs.ldap_user_search

    return {
        "use_ldap": use_ldap,
        "ldap_server": server,
        "ldap_base_dn": base_dn,
        "ldap_bind_dn": bind_dn,
        "ldap_bind_pw": bind_pw,
        "ldap_user_search": user_search,
    }