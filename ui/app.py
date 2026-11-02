"""Panel privado MotoTrade AI (Streamlit). Ejecutar: streamlit run ui/app.py"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Streamlit Community Cloud: los secretos llegan por st.secrets (TOML). Los volcamos a variables de entorno
# ANTES de cargar la configuración, así el mismo código corre en Docker/VPS (.env) y en la nube gratuita.
from mototrade.config.secrets_bridge import apply_streamlit_secrets  # noqa: E402

BRIDGE = apply_streamlit_secrets()

from mototrade.core import kill_switch  # noqa: E402
from mototrade.security import AuthError, authenticate, issue_token  # noqa: E402
from ui.common import db, inject_css, pending_badge, principal, settings, show_flash  # noqa: E402

st.set_page_config(page_title="MotoTrade AI", page_icon="🏍️", layout="wide", initial_sidebar_state="expanded")
inject_css()


def _show_db_diagnosis(exc: Exception | None = None):
    """Diagnóstico de conexión seguro: sin contraseñas, tokens ni URL completa. También se registra en los logs."""
    import logging

    from mototrade.db.diagnostics import diagnose_from_settings

    d = diagnose_from_settings()
    logging.getLogger("mototrade.ui").warning("diagnóstico de base: %s | %s | %s", d.category, d.message, d.url.redacted)
    titles = {"missing_url": "No llegó la URL de la base de datos", "url_invalid": "La URL de la base es inválida",
              "dns": "El host de la base no resuelve (DNS)", "network": "No se puede alcanzar la base (red/puerto/proyecto pausado)",
              "ssl": "Fallo de TLS/SSL", "tenant": "El pooler de Supabase no reconoce el usuario", "auth": "Contraseña o usuario incorrectos",
              "database": "La base de datos indicada no existe", "schema_pending": "Conecta, pero faltan migraciones",
              "ok": "La base responde", "unknown": "Error de base de datos"}
    st.error(f"🛑 {titles.get(d.category, d.category)}")
    st.markdown(f"**Categoría:** `{d.category}`  \n**Detalle (redactado):** {d.message}")
    st.markdown(f"**Conexión configurada:** `{d.url.redacted or '—'}`  \n"
                f"dialecto `{d.url.dialect or '?'}` · host `{d.url.host or '—'}` · puerto `{d.url.port or '—'}` · "
                f"usuario `{d.url.username or '—'}` · base `{d.url.database or '—'}`")
    if d.schema_current or d.schema_head:
        st.markdown(f"**Esquema:** aplicado `{d.schema_current or 'ninguno'}` · esperado `{d.schema_head}`")
    for h in d.hints:
        st.markdown(f"- {h}")
    with st.expander("Secretos detectados (solo nombres, nunca valores)"):
        st.write({"cargados desde st.secrets": BRIDGE.loaded, "ya estaban en el entorno": BRIDGE.skipped_existing,
                  "ignorados": BRIDGE.ignored, "error del puente": BRIDGE.error,
                  "MOTOTRADE_DATABASE_URL presente": BRIDGE.has_database_url})
    if exc is not None:
        from mototrade.db.diagnostics import redact

        st.caption("Excepción original (redactada): " + redact(f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")[:200])


def login_view():
    st.title("🏍️ MotoTrade AI")
    st.caption("Acceso privado. Ingresá con tu usuario del panel.")
    # Primer arranque: si no existe ningún usuario y hay MOTOTRADE_ADMIN_PASSWORD en los secretos, se crea el admin.
    # La contraseña nunca se muestra ni se registra; el evento queda en la auditoría como "user.created".
    try:
        from mototrade.security import ensure_admin_from_settings

        with db() as s:
            created = ensure_admin_from_settings(s)
        if created:
            st.info(f"Usuario administrador inicial '{created.username}' creado a partir de los secretos. Ingresá con esa contraseña.")
    except Exception as exc:  # p. ej. tablas no creadas todavía o conexión fallida
        _show_db_diagnosis(exc)
        st.stop()
    with st.form("login"):
        u = st.text_input("Usuario")
        pw = st.text_input("Contraseña", type="password")
        if st.form_submit_button("Ingresar", type="primary"):
            try:
                with db() as s:
                    p = authenticate(s, u, pw)
                st.session_state["token"] = issue_token(p)
                st.rerun()
            except AuthError as e:
                st.error(str(e))
    for w in settings().security_warnings():
        st.warning(w)
    st.info("Si todavía no hay usuarios, ejecutá `python -m mototrade.cli bootstrap` con MOTOTRADE_ADMIN_PASSWORD en .env.")


p = principal()
if p is None:
    login_view()
    st.stop()

VIEWS = ROOT / "ui" / "views"
pages = {
    "Operación": [
        st.Page(str(VIEWS / "dashboard.py"), title="Dashboard", icon="📊", default=True),
        st.Page(str(VIEWS / "notificaciones.py"), title="Notificaciones", icon="🔔"),
        st.Page(str(VIEWS / "centro_control.py"), title="Centro de Control", icon="🎛️"),
        st.Page(str(VIEWS / "aprobaciones.py"), title="Aprobaciones", icon="✅"),
        st.Page(str(VIEWS / "radar.py"), title="Radar de oportunidades", icon="📡"),
        st.Page(str(VIEWS / "tasaciones.py"), title="Tasaciones", icon="🧮"),
        st.Page(str(VIEWS / "inspeccion.py"), title="Inspección visual", icon="🔍"),
        st.Page(str(VIEWS / "repuestos.py"), title="Repuestos y reparaciones", icon="🔧"),
        st.Page(str(VIEWS / "negociaciones_compra.py"), title="Negociaciones de compra", icon="🤝"),
    ],
    "Venta": [
        st.Page(str(VIEWS / "inventario.py"), title="Inventario", icon="🏍️"),
        st.Page(str(VIEWS / "publicaciones.py"), title="Publicaciones de venta", icon="📢"),
        st.Page(str(VIEWS / "negociaciones_venta.py"), title="Negociaciones de venta", icon="💬"),
        st.Page(str(VIEWS / "crm.py"), title="CRM", icon="👥"),
    ],
    "Gestión": [
        st.Page(str(VIEWS / "finanzas.py"), title="Finanzas", icon="💰"),
        st.Page(str(VIEWS / "analitica.py"), title="Analítica", icon="📈"),
        st.Page(str(VIEWS / "configuracion.py"), title="Configuración de agentes", icon="⚙️"),
    ],
}

with st.sidebar:
    st.markdown(f"**👤 {p.username}** · rol `{p.role}`")
    with db() as s:
        engaged = kill_switch.is_engaged(s)
        paused = kill_switch.is_worker_paused(s)
        n_pending = pending_badge(s)
    if engaged:
        st.error("🛑 PARADA DE EMERGENCIA ACTIVA")
    elif paused:
        st.warning("⏸️ Worker pausado")
    else:
        st.success("🟢 Automatizaciones habilitadas")
    with db() as s:
        from mototrade.core.notifications import unread_count

        n_unread = unread_count(s)
    st.markdown(f"**Aprobaciones pendientes:** {n_pending} · **🔔 Notificaciones:** {n_unread}")
    from ui.common import gate_snapshot

    snap, pos, _ = gate_snapshot()
    for fn, label in (("search", "Búsqueda"), ("buyer_agent", "Agente comprador"), ("seller_agent", "Agente vendedor"), ("ai_calls", "IA")):
        intended, effective, lab, _r = snap[fn]
        st.caption(f"{'🟢' if effective else ('⚪' if not intended else '🟡')} {label}: {lab}")
    st.caption(f"Capital libre: $ {int(float(pos['free_capital'])):,} · motos {pos['units_active']}/{pos['max_units']}".replace(",", "."))
    if st.button("Cerrar sesión"):
        st.session_state.pop("token", None)
        st.rerun()

show_flash()
nav = st.navigation(pages)
nav.run()
