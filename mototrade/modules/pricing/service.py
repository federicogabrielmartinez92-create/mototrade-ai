"""Módulo 10 — Precios dinámicos de inventario (determinista, explicable).

Recomienda bajar el precio según días en stock y consultas recibidas, sin perforar
el mínimo autorizado ni superar el recorte acumulado máximo. Toda recomendación
pasa por aprobación PRICE_CHANGE antes de aplicarse.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mototrade.config.rules import BusinessRules
from mototrade.core import approvals, audit
from mototrade.core.enums import ApprovalKind, InventoryStatus
from mototrade.core.money import D, ONE, ZERO, money, round_down_to
from mototrade.db.models import InventoryItem, PriceRecommendation, Publication


@dataclass(frozen=True)
class PriceAdvice:
    current_price: Decimal
    recommended_price: Decimal
    change_pct: Decimal
    reason: str
    inputs: dict

    @property
    def should_change(self) -> bool:
        return self.recommended_price != self.current_price


def recommend_price(*, current_price: Decimal, original_price: Decimal, min_price: Decimal | None, days_in_stock: int,
                    inquiries_last_7d: int, market_price: Decimal | None, rules: BusinessRules) -> PriceAdvice:
    p = rules.pricing
    current, original = D(current_price), D(original_price)
    inputs = {"days_in_stock": days_in_stock, "inquiries_7d": inquiries_last_7d, "current": str(current),
              "original": str(original), "min_price": str(min_price) if min_price is not None else None,
              "market_price": str(market_price) if market_price is not None else None}
    # 1) Recorte sugerido por antigüedad
    cut = ZERO
    for threshold, step in zip(p.days_in_stock_thresholds, p.price_cut_steps):
        if days_in_stock >= threshold:
            cut = step
    reasons = []
    if cut:
        reasons.append(f"{days_in_stock} días en stock → recorte sugerido {cut:.0%}")
    # 2) Demanda: pocas consultas aceleran un escalón; muchas lo frenan
    if inquiries_last_7d < p.min_inquiries_per_week_ok and days_in_stock >= p.days_in_stock_thresholds[0]:
        idx = p.price_cut_steps.index(cut) if cut in p.price_cut_steps else -1
        if idx + 1 < len(p.price_cut_steps):
            cut = p.price_cut_steps[idx + 1]
            reasons.append(f"solo {inquiries_last_7d} consultas en 7 días → un escalón más ({cut:.0%})")
    elif inquiries_last_7d >= p.min_inquiries_per_week_ok * 3 and cut:
        reasons.append(f"{inquiries_last_7d} consultas en 7 días: demanda alta, se mantiene el precio")
        cut = ZERO
    # 3) Tope acumulado respecto del precio original
    target = original * (ONE - cut)
    floor_total = original * (ONE - p.max_total_cut_pct)
    if target < floor_total:
        target = floor_total
        reasons.append(f"tope de recorte acumulado {p.max_total_cut_pct:.0%} alcanzado")
    # 4) Nunca debajo del mínimo autorizado ni del mercado - 10 %
    if min_price is not None and target < D(min_price):
        target = D(min_price)
        reasons.append("limitado por el mínimo autorizado")
    if market_price is not None and target < D(market_price) * D("0.90"):
        target = D(market_price) * D("0.90")
        reasons.append("limitado a mercado −10 %")
    target = round_down_to(target, rules.valuation.offer_rounding)
    if target >= current:
        target = current
        if not reasons:
            reasons.append("sin cambios: dentro de los umbrales")
    change = ((target - current) / current).quantize(D("0.0001")) if current else ZERO
    return PriceAdvice(current_price=money(current), recommended_price=money(target), change_pct=change,
                       reason="; ".join(reasons), inputs=inputs)


class PricingService:
    def __init__(self, session: Session, rules: BusinessRules):
        self.session = session
        self.rules = rules

    def _inquiries_7d(self, item_id: int) -> int:
        # Las publicaciones acumulan inquiries_count; sin historial por fecha usamos el total como proxy
        # (limitación documentada: no hay timeline de consultas hasta integrar un canal).
        total = self.session.execute(
            select(func.coalesce(func.sum(Publication.inquiries_count), 0)).where(Publication.inventory_item_id == item_id)
        ).scalar_one()
        return int(total)

    def advise(self, item: InventoryItem, *, market_price: Decimal | None = None, now: datetime | None = None,
               original_price: Decimal | None = None) -> PriceAdvice:
        now = now or datetime.now(timezone.utc)
        if item.list_price is None:
            raise ValueError("La moto no tiene precio de lista.")
        acquired = item.acquired_at if item.acquired_at.tzinfo else item.acquired_at.replace(tzinfo=timezone.utc)
        days = max(0, (now - acquired).days)
        first = self.session.execute(
            select(PriceRecommendation.current_price).where(PriceRecommendation.inventory_item_id == item.id)
            .order_by(PriceRecommendation.id).limit(1)
        ).scalar_one_or_none()
        original = D(original_price) if original_price is not None else (D(first) if first is not None else D(item.list_price))
        return recommend_price(current_price=D(item.list_price), original_price=original, min_price=item.min_sale_price,
                               days_in_stock=days, inquiries_last_7d=self._inquiries_7d(item.id),
                               market_price=market_price, rules=self.rules)

    def recommend_and_request(self, item: InventoryItem, *, by: str = "pricing_engine", **kw) -> PriceRecommendation | None:
        advice = self.advise(item, **kw)
        if not advice.should_change:
            return None
        rec = PriceRecommendation(inventory_item_id=item.id, current_price=advice.current_price,
                                  recommended_price=advice.recommended_price, reason=advice.reason, inputs=advice.inputs,
                                  rules_hash=self.rules.rules_hash)
        self.session.add(rec)
        self.session.flush()
        req = approvals.request(self.session, kind=ApprovalKind.PRICE_CHANGE, entity_type="inventory_item",
                                entity_id=item.id, payload={"from": str(advice.current_price), "to": str(advice.recommended_price),
                                                            "recommendation_id": rec.id},
                                summary=f"Bajar {item.make} {item.model} de {advice.current_price:,.0f} a {advice.recommended_price:,.0f}",
                                requested_by=by, amount=advice.recommended_price, rules_hash=self.rules.rules_hash)
        rec.approval_id = req.id
        audit.record(self.session, action="pricing.recommended", entity_type="price_recommendation", entity_id=rec.id,
                     details={"item_id": item.id, "from": advice.current_price, "to": advice.recommended_price,
                              "reason": advice.reason}, actor_id=by, rules_hash=self.rules.rules_hash)
        return rec

    def apply_if_approved(self, rec: PriceRecommendation, *, by: str) -> bool:
        if rec.status != "pending" or not approvals.is_approved(self.session, rec.approval_id):
            return False
        item = self.session.get(InventoryItem, rec.inventory_item_id)
        item.list_price = rec.recommended_price
        rec.status = "applied"
        audit.record(self.session, action="pricing.applied", entity_type="inventory_item", entity_id=item.id,
                     details={"new_price": rec.recommended_price, "recommendation_id": rec.id}, actor_id=by)
        return True

    def items_for_review(self) -> list[InventoryItem]:
        return list(self.session.execute(
            select(InventoryItem).where(InventoryItem.status.in_([InventoryStatus.READY, InventoryStatus.PUBLISHED]),
                                        InventoryItem.list_price.is_not(None))
        ).scalars())
