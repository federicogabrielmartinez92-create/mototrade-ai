"""Motor y sesiones SQLAlchemy. PostgreSQL en producción; SQLite solo para tests/dev."""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def _json_dumps(obj) -> str:
    """Serializador JSON de la base: Decimal/datetime/Enum se guardan como texto."""
    import json

    return json.dumps(obj, default=str, ensure_ascii=False)


def _make_engine(url: str) -> Engine:
    kwargs: dict = {"future": True, "pool_pre_ping": True, "json_serializer": _json_dumps}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url.endswith("sqlite://"):
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def _resolve_url(url: str | None) -> str:
    if url is None:
        from mototrade.config import get_settings

        url = get_settings().database_url
    return url


@lru_cache(maxsize=8)
def _engine_for(url: str) -> Engine:
    return _make_engine(url)


@lru_cache(maxsize=8)
def _factory_for(url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=_engine_for(url), expire_on_commit=False, future=True)


def get_engine(url: str | None = None) -> Engine:
    """Un único motor por URL (la URL se resuelve ANTES de cachear, así `get_engine()` y
    `get_engine(url)` comparten instancia)."""
    return _engine_for(_resolve_url(url))


def get_session_factory(url: str | None = None) -> sessionmaker[Session]:
    return _factory_for(_resolve_url(url))


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """Transacción: commit si todo sale bien, rollback ante excepción."""
    session = get_session_factory(url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(url: str | None = None) -> None:
    """Crea todas las tablas (uso en tests/dev). En producción se usan migraciones Alembic."""
    from . import models  # noqa: F401  (registra los modelos)

    Base.metadata.create_all(get_engine(url))
