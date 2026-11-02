"""Módulo 12 — Analítica comercial. Solo cálculos sobre datos reales de la base; sin proyecciones inventadas."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mototrade.config.rules import BusinessRules
from mototrade.core.money import D, ZERO, money
from mototrade.db.models import Conversation, Deal, InventoryItem, Listing, Valuation
from mototrade.modules.finance.service import FinanceService


class AnalyticsService:
    def __init__(self, session: Session, rules: BusinessRules):
        self.session = session
        self.rules = rules
        self.fin = FinanceService(session, rules)

    def kpis(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        items = list(self.session.execute(select(InventoryItem)).scalars())
        pnls = [self.fin.unit_pnl(i, now) for i in items]
        sold = [p for p in pnls if p.profit is not None]
        stock = [p for p in pnls if p.profit is None]
        total_cost_sold = sum((p.total_cost for p in sold), ZERO)
        realized = sum((p.profit for p in sold), ZERO)
        days_sold = [p.days_in_stock for p in sold]
        avg_days = (sum(days_sold) / len(days_sold)) if days_sold else None
        # Margen por día de capital: cuánto rinde cada peso invertido por día (anualizable)
        capital_days = sum((p.total_cost * p.days_in_stock for p in sold), ZERO)
        margin_per_capital_day = (realized / capital_days) if capital_days else None
        listings_total = self.session.execute(select(func.count()).select_from(Listing)).scalar_one()
        deals_total = self.session.execute(select(func.count()).select_from(Deal)).scalar_one()
        deals_won = self.session.execute(select(func.count()).select_from(Deal).where(Deal.stage == "purchased")).scalar_one()
        convs = self.session.execute(select(func.count()).select_from(Conversation)).scalar_one()
        convs_won = self.session.execute(
            select(func.count()).select_from(Conversation).where(Conversation.status == "closed_won")).scalar_one()
        return {
            "units_sold": len(sold),
            "units_in_stock": len(stock),
            "realized_profit": money(realized),
            "margin_on_cost": (realized / total_cost_sold).quantize(D("0.0001")) if total_cost_sold else None,
            "avg_days_in_stock_sold": avg_days,
            "avg_days_in_stock_current": (sum(p.days_in_stock for p in stock) / len(stock)) if stock else None,
            "margin_per_capital_day": margin_per_capital_day.quantize(D("0.000001")) if margin_per_capital_day else None,
            "annualized_return_on_capital": (margin_per_capital_day * 365).quantize(D("0.0001")) if margin_per_capital_day else None,
            "capital_in_stock": money(sum((p.total_cost for p in stock), ZERO)),
            "available_capital": self.fin.available_capital(),
            "listings_total": listings_total,
            "deals_total": deals_total,
            "deals_won": deals_won,
            "deal_conversion": (D(deals_won) / D(deals_total)).quantize(D("0.0001")) if deals_total else None,
            "conversations_total": convs,
            "conversations_won": convs_won,
            "rotation_per_year": (365 / avg_days) if avg_days else None,
        }

    def performance_by_model(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        out: dict[tuple[str, str], dict] = {}
        for item in self.session.execute(select(InventoryItem)).scalars():
            p = self.fin.unit_pnl(item, now)
            key = (item.make, item.model)
            agg = out.setdefault(key, {"make": item.make, "model": item.model, "units": 0, "sold": 0,
                                       "profit": ZERO, "cost": ZERO, "days": 0})
            agg["units"] += 1
            if p.profit is not None:
                agg["sold"] += 1
                agg["profit"] += p.profit
                agg["cost"] += p.total_cost
                agg["days"] += p.days_in_stock
        rows = []
        for agg in out.values():
            rows.append({**agg, "profit": money(agg["profit"]), "cost": money(agg["cost"]),
                         "margin_on_cost": (agg["profit"] / agg["cost"]).quantize(D("0.0001")) if agg["cost"] else None,
                         "avg_days": (agg["days"] / agg["sold"]) if agg["sold"] else None})
        return sorted(rows, key=lambda r: (r["profit"]), reverse=True)

    def valuation_accuracy(self) -> dict:
        """Error del tasador: compara precio de mercado estimado vs. venta real de la misma moto."""
        errors: list[Decimal] = []
        for item in self.session.execute(select(InventoryItem).where(InventoryItem.sold_price.is_not(None))).scalars():
            val = self.session.execute(
                select(Valuation).where(Valuation.inventory_item_id == item.id).order_by(Valuation.id.desc()).limit(1)
            ).scalar_one_or_none()
            if val is None and item.deal_id is not None:
                val = self.session.execute(
                    select(Valuation).where(Valuation.deal_id == item.deal_id).order_by(Valuation.id.desc()).limit(1)
                ).scalar_one_or_none()
            if val is not None and D(val.market_price) > 0:
                errors.append(((D(item.sold_price) - D(val.market_price)) / D(val.market_price)).quantize(D("0.0001")))
        if not errors:
            return {"samples": 0, "mean_error": None, "mean_abs_error": None}
        mean = sum(errors, ZERO) / len(errors)
        mae = sum((abs(e) for e in errors), ZERO) / len(errors)
        return {"samples": len(errors), "mean_error": mean.quantize(D("0.0001")), "mean_abs_error": mae.quantize(D("0.0001"))}
