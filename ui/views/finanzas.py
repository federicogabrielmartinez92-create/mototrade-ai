import streamlit as st
from sqlalchemy import select

from mototrade.core.money import D
from mototrade.db.models import ApprovalRequest, CapitalLedger, InventoryItem
from mototrade.modules.finance import FinanceService
from mototrade.modules.finance.service import FinanceError
from ui.common import db, df, flash, has_role, money, pct, require_login, rules

p = require_login()
R = rules()
st.title("💰 Finanzas")

with db() as s:
    fin = FinanceService(s, R)
    summ = fin.summary()
    ledger = [{"fecha": l.occurred_at, "tipo": l.kind, "importe": money(l.amount), "moto": l.inventory_item_id, "detalle": l.memo, "por": l.created_by}
              for l in s.execute(select(CapitalLedger).order_by(CapitalLedger.id.desc()).limit(200)).scalars()]
    pnls = [fin.unit_pnl(i) for i in s.execute(select(InventoryItem)).scalars()]
    sales_appr = {}
    for a in s.execute(select(ApprovalRequest).where(ApprovalRequest.kind == "accept_sale", ApprovalRequest.status == "approved")).scalars():
        it = s.get(InventoryItem, int(a.entity_id))
        if it and it.status != "sold":
            sales_appr[a.id] = f"Aprobación #{a.id} · {it.make} {it.model} #{it.id} · mínimo aprobado {money(a.amount)}"

c = st.columns(5)
c[0].metric("Saldo de caja", money(summ["ledger_balance"]))
c[1].metric("Disponible p/ comprar", money(summ["available_capital"]), f"reserva {R.capital.min_cash_reserve_pct:.0%}")
c[2].metric("Capital en stock", money(summ["capital_in_stock"]))
c[3].metric("Utilidad realizada", money(summ["realized_profit"]))
c[4].metric("Días prom. vendidas", f"{summ['avg_days_sold']:.0f}" if summ["avg_days_sold"] else "—")
st.caption(f"Régimen configurado: {R.taxes.regime} · IVA s/margen {R.taxes.iva_margin_pct:.0%} · IIBB {R.taxes.iibb_pct:.1%} · {R.taxes.notes}")

t1, t2, t3 = st.tabs(["Resultado por moto", "Libro de caja", "Operaciones"])
with t1:
    st.dataframe(df([{"moto": x.item_id, "estado": x.status, "compra": money(x.purchase_price), "gastos": money(x.expenses), "costo total": money(x.total_cost),
                      "venta": money(x.sale_price), "impuestos est.": money(x.taxes_estimate), "resultado": money(x.profit), "ROI": pct(x.roi), "días": x.days_in_stock}
                     for x in pnls]), width="stretch", hide_index=True)
with t2:
    st.dataframe(df(ledger), width="stretch", hide_index=True)
with t3:
    st.subheader("Aporte / retiro de capital (admin)")
    with st.form("cap"):
        amt = st.number_input("Importe (negativo = retiro)", -100_000_000, 100_000_000, 0, step=10_000)
        memo = st.text_input("Detalle")
        if st.form_submit_button("Registrar", disabled=not has_role(p, "admin")) and amt:
            with db() as s:
                try:
                    FinanceService(s, R).add_capital(D(amt), memo=memo, by=p.username)
                except FinanceError as e:
                    flash("error", str(e))
            st.rerun()
    if st.button("Registrar capital inicial de las reglas (solo si el libro está vacío)", disabled=not has_role(p, "admin")):
        with db() as s:
            r = FinanceService(s, R).ensure_initial_capital(by=p.username)
        flash("success" if r else "info", "Capital inicial registrado." if r else "El libro ya tenía movimientos.")
        st.rerun()
    st.subheader("Registrar venta cerrada (requiere aprobación ACCEPT_SALE de un admin)")
    if not sales_appr:
        st.caption("No hay ventas aprobadas pendientes de registrar.")
    else:
        aid = st.selectbox("Venta aprobada", list(sales_appr), format_func=lambda i: sales_appr[i])
        with st.form("sale"):
            c1, c2 = st.columns(2)
            price = c1.number_input("Precio final de venta (ARS)", 0, 100_000_000, 0, step=10_000)
            trade = c2.number_input("Valor tomado en permuta (ARS, 0 si no hay)", 0, 100_000_000, 0, step=10_000)
            if st.form_submit_button("Registrar venta", type="primary", disabled=not has_role(p, "admin")):
                with db() as s:
                    a = s.get(ApprovalRequest, aid)
                    try:
                        FinanceService(s, R).register_sale(s.get(InventoryItem, int(a.entity_id)), price=D(price), approval_id=aid,
                                                           by=p.username, tradein_value=D(trade))
                        flash("success", "Venta registrada.")
                    except FinanceError as e:
                        flash("error", str(e))
                st.rerun()
