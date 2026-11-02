import streamlit as st
from sqlalchemy import select

from mototrade.core import approvals, kill_switch
from mototrade.db.models import Deal, JobRun, Listing
from mototrade.modules.finance import FinanceService
from ui.common import db, df, gate_snapshot, money, require_login, rules, settings

p = require_login()
st.title("📊 Dashboard")

with db() as s:
    fin = FinanceService(s, rules()).summary()
    pend = approvals.pending(s)
    engaged = kill_switch.is_engaged(s)
    deals = list(s.execute(select(Deal).where(Deal.stage.in_(["opportunity", "valuated", "negotiating", "pending_approval"]))
                           .order_by(Deal.opportunity_score.desc().nullslast()).limit(8)).scalars())
    deal_rows = [{"deal": d.id, "etapa": d.stage, "score": d.opportunity_score, "clase": d.opportunity_class,
                  "descuento": f"{float(d.discount_pct):.0%}" if d.discount_pct is not None else "—",
                  "moto": f"{d.listing.make} {d.listing.model} {d.listing.year or ''}" if d.listing else "—",
                  "precio pedido": money(d.listing.asking_price) if d.listing else "—",
                  "máx. compra": money(d.score_breakdown.get("max_purchase")) if d.score_breakdown.get("max_purchase") else "—",
                  "límite autorizado": money(d.authorized_max_price)} for d in deals]
    n_listings = s.execute(select(Listing).where(Listing.status.in_(["new", "watching"]))).scalars().all()
    last_runs = list(s.execute(select(JobRun).order_by(JobRun.id.desc()).limit(6)).scalars())
    run_rows = [{"job": r.job_name, "estado": r.status, "inicio": r.started_at, "stats": r.stats, "error": r.error} for r in last_runs]

if engaged:
    st.error("🛑 Parada de emergencia activa: ningún agente genera borradores ni se envían notificaciones.")
snap, pos, ai = gate_snapshot()
cols = st.columns(5)
for i, (fn, label) in enumerate((("search", "Búsqueda"), ("buyer_agent", "Comprador"), ("seller_agent", "Vendedor"),
                                 ("visual_inspection", "Inspección"), ("ai_calls", "IA"))):
    intended, effective, lab, _r = snap[fn]
    cols[i].markdown(f"{'🟢' if effective else ('⚪' if not intended else '🟡')} **{label}**  \n{lab}")
extra = [d for d in deals if d.opportunity_class == "extraordinary"]
if extra:
    st.error(f"🚨 {len(extra)} oportunidad(es) extraordinaria(s) pendientes de verificación: " +
             ", ".join(f"#{d.id} {d.listing.make} {d.listing.model}" for d in extra[:5]))

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Capital disponible", money(fin["available_capital"]))
c2.metric("Capital en stock", money(fin["capital_in_stock"]), f"{fin['units_in_stock']} motos")
c3.metric("Utilidad realizada", money(fin["realized_profit"]), f"{fin['units_sold']} vendidas")
c4.metric("Anuncios activos", len(n_listings))
c5.metric("Aprobaciones pendientes", len(pend))

left, right = st.columns([3, 2])
with left:
    st.subheader("Oportunidades en curso")
    if deal_rows:
        st.dataframe(df(deal_rows), width="stretch", hide_index=True)
    else:
        st.info("Sin oportunidades. Cargá anuncios en el Radar.")
with right:
    st.subheader("Pendientes de tu decisión")
    if pend:
        for a in pend[:8]:
            st.markdown(f"- **#{a.id}** `{a.kind}` — {a.summary}")
        st.page_link("views/aprobaciones.py", label="Ir a Aprobaciones →")
    else:
        st.success("Nada pendiente.")

st.subheader("Estado de integraciones")
cols = st.columns(3)
for i, (k, v) in enumerate(settings().integration_status().items()):
    with cols[i % 3]:
        icon = "🟢" if v["modo"] == "real" else ("🟡" if v["modo"] == "manual" else "🧪")
        st.markdown(f"{icon} **{k}** — {v['estado']}")

st.subheader("Últimas corridas del worker")
if run_rows:
    st.dataframe(df(run_rows), width="stretch", hide_index=True)
else:
    st.caption("El worker todavía no corrió. `python -m mototrade.cli worker --once`")
