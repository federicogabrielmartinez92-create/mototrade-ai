import streamlit as st
from sqlalchemy import select

from mototrade.core.money import D
from mototrade.db.models import Comparable, Deal, Valuation
from mototrade.modules.finance import FinanceService
from mototrade.modules.parts import PartsService
from mototrade.modules.valuation import CostInputs, ValuationService
from ui.common import db, df, has_role, money, pct, require_login, rules

p = require_login()
R = rules()
st.title("🧮 Tasaciones")
st.caption(f"Motor determinista · reglas v{R.version} hash `{R.rules_hash[:12]}` · ROI mínimo {R.valuation.minimum_roi:.0%} · "
           f"base de cálculo: {R.valuation.deal_basis}")

tab_val, tab_comp = st.tabs(["Tasar un deal", "Comparables"])

with tab_comp:
    st.subheader("Cargar comparable")
    st.caption("Distinguí siempre precio PEDIDO (asking) de VENTA EFECTIVA (sold). El motor nunca los confunde.")
    with st.form("comp", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns(4)
        kind = c1.selectbox("Tipo", ["asking", "sold"])
        make = c2.text_input("Marca", "Honda")
        model = c3.text_input("Modelo", "Wave 110")
        year = c4.number_input("Año", 1990, 2030, 2022)
        c1, c2, c3 = st.columns(3)
        km = c1.number_input("Km", 0, 500000, 15000, step=500)
        price = c2.number_input("Precio (ARS)", 0, 100_000_000, 2_500_000, step=10_000)
        source = c3.text_input("Fuente", "mercadolibre (manual)")
        ref = st.text_input("Referencia / URL")
        if st.form_submit_button("Agregar", disabled=not has_role(p, "operator")):
            with db() as s:
                ValuationService(s, R).add_comparable(kind=kind, make=make, model=model, year=int(year), kilometers=int(km),
                                                      price=D(price), source=source, source_ref=ref or None, by=p.username)
            st.success("Comparable agregado.")
    with db() as s:
        rows = [{"id": c.id, "tipo": c.kind, "moto": f"{c.make} {c.model} {c.year or ''}", "km": c.kilometers, "precio": money(c.price),
                 "fuente": c.source, "fecha": c.observed_at} for c in s.execute(select(Comparable).order_by(Comparable.id.desc()).limit(200)).scalars()]
    st.dataframe(df(rows), width="stretch", hide_index=True)

with tab_val:
    with db() as s:
        deals = list(s.execute(select(Deal).where(Deal.stage.notin_(["purchased", "lost", "discarded"])).order_by(Deal.id.desc())).scalars())
        opts = {d.id: f"#{d.id} {d.listing.make} {d.listing.model} {d.listing.year or ''} · pedido {money(d.listing.asking_price)} · {d.stage}"
                for d in deals if d.listing}
    if not opts:
        st.info("No hay deals abiertos. Creá anuncios en el Radar y escaneá oportunidades.")
        st.stop()
    deal_id = st.selectbox("Deal", list(opts), format_func=lambda i: opts[i])
    with db() as s:
        deal = s.get(Deal, deal_id)
        listing = deal.listing
        repair_ci = PartsService(s).cost_inputs_for(deal_id=deal.id)
        cap = FinanceService(s, R).available_capital()
        asking = D(listing.asking_price)
    st.markdown(f"**Reparaciones cargadas:** repuestos {money(repair_ci.repair_parts)} · mano de obra {money(repair_ci.repair_labor)} "
                f"({', '.join(f'{k}: {v}' for k, v in repair_ci.sources.items()) or 'sin reparaciones registradas'})")
    with st.form("valuate"):
        c1, c2, c3 = st.columns(3)
        purchase = c1.number_input("Precio a evaluar (ARS)", 0, 100_000_000, int(asking), step=10_000)
        manual = c2.number_input("Precio de mercado manual (0 = usar comparables)", 0, 100_000_000, 0, step=10_000)
        parts = c3.number_input("Repuestos (ARS)", 0, 50_000_000, int(repair_ci.repair_parts), step=1_000)
        c1, c2, c3, c4 = st.columns(4)
        labor = c1.number_input("Mano de obra", 0, 50_000_000, int(repair_ci.repair_labor), step=1_000)
        transfer = c2.number_input("Transferencia (0 = fórmula)", 0, 50_000_000, 0, step=1_000)
        logistics = c3.number_input("Traslado (0 = default)", 0, 50_000_000, 0, step=1_000)
        prep = c4.number_input("Preparación (0 = default)", 0, 50_000_000, 0, step=1_000)
        run = st.form_submit_button("Calcular y guardar tasación", type="primary", disabled=not has_role(p, "operator"))
    if run:
        ci = CostInputs(repair_parts=D(parts), repair_labor=D(labor), transfer=D(transfer) if transfer else None,
                        logistics=D(logistics) if logistics else None, preparation=D(prep) if prep else None)
        with db() as s:
            deal = s.get(Deal, deal_id)
            row, est, math = ValuationService(s, R).valuate_deal(deal, cost_inputs=ci, purchase_price=D(purchase),
                                                                 manual_market_price=D(manual) if manual else None,
                                                                 available_capital=cap, by=p.username)
            st.session_state["last_val"] = row.id
    with db() as s:
        vals = list(s.execute(select(Valuation).where(Valuation.deal_id == deal_id).order_by(Valuation.id.desc())).scalars())
    if not vals:
        st.info("Este deal todavía no tiene tasaciones.")
        st.stop()
    v = vals[0]
    st.subheader(f"Última tasación #{v.id} · {v.created_at:%d/%m/%Y %H:%M}")
    conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}[v.confidence]
    st.markdown(f"{conf_icon} **Confianza {v.confidence}** ({float(v.confidence_score):.2f}) · base `{v.basis}` · {v.comparables_used} comparables · reglas `{v.rules_hash[:12]}`")
    if v.confidence == "low":
        st.warning("Confianza BAJA: no negociar sin revisión humana y más comparables.")
    c = st.columns(5)
    c[0].metric("Precio de mercado", money(v.market_price))
    c[1].metric("Venta rápida", money(v.quick_sale_price))
    c[2].metric("Publicar a", money(v.suggested_list_price))
    c[3].metric("Oferta inicial", money(v.initial_offer))
    c[4].metric("COMPRA MÁXIMA", money(v.max_purchase_price))
    c = st.columns(4)
    c[0].metric("Costos totales", money(v.total_costs))
    c[1].metric("Ganancia esperada", money(v.expected_profit))
    c[2].metric("ROI s/ costo total", pct(v.expected_roi))
    c[3].metric("Precio evaluado", money(purchase if run else asking))
    l, r = st.columns(2)
    with l:
        st.markdown("**Desglose de costos**")
        st.dataframe(df([{"concepto": k, "ARS": money(val)} for k, val in v.costs.items()]), width="stretch", hide_index=True)
    with r:
        st.markdown("**Escenarios**")
        st.dataframe(df([{"escenario": k, "venta": money(sc["sale_price"]), "ganancia": money(sc["profit"]), "ROI": pct(sc["roi"])}
                         for k, sc in v.scenarios.items()]), width="stretch", hide_index=True)
    with st.expander("Fuentes y notas"):
        st.text(v.notes)
        st.dataframe(df(v.sources), width="stretch", hide_index=True)
