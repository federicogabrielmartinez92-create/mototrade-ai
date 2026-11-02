"""Componentes reutilizables: bandeja de conversación con agente, aprobación y envío manual."""
from __future__ import annotations

import streamlit as st

from mototrade.core import approvals
from mototrade.db.models import Conversation, Message
from mototrade.integrations.llm import get_llm
from mototrade.modules.negotiation import NegotiationService
from mototrade.modules.negotiation.service import NegotiationError
from ui.common import db, flash, has_role, money, rules, settings


def conversation_panel(conv_id: int, principal) -> None:
    R = rules()
    with db() as s:
        conv = s.get(Conversation, conv_id)
        svc = NegotiationService(s, R)
        msgs = svc.messages(conv)
        rows = [{"id": m.id, "sender": m.sender, "status": m.status, "text": m.text, "amount": m.proposed_amount,
                 "intent": m.intent, "approval_id": m.approval_id, "flags": m.injection_flags, "created": m.created_at,
                 "meta": m.llm_meta, "policy": m.policy_check} for m in msgs]
        header = {"status": conv.status, "rounds": conv.rounds, "agent": conv.agent_enabled, "channel": conv.channel,
                  "ours": conv.last_agent_offer, "theirs": conv.last_counterpart_offer, "direction": conv.direction}

    c = st.columns(5)
    c[0].metric("Estado", header["status"])
    c[1].metric("Rondas", header["rounds"])
    c[2].metric("Nuestra última", money(header["ours"]))
    c[3].metric("Última de ellos", money(header["theirs"]))
    c[4].metric("Agente", "activo" if header["agent"] else "humano al mando")
    llm_mode = "IA real (Anthropic)" if settings().llm_enabled else "proveedor FAKE (plantillas)"
    st.caption(f"Canal `{header['channel']}` · redacción: {llm_mode} · los montos los decide la estrategia determinista dentro de tus límites.")

    for m in rows:
        role = "user" if m["sender"] == "counterpart" else "assistant"
        with st.chat_message(role, avatar="🧑" if role == "user" else ("🤖" if m["sender"] == "agent" else "👤")):
            badge = {"draft": "📝 borrador", "approved": "✅ aprobado, pendiente de envío", "sent": "📤 enviado",
                     "received": "📥 recibido", "rejected": "❌ rechazado", "blocked": "⛔ bloqueado"}[m["status"]]
            st.markdown(f"**{m['sender']}** · {badge}" + (f" · {money(m['amount'])}" if m["amount"] is not None else "")
                        + (f" · intención `{m['intent']}`" if m["intent"] else ""))
            st.write(m["text"])
            if m["flags"]:
                st.warning(f"⚠️ Señales de instrucción maliciosa: {', '.join(m['flags'])}")
            if m["meta"].get("simulated"):
                st.caption("Redactado por proveedor simulado.")
            if m["status"] == "draft" and m["approval_id"] and has_role(principal, "operator"):
                b1, b2, _ = st.columns([1, 1, 4])
                if b1.button("Aprobar borrador", key=f"ap{m['id']}", type="primary"):
                    with db() as s:
                        try:
                            approvals.decide(s, m["approval_id"], approve=True, by=principal.username, role=principal.role)
                            NegotiationService(s, R).apply_approval(s.get(Message, m["id"]))
                            flash("success", "Borrador aprobado. Envialo por el canal y marcálo como enviado.")
                        except approvals.ApprovalError as e:
                            flash("error", str(e))
                    st.rerun()
                if b2.button("Rechazar", key=f"rj{m['id']}"):
                    with db() as s:
                        try:
                            approvals.decide(s, m["approval_id"], approve=False, by=principal.username, role=principal.role)
                            NegotiationService(s, R).apply_approval(s.get(Message, m["id"]))
                        except approvals.ApprovalError as e:
                            flash("error", str(e))
                    st.rerun()
            if m["status"] == "approved" and has_role(principal, "operator"):
                st.code(m["text"], language=None)
                if st.button("Ya lo envié por el canal → marcar enviado", key=f"snd{m['id']}"):
                    with db() as s:
                        try:
                            NegotiationService(s, R).mark_sent(s.get(Message, m["id"]), by=principal.username)
                            flash("success", "Mensaje marcado como enviado.")
                        except NegotiationError as e:
                            flash("error", str(e))
                    st.rerun()

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        with st.form(f"in{conv_id}", clear_on_submit=True):
            txt = st.text_area("Pegar mensaje recibido de la contraparte")
            if st.form_submit_button("Registrar entrante", disabled=not has_role(principal, "operator")) and txt.strip():
                with db() as s:
                    NegotiationService(s, R).record_incoming(s.get(Conversation, conv_id), txt)
                st.rerun()
    with col2:
        with st.form(f"op{conv_id}", clear_on_submit=True):
            txt = st.text_area("Escribir yo mismo (se registra como enviado)")
            if st.form_submit_button("Registrar mi mensaje", disabled=not has_role(principal, "operator")) and txt.strip():
                with db() as s:
                    NegotiationService(s, R).record_operator_message(s.get(Conversation, conv_id), txt, by=principal.username)
                st.rerun()
    b1, b2, b3 = st.columns(3)
    if b1.button("🤖 Pedir propuesta al agente", key=f"step{conv_id}", type="primary", disabled=not has_role(principal, "operator")):
        with db() as s:
            try:
                r = NegotiationService(s, R, get_llm()).agent_step(s.get(Conversation, conv_id), by=f"agent:{principal.username}")
                if r.blocked:
                    flash("warning", f"El agente no generó borrador: {r.decision.rationale} · " + " ".join(r.verdict["reasons"]))
                else:
                    flash("success", f"Borrador generado ({r.decision.intent}). Revisalo y aprobalo.")
            except NegotiationError as e:
                flash("error", str(e))
        st.rerun()
    if header["agent"]:
        if b2.button("👤 Tomar la conversación (desactivar agente)", key=f"take{conv_id}"):
            with db() as s:
                NegotiationService(s, R).human_takeover(s.get(Conversation, conv_id), by=principal.username)
            st.rerun()
    else:
        if b2.button("🤖 Devolver al agente", key=f"give{conv_id}"):
            with db() as s:
                NegotiationService(s, R).human_takeover(s.get(Conversation, conv_id), by=principal.username, enable_agent=True)
            st.rerun()
    if b3.button("Cerrar como perdida", key=f"lost{conv_id}"):
        with db() as s:
            s.get(Conversation, conv_id).status = "closed_lost"
        st.rerun()
