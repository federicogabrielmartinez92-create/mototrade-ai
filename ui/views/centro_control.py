from decimal import Decimal

import streamlit as st

from mototrade.control import ControlCenter
from mototrade.control.service import ControlError
from ui.common import control_settings, db, df, flash, gate_snapshot, has_role, money, require_login

p = require_login()
st.title("🎛️ Centro de Control")
st.caption("Configuración operativa persistente en la base, auditada y aplicada obligatoriamente por la API y el worker. "
           "Solo un admin puede modificarla. Las fórmulas (cómo se calcula) siguen en `config/business_rules.yaml`.")

cs = control_settings()
snap, pos, ai = gate_snapshot()

# ---- Estado efectivo ----------------------------------------------------------
st.subheader("Intención configurada vs. estado efectivo")
labels = {"search": "Búsqueda automática", "negotiable_analysis": "Oportunidades negociables", "buyer_agent": "Agente comprador",
          "seller_agent": "Agente vendedor", "visual_inspection": "Inspección visual", "parts_search": "Búsqueda de repuestos",
          "ai_calls": "Llamadas a IA", "auto_send": "Envío automático", "notifications": "Notificaciones"}
rows = []
for k, (intended, effective, label, reason) in snap.items():
    rows.append({"función": labels.get(k, k), "intención": "habilitada" if intended else "desactivada",
                 "estado efectivo": "🟢 activo" if effective else ("⚪ desactivado" if not intended else "🟡 pausado"),
                 "motivo": reason or "—"})
st.dataframe(df(rows), width="stretch", hide_index=True)

c = st.columns(6)
c[0].metric("Saldo de caja", money(pos["ledger_balance"]))
c[1].metric("Reserva mínima", money(pos["reserve"]))
c[2].metric("Reservado (compras pendientes)", money(pos["reserved_pending"]), f"{pos['pending_units']} pendientes")
c[3].metric("Capital libre", money(pos["free_capital"]))
c[4].metric("Motos activas", f"{pos['units_active']} / {pos['max_units']}")
c[5].metric("IA este mes (US$)", f"{float(ai['spent_month_usd']):.2f} / {float(ai['monthly_budget_usd']):.0f}")
if pos["reasons"]:
    st.warning("Compra bloqueada: " + "; ".join(pos["reasons"]))

admin = has_role(p, "admin")
if not admin:
    st.info("Solo lectura: tu rol no puede modificar la configuración.")


def _save(section: str, values: dict):
    try:
        with db() as s:
            ControlCenter(s).update_section(section, values, by=p.username)
        flash("success", f"Sección '{section}' guardada y auditada.")
    except ControlError as e:
        flash("error", str(e))
    st.rerun()


t1, t2, t3, t4 = st.tabs(["💰 Capital y exposición", "📡 Búsqueda", "🤖 Agentes", "🧠 Costos de IA"])

with t1:
    k = cs.capital
    with st.form("capital"):
        c1, c2, c3 = st.columns(3)
        capital_total = c1.number_input("Capital total (ARS)", 0, 1_000_000_000, int(k.capital_total), step=100_000)
        min_reserve_amount = c2.number_input("Reserva mínima (ARS)", 0, 1_000_000_000, int(k.min_reserve_amount), step=50_000)
        min_reserve_pct = c3.number_input("Reserva mínima (% del capital)", 0.0, 0.9, float(k.min_reserve_pct), step=0.01, format="%.2f")
        c1, c2, c3 = st.columns(3)
        max_units = c1.number_input("Máximo de motos simultáneas", 0, 100, int(k.max_units))
        max_unit_price = c2.number_input("Precio máximo de compra por unidad (ARS)", 0, 1_000_000_000, int(k.max_unit_price), step=50_000)
        max_committed = c3.number_input("Capital máximo comprometido (ARS)", 0, 1_000_000_000, int(k.max_committed_capital), step=100_000)
        c1, c2, c3 = st.columns(3)
        max_repair = c1.number_input("Gasto máximo de reparación por moto (ARS)", 0, 100_000_000, int(k.max_repair_cost), step=10_000)
        min_unit_budget = c2.number_input("Capital libre mínimo para seguir buscando (ARS)", 0, 1_000_000_000, int(k.min_unit_budget), step=50_000)
        buffer = c3.number_input("Colchón de gastos sobre compras pendientes (%)", 0.0, 0.9, float(k.pending_cost_buffer_pct), step=0.01, format="%.2f")
        st.caption("El capital libre se calcula: saldo de caja − reserva − compras pendientes (con colchón). Los aportes/retiros reales se registran en Finanzas.")
        if st.form_submit_button("Guardar capital", type="primary", disabled=not admin):
            _save("capital", {"capital_total": str(capital_total), "min_reserve_amount": str(min_reserve_amount),
                              "min_reserve_pct": str(round(min_reserve_pct, 4)), "max_units": int(max_units),
                              "max_unit_price": str(max_unit_price), "max_committed_capital": str(max_committed),
                              "max_repair_cost": str(max_repair), "min_unit_budget": str(min_unit_budget),
                              "pending_cost_buffer_pct": str(round(buffer, 4))})

