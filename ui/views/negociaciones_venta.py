import streamlit as st
from sqlalchemy import select

from mototrade.db.models import Conversation, InventoryItem
from mototrade.modules.negotiation import NegotiationService
from mototrade.modules.negotiation.service import NegotiationError
from ui.common import db, flash, has_role, money, require_login, rules
from ui.components import conversation_panel

p = require_login()
R = rules()
st.title("💬 Negociaciones de venta")
st.caption("El agente vendedor atiende interesados y nunca acepta por debajo del mínimo autorizado; la venta la aprueba el admin.")

with db() as s:
    items = list(s.execute(select(InventoryItem).where(InventoryItem.status.in_(["ready", "published", "reserved"])).order_by(InventoryItem.id.desc())).scalars())
    opts = {i.id: f"#{i.id} {i.make} {i.model} {i.year or ''} · lista {money(i.list_price)} · mínimo {money(i.min_sale_price)} · {i.status}" for i in items}
if not opts:
    st.info("No hay motos listas para vender (estado ready/published). Fijá precios en Inventario.")
    st.stop()
item_id = st.selectbox("Moto", list(opts), format_func=lambda i: opts[i])
with db() as s:
    convs = [c.id for c in s.execute(select(Conversation).where(Conversation.inventory_item_id == item_id).order_by(Conversation.id.desc())).scalars()]
c1, c2 = st.columns([1, 3])
channel = c1.selectbox("Canal", ["manual", "whatsapp_manual", "mercadolibre_manual", "telegram"])
if c2.button("Abrir conversación con un interesado", disabled=not has_role(p, "operator")):
    with db() as s:
        try:
            NegotiationService(s, R).start_sell_conversation(s.get(InventoryItem, item_id), channel=channel, by=p.username)
        except NegotiationError as e:
            flash("error", str(e))
    st.rerun()
if convs:
    cid = st.selectbox("Conversación", convs, format_func=lambda i: f"#{i}")
    conversation_panel(cid, p)
