"""Módulo 3 — Repuestos y reparaciones.

Lo que FUNCIONA: registro de reparaciones y presupuestos (repuesto, mano de obra,
envío, plazo), comparación reparar vs. sustituir y suma de costos para el tasador.
Lo que NO: búsqueda automática de repuestos en proveedores. No hay API pública
conocida de proveedores locales; Mercado Libre no permite búsqueda de terceros.
Los precios se cargan a mano o por CSV y quedan marcados con fuente y fecha.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from mototrade.core import audit
from mototrade.core.enums import ActorType, RepairState
from mototrade.core.money import D, ONE, ZERO, money
from mototrade.db.models import Repair, RepairQuote, Supplier
from mototrade.modules.valuation.engine import CostInputs


@dataclass(frozen=True)
class QuoteComparison:
    best_quote_id: int | None
    best_option: str | None
    best_total: Decimal | None
    options: list[dict]
    recommendation: str


def compare_quotes(quotes: list[RepairQuote], *, max_lead_days: int | None = None) -> QuoteComparison:
    """Elige el presupuesto de menor costo total entre los verificados (o todos, si ninguno lo está),
    descartando los que exceden el plazo máximo."""
    opts = []
    for q in quotes:
        total = q.total
        ok_lead = max_lead_days is None or q.lead_time_days is None or q.lead_time_days <= max_lead_days
        opts.append({"quote_id": q.id, "option": q.option, "total": money(total), "lead_days": q.lead_time_days,
                     "verified": q.availability_verified, "simulated": q.is_simulated, "within_lead": ok_lead,
                     "source": q.source})
    candidates = [o for o in opts if o["within_lead"]]
    verified = [o for o in candidates if o["verified"] and not o["simulated"]]
    pool = verified or candidates
    if not pool:
        return QuoteComparison(None, None, None, opts, "Sin presupuestos válidos: cargar al menos uno.")
    best = min(pool, key=lambda o: o["total"])
    rec = f"Mejor opción: {best['option']} por {best['total']:,.0f}"
    if not verified:
        rec += " (NINGÚN presupuesto verificado: confirmar disponibilidad antes de decidir)"
    return QuoteComparison(best["quote_id"], best["option"], best["total"], opts, rec)


@dataclass(frozen=True)
class RepairVsAsIs:
    repair_cost: Decimal
    sale_price_repaired: Decimal
    sale_price_as_is: Decimal
    profit_delta: Decimal        # > 0: conviene reparar
    recommendation: str          # repair | sell_as_is
    rationale: str


def compare_repair_vs_as_is(*, market_price: Decimal, repair_cost: Decimal, as_is_haircut_pct: Decimal,
                            extra_days_cost: Decimal = ZERO) -> RepairVsAsIs:
    """¿Conviene reparar y vender, o vender sin reparar con un descuento? Comparación simple y explícita:
    reparar suma costo (y días); no reparar baja el precio de venta en `as_is_haircut_pct` (estimación del operador)."""
    market, cost = D(market_price), D(repair_cost)
    as_is = money(market * (ONE - D(as_is_haircut_pct)))
    delta = money((market - cost - D(extra_days_cost)) - as_is)
    rec = "repair" if delta > 0 else "sell_as_is"
    why = (f"Reparar deja {delta:,.0f} más que vender sin reparar" if delta > 0
           else f"Vender sin reparar deja {-delta:,.0f} más (la reparación cuesta más que el descuento esperado)")
    return RepairVsAsIs(repair_cost=cost, sale_price_repaired=money(market), sale_price_as_is=as_is, profit_delta=delta,
                        recommendation=rec, rationale=why)


class PartsService:
    def __init__(self, session: Session):
        self.session = session

    def add_supplier(self, *, name: str, kind: str, contact: str = "", location: str | None = None, by: str = "operator") -> Supplier:
        s = Supplier(name=name.strip(), kind=kind, contact=contact, location=location)
        self.session.add(s)
        self.session.flush()
        audit.record(self.session, action="supplier.added", entity_type="supplier", entity_id=s.id,
                     details={"name": s.name, "kind": kind}, actor_type=ActorType.HUMAN, actor_id=by)
        return s

    def add_repair(self, *, issue: str, deal_id: int | None = None, inventory_item_id: int | None = None,
                   state: str = RepairState.OBSERVED, by: str = "operator") -> Repair:
        r = Repair(deal_id=deal_id, inventory_item_id=inventory_item_id, issue=issue.strip()[:255], state=state)
        self.session.add(r)
        self.session.flush()
        audit.record(self.session, action="repair.added", entity_type="repair", entity_id=r.id,
                     details={"issue": r.issue, "deal_id": deal_id, "item_id": inventory_item_id},
                     actor_type=ActorType.HUMAN, actor_id=by)
        return r

    def add_quote(self, repair: Repair, *, option: str, part_cost: Decimal = ZERO, labor_cost: Decimal = ZERO,
                  shipping_cost: Decimal = ZERO, lead_time_days: int | None = None, supplier_id: int | None = None,
                  source: str = "", availability_verified: bool = False, is_simulated: bool = False,
                  by: str = "operator") -> RepairQuote:
        if option not in ("repair", "replace"):
            raise ValueError("option debe ser 'repair' o 'replace'")
        q = RepairQuote(repair_id=repair.id, supplier_id=supplier_id, option=option, part_cost=D(part_cost),
                        labor_cost=D(labor_cost), shipping_cost=D(shipping_cost), lead_time_days=lead_time_days,
                        availability_verified=availability_verified, source=source, is_simulated=is_simulated,
                        quoted_at=datetime.now(timezone.utc))
        self.session.add(q)
        if repair.state in (RepairState.SUSPECTED, RepairState.OBSERVED):
            repair.state = RepairState.QUOTED
        self.session.flush()
        audit.record(self.session, action="repair.quoted", entity_type="repair_quote", entity_id=q.id,
                     details={"repair_id": repair.id, "option": option, "total": q.total, "verified": availability_verified},
                     actor_type=ActorType.HUMAN, actor_id=by)
        return q

    def choose_quote(self, repair: Repair, quote: RepairQuote, *, by: str) -> None:
        if quote.repair_id != repair.id:
            raise ValueError("El presupuesto no pertenece a esta reparación.")
        repair.chosen_quote_id = quote.id
        repair.decision = quote.option
        repair.state = RepairState.APPROVED
        audit.record(self.session, action="repair.quote_chosen", entity_type="repair", entity_id=repair.id,
                     details={"quote_id": quote.id, "total": quote.total}, actor_type=ActorType.HUMAN, actor_id=by)

    def cost_inputs_for(self, *, deal_id: int | None = None, inventory_item_id: int | None = None) -> CostInputs:
        """Suma de presupuestos elegidos (o el mejor disponible) para alimentar al tasador."""
        q = select(Repair).where(Repair.state != RepairState.DISCARDED)
        q = q.where(Repair.deal_id == deal_id) if deal_id is not None else q.where(Repair.inventory_item_id == inventory_item_id)
        parts = labor = ship = ZERO
        sources: dict = {}
        for repair in self.session.execute(q).scalars():
            chosen = None
            if repair.chosen_quote_id:
                chosen = self.session.get(RepairQuote, repair.chosen_quote_id)
            elif repair.quotes:
                cmp = compare_quotes(list(repair.quotes))
                chosen = self.session.get(RepairQuote, cmp.best_quote_id) if cmp.best_quote_id else None
            if chosen is None:
                sources[repair.issue] = "SIN PRESUPUESTO"
                continue
            parts += D(chosen.part_cost)
            labor += D(chosen.labor_cost)
            ship += D(chosen.shipping_cost)
            sources[repair.issue] = f"quote #{chosen.id} ({'verificado' if chosen.availability_verified else 'sin verificar'})"
        return CostInputs(repair_parts=money(parts), repair_labor=money(labor), repair_shipping=money(ship), sources=sources)