with t2:
    sc = cs.search
    with st.form("search"):
        c1, c2, c3 = st.columns(3)
        auto = c1.toggle("Búsqueda automática", sc.auto_search_enabled)
        negot = c2.toggle("Analizar oportunidades negociables (modo B)", sc.negotiable_mode_enabled)
        any_model = c3.toggle("Permitir oportunidades de cualquier modelo (solo extraordinarias/rentables)", sc.allow_any_model)
        c1, c2, c3 = st.columns(3)
        interval = c1.number_input("Frecuencia de búsqueda (minutos)", 5, 1440, int(sc.search_interval_minutes))
        radius = c2.number_input("Radio desde Rosario (km)", 0, 1000, int(sc.radius_km))
        min_roi = c3.number_input("Rentabilidad mínima (fracción)", 0.0, 0.9, float(sc.minimum_roi), step=0.01, format="%.2f")
        c1, c2, c3 = st.columns(3)
        d_min = c1.number_input("Descuento objetivo mínimo", 0.0, 0.9, float(sc.target_discount_min), step=0.01, format="%.2f")
        d_max = c2.number_input("Descuento objetivo máximo", 0.0, 0.9, float(sc.target_discount_max), step=0.01, format="%.2f")
        d_ext = c3.number_input("Umbral extraordinaria (requiere verificación)", 0.0, 0.9, float(sc.extraordinary_discount_pct), step=0.01, format="%.2f")
        c1, c2 = st.columns(2)
        alert = c1.number_input("Descuento mínimo de alerta", 0.0, 0.9, float(sc.min_alert_discount_pct), step=0.01, format="%.2f")
        days = c2.text_input("Objetivo de venta (días mín–máx)", f"{sc.target_sale_days_min}-{sc.target_sale_days_max}")
        prio = st.text_area("Modelos prioritarios (uno por línea: Marca | Modelo)", "\n".join(f"{m.make} | {m.model}" for m in sc.priority_models), height=140)
        excl_models = st.text_area("Modelos excluidos (Marca | Modelo por línea)", "\n".join(f"{m.make} | {m.model}" for m in sc.excluded_models), height=70)
        excl_makes = st.text_input("Marcas excluidas (separadas por coma)", ", ".join(sc.excluded_makes))
        if st.form_submit_button("Guardar búsqueda", type="primary", disabled=not admin):
            def _models(txt):
                out = []
                for line in txt.splitlines():
                    if "|" in line:
                        a, b = line.split("|", 1)
                        if a.strip() and b.strip():
                            out.append({"make": a.strip(), "model": b.strip()})
                return out
            try:
                dmin_days, dmax_days = [int(x) for x in days.replace(" ", "").split("-")]
            except ValueError:
                dmin_days, dmax_days = sc.target_sale_days_min, sc.target_sale_days_max
            _save("search", {"auto_search_enabled": bool(auto), "negotiable_mode_enabled": bool(negot), "allow_any_model": bool(any_model),
                             "search_interval_minutes": int(interval), "radius_km": int(radius), "minimum_roi": str(round(min_roi, 4)),
                             "target_discount_min": str(round(d_min, 4)), "target_discount_max": str(round(d_max, 4)),
                             "extraordinary_discount_pct": str(round(d_ext, 4)), "min_alert_discount_pct": str(round(alert, 4)),
                             "target_sale_days_min": dmin_days, "target_sale_days_max": dmax_days,
                             "priority_models": _models(prio), "excluded_models": _models(excl_models),
                             "excluded_makes": [m.strip() for m in excl_makes.split(",") if m.strip()]})

