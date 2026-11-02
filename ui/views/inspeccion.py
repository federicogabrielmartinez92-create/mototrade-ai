import streamlit as st
from sqlalchemy import select

from mototrade.db.models import Deal, Inspection, InspectionFinding, Listing
from mototrade.modules.inspection import InspectionBudgetExceeded, InspectionService
from mototrade.modules.inspection.service import get_inspector
from mototrade.modules.photos import PhotoError, PhotoService
from ui.common import db, df, flash, gate_snapshot, has_role, require_login, settings, simulated_banner

p = require_login()
st.title("🔍 Inspección visual")
if settings().llm_enabled:
    st.success("Inspector con IA real (Anthropic, visión) habilitado. Igual: todo hallazgo requiere confirmación humana.")
else:
    simulated_banner("Sin API key de IA: el inspector es SIMULADO (solo lee palabras clave de la descripción; no analiza fotos).")

with db() as s:
    listings = list(s.execute(select(Listing).where(Listing.status.in_(["new", "watching"])).order_by(Listing.id.desc())).scalars())
    opts = {l.id: f"#{l.id} {l.make} {l.model} {l.year or ''} ({len(l.photos)} fotos)" for l in listings}
if not opts:
    st.info("No hay anuncios activos.")
    st.stop()
lid = st.selectbox("Anuncio", list(opts), format_func=lambda i: opts[i])
with db() as s:
    l = s.get(Listing, lid)
    desc, moto = l.description, f"{l.make} {l.model} {l.year or ''}"
    deal = s.execute(select(Deal).where(Deal.listing_id == lid)).scalars().first()
    deal_id = deal.id if deal else None
st.text_area("Descripción del anuncio (texto de terceros, NO confiable)", desc, height=100, disabled=True)
with db() as s:
    ps = PhotoService(s)
    stored = ps.for_entity("listing", lid)
    thumbs = []
    for ph in stored[:8]:
        try:
            thumbs.append((ph.id, ps.read(ph)))
        except Exception:
            pass
if thumbs:
    cols = st.columns(min(4, len(thumbs)))
    for i, (pid, data) in enumerate(thumbs):
        cols[i % len(cols)].image(data, caption=f"foto #{pid}", width="stretch")
files = st.file_uploader("Cargar fotos reales del anuncio", accept_multiple_files=True, type=["jpg", "jpeg", "png", "webp"])
if files and st.button("Subir fotos", disabled=not has_role(p, "operator")):
    with db() as s:
        for f in files:
            try:
                PhotoService(s).store(entity_type="listing", entity_id=lid, data=f.getvalue(), filename=f.name, by=p.username)
            except PhotoError as e:
                flash("error", f"{f.name}: {e}")
    st.rerun()
snap, _, ai = gate_snapshot()
vis_ok = snap["visual_inspection"][1]
if not vis_ok:
    st.warning(f"Inspección visual: {snap['visual_inspection'][2]}")
st.caption(f"IA usada este mes: US$ {float(ai['spent_month_usd']):.3f} · análisis visuales: {ai['visual_this_month']}. "
           "Misma foto + misma descripción = resultado en caché (sin costo).")
if st.button("Ejecutar inspección", type="primary", disabled=not has_role(p, "operator") or (not vis_ok and settings().llm_enabled)):
    with db() as s:
        try:
            InspectionService(s, get_inspector()).run(entity_type="listing", entity_id=lid, description=desc, moto=moto,
                                                      by=f"inspector:{p.username}", deal_id=deal_id)
        except InspectionBudgetExceeded as e:
            flash("error", f"Presupuesto de IA: {e}")
    st.rerun()

with db() as s:
    insps = list(s.execute(select(Inspection).where(Inspection.entity_type == "listing", Inspection.entity_id == lid).order_by(Inspection.id.desc())).scalars())
    data = [(i.id, i.summary, i.is_simulated, i.provider, [(f.id, f.kind, f.area, f.description, f.severity, f.requires_human_check) for f in i.findings],
             i.requested_photos, len(i.photo_ids or [])) for i in insps]
for iid, summary, sim, provider, findings, req_photos, n_ph in data:
    with st.container(border=True):
        st.markdown(f"**Inspección #{iid}** · proveedor `{provider}` · {n_ph} foto(s) {'· 🧪 simulada' if sim else ''}")
        st.write(summary)
        if req_photos:
            st.caption("Fotos que faltan pedir: " + ", ".join(req_photos))
        for fid, kind, area, d, sev, needs in findings:
            icon = "👁️" if kind == "observation" else "❓"
            c1, c2 = st.columns([5, 1])
            c1.markdown(f"{icon} **{area}** · `{kind}` · severidad `{sev}` — {d}")
            if needs:
                b1, b2 = c2.columns(2)
                if b1.button("✔", key=f"cf{fid}", help="Confirmar (crea/activa reparación)", disabled=not has_role(p, "operator")):
                    with db() as s:
                        InspectionService(s).confirm_finding(s.get(InspectionFinding, fid), by=p.username, deal_id=deal_id)
                    st.rerun()
                if b2.button("✖", key=f"dc{fid}", help="Descartar hallazgo", disabled=not has_role(p, "operator")):
                    with db() as s:
                        InspectionService(s).discard_finding(s.get(InspectionFinding, fid), by=p.username)
                    st.rerun()
