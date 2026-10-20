"""
Authentifizierungs-Service.

Unterstützt zwei Auth-Modi:
1. DB-Auth: Username + bcrypt-Hashed-Password (Default)
2. LDAP-Auth: Username + Passwort gegen LDAP-Server

JWT-Tokens werden in httpOnly-Cookies gespeichert.
"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status, Request
from sqlmodel import Session, select

from config import (
    SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES,
    LDAP_SERVER, LDAP_BASE_DN, LDAP_USER_SEARCH,
    LDAP_BIND_DN, LDAP_BIND_PW,
)
from database.base import get_session
from database.models import Course, User, GlobalUserRole, CourseRole, UserCourse
import hashlib
import secrets
import logging
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# ─── Current-User-Kontext (z. B. für das LLM-Debug-Log) ───────────
# Wird von den Auth-Dependencies gesetzt. Propagiert automatisch auf
# daraus gestartete asyncio-Tasks (Import-, Grading-Jobs, …), sodass jeder
# LLM-Call dem User zugeordnet werden kann, der ihn ausgelöst hat.
_current_user_id: ContextVar[Optional[int]] = ContextVar("current_user_id", default=None)


def set_current_user_id(user_id: Optional[int]) -> None:
    _current_user_id.set(user_id)


def get_current_user_id() -> Optional[int]:
    return _current_user_id.get()

# ─── Password Hashing (SHA-256 + salt, kein passlib nötig) ─────
def hash_password(password: str) -> str:
    """Hashes ein Password mit SHA-256 + random salt.
    Format: sha256${salt}${hash}
    """
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"sha256${salt}${hashed}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifiziert ein Password gegen den Hash."""
    try:
        _, salt, stored_hash = hashed_password.split("$")
    except ValueError:
        return False
    computed = hashlib.sha256((salt + plain_password).encode()).hexdigest()
    return secrets.compare_digest(computed, stored_hash)


