"""Módulo 9 — Inventario y finanzas.

- Libro de capital (`capital_ledger`): cada peso que entra o sale es una fila. El
  saldo nunca se edita, se calcula.
- Compra: solo a partir de una aprobación ACCEPT_PURCHASE aprobada por un admin.
- Venta: solo a partir de una aprobación ACCEPT_SALE aprobada por un admin.
- Resultado por unidad (P&L): precio de venta − compra − gastos − impuestos estimados.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mototrade.config.rules import BusinessRules
from mototrade.core import approvals, audit
from mototrade.core.enums import ActorType, ApprovalStatus, DealStage, ExpenseCategory, InventoryStatus, LedgerKind, \
    ListingStatus
from mototrade.core.money import D, ZERO, money
from mototrade.db.models import ApprovalRequest, CapitalLedger, Deal, Expense, InventoryItem, Sale


class FinanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class UnitPnL:
    item_id: int
    purchase_price: Decimal
    expenses: Decimal
    expenses_by_category: dict[str, Decimal]
    total_cost: Decimal
    sale_price: Decimal | None
    taxes_estimate: Decimal
    profit: Decimal | None
    roi: Decimal | None
    days_in_stock: int
    status: str


class FinanceService:
    def __init__(self, session: Session, rules: BusinessRules):
        self.session = session
        self.rules = rules

    # ---- capital -------------------------------------------------------------
    def ledger_balance(self) -> Decimal:
        total = self.session.execute(select(func.coalesce(func.sum(CapitalLedger.amount), 0))).scalar_one()
        return money(total)

    def ensure_initial_capital(self, *, by: str = "bootstrap") -> CapitalLedger | None:
        """Registra el capital inicial de las reglas si el libro está vacío."""
        exists = self.session.execute(select(CapitalLedger.id).limit(1)).scalar_one_or_none()
        if exists:
            return None
        return self.add_capital(self.rules.capital.initial_capital, memo="Capital inicial (business_rules.yaml)", by=by)

    def add_capital(self, amount: Decimal, *, memo: str, by: str) -> CapitalLedger:
        amount = money(amount)
        if amount == 0:
            raise FinanceError("Importe cero.")
        row = CapitalLedger(kind=LedgerKind.CAPITAL_IN if amount > 0 else LedgerKind.CAPITAL_OUT, amount=amount,
                            memo=memo, created_by=by)
        self.session.add(row)
        self.session.flush()
        audit.record(self.session, action="capital.movement", entity_type="capital_ledger", entity_id=row.id,
                     details={"amount": amount, "memo": memo}, actor_type=ActorType.HUMAN, actor_id=by)
        return row

    def available_capital(self) -> Decimal:
        """Caja disponible para comprar = saldo del libro − reserva mínima intocable."""
        balance = self.ledger_balance()
        reserve = money(self.rules.capital.initial_capital * self.rules.capital.min_cash_reserve_pct)
        return max(ZERO, balance - reserve)

    def capital_in_stock(self) -> Decimal:
        rows = self.session.execute(
            select(InventoryItem).where(InventoryItem.status != InventoryStatus.SOLD)
        ).scalars()
        return money(sum((D(i.purchase_price) + self._expenses_total(i.id) for i in rows), ZERO))

    # ---- compra --------------------------------------------------------------
    def register_purchase(self, deal: Deal, *, price: Decimal, approval_id: int, by: str,
                          kilometers: int | None = None, color: str | None = None,
                          plate_masked: str | None = None) -> InventoryItem:
        """Convierte un deal en una moto de inventario. Exige aprobación ACCEPT_PURCHASE aprobada y vigente."""
        price = money(price)
        appr = self.session.get(ApprovalRequest, approval_id)
        if appr is None or appr.status != ApprovalStatus.APPROVED or appr.kind != "accept_purchase":
            raise FinanceError("La compra requiere una aprobación ACCEPT_PURCHASE aprobada por un administrador.")
        if appr.entity_type != "deal" or str(appr.entity_id) != str(deal.id):
            raise FinanceError("La aprobación no corresponde a este deal.")
        if appr.amount is not None and price > D(appr.amount):
            raise FinanceError(f"El precio ({price}) supera el importe aprobado ({appr.amount}).")
        if deal.inventory_item is not None:
            raise FinanceError("Este deal ya fue comprado (idempotencia).")
        if price > self.available_capital():
            raise FinanceError("Capital insuficiente para esta compra (respetando la reserva mínima).")
        listing = deal.listing
        if listing is None:
            raise FinanceError("El deal no tiene anuncio asociado.")
        item = InventoryItem(
            deal_id=deal.id, make=listing.make, model=listing.model, version=listing.version, year=listing.year,
            kilometers=kilometers if kilometers is not None else listing.kilometers, color=color,
            plate_masked=plate_masked, purchase_price=price, status=InventoryStatus.ACQUIRED,
        )
        self.session.add(item)
        self.session.flush()
        self.session.add(CapitalLedger(kind=LedgerKind.PURCHASE, amount=-price, inventory_item_id=item.id,
                                       memo=f"Compra {listing.make} {listing.model} (deal #{deal.id})", created_by=by))
        deal.stage = DealStage.PURCHASED
        listing.status = ListingStatus.PURCHASED
        audit.record(self.session, action="inventory.purchased", entity_type="inventory_item", entity_id=item.id,
                     details={"deal_id": deal.id, "price": price, "approval_id": approval_id},
                     actor_type=ActorType.HUMAN, actor_id=by)
        return item

    # ---- gastos --------------------------------------------------------------
    def add_expense(self, item: InventoryItem, *, category: ExpenseCategory | str, amount: Decimal, memo: str = "",
                    supplier_id: int | None = None, by: str = "operator") -> Expense:
        amount = money(amount)
        if amount <= 0:
            raise FinanceError("El gasto debe ser positivo.")
        if str(category) not in {c.value for c in ExpenseCategory}:
            raise FinanceError(f"Categoría inválida: {category}")
        if item.status == InventoryStatus.SOLD:
            raise FinanceError("No se pueden cargar gastos a una moto ya vendida.")
        row = Expense(inventory_item_id=item.id, category=str(category), amount=amount, memo=memo,
                      supplier_id=supplier_id)
        self.session.add(row)
        self.session.flush()
        self.session.add(CapitalLedger(kind=LedgerKind.EXPENSE, amount=-amount, inventory_item_id=item.id,
                                       memo=f"{category}: {memo}"[:255], created_by=by))
        audit.record(self.session, action="expense.added", entity_type="expense", entity_id=row.id,
                     details={"item_id": item.id, "category": str(category), "amount": amount},
                     actor_type=ActorType.HUMAN, actor_id=by)
        return row

    def _expenses_total(self, item_id: int) -> Decimal:
        total = self.session.execute(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(Expense.inventory_item_id == item_id)
        ).scalar_one()
        return money(total)

    # ---- venta ---------------------------------------------------------------
    def register_sale(self, item: InventoryItem, *, price: Decimal, approval_id: int, by: str,
                      tradein_value: Decimal = ZERO, contact_id: int | None = None, notes: str = "") -> Sale:
        price, tradein_value = money(price), money(tradein_value)
        appr = self.session.get(ApprovalRequest, approval_id)
        if appr is None or appr.status != ApprovalStatus.APPROVED or appr.kind != "accept_sale":
            raise FinanceError("La venta requiere una aprobación ACCEPT_SALE aprobada por un administrador.")
        if appr.entity_type != "inventory_item" or str(appr.entity_id) != str(item.id):
            raise FinanceError("La aprobación no corresponde a esta moto.")
        if appr.amount is not None and price < D(appr.amount):
            raise FinanceError(f"El precio ({price}) es menor al importe aprobado ({appr.amount}).")
        if item.status == InventoryStatus.SOLD:
            raise FinanceError("La moto ya está vendida (idempotencia).")
        if item.min_sale_price is not None and price < D(item.min_sale_price):
            raise FinanceError("Precio por debajo del mínimo autorizado para esta moto.")
        cash = price - tradein_value
        sale = Sale(inventory_item_id=item.id, contact_id=contact_id, price=price, tradein_value=tradein_value,
                    cash_received=cash, approval_id=approval_id, notes=notes)
        self.session.add(sale)
        item.status = InventoryStatus.SOLD
        item.sold_price = price
        item.sold_at = datetime.now(timezone.utc)
        self.session.flush()
        self.session.add(CapitalLedger(kind=LedgerKind.SALE, amount=cash, inventory_item_id=item.id,
                                       memo=f"Venta {item.make} {item.model}" + (" (con permuta)" if tradein_value else ""),
                                       created_by=by))
        taxes = money(price * self.rules.taxes.sales_tax_pct)
        if taxes:
            self.session.add(CapitalLedger(kind=LedgerKind.TAX, amount=-taxes, inventory_item_id=item.id,
                                           memo="Impuestos estimados sobre la venta", created_by=by))
        audit.record(self.session, action="inventory.sold", entity_type="inventory_item", entity_id=item.id,
                     details={"price": price, "tradein_value": tradein_value, "approval_id": approval_id},
                     actor_type=ActorType.HUMAN, actor_id=by)
        return sale

    def authorize_min_sale_price(self, item: InventoryItem, *, min_price: Decimal, list_price: Decimal, by: str) -> None:
        """El humano fija el piso que el agente vendedor NO puede perforar."""
        min_price, list_price = money(min_price), money(list_price)
        if min_price <= 0 or list_price < min_price:
            raise FinanceError("Precios inválidos: lista >= mínimo > 0.")
        item.min_sale_price = min_price
        item.min_sale_authorized_by = by
        item.list_price = list_price
        audit.record(self.session, action="inventory.min_sale_authorized", entity_type="inventory_item",
                     entity_id=item.id, details={"min_price": min_price, "list_price": list_price},
                     actor_type=ActorType.HUMAN, actor_id=by)

    # ---- resultado por unidad ------------------------------------------------
    def unit_pnl(self, item: InventoryItem, now: datetime | None = None) -> UnitPnL:
        now = now or datetime.now(timezone.utc)
        by_cat: dict[str, Decimal] = {}
        for e in item.expenses:
            by_cat[e.category] = by_cat.get(e.category, ZERO) + D(e.amount)
        expenses = money(sum(by_cat.values(), ZERO))
        total_cost = D(item.purchase_price) + expenses
        sale = D(item.sold_price) if item.sold_price is not None else None
        taxes = money(sale * self.rules.taxes.sales_tax_pct) if sale is not None else ZERO
        profit = money(sale - total_cost - taxes) if sale is not None else None
        roi = (profit / total_cost).quantize(D("0.0001")) if profit is not None and total_cost else None
        acquired = item.acquired_at if item.acquired_at.tzinfo else item.acquired_at.replace(tzinfo=timezone.utc)
        end = item.sold_at or now
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        days = max(0, (end - acquired).days)
        return UnitPnL(item_id=item.id, purchase_price=D(item.purchase_price), expenses=expenses,
                       expenses_by_category=by_cat, total_cost=total_cost, sale_price=sale, taxes_estimate=taxes,
                       profit=profit, roi=roi, days_in_stock=days, status=item.status)

    def summary(self) -> dict:
        items = list(self.session.execute(select(InventoryItem)).scalars())
        pnls = [self.unit_pnl(i) for i in items]
        sold = [p for p in pnls if p.profit is not None]
        in_stock = [p for p in pnls if p.profit is None]
        return {
            "ledger_balance": self.ledger_balance(),
            "available_capital": self.available_capital(),
            "capital_in_stock": money(sum((p.total_cost for p in in_stock), ZERO)),
            "units_in_stock": len(in_stock),
            "units_sold": len(sold),
            "realized_profit": money(sum((p.profit for p in sold), ZERO)),
            "avg_days_sold": (sum(p.days_in_stock for p in sold) / len(sold)) if sold else None,
        }
