from decimal import Decimal

import streamlit as st
from sqlalchemy import select

from mototrade.core.money import D
from mototrade.db.models import Deal, Listing
from mototrade.modules.photos import PhotoError, PhotoService
from mototrade.modules.radar import CsvSource, ManualSource, RadarService, RawListing
from ui.common import control_settings, db, df, flash, gate_snapshot, has_role, money, pct, require_login, rules, simulated_banner

p = require_login()
R = rules()
CS = control_settings()
st.title("📡 Radar de oportunidades")
snap, pos, _ = gate_snapshot()
intended, effective, label, reason = snap["search"]
if effective:
    st.success(f"Búsqueda automática: activa · capital libre {money(pos['free_capital'])} · motos {pos['units_active']}/{pos['max_units']}")
else:
    st.warning(f"Búsqueda automática: {label}. Podés analizar a mano igual; la compra queda limitada por capital/inventario.")
simulated_banner("Sin fuentes automáticas autorizadas (ML cerrado a terceros; Marketplace/RosarioGarage sin API). "
                 "Carga asistida: formulario, CSV o Telegram (/moto). Ver docs/INTEGRACIONES.md.")

tab_opp, tab_new, tab_csv, tab_list = st.tabs(["🎯 Oportunidades", "➕ Cargar anuncio", "📄 Importar CSV", "📋 Anuncios"])

CLASS_LABEL = {"extraordinary": "🚨 EXTRAORDINARIA", "interesting": "⭐ Interesante", "ordinary": "Ordinaria", "insufficient": "❔ Tasación insuficiente"}
RISK_ICON = {"low": "🟢", "medium": "🟡", "high": "🔴"}

