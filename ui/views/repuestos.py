import streamlit as st
from sqlalchemy import select

from mototrade.core.money import D
from mototrade.db.models import Deal, InventoryItem, Repair, RepairQuote, Supplier
from mototrade.modules.parts import PartsService, compare_quotes
from ui.common import db, df, has_role, money, require_login, simulated_banner

p = require_login()
st.title("🔧 Repuestos y reparaciones")
simulated_banner("No hay búsqueda automática de repuestos (sin API de proveedores). Cargá presupuestos con fuente y fecha; el sistema compara reparar vs. sustituir.")

tab_rep, tab_sup = st.tabs(["Reparaciones y presupuestos", "Proveedores / talleres"])
with tab_sup:
    with st.form("sup", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("Nombre")
        kind = c2.selectbox("Tipo", ["parts", "workshop", "transport", "other"])
        contact = c3.text_input("Contacto")
        if st.form_submit_button("Agregar", disabled=not has_role(p, "operator")) and name:
            with db() as s:
                PartsService(s).add_supplier(name=name, kind=kind, contact=contact, by=p.username)
            st.rerun()
    with db() as s:
        rows = [{"id": x.id, "nombre": x.name, "tipo": x.kind, "contacto": x.contact, "activo": x.is_active} for x in s.execute(select(Supplier)).scalars()]
    st.dataframe(df(rows), width="stretch", hide_index=True)

with tab_rep:
    with db() as s:
        deals = {f"deal:{d.id}": f"Deal #{d.id} {d.listing.make} {d.listing.model}" for d in s.execute(select(Deal).where(Deal.stage.notin_(["lost", "discarded"]))).scalars() if d.listing}
        items = {f"item:{i.id}": f"Inventario #{i.id} {i.make} {i.model}" for i in s.execute(select(InventoryItem).where(InventoryItem.status != "sold")).scalars()}
        sups = {x.id: x.name for x in s.execute(select(Supplier)).scalars()}
    targets = {**deals, **items}
    if not targets:
        st.info("No hay deals ni motos en inventario.")
        st.stop()
    target = st.selectbox("Moto", list(targets), format_func=lambda k: targets[k])
    kind, tid = target.split(":")
    deal_id = int(tid) if kind == "deal" else None
    item_id = int(tid) if kind == "item" else None
    with st.form("rep", clear_on_submit=True):
        issue = st.text_input("Daño / trabajo necesario")
        if st.form_submit_button("Agregar reparación", disabled=not has_role(p, "operator")) and issue:
            with db() as s:
                PartsService(s).add_repair(issue=issue, deal_id=deal_id, inventory_item_id=item_id, by=p.username)
            st.rerun()
    with db() as s:
        q = select(Repair).where(Repair.deal_id == deal_id) if deal_id else select(Repair).where(Repair.inventory_item_id == item_id)
        reps = [(r.id, r.issue, r.state, r.chosen_quote_id, [(x.id, x.option, x.part_cost, x.labor_cost, x.shipping_cost, x.total, x.lead_time_days, x.availability_verified, x.is_simulated, x.source) for x in r.quotes]) for r in s.execute(q).scalars()]
    for rid, issue, state, chosen, quotes in reps:
        with st.container(border=True):
            st.markdown(f"**#{rid} {issue}** · estado `{state}`" + (f" · presupuesto elegido #{chosen}" if chosen else ""))
            if quotes:
                st.dataframe(df([{"id": q[0], "opción": q[1], "repuesto": money(q[2]), "mano de obra": money(q[3]), "envío": money(q[4]),
                                  "total": money(q[5]), "plazo (días)": q[6], "verificado": q[7], "simulado": q[8], "fuente": q[9]} for q in quotes]),
                             width="stretch", hide_index=True)
                with db() as s:
                    cmp = compare_quotes(list(s.get(Repair, rid).quotes))
                st.caption(cmp.recommendation)
                qid = st.selectbox("Elegir presupuesto", [q[0] for q in quotes], key=f"ch{rid}")
                if st.button("Confirmar elección", key=f"chb{rid}", disabled=not has_role(p, "operator")):
                    with db() as s:
                        PartsService(s).choose_quote(s.get(Repair, rid), s.get(RepairQuote, qid), by=p.username)
                    st.rerun()
            with st.form(f"q{rid}", clear_on_submit=True):
                c = st.columns(6)
                option = c[0].selectbox("Opción", ["replace", "repair"])
                part = c[1].number_input("Repuesto", 0, 50_000_000, 0, step=1000)
                labor = c[2].number_input("Mano de obra", 0, 50_000_000, 0, step=1000)
                ship = c[3].number_input("Envío", 0, 50_000_000, 0, step=1000)
                lead = c[4].number_input("Plazo días", 0, 365, 0)
                sup = c[5].selectbox("Proveedor", [None] + list(sups), format_func=lambda i: "—" if i is None else sups[i])
                src = st.text_input("Fuente del precio (dónde/cuándo)")
                ver = st.checkbox("Disponibilidad verificada")
                if st.form_submit_button("Agregar presupuesto", disabled=not has_role(p, "operator")):
                    with db() as s:
                        PartsService(s).add_quote(s.get(Repair, rid), option=option, part_cost=D(part), labor_cost=D(labor), shipping_cost=D(ship),
                                                  lead_time_days=int(lead) or None, supplier_id=sup, source=src, availability_verified=ver, by=p.username)
                    st.rerun()
