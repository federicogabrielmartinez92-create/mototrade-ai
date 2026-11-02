"""Reproduce el problema de despliegue: secretos que no llegan, fallback a SQLite, y clasificación de errores de conexión."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mototrade.config.secrets_bridge import apply_secrets
from mototrade.db.diagnostics import classify_message, describe_url, diagnose, redact

ROOT = Path(__file__).resolve().parents[1]


# ---- puente st.secrets → entorno ---------------------------------------------
def test_bridge_flat_nested_and_case(monkeypatch):
    for k in ("MOTOTRADE_DATABASE_URL", "MOTOTRADE_SECRET_KEY", "MOTOTRADE_ADMIN_PASSWORD", "MOTOTRADE_SUPABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    rep = apply_secrets({
        "MOTOTRADE_DATABASE_URL": "postgresql+psycopg://u:p@h:5432/d",     # plana
        "mototrade": {"secret_key": "abc", "ADMIN_PASSWORD": "x"},        # sección TOML, sin prefijo, minúsculas
        "secrets": {"mototrade_supabase_url": "https://x.supabase.co"},
        "OTRA_COSA": "ignorar", "lista": [1, 2],
    })
    assert rep.has_database_url and os.environ["MOTOTRADE_DATABASE_URL"].startswith("postgresql+psycopg://")
    assert os.environ["MOTOTRADE_SECRET_KEY"] == "abc" and os.environ["MOTOTRADE_ADMIN_PASSWORD"] == "x"
    assert os.environ["MOTOTRADE_SUPABASE_URL"] == "https://x.supabase.co"
    assert "OTRA_COSA" in rep.ignored and "lista" in rep.ignored and not rep.error


def test_bridge_never_overrides_existing_env(monkeypatch):
    monkeypatch.setenv("MOTOTRADE_LLM_MODEL", "del-entorno")
    rep = apply_secrets({"MOTOTRADE_LLM_MODEL": "del-secreto"})
    assert os.environ["MOTOTRADE_LLM_MODEL"] == "del-entorno" and "MOTOTRADE_LLM_MODEL" in rep.skipped_existing


def test_bridge_tolerates_missing_secrets():
    assert apply_secrets(None).loaded == []


# ---- URL y redacción -------------------------------------------------------------
def test_describe_url_redacts_and_hints():
    info = describe_url("postgresql+psycopg://postgres:SuperSecreta@db.abcd.supabase.co:5432/postgres")
    assert info.has_password and "SuperSecreta" not in info.redacted and "***" in info.redacted
    assert any("pooler" in h for h in info.hints)
    info = describe_url("postgresql://postgres:pw@aws-0-sa-east-1.pooler.supabase.com:6543/postgres")
    assert any("psycopg" in h for h in info.hints) and any("postgres.<ref" in h for h in info.hints) and any("6543" in h for h in info.hints)
    good = describe_url("postgresql+psycopg://postgres.abcdefghijklmnopqrst:pw@aws-0-sa-east-1.pooler.supabase.com:5432/postgres")
    assert good.hints == []
    bad = describe_url("esto no es una url")
    assert bad.dialect == "" and "no interpretable" in bad.redacted
    assert describe_url(f"sqlite:///{ROOT}/mototrade.sqlite3").is_default_sqlite


def test_redact_removes_passwords_everywhere():
    msg = 'connection to "postgresql://u:MiPass@h/d" failed: password=MiPass token MiPass'
    out = redact(msg, "MiPass")
    assert "MiPass" not in out and "u:•••@" in out


@pytest.mark.parametrize("msg,cat", [
    ("FATAL: Tenant or user not found", "tenant"),
    ("FATAL: password authentication failed for user \"postgres\"", "auth"),
    ("could not translate host name \"x\" to address: Name or service not known", "dns"),
    ("connection to server at \"h\" (1.2.3.4), port 5432 failed: Connection refused", "network"),
    ("connection timed out", "network"),
    ("SSL SYSCALL error: EOF detected", "ssl"),
    ("FATAL: database \"nada\" does not exist", "database"),
    ("no such table: users", "schema_pending"),
    ("relation \"users\" does not exist", "schema_pending"),
    ("algo raro", "unknown"),
])
def test_classify_message(msg, cat):
    assert classify_message(msg) == cat


# ---- diagnóstico en vivo ------------------------------------------------------------
def test_diagnose_missing_url_in_production():
    d = diagnose(f"sqlite:///{ROOT}/mototrade.sqlite3", production=True)
    assert d.category == "missing_url" and any("Secrets" in h for h in d.hints)
    assert diagnose(None).category == "missing_url"
    assert diagnose("").category == "missing_url"


def test_diagnose_invalid_url():
    assert diagnose("ni-siquiera-una-url", production=True).category == "url_invalid"


def test_diagnose_network_refused():
    d = diagnose("postgresql+psycopg://postgres.abc:secreta@127.0.0.1:1/postgres", production=True, timeout=3)
    assert d.category in ("network", "unknown") and "secreta" not in d.message and "secreta" not in d.url.redacted
    assert any("pausa" in h for h in d.hints) or d.category == "unknown"


def test_diagnose_dns():
    d = diagnose("postgresql+psycopg://postgres.abc:secreta@no-existe-mototrade.invalid:5432/postgres", production=True, timeout=3)
    assert d.category in ("dns", "network") and "secreta" not in d.message


def test_diagnose_schema_pending_and_ok(tmp_path):
    url = f"sqlite:///{tmp_path}/fresh.sqlite3"
    d = diagnose(url)
    assert d.category == "schema_pending" and d.schema_current is None and d.schema_head
    env = {**os.environ, "MOTOTRADE_DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True, capture_output=True)
    d = diagnose(url)
    assert d.ok and d.schema_current == d.schema_head


def test_cli_db_check_never_prints_password(tmp_path):
    env = {**os.environ, "MOTOTRADE_DATABASE_URL": "postgresql+psycopg://postgres.abc:ClaveUltraSecreta@127.0.0.1:1/postgres",
           "MOTOTRADE_ENV": "production", "MOTOTRADE_LOG_LEVEL": "ERROR"}
    r = subprocess.run([sys.executable, "-m", "mototrade.cli", "db-check"], cwd=ROOT, env=env, capture_output=True, text=True)
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "ClaveUltraSecreta" not in out and "Categoría:" in out
    r = subprocess.run([sys.executable, "-m", "mototrade.cli", "db-status"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "ClaveUltraSecreta" not in (r.stdout + r.stderr)


def test_settings_reports_default_database_url(monkeypatch):
    from mototrade.config import Settings

    monkeypatch.delenv("MOTOTRADE_DATABASE_URL", raising=False)
    s = Settings(_env_file=None)
    assert s.database_url_is_default and any("no definida" in w for w in s.security_warnings())
    s2 = Settings(_env_file=None, database_url="postgresql+psycopg://u:p@h/d")
    assert not s2.database_url_is_default
