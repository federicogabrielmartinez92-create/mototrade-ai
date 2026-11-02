"""Fixtures compartidas.

Por defecto los tests corren sobre SQLite en memoria (rápido, sin servicios).
Si se define MOTOTRADE_TEST_DATABASE_URL (p. ej. PostgreSQL), se usa esa base:
las tablas se crean y se destruyen al inicio y fin de la sesión de tests.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

os.environ.setdefault("MOTOTRADE_ENV", "test")
os.environ.setdefault("MOTOTRADE_SECRET_KEY", "test-secret-key-not-for-production-0123456789")
os.environ.setdefault("MOTOTRADE_API_KEY", "test-api-key")
os.environ.setdefault("MOTOTRADE_LLM_PROVIDER", "fake")
os.environ.setdefault("MOTOTRADE_TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("MOTOTRADE_TELEGRAM_CHAT_ID", "")

TEST_DB_URL = os.environ.get("MOTOTRADE_TEST_DATABASE_URL", "sqlite://")
os.environ["MOTOTRADE_DATABASE_URL"] = TEST_DB_URL

from mototrade.config import get_rules, get_settings  # noqa: E402
from mototrade.db import Base, get_engine, get_session_factory, init_db  # noqa: E402

RULES_PATH = Path(__file__).resolve().parents[1] / "config" / "business_rules.yaml"


@pytest.fixture(scope="session")
def db_url() -> str:
    return TEST_DB_URL


@pytest.fixture(scope="session", autouse=True)
def _schema(db_url):
    get_settings.cache_clear()
    init_db(db_url)
    yield
    Base.metadata.drop_all(get_engine(db_url))


@pytest.fixture()
def session(db_url) -> Session:
    """Sesión limpia por test: se borran todas las filas al terminar."""
    s = get_session_factory(db_url)()
    try:
        yield s
        s.rollback()
    finally:
        s.close()
        _truncate_all(db_url)


def _truncate_all(db_url: str) -> None:
    engine = get_engine(db_url)
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def rules():
    return get_rules(RULES_PATH)
