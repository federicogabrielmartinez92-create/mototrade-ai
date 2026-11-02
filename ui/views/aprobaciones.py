import streamlit as st

from mototrade.core import approvals
from mototrade.db.models import Message
from mototrade.modules.negotiation import NegotiationService
from ui.common import db, flash, money, require_login, rules

p = require_login()
st.title("✅ Aprobaciones")
st.caption("Nada con efecto externo o financiero ocurre sin una decisión tuya acá. Los roles mínimos: mensajes/publicaciones/precios → operador; compras/ventas/pagos → admin.")

with db() as s:
    pend = approvals.pending(s)
    rows = [{"id": a.id, "kind": a.kind, "summary": a.summary, "amount": a.amount, "payload": a.payload,
             "requested_by": a.requested_by, "created_at": a.created_at, "expires_at": a.expires_at,
             "needed": approvals.REQUIRED_ROLE.get(a.kind, "admin")} for a in pend]

if not rows:
    st.success("No hay solicitudes pendientes.")
    st.stop()

for r in rows:
    with st.container(border=True):
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"**#{r['id']} · `{r['kind']}`** — {r['summary']}")
        c1.caption(f"Pedido por `{r['requested_by']}` · {r['created_at']:%d/%m %H:%M} · vence {r['expires_at']:%d/%m %H:%M} · rol mínimo `{r['needed']}`")
        if r["amount"] is not None:
            c2.metric("Importe", money(r["amount"]))
        if r["kind"] == "send_message":
            st.text_area("Borrador", r["payload"].get("text", ""), height=110, key=f"txt{r['id']}", disabled=True)
        else:
            st.json(r["payload"], expanded=False)
        note = st.text_input("Nota (opcional)", key=f"note{r['id']}")
        b1, b2, _ = st.columns([1, 1, 4])
        if b1.button("Aprobar", key=f"ok{r['id']}", type="primary"):
            try:
                with db() as s:
                    row = approvals.decide(s, r["id"], approve=True, by=p.username, role=p.role, note=note)
                    if row.entity_type == "message":
                        msg = s.get(Message, int(row.entity_id))
                        if msg:
                            NegotiationService(s, rules()).apply_approval(msg)
                flash("success", f"Solicitud #{r['id']} aprobada.")
            except approvals.ApprovalError as e:
                flash("error", str(e))
            st.rerun()
        if b2.button("Rechazar", key=f"no{r['id']}"):
            try:
                with db() as s:
                    row = approvals.decide(s, r["id"], approve=False, by=p.username, role=p.role, note=note)
                    if row.entity_type == "message":
                        msg = s.get(Message, int(row.entity_id))
                        if msg:
                            NegotiationService(s, rules()).apply_approval(msg)
                flash("warning", f"Solicitud #{r['id']} rechazada.")
            except approvals.ApprovalError as e:
                flash("error", str(e))
            st.rerun()
