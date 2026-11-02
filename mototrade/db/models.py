"""Esquema único de datos para los 12 módulos de MotoTrade AI.

Convenciones
------------
- Importes: Numeric(14, 2) en ARS (Decimal en Python). Nunca float.
- Fechas: timezone-aware (UTC).
- Estados: texto validado por enums de `mototrade.core.enums`.
- JSON: `JSON` con variante JSONB en PostgreSQL.
- Todo texto que viene de terceros (anuncios, mensajes) se trata como NO confiable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")
Money = Numeric(14, 2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# =============================================================================
# SEGURIDAD, AUDITORÍA Y CONTROL
# =============================================================================
class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="operator")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SystemState(Base):
    """Interruptores globales: kill switch, pausa del worker, modo mantenimiento."""

    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(64))


class AuditLog(Base):
    """Registro inmutable (append-only) con cadena de hashes: cada fila incluye el hash de la anterior."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)  # human|agent|system
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    rules_hash: Mapped[str | None] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)


class ApprovalRequest(TimestampMixin, Base):
    """Cola de aprobaciones humanas. Nada con efecto externo o financiero se ejecuta sin una fila aprobada acá."""

    __tablename__ = "approval_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    amount: Mapped[Decimal | None] = mapped_column(Money)
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    rules_hash: Mapped[str | None] = mapped_column(String(64))

    outbound_actions: Mapped[list[OutboundAction]] = relationship(back_populates="approval")


class OutboundAction(TimestampMixin, Base):
    """Acción con efecto externo (enviar mensaje, publicar, notificar). Idempotente y auditada."""

    __tablename__ = "outbound_actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approval_requests.id"))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)  # telegram|whatsapp|mercadolibre|manual
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_ref: Mapped[str | None] = mapped_column(String(128))

    approval: Mapped[ApprovalRequest | None] = relationship(back_populates="outbound_actions")


class AppSetting(Base):
    """Centro de Control: configuración operativa persistente (una fila por sección, valor JSON validado)."""

    __tablename__ = "app_settings"
    section: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(64))


class AIUsage(Base):
    """Registro de cada llamada a un proveedor de IA (tokens, costo estimado) para presupuestar y no repetir."""

    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)  # interpret|draft|vision|deep_analysis
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    est_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=0)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cache_key: Mapped[str | None] = mapped_column(String(64), index=True)

    __table_args__ = (Index("ix_ai_usage_time", "occurred_at"),)


class Notification(Base):
    """Centro de notificaciones persistente dentro de la app (canal principal; Telegram es opcional)."""

    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    level: Mapped[str] = mapped_column(String(8), nullable=False, default="info")  # info|warning|alert
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="general")  # opportunity|approval|pricing|system|...
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (Index("ix_notifications_unread", "read_at", "created_at"),)


class JobRun(Base):
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    stats: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


# =============================================================================
# CATÁLOGO
# =============================================================================
class MotoModel(TimestampMixin, Base):
    __tablename__ = "moto_models"
    id: Mapped[int] = mapped_column(primary_key=True)
    make: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    displacement_cc: Mapped[int | None] = mapped_column(Integer)
    category: Mapped[str | None] = mapped_column(String(32))
    is_watchlisted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (UniqueConstraint("make", "model", "version", name="uq_moto_model"),)


# =============================================================================
# MÓDULO 1 — RADAR (búsqueda de motos) / anuncios de terceros
# =============================================================================
class Listing(TimestampMixin, Base):
    __tablename__ = "listings"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128))
    source_url: Mapped[str | None] = mapped_column(String(512))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # dedup difuso
    make: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    year: Mapped[int | None] = mapped_column(Integer)
    kilometers: Mapped[int | None] = mapped_column(Integer)
    asking_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="ARS")
    location: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")  # NO confiable
    seller_alias: Mapped[str | None] = mapped_column(String(128))
    seller_type: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")  # particular|dealer|unknown
    condition_declared: Mapped[str | None] = mapped_column(String(32))  # excelente|bueno|regular|malo|unknown
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    photos: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    raw: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="new")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    injection_flags: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)

    price_history: Mapped[list[ListingPriceHistory]] = relationship(
        back_populates="listing", cascade="all, delete-orphan", order_by="ListingPriceHistory.observed_at"
    )
    deals: Mapped[list[Deal]] = relationship(back_populates="listing")

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_listing_source_external"),
        Index("ix_listing_model_year", "make", "model", "year"),
    )


