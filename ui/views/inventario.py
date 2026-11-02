import streamlit as st
from sqlalchemy import select

from mototrade.core.money import D
from mototrade.db.models import ApprovalRequest, Deal, InventoryItem
from mototrade.modules.documents import DocumentsService
from mototrade.modules.finance import FinanceService
from mototrade.modules.finance.service import FinanceError
from ui.common import db, df, flash, has_role, money, pct, require_login, rules

p = require_login()
R = rules()
st.title("🏍️ Inventario")

with st.expander("Registrar compra confirmada (requiere aprobación ACCEPT_PURCHASE de un admin)"):
    with db() as s:
        appr = list(s.execute(select(ApprovalRequest).where(ApprovalRequest.kind == "accept_purchase", ApprovalRequest.status == "approved")).scalars())
        opts = {}
        for a in appr:
            d = s.get(Deal, int(a.entity_id))
            if d and d.inventory_item is None and d.listing:
                opts[a.id] = f"Aprobación #{a.id} · deal #{d.id} {d.listing.make} {d.listing.model} · hasta {money(a.amount)}"
    if not opts:
        st.caption("No hay compras aprobadas pendientes de registrar.")
    else:
        aid = st.selectbox("Compra aprobada", list(opts), format_func=lambda i: opts[i])
        with st.form("buy"):
            c1, c2, c3 = st.columns(3)
            price = c1.number_input("Precio final pagado (ARS)", 0, 100_000_000, 0, step=10_000)
            km = c2.number_input("Km reales", 0, 500000, 0, step=100)
            plate = c3.text_input("Patente enmascarada (p. ej. A***123)")
            if st.form_submit_button("Registrar compra", type="primary", disabled=not has_role(p, "admin")):
                with db() as s:
                    a = s.get(ApprovalRequest, aid)
                    deal = s.get(Deal, int(a.entity_id))
                    try:
                        item = FinanceService(s, R).register_purchase(deal, price=D(price), approval_id=aid, by=p.username,
                                                                      kilometers=int(km) or None, plate_masked=plate or None)
                        DocumentsService(s).init_checklist(inventory_item_id=item.id, by=p.username)
                        flash("success", f"Moto #{item.id} ingresada al inventario.")
                    except FinanceError as e:
                        flash("error", str(e))
                st.rerun()

with db() as s:
    fin = FinanceService(s, R)
    items = list(s.execute(select(InventoryItem).order_by(InventoryItem.id.desc())).scalars())
    rows = []
    for i in items:
        pnl = fin.unit_pnl(i)
        rows.append({"id": i.id, "moto": f"{i.make} {i.model} {i.year or ''}", "estado": i.status, "compra": money(i.purchase_price),
                     "gastos": money(pnl.expenses), "costo total": money(pnl.total_cost), "lista": money(i.list_price),
                     "mínimo": money(i.min_sale_price), "venta": money(i.sold_price), "resultado": money(pnl.profit),
                     "ROI": pct(pnl.roi), "días": pnl.days_in_stock})
if rows:
    st.dataframe(df(rows), width="stretch", hide_index=True)
else:
    st.info("Inventario vacío.")
if not rows:
    st.stop()

st.subheader("Ficha de la moto")
iid = st.selectbox("Moto", [r["id"] for r in rows], format_func=lambda i: next(r["moto"] + f" (#{i})" for r in rows if r["id"] == i))
t1, t2, t3, t4 = st.tabs(["Estado y precios", "Gastos", "Documentación", "Notas"])
with t1:
    with db() as s:
        it = s.get(InventoryItem, iid)
        status, lp, mp, notes = it.status, int(it.list_price or 0), int(it.min_sale_price or 0), it.notes
    c1, c2 = st.columns(2)
    new_status = c1.selectbox("Estado", ["acquired", "reconditioning", "ready", "published", "reserved"],
                              index=["acquired", "reconditioning", "ready", "published", "reserved", "sold"].index(status) if status != "sold" else 0,
                              disabled=status == "sold")
    if status != "sold" and new_status != status and c1.button("Cambiar estado", disabled=not has_role(p, "operator")):
        with db() as s:
            s.get(InventoryItem, iid).status = new_status
        st.rerun()
    with c2.form("prices"):
        lst = st.number_input("Precio de lista (ARS)", 0, 100_000_000, lp, step=10_000)
        mn = st.number_input("Mínimo autorizado para el agente vendedor (ARS)", 0, 100_000_000, mp, step=10_000)
        if st.form_submit_button("Autorizar precios (admin)", disabled=not has_role(p, "admin")):
            with db() as s:
                try:
                    FinanceService(s, R).authorize_min_sale_price(s.get(InventoryItem, iid), min_price=D(mn), list_price=D(lst), by=p.username)
                    flash("success", "Precios autorizados.")
                except FinanceError as e:
                    flash("error", str(e))
            st.rerun()
with t2:
    with st.form("exp", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        cat = c1.selectbox("Categoría", ["parts", "labor", "transfer", "logistics", "preparation", "marketing", "tax", "other"])
        amt = c2.number_input("Importe (ARS)", 0, 50_000_000, 0, step=1000)
        memo = c3.text_input("Detalle")
        if st.form_submit_button("Registrar gasto", disabled=not has_role(p, "operator")):
            with db() as s:
                try:
                    FinanceService(s, R).add_expense(s.get(InventoryItem, iid), category=cat, amount=D(amt), memo=memo, by=p.username)
                except FinanceError as e:
                    flash("error", str(e))
            st.rerun()
    with db() as s:
        ex = [{"fecha": e.incurred_at, "categoría": e.category, "importe": money(e.amount), "detalle": e.memo} for e in s.get(InventoryItem, iid).expenses]
    st.dataframe(df(ex), width="stretch", hide_index=True)
with t3:
    with db() as s:
        docs = DocumentsService(s)
        rows_d = docs._rows(inventory_item_id=iid)
        if not rows_d and st.button("Crear checklist"):
            docs.init_checklist(inventory_item_id=iid, by=p.username)
            st.rerun()
        for r in rows_d:
            c1, c2 = st.columns([3, 2])
            c1.markdown(f"{'🟢' if r.status in ('verified_manual','not_applicable') else '🔴' if r.blocking else '🟡'} {r.label} — `{r.status}`")
            new = c2.selectbox("estado", ["pending", "provided", "verified_manual", "rejected", "not_applicable"],
                               index=["pending", "provided", "verified_manual", "rejected", "not_applicable"].index(r.status), key=f"idoc{r.id}",
                               label_visibility="collapsed")
            if new != r.status and has_role(p, "operator"):
                docs.set_status(r, new, by=p.username)
                st.rerun()
with t4:
    txt = st.text_area("Notas internas", notes)
    if st.button("Guardar notas", disabled=not has_role(p, "operator")):
        with db() as s:
            s.get(InventoryItem, iid).notes = txt
        st.rerun()
