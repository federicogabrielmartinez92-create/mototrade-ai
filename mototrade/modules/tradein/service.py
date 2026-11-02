"""Módulo 11 — Evaluador de permutas.

Una permuta son DOS operaciones: vendemos la moto A y compramos la moto B (la que
recibimos). La rentabilidad integral combina ambas:
  - valor de toma de B = compra máxima de B (según tasador) × (1 − haircut)
  - diferencia en efectivo = precio de venta de A − valor de toma de B
  - ganancia combinada = ganancia de A (al precio de venta) + ganancia esperada de B
    (comprándola al valor de toma)
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from mototrade.config.rules import BusinessRules
from mototrade.core import audit
from mototrade.core.money import D, ONE, ZERO, money, round_down_to
from mototrade.db.models import InventoryItem, TradeInEvaluation
from mototrade.modules.finance.service import FinanceService
from mototrade.modules.valuation.engine import CostInputs, DealMath, MarketEstimate, compute_deal
from mototrade.modules.valuation.service import ValuationService


@dataclass(frozen=True)
class TradeInResult:
    tradein_value: Decimal
    cash_difference: Decimal
    profit_a: Decimal
    expected_profit_b: Decimal
    combined_profit: Decimal
    combined_investment: Decimal
    combined_roi: Decimal
    verdict: str
    notes: list[str]
    b_math: DealMath
    b_estimate: MarketEstimate


def evaluate_tradein(*, sale_price_a: Decimal, cost_a: Decimal, taxes_a: Decimal, b_market: MarketEstimate,
                     b_costs: CostInputs, rules: BusinessRules, min_sale_a: Decimal | None = None) -> TradeInResult:
    notes: list[str] = []
    sale_a, cost_a, taxes_a = D(sale_price_a), D(cost_a), D(taxes_a)
    # 1) ¿Cuánto vale B para nosotros? Compra máxima con haircut de permuta
    probe = compute_deal(b_market.market_price, ZERO, b_costs, rules)
    tradein_value = round_down_to(probe.max_purchase_price * (ONE - rules.tradein.tradein_haircut_pct),
                                  rules.valuation.offer_rounding)
    b_math = compute_deal(b_market.market_price, tradein_value, b_costs, rules)
    # 2) Operación A
    profit_a = money(sale_a - cost_a - taxes_a)
    cash = money(sale_a - tradein_value)
    combined_profit = money(profit_a + b_math.expected_profit)
    combined_investment = cost_a + b_math.total_investment
    combined_roi = (combined_profit / combined_investment).quantize(D("0.0001")) if combined_investment else ZERO
    # 3) Veredicto
    verdict = "accept"
    if b_market.requires_human_review:
        notes.append("Tasación de la moto recibida con confianza baja: inspección y revisión humana obligatorias.")
        verdict = "counter"
    if min_sale_a is not None and sale_a < D(min_sale_a):
        notes.append("El precio de venta de A está por debajo del mínimo autorizado.")
        verdict = "reject"
    if combined_roi < rules.valuation.minimum_roi:
        notes.append(f"ROI combinado {combined_roi:.1%} < mínimo {rules.valuation.minimum_roi:.0%}.")
        verdict = "reject" if verdict != "reject" else verdict
    if tradein_value <= 0:
        notes.append("La moto recibida no tiene valor de toma positivo con estos costos.")
        verdict = "reject"
    if cash < 0:
        notes.append("Diferencia negativa: deberíamos poner dinero; revisar.")
        verdict = "reject" if verdict == "accept" else verdict
    return TradeInResult(tradein_value=tradein_value, cash_difference=cash, profit_a=profit_a,
                         expected_profit_b=b_math.expected_profit, combined_profit=combined_profit,
                         combined_investment=money(combined_investment), combined_roi=combined_roi, verdict=verdict,
                         notes=notes, b_math=b_math, b_estimate=b_market)


class TradeInService:
    def __init__(self, session: Session, rules: BusinessRules):
        self.session = session
        self.rules = rules

    def evaluate(self, item_a: InventoryItem, *, sale_price_a: Decimal, offered_make: str, offered_model: str,
                 offered_year: int | None, offered_km: int | None, b_costs: CostInputs | None = None,
                 manual_b_market_price: Decimal | None = None, by: str = "operator") -> tuple[TradeInEvaluation, TradeInResult]:
        fin = FinanceService(self.session, self.rules)
        pnl = fin.unit_pnl(item_a)
        from mototrade.modules.valuation.engine import estimate_taxes

        taxes_a = estimate_taxes(D(sale_price_a), D(item_a.purchase_price), self.rules)["total"]
        vs = ValuationService(self.session, self.rules)
        b_market = vs.estimate(offered_make, offered_model, offered_year, manual_market_price=manual_b_market_price)
        res = evaluate_tradein(sale_price_a=D(sale_price_a), cost_a=pnl.total_cost, taxes_a=taxes_a, b_market=b_market,
                               b_costs=b_costs or CostInputs(), rules=self.rules, min_sale_a=item_a.min_sale_price)
        row = TradeInEvaluation(
            inventory_item_id=item_a.id, offered_make=offered_make, offered_model=offered_model, offered_year=offered_year,
            offered_kilometers=offered_km, tradein_value=res.tradein_value, cash_difference=res.cash_difference,
            combined_profit=res.combined_profit, combined_roi=res.combined_roi, verdict=res.verdict,
            details={"notes": res.notes, "profit_a": str(res.profit_a), "expected_profit_b": str(res.expected_profit_b),
                     "b_market_price": str(b_market.market_price), "b_basis": b_market.basis,
                     "b_confidence": b_market.confidence, "b_max_purchase": str(res.b_math.max_purchase_price),
                     "sale_price_a": str(sale_price_a)},
            rules_hash=self.rules.rules_hash,
        )
        self.session.add(row)
        self.session.flush()
        audit.record(self.session, action="tradein.evaluated", entity_type="tradein_evaluation", entity_id=row.id,
                     details={"item_a": item_a.id, "verdict": res.verdict, "tradein_value": res.tradein_value,
                              "combined_roi": res.combined_roi}, actor_id=by, rules_hash=self.rules.rules_hash)
        return row, res