with tab_opp:
    c1, c2, c3 = st.columns([1, 1, 2])
    if c1.button("🔎 Analizar anuncios ahora", type="primary", disabled=not has_role(p, "operator")):
        with db() as s:
            deals = RadarService(s, R, CS).scan_opportunities(by=p.username)
        flash("success", f"{len(deals)} oportunidad(es) activas tras el análisis.")
        st.rerun()
    sort_by = c2.selectbox("Ordenar por", ["puntaje", "descuento", "ganancia", "rentabilidad", "riesgo", "confianza", "capital requerido"])
    with c3:
        f1, f2, f3 = st.columns(3)
        f_class = f1.multiselect("Clase", list(CLASS_LABEL), default=["extraordinary", "interesting", "ordinary"])
        f_risk = f2.multiselect("Riesgo", ["low", "medium", "high"], default=["low", "medium", "high"])
        f_mode = f3.multiselect("Modo", ["published", "negotiable", "none"], default=["published", "negotiable"])

    with db() as s:
        radar = RadarService(s, R, CS)
        deals = list(s.execute(select(Deal).where(Deal.stage.notin_(["purchased", "lost", "discarded"]))).scalars())
        cards = []
        for d in deals:
            if not d.listing or not d.analysis:
                continue
            a = d.analysis
            at = a.get("at_target") or a.get("at_asking") or {}
            photos = PhotoService(s).for_entity("listing", d.listing.id)
            thumb = None
            if photos:
                try:
                    thumb = PhotoService(s).read(photos[0])
                except Exception:
                    thumb = None
            elif d.listing.photos and str(d.listing.photos[0]).startswith("http"):
                thumb = d.listing.photos[0]
            cards.append({
                "deal": d.id, "listing": d.listing.id, "stage": d.stage, "class": a.get("classification"), "mode": a.get("mode"),
                "risk": a.get("risk_level"), "risk_flags": a.get("risk_flags", []), "score": d.opportunity_score or 0,
                "moto": f"{d.listing.make} {d.listing.model} {d.listing.version or ''} {d.listing.year or ''}".strip(),
                "km": d.listing.kilometers, "asking": D(d.listing.asking_price), "ref": a.get("reference_asking"),
                "discount": D(a["discount_pct"]) if a.get("discount_pct") else None,
                "market": a["market"]["price"], "confidence": a["market"]["confidence"], "conf_score": a["market"]["confidence_score"],
                "quick": at.get("quick_sale_price"), "initial_offer": at.get("initial_offer"), "max": at.get("max_purchase_price"),
                "target": a.get("target_price"), "costs": at.get("total_costs"), "profit": at.get("expected_profit"),
                "roi": at.get("expected_roi"), "action": a.get("recommended_action"), "source": d.listing.source,
                "url": d.listing.source_url, "location": d.listing.location, "thumb": thumb, "notes": a.get("notes", []),
                "capital_req": at.get("total_investment") or (str(D(d.listing.asking_price) + D(at.get("total_costs") or 0))),
                "authorized": d.authorized_max_price, "scenarios": a.get("scenarios", {}),
            })
    cards = [c for c in cards if c["class"] in f_class and c["risk"] in f_risk and (c["mode"] in f_mode or c["class"] == "insufficient")]
    keyf = {"puntaje": lambda c: c["score"], "descuento": lambda c: c["discount"] or Decimal(-1),
            "ganancia": lambda c: Decimal(c["profit"] or 0), "rentabilidad": lambda c: Decimal(c["roi"] or 0),
            "riesgo": lambda c: {"low": 3, "medium": 2, "high": 1}.get(c["risk"], 0),
            "confianza": lambda c: Decimal(c["conf_score"] or 0), "capital requerido": lambda c: -Decimal(c["capital_req"] or 0)}[sort_by]
    cards.sort(key=keyf, reverse=True)
    extra = [c for c in cards if c["class"] == "extraordinary"]
    if extra:
        st.error(f"🚨 {len(extra)} oportunidad(es) EXTRAORDINARIA(s): un descuento tan grande también puede ser estafa, daño o error. "
                 "Verificar documentación y mecánica antes de cualquier oferta.")
    if not cards:
        st.info("No hay oportunidades con esos filtros. Cargá anuncios y comparables, y analizá.")
    for c in cards:
        with st.container(border=True):
            col_img, col_main, col_num = st.columns([1, 2, 2])
            with col_img:
                if c["thumb"] is not None:
                    st.image(c["thumb"], width="stretch")
                else:
                    st.caption("sin foto")
                st.markdown(f"**{CLASS_LABEL.get(c['class'], c['class'])}**")
                st.markdown(f"{RISK_ICON.get(c['risk'], '⚪')} riesgo `{c['risk']}` · score **{c['score']}**")
            with col_main:
                st.markdown(f"### {c['moto']}")
                st.markdown(f"{c['km']:,} km · {c['location'] or 's/ubicación'} · fuente `{c['source']}`".replace(",", ".") if c["km"] is not None
                            else f"km s/d · {c['location'] or 's/ubicación'} · fuente `{c['source']}`")
                if c["url"]:
                    st.markdown(f"[Ver anuncio original]({c['url']})")
                st.markdown(f"**Pedido:** {money(c['asking'])} · **Comparable:** {money(c['ref'])} · "
                            f"**Descuento:** {pct(c['discount'])}")
                st.markdown(f"Modo `{c['mode']}` · acción sugerida `{c['action']}` · etapa `{c['stage']}`"
                            + (f" · límite autorizado {money(c['authorized'])}" if c["authorized"] else ""))
                if c["risk_flags"]:
                    st.caption("Riesgos: " + ", ".join(c["risk_flags"]))
                if c["notes"]:
                    st.caption(" · ".join(c["notes"])[:300])
            with col_num:
                m = st.columns(3)
                m[0].metric("Venta rápida", money(c["quick"]))
                m[1].metric("Oferta inicial", money(c["initial_offer"]))
                m[2].metric("Compra máxima", money(c["max"]))
                m = st.columns(3)
                m[0].metric("Objetivo", money(c["target"]))
                m[1].metric("Gastos", money(c["costs"]))
                m[2].metric("Ganancia", money(c["profit"]))
                m = st.columns(3)
                m[0].metric("ROI", pct(c["roi"]))
                m[1].metric("Confianza", f"{c['confidence']} ({float(c['conf_score']):.2f})")
                m[2].metric("Capital req.", money(c["capital_req"]))
                if c["scenarios"]:
                    st.caption("Escenarios al objetivo: " + " · ".join(f"{k}: {money(v['profit'])}" for k, v in c["scenarios"].items()))
            st.caption("Ninguna oportunidad es 'compra recomendada' hasta verificar documentación y mecánica (Negociaciones de compra → checklist).")

