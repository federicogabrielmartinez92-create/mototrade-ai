import streamlit as st
from sqlalchemy import select

from mototrade.db.models import Contact
from mototrade.modules.crm import CrmService
from ui.common import db, df, has_role, require_login

p = require_login()
st.title("👥 CRM")
with db() as s:
    crm = CrmService(s)
    pipeline = crm.pipeline()
    attention = [{"id": c.id, "dirección": c.direction, "canal": c.channel, "estado": c.status, "rondas": c.rounds} for c in crm.conversations_needing_attention()]
    stale = [{"id": c.id, "dirección": c.direction, "estado": c.status} for c in crm.stale_conversations()]
    contacts = [{"id": c.id, "alias": c.alias, "canal": c.channel, "tipo": c.kind, "opt-in": c.opt_in_messaging, "no contactar": c.do_not_contact} for c in s.execute(select(Contact)).scalars()]

st.subheader("Pipeline de compras")
cols = st.columns(max(1, len(pipeline)) if pipeline else 1)
for i, (stage, n) in enumerate(pipeline.items()):
    cols[i].metric(stage, n)
if not pipeline:
    st.caption("Sin deals todavía.")
c1, c2 = st.columns(2)
with c1:
    st.subheader("Conversaciones que esperan tu decisión")
    if attention:
        st.dataframe(df(attention), width="stretch", hide_index=True)
    else:
        st.success("Ninguna.")
with c2:
    st.subheader("Sin movimiento hace 3+ días")
    if stale:
        st.dataframe(df(stale), width="stretch", hide_index=True)
    else:
        st.success("Ninguna.")
st.subheader("Contactos")
st.caption("Datos mínimos. El opt-in de mensajería es obligatorio para cualquier envío automático futuro (WhatsApp).")
with st.form("contact", clear_on_submit=True):
    c = st.columns(5)
    alias = c[0].text_input("Alias")
    channel = c[1].selectbox("Canal", ["whatsapp", "mercadolibre", "facebook", "telegram", "phone", "other"])
    handle = c[2].text_input("Identificador en el canal")
    kind = c[3].selectbox("Tipo", ["seller", "buyer"])
    opt = c[4].checkbox("Dio opt-in")
    if st.form_submit_button("Guardar", disabled=not has_role(p, "operator")) and alias:
        with db() as s:
            CrmService(s).upsert_contact(alias=alias, channel=channel, kind=kind, channel_handle=handle or None, opt_in_messaging=opt, by=p.username)
        st.rerun()
st.dataframe(df(contacts), width="stretch", hide_index=True)
