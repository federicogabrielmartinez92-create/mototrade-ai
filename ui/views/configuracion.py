import streamlit as st
from sqlalchemy import select

from mototrade.core import audit, kill_switch
from mototrade.db.models import AuditLog, User
from mototrade.security import AuthError, create_user
from ui.common import db, df, flash, has_role, require_login, rules, settings

p = require_login()
R = rules()
S = settings()
st.title("⚙️ Configuración de agentes")

st.subheader("🛑 Parada de emergencia")
with db() as s:
    engaged, paused = kill_switch.is_engaged(s), kill_switch.is_worker_paused(s)
c1, c2 = st.columns(2)
if engaged:
    c1.error("ACTIVA: no hay borradores de agentes, notificaciones ni jobs.")
    if c1.button("Liberar (solo admin)", disabled=not has_role(p, "admin")):
        with db() as s:
            kill_switch.release(s, by=p.username)
        st.rerun()
else:
    reason = c1.text_input("Motivo")
    if c1.button("🛑 ACTIVAR PARADA DE EMERGENCIA", type="primary", disabled=not has_role(p, "operator")):
        with db() as s:
            kill_switch.engage(s, by=p.username, reason=reason)
        st.rerun()
if paused:
    if c2.button("▶️ Reanudar worker", disabled=not has_role(p, "admin")):
        with db() as s:
            kill_switch.set_worker_paused(s, False, by=p.username)
        st.rerun()
else:
    if c2.button("⏸️ Pausar worker (sin jobs automáticos)", disabled=not has_role(p, "admin")):
        with db() as s:
            kill_switch.set_worker_paused(s, True, by=p.username)
        st.rerun()

st.subheader("Reglas de negocio (solo lectura)")
st.caption(f"Archivo `{R.source_path}` · versión {R.version} · SHA-256 `{R.rules_hash}`. Se editan en el archivo y se reinician los servicios; la IA no puede modificarlas.")
c = st.columns(4)
c[0].metric("Capital inicial", f"$ {int(R.capital.initial_capital):,}".replace(",", "."))
c[1].metric("ROI mínimo", f"{R.valuation.minimum_roi:.0%}")
c[2].metric("Máx. por moto", f"{R.capital.max_capital_per_unit_pct:.0%} del capital")
c[3].metric("Rondas máx. comprador", R.negotiation.buyer.max_rounds)
with st.expander("Ver todas las reglas"):
    st.json(R.model_dump(mode="json", exclude={"rules_hash", "source_path"}))

st.subheader("Integraciones")
for k, v in S.integration_status().items():
    st.markdown(f"- **{k}**: {v['estado']} · modo `{v['modo']}`")
for w in S.security_warnings():
    st.warning(w)

st.subheader("Usuarios")
with db() as s:
    users = [{"usuario": u.username, "rol": u.role, "activo": u.is_active, "último acceso": u.last_login_at} for u in s.execute(select(User)).scalars()]
st.dataframe(df(users), width="stretch", hide_index=True)
with st.form("newuser", clear_on_submit=True):
    c1, c2, c3 = st.columns(3)
    un = c1.text_input("Usuario nuevo")
    pw = c2.text_input("Contraseña (mín. 8)", type="password")
    role = c3.selectbox("Rol", ["operator", "viewer", "admin"])
    if st.form_submit_button("Crear usuario", disabled=not has_role(p, "admin")):
        with db() as s:
            try:
                create_user(s, un, pw, role, created_by=p.username)
                flash("success", "Usuario creado.")
            except AuthError as e:
                flash("error", str(e))
        st.rerun()

st.subheader("Auditoría")
with db() as s:
    ok, broken = audit.verify_chain(s)
    log = [{"id": a.id, "fecha": a.occurred_at, "actor": f"{a.actor_type}:{a.actor_id}", "acción": a.action, "entidad": f"{a.entity_type}:{a.entity_id}",
            "detalle": a.details} for a in s.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(100)).scalars()]
if ok:
    st.success("Cadena de hashes íntegra.")
else:
    st.error(f"⚠️ Cadena de auditoría ROTA en la fila {broken}.")
st.dataframe(df(log), width="stretch", hide_index=True)
