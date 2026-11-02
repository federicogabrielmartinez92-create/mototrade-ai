"""Detector de anomalías de precio (incremento 2).

Responde dos preguntas sobre cada anuncio, con métodos robustos (medianas, percentiles, IQR), nunca promedios de
modelos no equivalentes:

  Modo A — ¿Está publicada significativamente por debajo de comparables equivalentes? (descuento detectado)
  Modo B — ¿A precio normal, sería rentable si logramos una rebaja del 15–25 %? (objetivo negociable)

Y siempre: ¿la diferencia es una oportunidad REAL después de todos los costos? (usa la matemática del tasador).

Reglas de honestidad:
- Precio pedido ≠ venta. El descuento se mide pedido-contra-pedido (comparables ajustados); la rentabilidad, contra la
  estimación de venta (que ya descuenta publicación→venta).
- Con menos de `min_comparables` equivalentes → "insufficient": no se clasifica.
- Un descuento enorme es también señal de estafa, daño, problema documental o error: se marca y exige verificación.
- Nunca produce "compra recomendada": eso requiere documentación y mecánica verificadas por un humano.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from statistics import median

from mototrade.config.rules import BusinessRules
from mototrade.control.settings_model import CapitalSettings, SearchSettings
from mototrade.core.money import D, ONE, ZERO, money, round_down_to
from mototrade.modules.valuation.engine import (ComparablePoint, CostInputs, DealMath, MarketEstimate, compute_deal,
                                                estimate_market_price)


@dataclass(frozen=True)
class ComparableRecord:
    price: Decimal
    kind: str
    observed_at: datetime
    year: int | None = None
    kilometers: int | None = None
    version: str = ""
    seller_type: str = "unknown"
    condition: str | None = None
    source: str = ""
    source_ref: str | None = None
    ref_id: int | None = None


@dataclass(frozen=True)
class TargetListing:
    make: str
    model: str
    asking_price: Decimal
    version: str = ""
    year: int | None = None
    kilometers: int | None = None
    location: str | None = None
    seller_type: str = "unknown"
    condition_declared: str | None = None
    photos_count: int = 0
    published_at: datetime | None = None
    first_seen_at: datetime | None = None
    price_history: list[tuple[datetime, Decimal]] = field(default_factory=list)
    injection_flags: list[str] = field(default_factory=list)
    description: str = ""


@dataclass(frozen=True)
class OpportunityAnalysis:
    classification: str            # insufficient | ordinary | interesting | extraordinary
    mode: str                      # published | negotiable | none
    discount_pct: Decimal | None   # pedido vs. referencia de comparables ajustados (pedido-contra-pedido)
    reference_asking: Decimal | None
    reference_p25: Decimal | None
    reference_p75: Decimal | None
    comparables_used: int
    versions_matched: bool
    market: MarketEstimate
    math: DealMath | None          # al precio pedido
    target_price: Decimal | None   # objetivo de compra (modo B) o precio pedido (modo A)
    target_discount_range: tuple[Decimal, Decimal] | None
    math_at_target: DealMath | None
    scenarios: dict[str, dict]     # conservador / central / adverso, al precio objetivo
    risk_flags: list[str]
    risk_level: str                # low | medium | high
    recommended_action: str        # verify | negotiate | negotiate_target | review | skip
    score: int
    breakdown: dict
    notes: list[str]
    adjustments: list[dict]
    analyzed_at: datetime

    def as_dict(self) -> dict:
        def s(v):
            return str(v) if isinstance(v, Decimal) else v
        return {
            "classification": self.classification, "mode": self.mode, "discount_pct": s(self.discount_pct),
            "reference_asking": s(self.reference_asking), "reference_p25": s(self.reference_p25),
            "reference_p75": s(self.reference_p75), "comparables_used": self.comparables_used,
            "versions_matched": self.versions_matched,
            "market": {"price": s(self.market.market_price), "basis": self.market.basis, "confidence": self.market.confidence,
                       "confidence_score": s(self.market.confidence_score), "sources": self.market.sources,
                       "notes": self.market.notes},
            "at_asking": _math_dict(self.math), "target_price": s(self.target_price),
            "target_discount_range": [s(x) for x in self.target_discount_range] if self.target_discount_range else None,
            "at_target": _math_dict(self.math_at_target), "scenarios": self.scenarios, "risk_flags": self.risk_flags,
            "risk_level": self.risk_level, "recommended_action": self.recommended_action, "score": self.score,
            "breakdown": self.breakdown, "notes": self.notes, "adjustments": self.adjustments,
            "analyzed_at": self.analyzed_at.isoformat(),
        }


def _math_dict(m: DealMath | None) -> dict | None:
    if m is None:
        return None
    return {"market_price": str(m.market_price), "quick_sale_price": str(m.quick_sale_price),
            "suggested_list_price": str(m.suggested_list_price), "initial_offer": str(m.initial_offer),
            "max_purchase_price": str(m.max_purchase_price), "evaluated_price": str(m.evaluated_purchase_price),
            "total_costs": str(m.total_costs), "expected_profit": str(m.expected_profit), "expected_roi": str(m.expected_roi),
            "meets_minimum_roi": m.meets_minimum_roi, "within_capital_limit": m.within_capital_limit,
            "costs": m.costs_as_str(), "warnings": m.warnings}


def _pct(sorted_vals: list[Decimal], q: Decimal) -> Decimal:
    if not sorted_vals:
        return ZERO
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return money(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo))


def adjust_comparable(c: ComparableRecord, target: TargetListing, rules: BusinessRules) -> tuple[Decimal, dict]:
    """Ajusta el precio del comparable para que sea equivalente al anuncio objetivo (km y tipo de vendedor)."""
    a = rules.anomaly
    price = D(c.price)
    adj: dict = {"comparable_id": c.ref_id, "raw": str(price), "km_factor": "0", "seller_factor": "0"}
    factor = ZERO
    if c.kilometers is not None and target.kilometers is not None:
        diff = D(c.kilometers - target.kilometers) / D(10_000)  # positivo: el comparable tiene más km
        f = diff * a.km_adjustment_pct_per_10k
        f = max(-a.km_adjustment_cap, min(a.km_adjustment_cap, f))
        factor += f
        adj["km_factor"] = str(f.quantize(D("0.0001")))
    if c.seller_type == "dealer" and target.seller_type == "particular":
        factor -= a.dealer_premium_pct
        adj["seller_factor"] = str(-a.dealer_premium_pct)
    elif c.seller_type == "particular" and target.seller_type == "dealer":
        factor += a.dealer_premium_pct
        adj["seller_factor"] = str(a.dealer_premium_pct)
    adjusted = money(price * (ONE + factor))
    adj["adjusted"] = str(adjusted)
    return adjusted, adj


class AnomalyDetector:
    def __init__(self, rules: BusinessRules, search: SearchSettings, capital: CapitalSettings):
        self.rules = rules
        self.search = search
        self.capital = capital

    # ---- comparables -----------------------------------------------------------
    def _select(self, target: TargetListing, comps: list[ComparableRecord], now: datetime) -> tuple[list[ComparableRecord], bool, list[str]]:
        notes: list[str] = []
        v = self.rules.valuation
        fresh = [c for c in comps if (now - (c.observed_at if c.observed_at.tzinfo else c.observed_at.replace(tzinfo=timezone.utc))).days
                 <= v.comparables_max_age_days]
        if len(fresh) < len(comps):
            notes.append(f"{len(comps) - len(fresh)} comparable(s) descartado(s) por antigüedad.")
        tv = (target.version or "").strip().lower()
        if tv:
            same = [c for c in fresh if (c.version or "").strip().lower() == tv]
            if len(same) >= v.min_comparables:
                notes.append(f"Se usan {len(same)} comparables de la misma versión '{target.version}'.")
                return same, True, notes
            if same:
                notes.append(f"Solo {len(same)} comparable(s) de la versión '{target.version}': se amplía a todas las versiones.")
        return fresh, False, notes

    # ---- análisis --------------------------------------------------------------
    def analyze(self, target: TargetListing, comparables: list[ComparableRecord], *, cost_inputs: CostInputs | None = None,
                purchase_budget: Decimal | None = None, now: datetime | None = None) -> OpportunityAnalysis:
        now = now or datetime.now(timezone.utc)
        a, v, s = self.rules.anomaly, self.rules.valuation, self.search
        asking = D(target.asking_price)
        used, versions_matched, notes = self._select(target, comparables, now)
        adjustments: list[dict] = []
        points: list[ComparablePoint] = []
        for c in used:
            adjusted, info = adjust_comparable(c, target, self.rules)
            adjustments.append(info)
            points.append(ComparablePoint(price=adjusted, kind=c.kind, observed_at=c.observed_at, year=c.year,
                                          kilometers=c.kilometers, source=c.source, source_ref=c.source_ref, ref_id=c.ref_id))
        market = estimate_market_price(points, self.rules, now=now)
        if versions_matched and market.basis != "insufficient":
            market = MarketEstimate(**{**market.__dict__, "confidence_score": min(ONE, market.confidence_score + a.version_match_bonus)})
        asking_pts = sorted(p.price for p in points if p.kind == "asking")
        risk: list[str] = []
        breakdown: dict = {}

        # --- referencia pedido-contra-pedido y descuento ---
        reference = p25 = p75 = discount = None
        if len(asking_pts) >= v.min_comparables:
            reference = _pct(asking_pts, D("0.5"))
            p25, p75 = _pct(asking_pts, D("0.25")), _pct(asking_pts, D("0.75"))
            discount = ((reference - asking) / reference).quantize(D("0.0001")) if reference else None
            if reference and (p75 - p25) / reference > a.high_dispersion_iqr:
                risk.append("high_dispersion")
        elif market.basis == "sold":
            reference = money(market.market_price / (ONE - v.listing_to_sale_discount))
            discount = ((reference - asking) / reference).quantize(D("0.0001"))
            notes.append("Referencia derivada de ventas efectivas (sin suficientes precios pedidos).")

        # --- clasificación ---
        if discount is None or market.basis == "insufficient" or market.comparables_used < v.min_comparables:
            classification = "insufficient"
            risk.append("insufficient_comparables")
            notes.append("Tasación insuficiente: cargar más comparables equivalentes antes de decidir.")
        elif discount >= s.extraordinary_discount_pct:
            classification = "extraordinary"
            risk.append("extraordinary_needs_verification")
        elif discount >= s.target_discount_min:
            classification = "interesting"
        else:
            classification = "ordinary"
        if discount is not None and discount >= a.too_cheap_scam_pct:
            risk.append("too_cheap_possible_scam_or_damage")
        if not versions_matched and target.version:
            risk.append("mixed_versions")

        # --- riesgos del anuncio ---
        if target.year and target.kilometers is not None:
            age = max(1, now.year - target.year)
            if target.kilometers > a.high_km_per_year * age:
                risk.append("high_km")
        if target.year is None:
            risk.append("unknown_year")
        if target.kilometers is None:
            risk.append("unknown_km")
        if target.photos_count == 0:
            risk.append("no_photos")
        if target.injection_flags:
            risk.append("suspicious_text")
        if target.seller_type == "dealer":
            notes.append("Vendedor concesionaria: menos margen de negociación habitual.")
        if (target.condition_declared or "").lower() in ("regular", "malo", "mala", "a reparar"):
            risk.append("declared_poor_condition")
        ref_date = target.published_at or target.first_seen_at
        if ref_date is not None:
            if ref_date.tzinfo is None:
                ref_date = ref_date.replace(tzinfo=timezone.utc)
            if (now - ref_date).days > a.stale_listing_days:
                risk.append("stale_listing")
        drops = sum(1 for (_, p1), (_, p2) in zip(target.price_history, target.price_history[1:]) if D(p2) < D(p1))
        if drops >= 2:
            risk.append("repeated_price_drops")
        if asking > self.capital.max_unit_price:
            risk.append("above_max_unit_price")

        # --- matemática al precio pedido y objetivo ---
        math = target_math = None
        target_price = None
        target_range = None
        scenarios: dict[str, dict] = {}
        mode = "none"
        action = "skip"
        if market.basis != "insufficient" and classification != "insufficient":
            ci = cost_inputs or CostInputs()
            math = compute_deal(market.market_price, asking, ci, self.rules, available_capital=purchase_budget)
            if purchase_budget is not None and math.max_purchase_price > purchase_budget:
                risk.append("exceeds_budget")
            # Modo A: ya está barata y es rentable al precio pedido (o cerca)
            if classification in ("interesting", "extraordinary") and asking <= math.max_purchase_price:
                mode, target_price = "published", asking
                action = "verify" if classification == "extraordinary" else "negotiate"
            else:
                # Modo B: objetivo con descuento 15–25 % sobre el pedido, acotado por la compra máxima
                lo = round_down_to(asking * (ONE - s.target_discount_max), v.offer_rounding)
                hi = round_down_to(asking * (ONE - s.target_discount_min), v.offer_rounding)
                target_range = (lo, hi)
                if s.negotiable_mode_enabled and math.max_purchase_price >= lo:
                    mode = "negotiable"
                    target_price = min(hi, math.max_purchase_price)
                    action = "negotiate_target" if classification != "insufficient" else "review"
                    if classification == "extraordinary":
                        action = "verify"
                else:
                    notes.append("Ni con un 25 % de rebaja alcanza la rentabilidad mínima con estos costos.")
                    action = "skip" if classification in ("ordinary", "insufficient") else "verify"
            if target_price is not None:
                target_math = compute_deal(market.market_price, target_price, ci, self.rules, available_capital=purchase_budget)
                scenarios = {
                    "conservador": target_math.scenarios["base"].as_dict(),     # venta rápida
                    "central": target_math.scenarios["market"].as_dict(),       # precio de mercado
                    "adverso": target_math.scenarios["severe"].as_dict(),       # venta −12 % y costos +25 %
                }
        if classification == "insufficient":
            action, mode = "review", "none"
            if market.basis != "insufficient":  # matemática informativa al precio pedido, sin proponer modo
                math = compute_deal(market.market_price, asking, cost_inputs or CostInputs(), self.rules,
                                    available_capital=purchase_budget)

        # --- riesgo y puntaje ---
        high = {"too_cheap_possible_scam_or_damage", "suspicious_text", "declared_poor_condition", "insufficient_comparables"}
        medium = {"extraordinary_needs_verification", "high_km", "mixed_versions", "high_dispersion", "stale_listing",
                  "no_photos", "repeated_price_drops", "exceeds_budget", "above_max_unit_price", "unknown_km", "unknown_year"}
        if any(r in high for r in risk):
            risk_level = "high"
        elif any(r in medium for r in risk):
            risk_level = "medium"
        else:
            risk_level = "low"
        score = self._score(classification, discount, market, math, target_math, risk, breakdown)
        return OpportunityAnalysis(
            classification=classification, mode=mode, discount_pct=discount, reference_asking=reference,
            reference_p25=p25, reference_p75=p75, comparables_used=market.comparables_used,
            versions_matched=versions_matched, market=market, math=math, target_price=target_price,
            target_discount_range=target_range, math_at_target=target_math, scenarios=scenarios, risk_flags=risk,
            risk_level=risk_level, recommended_action=action, score=score, breakdown=breakdown, notes=notes,
            adjustments=adjustments, analyzed_at=now,
        )

    def _score(self, classification, discount, market, math, target_math, risk, breakdown) -> int:
        pts = 0
        if discount is not None:
            d = max(ZERO, min(D("0.40"), discount))
            breakdown["discount"] = int(d / D("0.40") * 35)
            pts += breakdown["discount"]
        ref = target_math or math
        if ref is not None:
            roi = max(ZERO, min(D("0.30"), ref.expected_roi))
            breakdown["roi"] = int(roi / D("0.30") * 30)
            pts += breakdown["roi"]
        breakdown["confidence"] = int(market.confidence_score * 20)
        pts += breakdown["confidence"]
        penalty = 0
        for r in risk:
            penalty += {"too_cheap_possible_scam_or_damage": 20, "suspicious_text": 25, "insufficient_comparables": 30,
                        "declared_poor_condition": 15, "high_km": 8, "mixed_versions": 5, "high_dispersion": 5,
                        "no_photos": 5, "stale_listing": 3, "exceeds_budget": 10, "above_max_unit_price": 10}.get(r, 2)
        breakdown["risk_penalty"] = -penalty
        pts -= penalty
        if classification in ("interesting", "extraordinary"):
            breakdown["class_bonus"] = 15
            pts += 15
        return max(0, min(100, pts))
