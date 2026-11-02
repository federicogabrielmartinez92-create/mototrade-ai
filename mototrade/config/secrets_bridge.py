"""Puente entre `st.secrets` (Streamlit Community Cloud, TOML) y las variables de entorno que lee `Settings`.

Reglas:
- Acepta claves planas (`MOTOTRADE_DATABASE_URL = "..."`) y también dentro de una sección TOML
  (`[mototrade]` / `[secrets]` / cualquiera): se recorren los diccionarios anidados.
- Normaliza el nombre: mayúsculas; acepta `mototrade_database_url` y también `DATABASE_URL` sin prefijo.
- Nunca pisa una variable de entorno ya definida (el entorno manda sobre los secretos).
- Devuelve un informe SIN valores (solo nombres) para mostrarlo en el diagnóstico.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

KNOWN = {
    "ENV", "LOG_LEVEL", "DATABASE_URL", "RULES_PATH", "SECRET_KEY", "API_KEY", "ADMIN_USERNAME", "ADMIN_PASSWORD",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "MELI_CLIENT_ID",
    "MELI_CLIENT_SECRET", "MELI_REFRESH_TOKEN", "WORKER_INTERVAL_SECONDS", "PHOTOS_DIR", "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY", "SUPABASE_BUCKET",
}


@dataclass
class BridgeReport:
    loaded: list[str] = field(default_factory=list)      # claves volcadas al entorno
    skipped_existing: list[str] = field(default_factory=list)  # ya estaban en el entorno
    ignored: list[str] = field(default_factory=list)     # no reconocidas
    error: str | None = None

    @property
    def has_database_url(self) -> bool:
        return "MOTOTRADE_DATABASE_URL" in self.loaded or "MOTOTRADE_DATABASE_URL" in self.skipped_existing


def _normalize(key: str) -> str | None:
    k = str(key).strip().upper().replace("-", "_")
    if k.startswith("MOTOTRADE_"):
        return k
    if k in KNOWN:
        return "MOTOTRADE_" + k
    return None


def _walk(mapping: Mapping[str, Any], out: dict[str, str], ignored: list[str]) -> None:
    for key, value in mapping.items():
        if isinstance(value, Mapping):
            _walk(value, out, ignored)
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        if not isinstance(value, (str, int, float)):
            ignored.append(str(key))
            continue
        norm = _normalize(key)
        if norm is None:
            ignored.append(str(key))
            continue
        out[norm] = str(value)


def apply_secrets(secrets: Mapping[str, Any] | None) -> BridgeReport:
    rep = BridgeReport()
    if secrets is None:
        return rep
    found: dict[str, str] = {}
    try:
        _walk(secrets, found, rep.ignored)
    except Exception as exc:  # st.secrets puede lanzar si no hay secrets.toml
        rep.error = f"{type(exc).__name__}: {exc}"[:200]
        return rep
    for k, v in found.items():
        if k in os.environ and os.environ[k] != "":
            rep.skipped_existing.append(k)
            continue
        os.environ[k] = v
        rep.loaded.append(k)
    return rep


def apply_streamlit_secrets() -> BridgeReport:
    """Lee `st.secrets` si Streamlit está disponible. Nunca lanza."""
    try:
        import streamlit as st

        try:
            data = dict(st.secrets)  # fuerza la carga; sin secrets.toml lanza
        except Exception as exc:
            rep = BridgeReport(error=f"sin st.secrets ({type(exc).__name__})")
            return rep
        return apply_secrets(data)
    except Exception as exc:  # pragma: no cover - sin streamlit
        return BridgeReport(error=f"{type(exc).__name__}")
