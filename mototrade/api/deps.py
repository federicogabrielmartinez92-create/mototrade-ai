"""Dependencias de FastAPI: sesión de base y autenticación."""
from __future__ import annotations

import hmac
from typing import Iterator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from mototrade.config import BusinessRules, get_rules, get_settings
from mototrade.db import get_session_factory
from mototrade.security.auth import Principal, verify_token


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_business_rules(db: Session = Depends(get_db)) -> BusinessRules:
    """Reglas EFECTIVAS: YAML + Centro de Control. Obligatorio para todos los endpoints."""
    from mototrade.control import ControlCenter

    return ControlCenter(db).effective_rules(get_rules())


def current_principal(x_api_key: str | None = Header(default=None), authorization: str | None = Header(default=None)) -> Principal:
    settings = get_settings()
    if x_api_key and hmac.compare_digest(x_api_key, settings.api_key.get_secret_value()):
        return Principal(username="api", role="admin")
    if authorization and authorization.lower().startswith("bearer "):
        p = verify_token(authorization.split(" ", 1)[1].strip())
        if p:
            return p
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales inválidas.")


def require_role(*roles: str):
    def _dep(p: Principal = Depends(current_principal)) -> Principal:
        if p.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Se requiere rol {roles}.")
        return p
    return _dep
