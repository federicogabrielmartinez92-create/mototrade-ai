"""Configuración de infraestructura (secretos y entorno) vía variables de entorno.

Las reglas de NEGOCIO (límites financieros, fórmulas) NO viven acá: ver `rules.py`.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MOTOTRADE_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    database_url: str = Field(
        default=f"sqlite:///{PROJECT_ROOT / 'mototrade.sqlite3'}",
        description="URL SQLAlchemy. Producción: postgresql+psycopg://user:pass@host:5432/db",
    )
    rules_path: Path = PROJECT_ROOT / "config" / "business_rules.yaml"

    secret_key: SecretStr = SecretStr("dev-insecure-change-me")
    api_key: SecretStr = SecretStr("dev-api-key-change-me")
    admin_username: str = "admin"
    admin_password: SecretStr = SecretStr("")

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    anthropic_api_key: SecretStr = SecretStr("")
    llm_provider: Literal["fake", "anthropic"] = "fake"
    llm_model: str = "claude-sonnet-4-5"

    meli_client_id: str = ""
    meli_client_secret: SecretStr = SecretStr("")
    meli_refresh_token: SecretStr = SecretStr("")

    worker_interval_seconds: int = 300
    photos_dir: Path = PROJECT_ROOT / "data" / "photos"

    # Supabase Storage (opcional; nube gratuita). Sin estos valores las fotos van a photos_dir.
    supabase_url: str = ""
    supabase_service_key: SecretStr = SecretStr("")
    supabase_bucket: str = "fotos"

    # ---- helpers -------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token.get_secret_value()) and bool(self.telegram_chat_id)

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider == "anthropic" and bool(self.anthropic_api_key.get_secret_value())

    @property
    def supabase_storage_enabled(self) -> bool:
        return bool(self.supabase_url) and bool(self.supabase_service_key.get_secret_value())

    @property
    def meli_enabled(self) -> bool:
        return bool(self.meli_client_id) and bool(self.meli_client_secret.get_secret_value())

    def integration_status(self) -> dict[str, dict[str, str]]:
        """Estado honesto de cada integración, para mostrar en el panel."""
        return {
            "telegram": {
                "estado": "activo" if self.telegram_enabled else "deshabilitado (falta token/chat_id)",
                "modo": "real" if self.telegram_enabled else "simulado",
            },
            "fotos": {
                "estado": "Supabase Storage" if self.supabase_storage_enabled else f"disco local ({self.photos_dir})",
                "modo": "real",
            },
            "telegram_inbox": {
                "estado": "activo: enviá /moto al bot para cargar anuncios" if self.telegram_enabled else
                          "deshabilitado (falta token)",
                "modo": "real" if self.telegram_enabled else "simulado",
            },
            "ia_llm": {
                "estado": "activo" if self.llm_enabled else "proveedor fake (sin API key)",
                "modo": "real" if self.llm_enabled else "simulado",
            },
            "mercadolibre": {
                "estado": "búsqueda de anuncios ajenos cerrada a terceros (403); publicar/responder preguntas propias "
                          "requiere app OAuth + paquete contratado. Hoy: carga manual/CSV",
                "modo": "simulado" if not self.meli_enabled else "credenciales cargadas, cliente no implementado",
            },
            "facebook_marketplace": {
                "estado": "sin API pública (listados ni chats); scraping prohibido por ToS. Solo carga manual",
                "modo": "manual",
            },
            "whatsapp": {
                "estado": "requiere WhatsApp Business Platform, número dedicado y OPT-IN; no se puede iniciar "
                          "contacto con vendedores sin opt-in. No implementado",
                "modo": "manual",
            },
            "rosariogarage": {
                "estado": "sin API/feed; captcha y Cloudflare; no se hace scraping. Solo carga manual",
                "modo": "manual",
            },
        }

    def security_warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.secret_key.get_secret_value().startswith("dev-insecure"):
            warnings.append("MOTOTRADE_SECRET_KEY usa el valor por defecto inseguro.")
        if self.api_key.get_secret_value().startswith("dev-api-key"):
            warnings.append("MOTOTRADE_API_KEY usa el valor por defecto inseguro.")
        if self.database_url.startswith("sqlite") and self.is_production:
            warnings.append("Producción con SQLite: usar PostgreSQL.")
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