class ListingPriceHistory(Base):
    __tablename__ = "listing_price_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)

    listing: Mapped[Listing] = relationship(back_populates="price_history")


# =============================================================================
# MÓDULO 2 — TASADOR / comparables y tasaciones
# =============================================================================
class Comparable(TimestampMixin, Base):
    """Dato de mercado usado para tasar. `kind` distingue precio pedido vs venta efectiva."""

    __tablename__ = "comparables"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # asking | sold
    make: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    year: Mapped[int | None] = mapped_column(Integer)
    kilometers: Mapped[int | None] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    location: Mapped[str | None] = mapped_column(String(128))
    seller_type: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    condition: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(512))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id", ondelete="SET NULL"))
    inventory_item_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_items.id", ondelete="SET NULL"))

    __table_args__ = (Index("ix_comparable_model", "make", "model", "year", "kind"),)


class Valuation(TimestampMixin, Base):
    """Resultado completo de una tasación. Inmutable: una nueva tasación crea una nueva fila."""

    __tablename__ = "valuations"
    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id", ondelete="CASCADE"))
    inventory_item_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_items.id", ondelete="CASCADE"))
    make: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    year: Mapped[int | None] = mapped_column(Integer)
    kilometers: Mapped[int | None] = mapped_column(Integer)
    market_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    quick_sale_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    suggested_list_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    initial_offer: Mapped[Decimal] = mapped_column(Money, nullable=False)
    max_purchase_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    costs: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    total_costs: Mapped[Decimal] = mapped_column(Money, nullable=False)
    expected_profit: Mapped[Decimal] = mapped_column(Money, nullable=False)
    expected_roi: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    scenarios: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    basis: Mapped[str] = mapped_column(String(32), nullable=False)  # asking_adjusted | sold | manual
    comparables_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sources: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    rules_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    computed_by: Mapped[str] = mapped_column(String(64), nullable=False, default="valuation_engine")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


# =============================================================================
# PIPELINE DE COMPRA — deals (oportunidad → compra)
# =============================================================================
class Deal(TimestampMixin, Base):
    """Una oportunidad de compra sobre un anuncio. Lleva los LÍMITES AUTORIZADOS por el humano."""

    __tablename__ = "deals"
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id", ondelete="SET NULL"))
    stage: Mapped[str] = mapped_column(String(24), nullable=False, default="opportunity")
    opportunity_score: Mapped[int | None] = mapped_column(Integer)
    score_breakdown: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # Detector de anomalías (incremento 2)
    opportunity_class: Mapped[str | None] = mapped_column(String(16))  # insufficient|ordinary|interesting|extraordinary
    opportunity_mode: Mapped[str | None] = mapped_column(String(16))   # published|negotiable|none
    discount_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    risk_flags: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    analysis: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Límites autorizados por el humano para el agente comprador. NULL = no autorizado todavía.
    authorized_max_price: Mapped[Decimal | None] = mapped_column(Money)
    authorized_by: Mapped[str | None] = mapped_column(String(64))
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_offer: Mapped[Decimal | None] = mapped_column(Money)
    counterpart_last_price: Mapped[Decimal | None] = mapped_column(Money)
    documents_ok: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mechanic_ok: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lost_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    listing: Mapped[Listing | None] = relationship(back_populates="deals")
    valuations: Mapped[list[Valuation]] = relationship(
        primaryjoin="Deal.id==Valuation.deal_id", order_by="Valuation.id", cascade="all, delete-orphan"
    )
    inventory_item: Mapped[InventoryItem | None] = relationship(back_populates="deal", uselist=False)


# =============================================================================
# MÓDULO 3 — INSPECTOR VISUAL
# =============================================================================
class Photo(TimestampMixin, Base):
    __tablename__ = "photos"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)  # listing|deal|inventory_item
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str | None] = mapped_column(String(512))
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    is_authentic: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # foto real, no generada
    caption: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    mime_type: Mapped[str | None] = mapped_column(String(32))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="upload")  # upload|telegram|url

    __table_args__ = (Index("ix_photo_entity", "entity_type", "entity_id"),)


class Inspection(TimestampMixin, Base):
    __tablename__ = "inspections"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    inspector: Mapped[str] = mapped_column(String(32), nullable=False)  # ai_vision|human
    provider: Mapped[str | None] = mapped_column(String(64))
    model_name: Mapped[str | None] = mapped_column(String(64))
    photo_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cache_key: Mapped[str | None] = mapped_column(String(64), index=True)
    requested_photos: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)

    findings: Mapped[list[InspectionFinding]] = relationship(cascade="all, delete-orphan")


