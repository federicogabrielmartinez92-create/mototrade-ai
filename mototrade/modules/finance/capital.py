"""Posición de capital y capacidad operativa (incremento 2).

- Capital libre = saldo de caja − reserva mínima − capital reservado por compras pendientes (con colchón de gastos).
- Unidades activas = motos sin vender + compras pendientes de ejecutar.
- Las compras pendientes (aprobación ACCEPT_PURCHASE pendiente o aprobada y no ejecutada) RESERVAN capital.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mototrade.control.settings_model import CapitalSettings
from mototrade.core.money import D, ONE, ZERO, money
from mototrade.db.models import ApprovalRequest, CapitalLedger, Deal, Expense, InventoryItem


@dataclass(frozen=True)
class CapitalPosition:
    ledger_balance: Decimal
    reserve: Decimal
    reserved_pending: Decimal
    pending_units: int
    committed_in_stock: Decimal
    units_in_stock: int
    free_capital: Decimal
    units_active: int
    max_units: int
    units_left: int
    max_committed: Decimal
    committed_total: Decimal
    can_buy_another: bool
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items()}


def pending_purchase_reservations(session: Session, buffer_pct: Decimal) -> tuple[Decimal, int]:
    """Suma de compras aprobadas/pendientes aún no ejecutadas (deal sin inventario), con colchón de gastos."""
    rows = session.execute(
        select(ApprovalRequest).where(ApprovalRequest.kind == "accept_purchase",
                                      ApprovalRequest.status.in_(["pending", "approved"]))
    ).scalars().all()
    total, n = ZERO, 0
    seen: set[str] = set()
    for a in rows:
        if a.entity_id in seen:
            continue
        deal = session.get(Deal, int(a.entity_id)) if a.entity_id else None
        if deal is None or deal.inventory_item is not None or deal.stage in ("purchased", "lost", "discarded"):
            continue
        seen.add(a.entity_id)
        amt = D(a.amount or (deal.current_offer or deal.authorized_max_price or 0))
        total += amt * (ONE + buffer_pct)
        n += 1
    return money(total), n


def capital_position(session: Session, cs: CapitalSettings) -> CapitalPosition:
    balance = money(session.execute(select(func.coalesce(func.sum(CapitalLedger.amount), 0))).scalar_one())
    reserve = money(max(cs.min_reserve_amount, cs.capital_total * cs.min_reserve_pct))
    reserved, pending_units = pending_purchase_reservations(session, cs.pending_cost_buffer_pct)
    stock = session.execute(select(InventoryItem).where(InventoryItem.status != "sold")).scalars().all()
    committed = ZERO
    for it in stock:
        exp = session.execute(select(func.coalesce(func.sum(Expense.amount), 0)).where(Expense.inventory_item_id == it.id)).scalar_one()
        committed += D(it.purchase_price) + D(exp)
    committed = money(committed)
    free = money(max(ZERO, balance - reserve - reserved))
    units_active = len(stock) + pending_units
    units_left = max(0, cs.max_units - units_active)
    committed_total = money(committed + reserved)
    reasons: list[str] = []
    if units_left <= 0:
        reasons.append(f"inventario completo ({units_active}/{cs.max_units} motos)")
    if free < cs.min_unit_budget:
        reasons.append(f"capital libre {free:,.0f} < mínimo por unidad {cs.min_unit_budget:,.0f}")
    if committed_total >= cs.max_committed_capital:
        reasons.append(f"capital comprometido {committed_total:,.0f} ≥ máximo {cs.max_committed_capital:,.0f}")
    return CapitalPosition(ledger_balance=balance, reserve=reserve, reserved_pending=reserved, pending_units=pending_units,
                           committed_in_stock=committed, units_in_stock=len(stock), free_capital=free,
                           units_active=units_active, max_units=cs.max_units, units_left=units_left,
                           max_committed=cs.max_committed_capital, committed_total=committed_total,
                           can_buy_another=not reasons, reasons=reasons)


def purchase_budget_for_unit(pos: CapitalPosition, cs: CapitalSettings) -> Decimal:
    """Presupuesto máximo de compra para UNA moto nueva, respetando todos los topes."""
    if not pos.can_buy_another:
        return ZERO
    room_committed = max(ZERO, cs.max_committed_capital - pos.committed_total)
    return money(min(pos.free_capital, cs.max_unit_price, room_committed))