# ─── JWT Token ──────────────────────────────────────────────────

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Erzeugt JWT-Token.
    
    Wichtig: 'sub' MUSS ein String sein (pyjwt >= 2.0 Requirement).
    """
    to_encode = data.copy()
    # pyjwt >= 2.0: 'sub' must be str
    if "sub" in to_encode and not isinstance(to_encode["sub"], str):
        to_encode["sub"] = str(to_encode["sub"])
    expire = datetime.now() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    import jwt
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """
    Decodiert JWT-Token.
    Gibt dict mit Payload zurück, oder None bei ungültigem Token.
    """
    import jwt
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        # 'sub' kommt als String zurück, konvertieren zu int
        if "sub" in payload:
            try:
                payload["sub"] = int(payload["sub"])
            except (ValueError, TypeError):
                pass
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.PyJWTError:
        return None


# ─── Login Logic ────────────────────────────────────────────────

async def authenticate_user(
    username: str,
    password: str,
    session: Session = Depends(get_session),
    use_ldap: bool = False,
    ldap_server: Optional[str] = None,
    ldap_base_dn: Optional[str] = None,
    ldap_bind_dn: Optional[str] = None,
    ldap_bind_pw: Optional[str] = None,
    ldap_user_search: Optional[str] = None,
) -> Optional[User]:
    """
    Authenticiert einen User via DB ODER LDAP.
    Gibt den User zurueck, oder None bei Fehler.

    Bei aktiviertem LDAP: Falls User nicht in DB existiert, wird er automatisch
    erstellt, wenn die LDAP-Auth erfolgreich ist (ohne password_hash, d.h. er
    muss sich danach immer per LDAP anmelden).
    """
    # 1. User aus DB laden
    statement = select(User).where(User.username == username)
    user = session.exec(statement).first()

    if not user:
        if use_ldap:
            # LDAP-Auth versuchen — falls erfolgreich, User in DB erstellen
            user_dn = try_ldap_auth(
                username, password,
                ldap_server=ldap_server,
                ldap_base_dn=ldap_base_dn,
                ldap_bind_dn=ldap_bind_dn,
                ldap_bind_pw=ldap_bind_pw,
                ldap_user_search=ldap_user_search,
            )
            if user_dn:
                logger.info(f"[Auth] Neuer LDAP-User '{username}' wird in DB angelegt.")
                user = User(
                    username=username,
                    email=f"{username}@ldap",
                    name=username,
                    role=GlobalUserRole.USER,
                    password_hash=None,
                    ldap_dn=user_dn,
                )
                session.add(user)
                session.commit()
                session.refresh(user)
                return user
            else:
                logger.warning(f"[Auth] LDAP-Auth fuer '{username}' fehlgeschlagen.")
                return None
        else:
            logger.warning(f"[Auth] User '{username}' nicht in DB gefunden.")
            return None

    # 2. Auth-Modus waehlen
    if use_ldap and not user.password_hash:
        # LDAP-User (kein DB-Password) -> nur LDAP
        user_dn = try_ldap_auth(
            user.username, password,
            ldap_server=ldap_server,
            ldap_base_dn=ldap_base_dn,
            ldap_bind_dn=ldap_bind_dn,
            ldap_bind_pw=ldap_bind_pw,
            ldap_user_search=ldap_user_search,
        )
        if user_dn and user.ldap_dn != user_dn:
            # DN hat sich geaendert (z.B. User im LDAP verschoben)
            user.ldap_dn = user_dn
            session.add(user)
            session.commit()
        return user if user_dn else None
    else:
        # DB-User (hat password_hash) -> DB-Auth
        return authenticate_db(user, password)


def authenticate_db(user: User, password: str) -> Optional[User]:
    """DB-Auth: bcrypt password hash"""
    if not user.password_hash:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def try_ldap_auth(
    username: str,
    password: str,
    ldap_server: Optional[str] = None,
    ldap_base_dn: Optional[str] = None,
    ldap_bind_dn: Optional[str] = None,
    ldap_bind_pw: Optional[str] = None,
    ldap_user_search: Optional[str] = None,
) -> Optional[str]:
    """
    LDAP-Auth mit ldap3: Bind mit Service-Account, User suchen, dann User-DN+PW authentifizieren.

    Gibt den user_dn zurueck, wenn Auth erfolgreich, sonst None.
    (Kein DB-User erforderlich — eignet sich fuer automatische Registrierung.)

    Falls course-spezifische Parameter uebergeben wurden, nutzen diese.
    Sonst fallback zu globalen LDAP-Config aus .env.
    """
    from ldap3 import Server, Connection, SUBTREE, ALL

    # Course-spezifische Settings oder globale Config
    server = ldap_server or LDAP_SERVER
    base_dn = ldap_base_dn or LDAP_BASE_DN
    bind_dn = ldap_bind_dn or LDAP_BIND_DN
    bind_pw = ldap_bind_pw or LDAP_BIND_PW
    search_filter = ldap_user_search or LDAP_USER_SEARCH

    if not server:
        logger.error("[LDAP] Kein Server konfiguriert.")
        return None

    try:
        # 1. Server-Verbindung (ohne TLS-Verifikation fuer Dev; fuer Prod: tls=Tls(validate=ssl.CERT_REQUIRED))
        ldap_conn = None
        try:
            ldap_server_obj = Server(server, get_info=ALL)
            # Zuerst mit Bind-DN verbinden (Service-Account)
            if bind_dn and bind_pw:
                logger.info(f"[LDAP] Bind als Service-Account: {bind_dn}")
                ldap_conn = Connection(ldap_server_obj, user=bind_dn, password=bind_pw, auto_bind=True)
            else:
                logger.info("[LDAP] Anonymes Bind (kein Bind-DN konfiguriert)")
                ldap_conn = Connection(ldap_server_obj, auto_bind=True)
        except Exception as e:
            logger.error(f"[LDAP] Bind mit Service-Account fehlgeschlagen: {e}")
            return None

        # 2. User im LDAP suchen
        user_filter = search_filter.format(username=username)
        logger.info(f"[LDAP] Suche User '{username}' mit Filter '{user_filter}' in {base_dn}")

        result = ldap_conn.search(base_dn, user_filter, search_scope=SUBTREE)
        entries = ldap_conn.entries

        if not result or not entries:
            logger.error(f"[LDAP] User '{username}' nicht gefunden.")
            ldap_conn.unbind()
            return None

        # ldap3: entry_dn gibt den DN zurueck (auch bei AD)
        user_dn = entries[0].entry_dn
        logger.info(f"[LDAP] Gefunden: {user_dn}")

        ldap_conn.unbind()

        # 3. Mit User-DN + Passwort binden
        try:
            ldap_server_obj2 = Server(server, get_info=ALL)
            user_conn = Connection(ldap_server_obj2, user=user_dn, password=password, auto_bind=True)
        except Exception as e:
            logger.error(f"[LDAP] Ungueltige Credentials fuer '{username}': {e}")
            return None

        logger.info(f"[LDAP] Login erfolgreich fuer '{username}'")
        user_conn.unbind()

        return user_dn
    except ImportError:
        logger.error("[LDAP] Modul 'ldap3' nicht installiert.")
        return None
    except Exception as e:
        logger.error(f"[LDAP] Fehler bei Auth fuer '{username}': {e}")
        return None


# ─── RBAC Middleware / Dependencies ─────────────────────────────


async def get_current_user(
    request: Request,
    session: Session = Depends(get_session),
) -> User:
    """
    Liest JWT aus Cookie, decodiert, lädt User aus DB.
    Wird als dependency in alle geschützten Routen verwendet.
    """
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nicht authentifiziert. Bitte einloggen.",
        )
    
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ungültiges oder abgelaufenes Token.",
        )
    
    user_id: int = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Ungültiges Token.")
    
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User nicht gefunden.")
    
    set_current_user_id(user.id)
    return user


def require_global_admin():
    """
    Dependency-Factory: Prüft, ob der User global ADMIN ist.
    
    Usage:
        @router.get("/admin/stuff")
        async def admin_stuff(user: User = Depends(require_global_admin())):
    """
    async def role_check(
        request: Request,
        session: Session = Depends(get_session),
    ) -> User:
        # Manueler Auth-Check (get_current_user braucht Request)
        token = request.cookies.get("access_token")
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Nicht authentifiziert.",
            )
        
        payload = decode_access_token(token)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Ungültiges oder abgelaufenes Token.",
            )
        
        user_id: int = payload.get("sub")
        user = session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=401, detail="User nicht gefunden.")
        
        if user.role != GlobalUserRole.ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Zugriff verweigert. Nur Administratoren.",
            )
        
        set_current_user_id(user.id)
        return user
    return role_check


_COURSE_ID_CACHE = {}


def require_course_access(*allowed_roles: CourseRole):
    """
    Dependency-Factory: Prüft Kurs-Zugehörigkeit + Rolle im Kurs.
    
    Die course_id wird automatisch vom URL-Path extrahiert (z.B. /courses/{course_id}/...).
    Kein manueller Parameter nötig!
    
    Global-Admin hat immer Zugriff auf alle Kurse.
    
    Usage:
        @router.get("/courses/{course_id}/tasks")
        async def list_tasks(
            course_id: int,
            session: Session = Depends(get_session),
            user: User = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
        ):
    """
    async def course_check(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ) -> tuple[User, int]:
        # course_id aus Path-Parametern extrahieren
        course_id = request.path_params.get("course_id")
        if course_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Keine course_id im Pfad gefunden.",
            )
        course_id = int(course_id)

        # Kurs muss existieren: veraltete Seiten dürfen nicht in gelöschte
        # Kurse schreiben (sonst entstehen verwaiste Zeilen, die bei
        # ID-Wiederverwendung in einen neuen Kurs wandern).
        if session.get(Course, course_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Kurs nicht gefunden (vermutlich gelöscht). Bitte Seite neu laden.",
            )

        # Global-Admin hat überall Zugriff
        if user.role == GlobalUserRole.ADMIN:
            return user, course_id
        
        # Prüfen, ob User Mitglied des Kurses ist
        statement = (
            select(UserCourse)
            .where(UserCourse.user_id == user.id)
            .where(UserCourse.course_id == course_id)
        )
        membership = session.exec(statement).first()
        
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Du bist kein Mitglied dieses Kurses.",
            )
        
        if membership.role_in_course not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Zugriff verweigert. Deine Rolle im Kurs: {membership.role_in_course.value}",
            )
        
        return user, course_id
    return course_check