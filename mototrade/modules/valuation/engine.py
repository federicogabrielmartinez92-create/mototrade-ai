"""Motor de tasación y matemática del negocio (determinista, sin IA).

Entradas: comparables de mercado + reglas de negocio + costos.
Salidas: los 15 resultados exigidos (precio de mercado, venta rápida, publicación,
oferta inicial, compra máxima, desglose de costos, ganancia, ROI, escenarios...)
con fuentes, fecha, nivel de confianza y hash de reglas.

Principios:
- Precio PEDIDO (asking) ≠ precio de VENTA (sold). Si solo hay anuncios, se aplica
  `listing_to_sale_discount` y se informa `basis = "asking_adjusted"`.
- Todas las fórmulas viven acá y en `business_rules.yaml`; la IA no las toca.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from statistics import median
from typing import Iterable, Sequence

from mototrade.config.rules import BusinessRules
from mototrade.core.enums import Confidence
from mototrade.core.money import D, ONE, ZERO, money, round_down_to


# =============================================================================
# 1. Estimación del precio de mercado a partir de comparables
# =============================================================================
@dataclass(frozen=True)
class ComparablePoint:
    price: Decimal
    kind: str  # "asking" | "sold"
    observed_at: datetime
    year: int | None = None
    kilometers: int | None = None
    source: str = ""
    source_ref: str | None = None
    ref_id: int | None = None


@dataclass(frozen=True)
class MarketEstimate:
    market_price: Decimal
    basis: str  # sold | asking_adjusted | manual | insufficient
    confidence_score: Decimal
    confidence: str
    comparables_used: int
    p25: Decimal | None
    p75: Decimal | None
    median_age_days: Decimal | None
    median_km: int | None
    sources: list[dict]
    notes: list[str]
    computed_at: datetime

    @property
    def requires_human_review(self) -> bool:
        return self.confidence == Confidence.LOW or self.basis in ("insufficient", "manual")


def _percentile(sorted_vals: Sequence[Decimal], q: Decimal) -> Decimal:
    if not sorted_vals:
        return ZERO
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return money(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)


def _age_days(observed_at: datetime, now: datetime) -> Decimal:
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return D((now - observed_at).total_seconds() / 86400)


def confidence_from(n: int, dispersion: Decimal | None, median_age: Decimal | None, basis: str,
                    rules: BusinessRules) -> tuple[Decimal, str]:
    """Score 0–1 determinista y etiqueta. Fórmula documentada en docs/ARQUITECTURA.md."""
    v = rules.valuation
    if n < v.min_comparables:
        n_score = D(n) / D(v.min_comparables) * D("0.3")  # nunca más de 0.3 con pocos datos
    else:
        n_score = min(D(n) / D(v.min_comparables * 2), ONE)
    disp_score = ONE if dispersion is None else max(ZERO, ONE - dispersion / D("0.30"))
    rec_score = ONE if median_age is None else max(ZERO, ONE - median_age / D(v.comparables_max_age_days))
    score = D("0.45") * n_score + D("0.30") * disp_score + D("0.25") * rec_score
    if basis == "sold":
        score = min(ONE, score + D("0.10"))
    score = score.quantize(D("0.0001"))
    if n < v.min_comparables:
        label = Confidence.LOW
    elif score >= D("0.70"):
        label = Confidence.HIGH
    elif score >= D("0.40"):
        label = Confidence.MEDIUM
    else:
        label = Confidence.LOW
    return score, str(label)


def estimate_market_price(comparables: Iterable[ComparablePoint], rules: BusinessRules,
                          now: datetime | None = None) -> MarketEstimate:
    now = now or datetime.now(timezone.utc)
    v = rules.valuation
    fresh = [c for c in comparables if _age_days(c.observed_at, now) <= v.comparables_max_age_days]
    sold = [c for c in fresh if c.kind == "sold"]
    asking = [c for c in fresh if c.kind == "asking"]
    notes: list[str] = []

    if len(sold) >= v.min_comparables:
        used, basis = sold, "sold"
        prices = sorted(c.price for c in used)
        market = _percentile(prices, D("0.5"))
        notes.append(f"Base: {len(used)} ventas efectivas (mediana).")
    elif asking:
        used, basis = asking, "asking_adjusted"
        prices = sorted(c.price for c in used)
        raw_median = _percentile(prices, D("0.5"))
        market = money(raw_median * (ONE - v.listing_to_sale_discount))
        notes.append(
            f"Base: {len(used)} precios PEDIDOS (mediana {raw_median:,.0f}) menos {v.listing_to_sale_discount:.0%} "
            "de descuento publicación→venta (supuesto configurable)."
        )
        if sold:
            notes.append(f"Hay {len(sold)} venta(s) efectiva(s) registradas, insuficientes para usar como base.")
    else:
        return MarketEstimate(
            market_price=ZERO, basis="insufficient", confidence_score=ZERO, confidence=str(Confidence.LOW),
            comparables_used=0, p25=None, p75=None, median_age_days=None, median_km=None, sources=[],
            notes=["Sin comparables vigentes: cargar comparables o ingresar precio de mercado manual."],
            computed_at=now,
        )

    p25, p75 = _percentile(prices, D("0.25")), _percentile(prices, D("0.75"))
    med = _percentile(prices, D("0.5"))
    dispersion = (p75 - p25) / med if med else None
    ages = [_age_days(c.observed_at, now) for c in used]
    median_age = D(median(ages)).quantize(D("0.1")) if ages else None
    kms = [c.kilometers for c in used if c.kilometers is not None]
    median_km = int(median(kms)) if kms else None
    score, label = confidence_from(len(used), dispersion, median_age, basis, rules)
    if dispersion is not None and dispersion > D("0.25"):
        notes.append(f"Dispersión alta entre comparables (IQR/mediana = {dispersion:.0%}).")
    if len(used) < v.min_comparables:
        notes.append(f"Menos de {v.min_comparables} comparables: revisión humana obligatoria.")
    sources = [
        {"id": c.ref_id, "kind": c.kind, "price": str(c.price), "source": c.source, "ref": c.source_ref,
         "observed_at": c.observed_at.isoformat(), "year": c.year, "km": c.kilometers}
        for c in used
    ]
    return MarketEstimate(
        market_price=money(market), basis=basis, confidence_score=score, confidence=label,
        comparables_used=len(used), p25=p25, p75=p75, median_age_days=median_age, median_km=median_km,
        sources=sources, notes=notes, computed_at=now,
    )


def manual_market_estimate(market_price: Decimal, note: str = "", now: datetime | None = None) -> MarketEstimate:
    now = now or datetime.now(timezone.utc)
    return MarketEstimate(
        market_price=money(market_price), basis="manual", confidence_score=D("0.25"), confidence=str(Confidence.LOW),
        comparables_used=0, p25=None, p75=None, median_age_days=None, median_km=None,
        sources=[{"kind": "manual", "note": note}], notes=["Precio de mercado ingresado manualmente por el operador."],
        computed_at=now,
    )


# =============================================================================
# 2. Matemática del negocio
# =============================================================================
@dataclass(frozen=True)
class CostInputs:
    """Costos específicos. None → se usa el valor por defecto de las reglas."""

    repair_parts: Decimal = ZERO
    repair_labor: Decimal = ZERO
    repair_shipping: Decimal = ZERO
    transfer: Decimal | None = None
    logistics: Decimal | None = None
    preparation: Decimal | None = None
    marketing: Decimal | None = None
    sources: dict = field(default_factory=dict)  # de dónde salió cada número (para auditoría)

    def fixed(self, rules: BusinessRules) -> dict[str, Decimal]:
        """Costos que NO dependen del precio de compra."""
        c = rules.costs
        return {
            "repair_parts": money(self.repair_parts),
            "repair_labor": money(self.repair_labor),
            "repair_shipping": money(self.repair_shipping),
            "logistics": money(c.logistics_default if self.logistics is None else self.logistics),
            "preparation": money(c.preparation_default if self.preparation is None else self.preparation),
            "marketing": money(c.marketing_default if self.marketing is None else self.marketing),
        }


def estimate_transfer_cost(purchase_price: Decimal, rules: BusinessRules) -> Decimal:
    """Costo de transferencia al COMPRAR (arancel registral + mitad de sellos + fijos).
    Usa el precio de compra como aproximación del valor de tabla DNRPA."""
    c = rules.costs
    p = D(purchase_price)
    registral = max(p * c.transfer_registral_pct, c.transfer_registral_min)
    sellos = p * c.transfer_sellos_pct * c.transfer_sellos_buyer_share
    return money(registral + sellos + c.transfer_verification_fixed + c.transfer_misc_fixed)


def estimate_taxes(sale_price: Decimal, purchase_price: Decimal, rules: BusinessRules) -> dict[str, Decimal]:
    """Impuestos estimados sobre la operación (modelo simplificado, parametrizado por el contador)."""
    t = rules.taxes
    sale, purchase = D(sale_price), D(purchase_price)
    margin = max(ZERO, sale - purchase)
    iva = margin * t.iva_margin_pct if t.regime == "responsable_inscripto" else ZERO
    iibb_base = max(margin, sale * t.iibb_min_base_pct_of_sale)
    iibb = iibb_base * t.iibb_pct
    other = sale * t.sales_tax_pct
    return {"iva": money(iva), "iibb": money(iibb), "other": money(other), "total": money(iva + iibb + other)}


@dataclass(frozen=True)
class Scenario:
    name: str
    sale_price: Decimal
    total_costs: Decimal
    total_investment: Decimal
    profit: Decimal
    roi: Decimal

    def as_dict(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in self.__dict__.items()}


@dataclass(frozen=True)
class DealMath:
    market_price: Decimal
    quick_sale_price: Decimal
    suggested_list_price: Decimal
    sale_basis_price: Decimal        # precio usado para calcular (quick_sale o market según reglas)
    initial_offer: Decimal
    max_purchase_price: Decimal
    evaluated_purchase_price: Decimal  # precio al que se evaluó (pedido u oferta)
    costs: dict[str, Decimal]        # desglose
    total_costs: Decimal
    total_investment: Decimal
    expected_profit: Decimal
    expected_roi: Decimal
    meets_minimum_roi: bool
    within_capital_limit: bool | None
    scenarios: dict[str, Scenario]
    rules_hash: str
    warnings: list[str]

    def costs_as_str(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.costs.items()}

    def scenarios_as_dict(self) -> dict[str, dict]:
        return {k: s.as_dict() for k, s in self.scenarios.items()}


def _contingency(purchase_price: Decimal, rules: BusinessRules) -> Decimal:
    v = rules.valuation
    return money(max(purchase_price * v.contingency_reserve_pct, v.contingency_reserve_min))


def _costs_at(purchase_price: Decimal, inputs: CostInputs, rules: BusinessRules, sale_price: Decimal,
              overrun: Decimal = ZERO) -> dict[str, Decimal]:
    """Desglose completo de costos para un precio de compra y un precio de venta dados."""
    out = inputs.fixed(rules)
    out["transfer"] = money(inputs.transfer) if inputs.transfer is not None else estimate_transfer_cost(purchase_price, rules)
    out["contingency"] = _contingency(purchase_price, rules)
    if overrun:
        out = {k: money(v * (ONE + overrun)) for k, v in out.items()}
    out["taxes_estimate"] = estimate_taxes(sale_price, purchase_price, rules)["total"]
    return out


def _roi_at(purchase_price: Decimal, inputs: CostInputs, rules: BusinessRules, sale_price: Decimal) -> Decimal:
    costs = sum(_costs_at(purchase_price, inputs, rules, sale_price).values(), ZERO)
    total = purchase_price + costs
    return (sale_price - total) / total if total else ZERO


def _max_purchase(sale_basis: Decimal, inputs: CostInputs, rules: BusinessRules) -> Decimal:
    """Mayor precio de compra (múltiplo de `offer_rounding`) tal que ROI(p) >= minimum_roi.
    ROI(p) es monótona decreciente en p, así que se resuelve por búsqueda binaria sobre múltiplos
    del redondeo. Esto funciona con cualquier modelo de costos (transferencia e impuestos
    dependientes del precio) sin depender de una solución algebraica."""
    v = rules.valuation
    step = v.offer_rounding
    hi = int(sale_basis // step)
    if hi <= 0 or _roi_at(ZERO, inputs, rules, sale_basis) < v.minimum_roi:
        return ZERO
    lo = 0  # ROI(0) >= min garantizado arriba
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _roi_at(D(mid) * step, inputs, rules, sale_basis) >= v.minimum_roi:
            lo = mid
        else:
            hi = mid - 1
    return money(D(lo) * step)


def compute_deal(
    market_price: Decimal,
    purchase_price: Decimal,
    cost_inputs: CostInputs,
    rules: BusinessRules,
    *,
    available_capital: Decimal | None = None,
) -> DealMath:
    """Calcula toda la matemática del negocio para una moto, a un precio de compra dado."""
    v = rules.valuation
    market_price = money(market_price)
    purchase_price = money(purchase_price)
    warnings: list[str] = []

    quick_sale = money(market_price * (ONE - v.quick_sale_discount))
    list_price = money(market_price * (ONE + v.publish_markup))
    sale_basis = quick_sale if v.deal_basis == "quick_sale" else market_price

    max_purchase = _max_purchase(sale_basis, cost_inputs, rules)
    initial_offer = round_down_to(max_purchase * rules.negotiation.buyer.opening_offer_pct_of_max, v.offer_rounding)

    costs = _costs_at(purchase_price, cost_inputs, rules, sale_basis)
    total_costs = sum(costs.values(), ZERO)
    total_investment = purchase_price + total_costs
    profit = money(sale_basis - total_investment)
    roi = (profit / total_investment).quantize(D("0.0001")) if total_investment else ZERO
    meets = roi >= v.minimum_roi

    within_capital: bool | None = None
    if available_capital is not None:
        cap = available_capital * rules.capital.max_capital_per_unit_pct
        within_capital = total_investment <= cap
        if not within_capital:
            warnings.append(
                f"La inversión total ({total_investment:,.0f}) supera el {rules.capital.max_capital_per_unit_pct:.0%} "
                f"del capital disponible ({cap:,.0f})."
            )
    if purchase_price > max_purchase:
        warnings.append(f"El precio evaluado supera la compra máxima ({max_purchase:,.0f}).")
    if max_purchase == ZERO:
        warnings.append("No hay precio de compra que cumpla el ROI mínimo con estos costos.")

    def scen(name: str, sale: Decimal, overrun: Decimal = ZERO) -> Scenario:
        c = _costs_at(purchase_price, cost_inputs, rules, sale, overrun)
        tc = sum(c.values(), ZERO)
        ti = purchase_price + tc
        pr = money(sale - ti)
        return Scenario(name, money(sale), tc, ti, pr, (pr / ti).quantize(D("0.0001")) if ti else ZERO)

    scenarios = {
        "base": scen("base", sale_basis),
        "market": scen("market", market_price),
        "adverse": scen("adverse", sale_basis * (ONE - v.downside_scenario_discount)),
        "severe": scen("severe", sale_basis * (ONE - v.severe_scenario_discount), v.severe_scenario_cost_overrun),
    }

    return DealMath(
        market_price=market_price, quick_sale_price=quick_sale, suggested_list_price=list_price,
        sale_basis_price=sale_basis, initial_offer=initial_offer, max_purchase_price=max_purchase,
        evaluated_purchase_price=purchase_price, costs=costs, total_costs=total_costs,
        total_investment=total_investment, expected_profit=profit, expected_roi=roi, meets_minimum_roi=meets,
        within_capital_limit=within_capital, scenarios=scenarios, rules_hash=rules.rules_hash, warnings=warnings,
    )
