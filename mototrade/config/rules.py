"""Reglas de negocio versionadas y validadas.

- Se cargan desde `config/business_rules.yaml`.
- Se validan con Pydantic: un valor fuera de rango impide arrancar (fail-fast).
- Son INMUTABLES en memoria (`frozen=True`): ningún módulo —y menos un agente de IA—
  puede alterarlas en tiempo de ejecución.
- `rules_hash` identifica la versión exacta usada en cada tasación/decisión.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Fraction = Decimal  # alias semántico: valores entre 0 y 1


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CapitalRules(_Frozen):
    initial_capital: Decimal = Field(ge=0)
    max_capital_per_unit_pct: Fraction = Field(gt=0, le=1)
    min_cash_reserve_pct: Fraction = Field(ge=0, lt=1)


class ValuationRules(_Frozen):
    min_comparables: int = Field(ge=1)
    comparables_max_age_days: int = Field(ge=1)
    listing_to_sale_discount: Fraction = Field(ge=0, lt=1)
    quick_sale_discount: Fraction = Field(ge=0, lt=1)
    publish_markup: Fraction = Field(ge=0, lt=1)
    deal_basis: Literal["quick_sale", "market"] = "quick_sale"
    minimum_roi: Fraction = Field(ge=0, lt=1)
    target_roi: Fraction = Field(ge=0, lt=1)
    downside_scenario_discount: Fraction = Field(ge=0, lt=1)
    severe_scenario_discount: Fraction = Field(ge=0, lt=1)
    severe_scenario_cost_overrun: Fraction = Field(ge=0, lt=2)
    contingency_reserve_pct: Fraction = Field(ge=0, lt=1)
    contingency_reserve_min: Decimal = Field(ge=0)
    offer_rounding: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def _coherence(self):
        if self.target_roi < self.minimum_roi:
            raise ValueError("target_roi no puede ser menor que minimum_roi")
        if self.severe_scenario_discount < self.downside_scenario_discount:
            raise ValueError("severe_scenario_discount debe ser >= downside_scenario_discount")
        return self


class CostRules(_Frozen):
    transfer_registral_pct: Fraction = Field(ge=0, lt=1)
    transfer_registral_min: Decimal = Field(ge=0)
    transfer_sellos_pct: Fraction = Field(ge=0, lt=1)
    transfer_sellos_buyer_share: Fraction = Field(ge=0, le=1)
    transfer_verification_fixed: Decimal = Field(ge=0)
    transfer_misc_fixed: Decimal = Field(ge=0)
    logistics_default: Decimal = Field(ge=0)
    preparation_default: Decimal = Field(ge=0)
    marketing_default: Decimal = Field(ge=0)


class TaxRules(_Frozen):
    regime: Literal["monotributo", "responsable_inscripto"]
    iva_margin_pct: Fraction = Field(ge=0, lt=1)
    iibb_pct: Fraction = Field(ge=0, lt=1)
    iibb_min_base_pct_of_sale: Fraction = Field(ge=0, lt=1)
    sales_tax_pct: Fraction = Field(ge=0, lt=1)
    notes: str = ""


class IdentityRules(_Frozen):
    business_name: str
    operator_first_name: str = "el responsable"
    disclose_automation: bool = True


class BuyerNegotiationRules(_Frozen):
    opening_offer_pct_of_max: Fraction = Field(gt=0, le=1)
    step_pct: Fraction = Field(ge=0, lt=1)
    max_rounds: int = Field(ge=1, le=20)


class SellerNegotiationRules(_Frozen):
    min_price_pct_of_list: Fraction = Field(gt=0, le=1)
    step_pct: Fraction = Field(ge=0, lt=1)
    max_rounds: int = Field(ge=1, le=20)


class NegotiationRules(_Frozen):
    buyer: BuyerNegotiationRules
    seller: SellerNegotiationRules
    daily_message_cap_per_conversation: int = Field(ge=1, le=50)
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "08:00"

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        h, m = v.split(":")
        if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError("hora inválida")
        return v


class PricingRules(_Frozen):
    days_in_stock_thresholds: list[int]
    price_cut_steps: list[Fraction]
    min_inquiries_per_week_ok: int = Field(ge=0)
    max_total_cut_pct: Fraction = Field(ge=0, lt=1)

    @model_validator(mode="after")
    def _same_len(self):
        if len(self.days_in_stock_thresholds) != len(self.price_cut_steps):
            raise ValueError("days_in_stock_thresholds y price_cut_steps deben tener la misma longitud")
        if self.days_in_stock_thresholds != sorted(self.days_in_stock_thresholds):
            raise ValueError("days_in_stock_thresholds debe estar ordenado")
        return self


class AnomalyRules(_Frozen):
    km_adjustment_pct_per_10k: Fraction = Field(ge=0, lt=1)
    km_adjustment_cap: Fraction = Field(ge=0, lt=1)
    dealer_premium_pct: Fraction = Field(ge=0, lt=1)
    high_km_per_year: int = Field(ge=1000)
    high_dispersion_iqr: Fraction = Field(gt=0, lt=2)
    stale_listing_days: int = Field(ge=1)
    too_cheap_scam_pct: Fraction = Field(gt=0, lt=1)
    version_match_bonus: Fraction = Field(ge=0, lt=1)


class TradeInRules(_Frozen):
    tradein_haircut_pct: Fraction = Field(ge=0, lt=1)


class WatchlistEntry(_Frozen):
    make: str
    model: str


class RadarRules(_Frozen):
    opportunity_min_score: int = Field(ge=0, le=100)
    notify_price_drop_pct: Fraction = Field(ge=0, lt=1)
    watchlist: list[WatchlistEntry]
    locations: list[str]


class BusinessRules(_Frozen):
    version: int
    currency: str = "ARS"
    capital: CapitalRules
    valuation: ValuationRules
    costs: CostRules
    taxes: TaxRules
    identity: IdentityRules
    negotiation: NegotiationRules
    pricing: PricingRules
    anomaly: AnomalyRules
    tradein: TradeInRules
    radar: RadarRules
    # Metadatos de trazabilidad (no vienen del YAML)
    rules_hash: str = ""
    source_path: str = ""

    def summary(self) -> dict:
        return {
            "version": self.version,
            "rules_hash": self.rules_hash,
            "minimum_roi": str(self.valuation.minimum_roi),
            "max_capital_per_unit_pct": str(self.capital.max_capital_per_unit_pct),
            "initial_capital": str(self.capital.initial_capital),
        }


def _to_decimal_tree(obj):
    """Convierte floats/ints del YAML a Decimal para evitar errores de coma flotante."""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return Decimal(repr(obj))
    if isinstance(obj, int):
        return obj
    if isinstance(obj, dict):
        return {k: _to_decimal_tree(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_decimal_tree(v) for v in obj]
    return obj


def load_rules(path: str | Path) -> BusinessRules:
    path = Path(path)
    raw_bytes = path.read_bytes()
    data = yaml.safe_load(raw_bytes) or {}
    data = _to_decimal_tree(data)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    return BusinessRules(**data, rules_hash=digest, source_path=str(path))


@lru_cache(maxsize=4)
def _cached_rules(path: str) -> BusinessRules:
    return load_rules(path)


def get_rules(path: str | Path | None = None) -> BusinessRules:
    if path is None:
        from .settings import get_settings

        path = get_settings().rules_path
    return _cached_rules(str(path))
