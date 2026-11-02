"""Autenticación de usuarios del panel y tokens de sesión firmados.

- Contraseñas: Argon2id (argon2-cffi).
- Tokens: HMAC-SHA256 con `MOTOTRADE_SECRET_KEY`, con expiración. Sin dependencias extra.
- Roles: admin | operator | viewer (ver `core.enums.UserRole`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from mototrade.config import get_settings
from mototrade.core import audit
from mototrade.core.enums import ActorType, UserRole
from mototrade.db.models import User, utcnow

_ph = PasswordHasher()


class AuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    user_id: int | None = None


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise AuthError("La contraseña debe tener al menos 8 caracteres.")
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def create_user(session: Session, username: str, password: str, role: UserRole | str = UserRole.OPERATOR,
                *, created_by: str = "system") -> User:
    username = username.strip().lower()
    if not username:
        raise AuthError("Usuario vacío.")
    if str(role) not in {r.value for r in UserRole}:
        raise AuthError(f"Rol inválido: {role}")
    if session.execute(select(User).where(User.username == username)).scalar_one_or_none():
        raise AuthError("El usuario ya existe.")
    user = User(username=username, password_hash=hash_password(password), role=str(role), is_active=True)
    session.add(user)
    session.flush()
    audit.record(session, action="user.created", entity_type="user", entity_id=user.id,
                 details={"username": username, "role": str(role)}, actor_type=ActorType.HUMAN, actor_id=created_by)
    return user


def authenticate(session: Session, username: str, password: str) -> Principal:
    username = (username or "").strip().lower()
    user = session.execute(select(User).where(User.username == username)).scalar_one_or_none()
    # Verificación en tiempo (casi) constante aunque el usuario no exista.
    dummy = "$argon2id$v=19$m=65536,t=3,p=4$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    ok = verify_password(user.password_hash if user else dummy, password or "")
    if not user or not ok or not user.is_active:
        audit.record(session, action="auth.failed", entity_type="user", details={"username": username},
                     actor_type=ActorType.SYSTEM)
        raise AuthError("Usuario o contraseña incorrectos.")
    user.last_login_at = utcnow()
    audit.record(session, action="auth.login", entity_type="user", entity_id=user.id,
                 actor_type=ActorType.HUMAN, actor_id=user.username)
    return Principal(username=user.username, role=user.role, user_id=user.id)


def ensure_admin_from_settings(session: Session) -> User | None:
    """Crea el admin inicial a partir de MOTOTRADE_ADMIN_USERNAME/PASSWORD si no existe ningún usuario."""
    settings = get_settings()
    if session.execute(select(User).limit(1)).scalar_one_or_none():
        return None
    pwd = settings.admin_password.get_secret_value()
    if not pwd:
        return None
    return create_user(session, settings.admin_username, pwd, UserRole.ADMIN, created_by="bootstrap")


# ---- Tokens de sesión --------------------------------------------------------
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(principal: Principal, ttl_seconds: int = 8 * 3600) -> str:
    secret = get_settings().secret_key.get_secret_value().encode()
    payload = {"u": principal.username, "r": principal.role, "id": principal.user_id,
               "exp": int(time.time()) + ttl_seconds}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str) -> Principal | None:
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    secret = get_settings().secret_key.get_secret_value().encode()
    expected = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return Principal(username=payload["u"], role=payload["r"], user_id=payload.get("id"))