with t3:
    ag = cs.agents
    with st.form("agents"):
        c1, c2, c3 = st.columns(3)
        buyer = c1.toggle("Agente comprador", ag.buyer_agent_enabled)
        seller = c2.toggle("Agente vendedor", ag.seller_agent_enabled)
        auto_send = c3.toggle("Envío automático en canales autorizados (hoy: ninguno)", ag.auto_send_enabled)
        c1, c2 = st.columns(2)
        vis = c1.toggle("Inspección visual con IA", ag.visual_inspection_enabled)
        parts = c2.toggle("Búsqueda de repuestos (solo manual por ahora)", ag.parts_search_enabled)
        c1, c2, c3 = st.columns(3)
        b_open = c1.number_input("Comprador: apertura (% del máximo autorizado)", 0.1, 1.0, float(ag.buyer_opening_pct_of_max), step=0.01, format="%.2f")
        b_step = c2.number_input("Comprador: mejora por ronda (fracción del máximo)", 0.0, 0.5, float(ag.buyer_step_pct), step=0.01, format="%.2f")
        b_rounds = c3.number_input("Comprador: rondas máximas", 1, 20, int(ag.buyer_max_rounds))
        c1, c2, c3 = st.columns(3)
        s_step = c1.number_input("Vendedor: concesión por ronda (fracción de lista)", 0.0, 0.5, float(ag.seller_step_pct), step=0.01, format="%.2f")
        s_rounds = c2.number_input("Vendedor: rondas máximas", 1, 20, int(ag.seller_max_rounds))
        cap = c3.number_input("Tope diario de mensajes por conversación", 1, 50, int(ag.daily_message_cap_per_conversation))
        st.caption("Los límites de cada negociación (precio máximo de compra / mínimo de venta) se autorizan por moto en Negociaciones e Inventario.")
        if st.form_submit_button("Guardar agentes", type="primary", disabled=not admin):
            _save("agents", {"buyer_agent_enabled": bool(buyer), "seller_agent_enabled": bool(seller), "auto_send_enabled": bool(auto_send),
                             "visual_inspection_enabled": bool(vis), "parts_search_enabled": bool(parts),
                             "buyer_opening_pct_of_max": str(round(b_open, 4)), "buyer_step_pct": str(round(b_step, 4)),
                             "buyer_max_rounds": int(b_rounds), "seller_step_pct": str(round(s_step, 4)),
                             "seller_max_rounds": int(s_rounds), "daily_message_cap_per_conversation": int(cap)})

with t4:
    b = cs.ai_budget
    with st.form("ai"):
        c1, c2, c3 = st.columns(3)
        monthly = c1.number_input("Presupuesto mensual máximo de IA (US$)", 0.0, 10_000.0, float(b.monthly_budget_usd), step=1.0)
        daily = c2.number_input("Límite diario (US$)", 0.0, 1_000.0, float(b.daily_budget_usd), step=0.5)
        notif = c3.number_input("Frecuencia de notificaciones (min)", 1, 1440, int(b.notification_frequency_minutes))
        c1, c2 = st.columns(2)
        max_vis = c1.number_input("Máximo de análisis visuales por mes", 0, 10_000, int(b.max_visual_analyses_per_month))
        max_deep = c2.number_input("Máximo de análisis en profundidad por día", 0, 1_000, int(b.max_deep_analyses_per_day))
        st.caption(f"Gastado: US$ {float(ai['spent_month_usd']):.3f} este mes · US$ {float(ai['spent_today_usd']):.3f} hoy · "
                   f"{ai['visual_this_month']} análisis visuales este mes. Al agotarse el presupuesto se detienen redacción con IA, "
                   "inspección y análisis profundo; inventario, finanzas y conversaciones siguen disponibles.")
        if st.form_submit_button("Guardar presupuesto de IA", type="primary", disabled=not admin):
            _save("ai_budget", {"monthly_budget_usd": str(round(monthly, 2)), "daily_budget_usd": str(round(daily, 2)),
                                "notification_frequency_minutes": int(notif), "max_visual_analyses_per_month": int(max_vis),
                                "max_deep_analyses_per_day": int(max_deep)})

with st.expander("Historial de versiones de configuración"):
    with db() as s:
        st.json(ControlCenter(s).versions())