class InspectionFinding(Base):
    __tablename__ = "inspection_findings"
    id: Mapped[int] = mapped_column(primary_key=True)
    inspection_id: Mapped[int] = mapped_column(ForeignKey("inspections.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # observation | hypothesis
    area: Mapped[str] = mapped_column(String(64), nullable=False)  # carenado, escape, neumáticos...
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False, default="low")  # low|medium|high
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    evidence_photo_id: Mapped[int | None] = mapped_column(Integer)
    requires_human_check: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# =============================================================================
# MÓDULO 4 — REPUESTOS Y REPARACIONES
# =============================================================================
class Supplier(TimestampMixin, Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # parts|workshop|transport|other
    contact: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    location: Mapped[str | None] = mapped_column(String(128))
    rating: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Part(TimestampMixin, Base):
    __tablename__ = "parts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    part_number: Mapped[str | None] = mapped_column(String(64))
    compatible_models: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    compatibility_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Repair(TimestampMixin, Base):
    """Una reparación necesaria/realizada sobre una moto (deal o inventario)."""

    __tablename__ = "repairs"
    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id", ondelete="CASCADE"))
    inventory_item_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_items.id", ondelete="CASCADE"))
    finding_id: Mapped[int | None] = mapped_column(ForeignKey("inspection_findings.id", ondelete="SET NULL"))
    issue: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="suspected")
    decision: Mapped[str | None] = mapped_column(String(16))  # repair | replace | skip
    chosen_quote_id: Mapped[int | None] = mapped_column(Integer)
    actual_cost: Mapped[Decimal | None] = mapped_column(Money)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    quotes: Mapped[list[RepairQuote]] = relationship(back_populates="repair", cascade="all, delete-orphan")


class RepairQuote(TimestampMixin, Base):
    __tablename__ = "repair_quotes"
    id: Mapped[int] = mapped_column(primary_key=True)
    repair_id: Mapped[int] = mapped_column(ForeignKey("repairs.id", ondelete="CASCADE"), nullable=False)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="SET NULL"))
    part_id: Mapped[int | None] = mapped_column(ForeignKey("parts.id", ondelete="SET NULL"))
    option: Mapped[str] = mapped_column(String(16), nullable=False)  # repair | replace
    part_cost: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    labor_cost: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    shipping_cost: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    availability_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False, default="")  # de dónde salió el precio
    quoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    repair: Mapped[Repair] = relationship(back_populates="quotes")

    @property
    def total(self) -> Decimal:
        return Decimal(self.part_cost) + Decimal(self.labor_cost) + Decimal(self.shipping_cost)


# =============================================================================
# MÓDULO 5 — AUDITOR DOCUMENTAL
# =============================================================================
class DocumentRequirement(TimestampMixin, Base):
    """Checklist documental por deal/inventario. El sistema NO verifica oficialmente: registra evidencia."""

    __tablename__ = "document_requirements"
    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id", ondelete="CASCADE"))
    inventory_item_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_items.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(32), nullable=False)  # titulo, cedula, 08, informe_dominio...
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    blocking: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # bloquea la compra si falta
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    evidence_path: Mapped[str | None] = mapped_column(String(512))
    verified_by: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


# =============================================================================
# MÓDULOS 6 y 8 — NEGOCIADOR Y CRM
# =============================================================================
class Contact(TimestampMixin, Base):
    """Contraparte (vendedor o comprador). Datos mínimos; evitar datos personales innecesarios."""

    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    alias: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)  # whatsapp|telegram|mercadolibre|...
    channel_handle: Mapped[str | None] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # seller | buyer | both
    tags: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    opt_in_messaging: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # buy | sell
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id", ondelete="SET NULL"))
    inventory_item_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_items.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    rounds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    agent_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # False = humano al mando
    last_agent_offer: Mapped[Decimal | None] = mapped_column(Money)
    last_counterpart_offer: Mapped[Decimal | None] = mapped_column(Money)
    external_thread_id: Mapped[str | None] = mapped_column(String(128))
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.id"
    )
    contact: Mapped[Contact | None] = relationship()


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    sender: Mapped[str] = mapped_column(String(16), nullable=False)  # agent|operator|counterpart
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_amount: Mapped[Decimal | None] = mapped_column(Money)
    intent: Mapped[str | None] = mapped_column(String(32))  # ask_info|offer|counter|accept|reject|escalate
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approval_requests.id", ondelete="SET NULL"))
    external_message_id: Mapped[str | None] = mapped_column(String(128))
    policy_check: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    injection_flags: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    llm_meta: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


