"""Diagnóstico de conexión a la base, SEGURO para mostrar y registrar (sin contraseñas ni URLs completas).

Categorías:
  missing_url     no hay MOTOTRADE_DATABASE_URL (se estaría usando el SQLite por defecto)
  url_invalid     la URL no se puede interpretar (prefijo, formato, caracteres sin codificar)
  dns             el host no resuelve
  network         el host resuelve pero no responde / rechaza / expira (puerto, firewall, proyecto pausado)
  ssl             fallo de negociación TLS
  tenant          Supabase pooler: "Tenant or user not found" (usuario sin el sufijo .<ref> o ref incorrecto)
  auth            contraseña incorrecta / rol inexistente
  database        la base de datos indicada no existe o sin permisos
  schema_pending  conecta pero faltan migraciones (o no hay tablas)
  ok              conecta y el esquema está al día
  unknown         otro error (mensaje redactado)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@dataclass
class UrlInfo:
    dialect: str = ""
    driver: str = ""
    username: str | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    is_sqlite: bool = False
    is_default_sqlite: bool = False
    has_password: bool = False
    redacted: str = ""
    hints: list[str] = field(default_factory=list)


def describe_url(url: str, *, default_sqlite_marker: str = "mototrade.sqlite3") -> UrlInfo:
    info = UrlInfo()
    try:
        u = make_url(url)
    except Exception:
        info.redacted = _mask_scheme_only(url)
        info.hints.append("La URL no tiene un formato válido para SQLAlchemy.")
        return info
    info.dialect = u.get_backend_name()
    info.driver = u.get_driver_name() if "+" in (u.drivername or "") else ""
    info.username, info.host, info.port, info.database = u.username, u.host, u.port, u.database
    info.has_password = bool(u.password)
    info.is_sqlite = info.dialect == "sqlite"
    info.is_default_sqlite = info.is_sqlite and default_sqlite_marker in (u.database or "")
    info.redacted = u.render_as_string(hide_password=True)
    if info.dialect == "postgresql" and not info.driver:
        info.hints.append("Usar el prefijo 'postgresql+psycopg://' (psycopg 3) en vez de 'postgresql://'.")
    if info.host and info.host.endswith("supabase.co") and "pooler" not in info.host:
        info.hints.append("Host directo de Supabase (IPv6): usar el Session pooler '…pooler.supabase.com' puerto 5432.")
    if info.host and "pooler.supabase.com" in info.host and info.username and "." not in info.username:
        info.hints.append("Con el pooler de Supabase el usuario debe ser 'postgres.<ref-del-proyecto>'.")
    if info.host and "pooler.supabase.com" in info.host and info.port == 6543:
        info.hints.append("Puerto 6543 = transaction mode; para esta app conviene 5432 (session mode).")
    return info


def _mask_scheme_only(url: str) -> str:
    scheme = url.split("://", 1)[0] if "://" in url else "?"
    return f"{scheme}://…(no interpretable)"


def redact(text_: str, *secrets: str | None) -> str:
    """Quita contraseñas/tokens conocidos y cualquier 'user:pass@' de un texto de error."""
    out = text_ or ""
    for s in secrets:
        if s and len(s) >= 4:
            out = out.replace(s, "•••")
    out = re.sub(r"://([^:/@\s]+):([^@\s]+)@", r"://\1:•••@", out)
    out = re.sub(r"(password|contraseña)\s*=\s*\S+", r"\1=•••", out, flags=re.I)
    return out


def classify_message(msg: str) -> str:
    m = (msg or "").lower()
    if "tenant or user not found" in m:
        return "tenant"
    if "password authentication failed" in m or "no password supplied" in m or "role \"" in m and "does not exist" in m:
        return "auth"
    if "could not translate host name" in m or "name or service not known" in m or "nodename nor servname" in m \
            or "getaddrinfo" in m or "temporary failure in name resolution" in m:
        return "dns"
    if "ssl" in m or "tls" in m or "certificate" in m:
        return "ssl"
    if "database \"" in m and "does not exist" in m or "permission denied for database" in m:
        return "database"
    if "connection refused" in m or "timed out" in m or "timeout expired" in m or "network is unreachable" in m \
            or "connection reset" in m or "server closed the connection" in m or "could not connect" in m \
            or "no route to host" in m:
        return "network"
    if "no such table" in m or "does not exist" in m and "relation" in m:
        return "schema_pending"
    if "invalid dsn" in m or "invalid connection option" in m or "could not parse" in m or "unknown" in m and "dialect" in m:
        return "url_invalid"
    return "unknown"


HINTS = {
    "missing_url": ["No llegó MOTOTRADE_DATABASE_URL: en Streamlit Cloud va en App settings → Secrets (formato TOML, "
                    "clave entre comillas); en GitHub Actions, en Secrets del repositorio."],
    "url_invalid": ["Formato esperado: postgresql+psycopg://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres",
                    "Si la contraseña tiene @ # / % ? : codificarlos (p. ej. @ → %40). Sin comillas sobrantes ni espacios."],
    "dns": ["El host no existe o está mal escrito. Copiá el host exacto del panel de Supabase (Connect → Session pooler)."],
    "network": ["Supabase pausa los proyectos gratuitos tras 7 días sin actividad: revisá si dice 'Paused' y reanudalo.",
                "Verificá puerto 5432 (session mode). Streamlit Cloud y GitHub Actions no tienen IPv6: usar el pooler."],
    "ssl": ["El pooler exige TLS. Probá agregar '?sslmode=require' al final de la URL."],
    "tenant": ["Con el pooler el usuario es 'postgres.<ref>' (ref = identificador del proyecto, 20 letras). "
               "Copiá la URI del Session pooler tal cual y solo cambiá el prefijo a postgresql+psycopg://."],
    "auth": ["Contraseña incorrecta: es la contraseña de la BASE (Project Settings → Database), no la del login de Supabase. "
             "Se puede resetear ahí mismo; después actualizar el secreto en GitHub y en Streamlit."],
    "database": ["La base indicada no existe: en Supabase es 'postgres'."],
    "schema_pending": ["Conecta, pero faltan migraciones o se está mirando otra base: ejecutar el workflow setup-database "
                       "y confirmar que Streamlit y GitHub usan el MISMO MOTOTRADE_DATABASE_URL (mismo host y mismo ref)."],
    "ok": [],
    "unknown": ["Revisá el mensaje redactado; si persiste, comparar la URL de Streamlit con la de GitHub carácter por carácter."],
}


@dataclass
class Diagnosis:
    category: str
    message: str
    url: UrlInfo
    schema_current: str | None = None
    schema_head: str | None = None
    server_version: str | None = None
    hints: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.category == "ok"

    def as_dict(self) -> dict:
        return {"category": self.category, "message": self.message, "url": self.url.redacted, "dialect": self.url.dialect,
                "host": self.url.host, "port": self.url.port, "user": self.url.username, "database": self.url.database,
                "schema_current": self.schema_current, "schema_head": self.schema_head,
                "server_version": self.server_version, "hints": self.hints}


def _alembic_head() -> str | None:
    try:
        from pathlib import Path

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        root = Path(__file__).resolve().parents[2]
        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "alembic"))
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:
        return None


def diagnose(url: str | None, *, production: bool = False, timeout: int = 6, secrets_to_redact: tuple = ()) -> Diagnosis:
    """Conecta con timeout corto y clasifica el resultado. Nunca lanza; nunca incluye credenciales."""
    if not url:
        return Diagnosis("missing_url", "MOTOTRADE_DATABASE_URL vacía.", UrlInfo(), hints=HINTS["missing_url"])
    info = describe_url(url)
    pwd = None
    try:
        pwd = make_url(url).password
    except Exception:
        pass
    scrub = (pwd, *secrets_to_redact)
    if info.is_default_sqlite and production:
        return Diagnosis("missing_url", "Se está usando el SQLite por defecto: MOTOTRADE_DATABASE_URL no llegó a la aplicación.",
                         info, hints=HINTS["missing_url"] + info.hints)
    if not info.dialect:
        return Diagnosis("url_invalid", "La URL no se pudo interpretar.", info, hints=HINTS["url_invalid"] + info.hints)
    kwargs: dict = {"pool_pre_ping": True}
    if info.dialect == "postgresql":
        kwargs["connect_args"] = {"connect_timeout": timeout}
    try:
        engine = create_engine(url, **kwargs)
        with engine.connect() as conn:
            version = None
            if info.dialect == "postgresql":
                version = conn.execute(text("select version()")).scalar_one()
                version = version.split(",")[0] if version else None
            from alembic.runtime.migration import MigrationContext

            current = MigrationContext.configure(conn).get_current_revision()
        engine.dispose()
    except Exception as exc:
        msg = redact(f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}", *scrub)[:300]
        cat = classify_message(str(exc))
        return Diagnosis(cat, msg, info, hints=HINTS.get(cat, []) + info.hints)
    head = _alembic_head()
    if current != head:
        return Diagnosis("schema_pending", f"Conecta, pero el esquema está en '{current or 'ninguna'}' y el código espera '{head}'.",
                         info, schema_current=current, schema_head=head, server_version=version, hints=HINTS["schema_pending"] + info.hints)
    return Diagnosis("ok", "Conexión y esquema correctos.", info, schema_current=current, schema_head=head,
                     server_version=version, hints=info.hints)


def diagnose_from_settings() -> Diagnosis:
    from mototrade.config import get_settings

    s = get_settings()
    return diagnose(s.database_url, production=s.is_production,
                    secrets_to_redact=(s.secret_key.get_secret_value(), s.admin_password.get_secret_value(),
                                       s.supabase_service_key.get_secret_value()))
