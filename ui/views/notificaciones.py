import streamlit as st

from mototrade.core import notifications as notif
from ui.common import db, flash, require_login

p = require_login()
st.title("🔔 Notificaciones")
st.caption("Canal principal del sistema: oportunidades, precios, seguimientos y avisos. Telegram es opcional y solo recibe una copia.")

c1, c2, c3 = st.columns([1, 1, 2])
unread_only = c1.toggle("Solo no leídas", True)
category = c2.selectbox("Categoría", ["todas", "opportunity", "pricing", "crm", "approval", "system", "general"])
if c3.button("Marcar todas como leídas"):
    with db() as s:
        n = notif.mark_all_read(s, by=p.username)
    flash("success", f"{n} notificación(es) marcadas como leídas.")
    st.rerun()

with db() as s:
    rows = [{"id": r.id, "created": r.created_at, "level": r.level, "category": r.category, "title": r.title, "body": r.body,
             "read": r.read_at is not None, "entity": f"{r.entity_type}:{r.entity_id}" if r.entity_type else ""}
            for r in notif.list_notifications(s, unread_only=unread_only, category=None if category == "todas" else category)]
if not rows:
    st.success("Sin notificaciones pendientes.")
ICON = {"alert": "🚨", "warning": "⚠️", "info": "ℹ️"}
for r in rows:
    with st.container(border=True):
        a, b = st.columns([6, 1])
        a.markdown(f"{ICON.get(r['level'], 'ℹ️')} **{r['title']}**  \n{r['body']}")
        a.caption(f"{r['created']:%d/%m %H:%M} · `{r['category']}` · {r['entity']}" + (" · leída" if r["read"] else ""))
        if not r["read"] and b.button("Leída", key=f"rd{r['id']}"):
            with db() as s:
                notif.mark_read(s, [r["id"]], by=p.username)
            st.rerun()