# =============================================================================
# MÓDULO 7 — VISITAS Y TALLERES
# =============================================================================
class Visit(TimestampMixin, Base):
    __tablename__ = "visits"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # inspection|pickup|workshop|delivery
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id", ondelete="SET NULL"))
    inventory_item_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_items.id", ondelete="SET NULL"))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="SET NULL"))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    location: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    outcome: Mapped[str] = mapped_column(Text, nullable=False, default="")


# =============================================================================
# MÓDULO 9 — INVENTARIO Y FINANZAS
# =============================================================================
class InventoryItem(TimestampMixin, Base):
    """Moto de nuestra propiedad: la ficha centralizada desde la compra hasta la venta."""

    __tablename__ = "inventory_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id", ondelete="SET NULL"), unique=True)
    make: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    year: Mapped[int | None] = mapped_column(Integer)
    kilometers: Mapped[int | None] = mapped_column(Integer)
    color: Mapped[str | None] = mapped_column(String(32))
    plate_masked: Mapped[str | None] = mapped_column(String(16))  # p. ej. "A***123"
    purchase_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="acquired")
    list_price: Mapped[Decimal | None] = mapped_column(Money)
    min_sale_price: Mapped[Decimal | None] = mapped_column(Money)  # autorizado por humano para el agente vendedor
    min_sale_authorized_by: Mapped[str | None] = mapped_column(String(64))
    sold_price: Mapped[Decimal | None] = mapped_column(Money)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tradein_of_sale_id: Mapped[int | None] = mapped_column(Integer)  # si entró como permuta en una venta
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    deal: Mapped[Deal | None] = relationship(back_populates="inventory_item")
    expenses: Mapped[list[Expense]] = relationship(back_populates="item", cascade="all, delete-orphan")


class Expense(TimestampMixin, Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    memo: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="SET NULL"))
    incurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    item: Mapped[InventoryItem] = relationship(back_populates="expenses")


class CapitalLedger(Base):
    """Libro de caja/capital. Cada movimiento es una fila; el saldo se calcula, nunca se edita."""

    __tablename__ = "capital_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)  # positivo entra, negativo sale
    inventory_item_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_items.id", ondelete="SET NULL"))
    memo: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")


class Sale(TimestampMixin, Base):
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    tradein_value: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    cash_received: Mapped[Decimal] = mapped_column(Money, nullable=False)
    sold_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approval_requests.id", ondelete="SET NULL"))
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


# =============================================================================
# MÓDULO 7b — PUBLICACIONES DE VENTA
# =============================================================================
class Publication(TimestampMixin, Base):
    __tablename__ = "publications"
    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    photo_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    external_id: Mapped[str | None] = mapped_column(String(128))
    external_url: Mapped[str | None] = mapped_column(String(512))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inquiries_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    views_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# =============================================================================
# MÓDULO 10 — PRECIOS DINÁMICOS
# =============================================================================
class PriceRecommendation(TimestampMixin, Base):
    __tablename__ = "price_recommendations"
    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    recommended_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    inputs: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending|applied|rejected
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approval_requests.id", ondelete="SET NULL"))
    rules_hash: Mapped[str] = mapped_column(String(64), nullable=False)


# =============================================================================
# MÓDULO 11 — PERMUTAS
# =============================================================================
class TradeInEvaluation(TimestampMixin, Base):
    __tablename__ = "tradein_evaluations"
    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False)
    offered_make: Mapped[str] = mapped_column(String(64), nullable=False)
    offered_model: Mapped[str] = mapped_column(String(64), nullable=False)
    offered_year: Mapped[int | None] = mapped_column(Integer)
    offered_kilometers: Mapped[int | None] = mapped_column(Integer)
    tradein_valuation_id: Mapped[int | None] = mapped_column(ForeignKey("valuations.id", ondelete="SET NULL"))
    tradein_value: Mapped[Decimal] = mapped_column(Money, nullable=False)
    cash_difference: Mapped[Decimal] = mapped_column(Money, nullable=False)
    combined_profit: Mapped[Decimal] = mapped_column(Money, nullable=False)
    combined_roi: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)  # accept|counter|reject
    details: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    rules_hash: Mapped[str] = mapped_column(String(64), nullable=False)
