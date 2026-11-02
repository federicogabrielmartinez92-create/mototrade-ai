"""Centro de Control — configuración operativa editable desde la app y persistida en la base.

Separación de responsabilidades:
- `config/business_rules.yaml`: FÓRMULAS y constantes técnicas (cómo se calcula). Lo edita un humano en el repo.
- `app_settings` (esta configuración): PARÁMETROS OPERATIVOS (capital, límites, estrategia, agentes, presupuesto de IA).
  Se edita desde el panel, queda auditada y la aplican obligatoriamente API y workers vía `ControlCenter.effective_rules()`.
Donde ambos definen lo mismo (ROI mínimo, capital, apertura de oferta, rondas, modelos prioritarios), MANDA la base.
"""
from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ModelRef(_Section):
    make: str
    model: str


class CapitalSettings(_Section):
    capital_total: Decimal = Field(default=Decimal("4000000"), ge=0, description="Capital total asignado al negocio (ARS)")
    min_reserve_amount: Decimal = Field(default=Decimal("300000"), ge=0, description="Reserva mínima intocable (ARS)")
    min_reserve_pct: Decimal = Field(default=Decimal("0.10"), ge=0, lt=1, description="Reserva mínima como % del capital total (se usa el mayor)")
    max_units: int = Field(default=2, ge=0, le=100, description="Máximo de motos simultáneas (en stock + compras pendientes)")
    max_unit_price: Decimal = Field(default=Decimal("2500000"), ge=0, description="Precio máximo de compra por unidad (ARS)")
    max_committed_capital: Decimal = Field(default=Decimal("3600000"), ge=0, description="Capital máximo comprometido en stock + compras pendientes (ARS)")
    max_repair_cost: Decimal = Field(default=Decimal("400000"), ge=0, description="Gasto máximo de reparación por moto (ARS)")
    min_unit_budget: Decimal = Field(default=Decimal("800000"), ge=0, description="Capital libre mínimo para que tenga sentido seguir buscando (ARS)")
    pending_cost_buffer_pct: Decimal = Field(default=Decimal("0.15"), ge=0, lt=1, description="Colchón sobre compras pendientes para reservar gastos")


class SearchSettings(_Section):
    auto_search_enabled: bool = Field(default=True, description="Intención: buscar y puntuar oportunidades automáticamente")
    negotiable_mode_enabled: bool = Field(default=True, description="Analizar oportunidades negociables (modo B)")
    search_interval_minutes: int = Field(default=30, ge=5, le=1440)
    radius_km: int = Field(default=60, ge=0, le=1000, description="Radio desde Rosario (informativo hasta tener geolocalización)")
    priority_models: list[ModelRef] = Field(default_factory=lambda: [
        ModelRef(make="Honda", model="Wave 110"), ModelRef(make="Gilera", model="Smash 110"),
        ModelRef(make="Corven", model="Energy 110"), ModelRef(make="Motomel", model="Blitz 110"),
        ModelRef(make="Honda", model="GLH 150"), ModelRef(make="Yamaha", model="YBR 125"),
        ModelRef(make="Bajaj", model="Rouser NS200"),
    ])
    allow_any_model: bool = Field(default=True, description="Permitir oportunidades extraordinarias en modelos fuera de la lista")
    excluded_models: list[ModelRef] = Field(default_factory=list)
    excluded_makes: list[str] = Field(default_factory=list)
    min_alert_discount_pct: Decimal = Field(default=Decimal("0.15"), ge=0, lt=1, description="Descuento mínimo para alertar")
    target_discount_min: Decimal = Field(default=Decimal("0.15"), ge=0, lt=1)
    target_discount_max: Decimal = Field(default=Decimal("0.25"), ge=0, lt=1)
    extraordinary_discount_pct: Decimal = Field(default=Decimal("0.25"), ge=0, lt=1, description="Umbral de oportunidad extraordinaria (requiere verificación extra)")
    minimum_roi: Decimal = Field(default=Decimal("0.10"), ge=0, lt=1, description="Rentabilidad mínima sobre costo total")
    target_sale_days_min: int = Field(default=15, ge=1)
    target_sale_days_max: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _coherent(self):
        if self.target_discount_max < self.target_discount_min:
            raise ValueError("target_discount_max debe ser >= target_discount_min")
        if self.target_sale_days_max < self.target_sale_days_min:
            raise ValueError("target_sale_days_max debe ser >= target_sale_days_min")
        return self


class AgentsSettings(_Section):
    buyer_agent_enabled: bool = True
    seller_agent_enabled: bool = True
    auto_send_enabled: bool = Field(default=False, description="Enviar automáticamente en canales autorizados (hoy ninguno lo está)")
    visual_inspection_enabled: bool = True
    parts_search_enabled: bool = True
    buyer_opening_pct_of_max: Decimal = Field(default=Decimal("0.88"), gt=0, le=1)
    buyer_step_pct: Decimal = Field(default=Decimal("0.03"), ge=0, lt=1)
    buyer_max_rounds: int = Field(default=4, ge=1, le=20)
    seller_step_pct: Decimal = Field(default=Decimal("0.02"), ge=0, lt=1)
    seller_max_rounds: int = Field(default=4, ge=1, le=20)
    daily_message_cap_per_conversation: int = Field(default=6, ge=1, le=50)
    require_admin_above_amount: Decimal = Field(default=Decimal("0"), ge=0, description="Reservado: montos sobre este valor exigen admin (0 = siempre)")


class AIBudgetSettings(_Section):
    monthly_budget_usd: Decimal = Field(default=Decimal("10"), ge=0)
    daily_budget_usd: Decimal = Field(default=Decimal("1"), ge=0)
    max_visual_analyses_per_month: int = Field(default=60, ge=0)
    max_deep_analyses_per_day: int = Field(default=10, ge=0)
    notification_frequency_minutes: int = Field(default=30, ge=1, le=1440)
    # Precios de referencia USD por millón de tokens (input, output). Editables; ver docs/INTEGRACIONES.md.
    model_prices: dict[str, list[Decimal]] = Field(default_factory=lambda: {
        "claude-haiku-5-5": [Decimal("0.10"), Decimal("0.50")],
        "claude-sonnet-5-5": [Decimal("2"), Decimal("10")],
        "claude-sonnet-4-5": [Decimal("3"), Decimal("15")],
        "claude-opus-5-5": [Decimal("4"), Decimal("20")],
        "fake-rules-v1": [Decimal("0"), Decimal("0")],
    })


class ControlSettings(_Section):
    capital: CapitalSettings = Field(default_factory=CapitalSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    agents: AgentsSettings = Field(default_factory=AgentsSettings)
    ai_budget: AIBudgetSettings = Field(default_factory=AIBudgetSettings)

    SECTIONS: ClassVar[tuple[str, ...]] = ("capital", "search", "agents", "ai_budget")
