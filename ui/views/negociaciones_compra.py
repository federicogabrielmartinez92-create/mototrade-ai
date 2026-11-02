import streamlit as st
from sqlalchemy import select

from mototrade.core.money import D
from mototrade.db.models import Conversation, Deal
from mototrade.modules.documents import DocumentsService
from mototrade.modules.negotiation import NegotiationService
from mototrade.modules.negotiation.service import NegotiationError
from ui.common import db, flash, has_role, money, require_login, rules
from ui.components import conversation_panel

p = require_login()
R = rules()
st.title("🤝 Negociaciones de compra")
st.caption("El agente comprador propone; vos aprobás cada mensaje y el admin aprueba la compra. Nunca supera el límite autorizado.")

with db() as s:
    deals = list(s.execute(select(Deal).where(Deal.stage.notin_(["purchased", "lost", "discarded"])).order_by(Deal.id.desc())).scalars())
    opts = {d.id: f"#{d.id} {d.listing.make} {d.listing.model} {d.listing.year or ''} · pedido {money(d.listing.asking_price)} · {d.stage} · límite {money(d.authorized_max_price)}"
            for d in deals if d.listing}
if not opts:
    st.info("No hay deals abiertos.")
    st.stop()
deal_id = st.selectbox("Deal", list(opts), format_func=lambda i: opts[i])

with db() as s:
    deal = s.get(Deal, deal_id)
    last_val = deal.valuations[-1] if deal.valuations else None
    suggested = int(last_val.max_purchase_price) if last_val else 0
    convs = list(s.execute(select(Conversation).where(Conversation.deal_id == deal_id).order_by(Conversation.id.desc())).scalars())
    conv_ids = [c.id for c in convs]
    docs = DocumentsService(s)
    blockers = docs.blockers(deal_id=deal_id) if docs._rows(deal_id=deal_id) else None
    authorized = deal.authorized_max_price

with st.expander("1) Límite autorizado (solo admin)", expanded=authorized is None):
    if last_val:
        st.markdown(f"Tasador: compra máxima **{money(last_val.max_purchase_price)}**, oferta inicial {money(last_val.initial_offer)}, confianza `{last_val.confidence}`.")
    else:
        st.warning("Sin tasación: andá a Tasaciones primero.")
    lim = st.number_input("Precio máximo autorizado (ARS)", 0, 100_000_000, int(authorized or suggested), step=10_000)
    if st.button("Autorizar límite", disabled=not has_role(p, "admin")):
        with db() as s:
            NegotiationService(s, R).authorize_deal_limit(s.get(Deal, deal_id), max_price=D(lim), by=p.username)
        flash("success", "Límite autorizado.")
        st.rerun()

with st.expander("2) Documentación (bloquea la compra)", expanded=False):
    if blockers is None:
        if st.button("Crear checklist documental", disabled=not has_role(p, "operator")):
            with db() as s:
                DocumentsService(s).init_checklist(deal_id=deal_id, by=p.username)
            st.rerun()
    else:
        st.markdown("**Faltantes bloqueantes:** " + (", ".join(blockers) if blockers else "ninguno ✅"))
        with db() as s:
            rows = DocumentsService(s)._rows(deal_id=deal_id)
            for r in rows:
                c1, c2 = st.columns([3, 2])
                c1.markdown(f"{'🔴' if r.blocking and r.status not in ('verified_manual','not_applicable') else '🟢'} {r.label} — `{r.status}`")
                new = c2.selectbox("estado", ["pending", "provided", "verified_manual", "rejected", "not_applicable"],
                                   index=["pending", "provided", "verified_manual", "rejected", "not_applicable"].index(r.status),
                                   key=f"doc{r.id}", label_visibility="collapsed")
                if new != r.status and has_role(p, "operator"):
                    DocumentsService(s).set_status(r, new, by=p.username)
                    st.rerun()

st.subheader("3) Conversación")
if authorized is None:
    st.warning("Primero autorizá el límite.")
    st.stop()
c1, c2 = st.columns([1, 3])
channel = c1.selectbox("Canal", ["manual", "whatsapp_manual", "mercadolibre_manual", "telegram"])
if c2.button("Abrir nueva conversación de compra", disabled=not has_role(p, "operator")):
    with db() as s:
        try:
            NegotiationService(s, R).start_buy_conversation(s.get(Deal, deal_id), channel=channel, by=p.username)
        except NegotiationError as e:
            flash("error", str(e))
    st.rerun()
if conv_ids:
    cid = st.selectbox("Conversación", conv_ids, format_func=lambda i: f"#{i}")
    conversation_panel(cid, p)