with tab_new:
    with st.form("new_listing", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns(4)
        source = c1.selectbox("Fuente", ["manual", "mercadolibre", "facebook_marketplace", "rosariogarage", "other"])
        make = c2.text_input("Marca", "Honda")
        model = c3.text_input("Modelo", "Wave 110")
        version = c4.text_input("Versión (opcional)")
        c1, c2, c3, c4 = st.columns(4)
        year = c1.number_input("Año", 1990, 2030, 2022)
        km = c2.number_input("Kilómetros", 0, 500000, 15000, step=500)
        price = c3.number_input("Precio pedido (ARS)", 0, 100_000_000, 2_400_000, step=10_000)
        seller = c4.selectbox("Vendedor", ["unknown", "particular", "dealer"])
        c1, c2, c3 = st.columns(3)
        location = c1.text_input("Ubicación", "Rosario")
        url = c2.text_input("URL del anuncio (opcional)")
        cond = c3.selectbox("Estado declarado", ["", "excelente", "bueno", "regular", "malo"])
        ext = st.text_input("ID externo (opcional, p. ej. MLA123)")
        desc = st.text_area("Descripción del anuncio (se trata como texto NO confiable)")
        photos = st.text_input("URLs de fotos separadas por |")
        files = st.file_uploader("Fotos (JPEG/PNG/WebP)", accept_multiple_files=True, type=["jpg", "jpeg", "png", "webp"])
        if st.form_submit_button("Guardar anuncio", type="primary", disabled=not has_role(p, "operator")):
            raw = RawListing(source=source, make=make, model=model, version=version, year=int(year), kilometers=int(km), asking_price=D(price),
                             location=location or None, source_url=url or None, external_id=ext or None, description=desc,
                             photos=[x.strip() for x in photos.split("|") if x.strip()], seller_type=seller, condition_declared=cond or None)
            with db() as s:
                res = RadarService(s, R, CS).ingest(ManualSource([raw]), by=p.username)
                lid = (res.created or res.updated or [None])[0]
                n_ph = 0
                if lid and files:
                    ps = PhotoService(s)
                    for f in files:
                        try:
                            ps.store(entity_type="listing", entity_id=lid, data=f.getvalue(), filename=f.name, by=p.username)
                            n_ph += 1
                        except PhotoError as e:
                            st.error(f"{f.name}: {e}")
            if res.errors:
                st.error(res.errors)
            else:
                st.success(f"Creados {len(res.created)}, actualizados {len(res.updated)}, cambios de precio {len(res.price_changes)}, "
                           f"duplicados {len(res.duplicates)}, sospechosos {len(res.flagged)}, fotos {n_ph}")

with tab_csv:
    st.caption("Columnas: make,model,year,kilometers,asking_price[,version,location,url,external_id,title,description,seller_alias,photos,seller_type,condition,published_at]")
    up = st.file_uploader("CSV", type=["csv"])
    label = st.text_input("Etiqueta de fuente", "csv_import")
    if up and st.button("Importar", disabled=not has_role(p, "operator")):
        try:
            with db() as s:
                res = RadarService(s, R, CS).ingest(CsvSource(up.getvalue(), label), by=p.username)
            st.success(res.summary())
            if res.errors:
                st.warning(res.errors)
        except ValueError as e:
            st.error(str(e))

with tab_list:
    with db() as s:
        rows = [{"id": l.id, "estado": l.status, "fuente": l.source, "moto": f"{l.make} {l.model} {l.year or ''}", "km": l.kilometers,
                 "precio": money(l.asking_price), "vendedor": l.seller_type, "ubicación": l.location, "visto": l.last_seen_at,
                 "flags": ", ".join(l.injection_flags), "url": l.source_url}
                for l in s.execute(select(Listing).order_by(Listing.id.desc()).limit(300)).scalars()]
    if rows:
        st.dataframe(df(rows), width="stretch", hide_index=True)
        lid = st.selectbox("Anuncio", [r["id"] for r in rows])
        with db() as s:
            l = s.get(Listing, lid)
            hist = [{"fecha": h.observed_at, "precio": money(h.price)} for h in l.price_history]
            n_photos = len(PhotoService(s).for_entity("listing", lid))
            if l.status in ("new", "watching") and st.button("Descartar anuncio", disabled=not has_role(p, "operator")):
                l.status = "discarded"
                flash("info", "Anuncio descartado.")
                st.rerun()
        st.dataframe(df(hist), width="stretch", hide_index=True)
        up_files = st.file_uploader(f"Agregar fotos al anuncio #{lid} ({n_photos} cargadas)", accept_multiple_files=True,
                                    type=["jpg", "jpeg", "png", "webp"], key="up_list")
        if up_files and st.button("Subir fotos", disabled=not has_role(p, "operator")):
            with db() as s:
                ps = PhotoService(s)
                for f in up_files:
                    try:
                        ps.store(entity_type="listing", entity_id=lid, data=f.getvalue(), filename=f.name, by=p.username)
                    except PhotoError as e:
                        flash("error", f"{f.name}: {e}")
            st.rerun()
    else:
        st.info("Todavía no hay anuncios.")
