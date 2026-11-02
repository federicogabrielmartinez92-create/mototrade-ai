import streamlit as st
from sqlalchemy import select

from mototrade.core.money import D
from mototrade.db.models import InventoryItem, Publication
from mototrade.modules.publications import PublicationsService
from ui.common import db, df, flash, has_role, money, require_login, rules, simulated_banner

p = require_login()
R = rules()
st.title("📢 Publicaciones de venta")
simulated_banner("La publicación automática por API está PENDIENTE (ML exige paquete contratado; Marketplace/RosarioGarage no tienen API). "
                 "El sistema genera el aviso con datos reales, pide aprobación y vos lo publicás a mano.")

with db() as s:
    items = {i.id: f"#{i.id} {i.make} {i.model} {i.year or ''} · lista {money(i.list_price)}" for i in
             s.execute(select(InventoryItem).where(InventoryItem.status.in_(["ready", "published"]))).scalars()}
if items:
    iid = st.selectbox("Moto", list(items), format_func=lambda i: items[i])
    c1, c2 = st.columns(2)
    channel = c1.selectbox("Canal", ["mercadolibre_manual", "facebook_marketplace_manual", "rosariogarage_manual", "instagram_manual"])
    extras = c2.text_input("Extras reales a mencionar (separados por ;)")
    if st.button("Generar borrador de aviso", type="primary", disabled=not has_role(p, "operator")):
        with db() as s:
            try:
                PublicationsService(s, R.identity.business_name).draft(s.get(InventoryItem, iid), channel=channel,
                                                                        extras=[e.strip() for e in extras.split(";") if e.strip()], by=p.username)
                flash("success", "Borrador creado; aprobalo en Aprobaciones.")
            except ValueError as e:
                flash("error", str(e))
        st.rerun()
else:
    st.info("No hay motos en estado ready/published.")

with db() as s:
    pubs = [(x.id, x.inventory_item_id, x.channel, x.title, x.body, x.price, x.status, x.external_url, x.inquiries_count)
            for x in s.execute(select(Publication).order_by(Publication.id.desc()).limit(50)).scalars()]
for pid, iid_, ch, title, body, price, status, url, inq in pubs:
    with st.container(border=True):
        st.markdown(f"**#{pid} {title}** · `{ch}` · {money(price)} · estado `{status}` · consultas {inq}")
        st.code(body, language=None)
        c1, c2, c3 = st.columns(3)
        if status == "pending_approval":
            st.caption("Pendiente de aprobación (sección Aprobaciones).")
        if status in ("pending_approval", "draft"):
            u = c1.text_input("URL donde lo publicaste", key=f"u{pid}")
            if c1.button("Marcar publicado (manual)", key=f"pub{pid}", disabled=not has_role(p, "operator")):
                with db() as s:
                    try:
                        PublicationsService(s).mark_published_manual(s.get(Publication, pid), by=p.username, external_url=u or None)
                    except ValueError as e:
                        flash("error", str(e))
                st.rerun()
        if status.startswith("published"):
            if c2.button("+1 consulta recibida", key=f"inq{pid}"):
                with db() as s:
                    PublicationsService(s).record_inquiry(s.get(Publication, pid))
                st.rerun()
            if c3.button("Cerrar publicación", key=f"cl{pid}"):
                with db() as s:
                    PublicationsService(s).close(s.get(Publication, pid), by=p.username)
                st.rerun()
