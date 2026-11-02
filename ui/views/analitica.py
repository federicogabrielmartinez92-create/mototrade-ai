import streamlit as st

from mototrade.modules.analytics import AnalyticsService
from ui.common import db, df, money, pct, require_login, rules

p = require_login()
st.title("📈 Analítica comercial")
st.caption("Solo datos reales registrados. Sin proyecciones.")
with db() as s:
    an = AnalyticsService(s, rules())
    k = an.kpis()
    by_model = an.performance_by_model()
    acc = an.valuation_accuracy()

c = st.columns(4)
c[0].metric("Vendidas", k["units_sold"])
c[1].metric("En stock", k["units_in_stock"])
c[2].metric("Margen s/ costo", pct(k["margin_on_cost"]))
c[3].metric("Retorno anualizado s/ capital", pct(k["annualized_return_on_capital"]))
c = st.columns(4)
c[0].metric("Días prom. (vendidas)", f"{k['avg_days_in_stock_sold']:.0f}" if k["avg_days_in_stock_sold"] else "—")
c[1].metric("Días prom. (stock actual)", f"{k['avg_days_in_stock_current']:.0f}" if k["avg_days_in_stock_current"] else "—")
c[2].metric("Rotación anual", f"{k['rotation_per_year']:.1f}x" if k["rotation_per_year"] else "—")
c[3].metric("Conversión de deals", pct(k["deal_conversion"]))
c = st.columns(4)
c[0].metric("Anuncios", k["listings_total"])
c[1].metric("Deals", k["deals_total"])
c[2].metric("Conversaciones", k["conversations_total"])
c[3].metric("Utilidad realizada", money(k["realized_profit"]))

st.subheader("Rendimiento por modelo")
if by_model:
    rows = [{**r, "profit": money(r["profit"]), "cost": money(r["cost"]), "margin_on_cost": pct(r["margin_on_cost"])} for r in by_model]
    st.dataframe(df(rows), width="stretch", hide_index=True)
else:
    st.info("Sin datos todavía.")
st.subheader("Precisión del tasador")
st.markdown(f"Muestras: **{acc['samples']}** · error medio: **{pct(acc['mean_error'])}** · error absoluto medio: **{pct(acc['mean_abs_error'])}**")
st.caption("Se calcula comparando el precio de mercado estimado contra la venta real de la misma moto. Necesita ventas registradas.")
