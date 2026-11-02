"""Trabajos autónomos del worker. Cada uno es idempotente, corto y registra `job_runs`.

Todos usan las REGLAS EFECTIVAS (YAML + Centro de Control) y consultan la puerta operativa antes de actuar:
un job que no puede correr queda `skipped` con el motivo (p. ej. "pausado: capital libre insuficiente") y vuelve
a correr solo cuando la condición cambia, salvo que el operador lo haya desactivado manualmente.

Jobs (en este orden):
- expire_approvals: vence solicitudes de aprobación viejas.
- telegram_inbox: lee /moto y fotos enviados al bot (importación asistida).
- radar_scan: analiza anuncios activos (detector de anomalías), crea oportunidades, alerta las extraordinarias.
- pricing_review: recomendaciones de precio para inventario publicado.
- stale_followups: avisa de conversaciones sin movimiento.
- outbound_dispatch: despacha notificaciones aprobadas (Telegram) con reintentos. Siempre último.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from mototrade.config import get_settings
from mototrade.control import ControlCenter, OperationsGate
from mototrade.core import approvals, kill_switch, outbound
from mototrade.core.money import fmt_ars
from mototrade.db.models import JobRun
from mototrade.integrations.telegram import Notifier, get_notifier
from mototrade.modules.crm import CrmService
from mototrade.modules.pricing import PricingService
from mototrade.modules.radar import RadarService

log = logging.getLogger(__name__)
JobFn = Callable[[Session, Notifier], dict]


class JobSkipped(Exception):
    """Lanzada por un job cuando la puerta operativa no permite correrlo. Se registra como 'skipped' con motivo."""


def _ctx(session: Session):
    cc = ControlCenter(session)
    settings = cc.load()
    rules = cc.effective_rules()
    gate = OperationsGate(session, settings)
    return cc, settings, rules, gate


def job_expire_approvals(session: Session, notifier: Notifier) -> dict:
    return {"expired": approvals.expire_stale(session)}


def job_telegram_inbox(session: Session, notifier: Notifier) -> dict:
    s = get_settings()
    if not s.telegram_enabled:
        raise JobSkipped("Telegram no configurado (sin token/chat_id)")
    from mototrade.integrations.telegram_inbox import TelegramInbox

    _, _, rules, _ = _ctx(session)
    inbox = TelegramInbox(s.telegram_bot_token.get_secret_value(), allowed_chat_id=s.telegram_chat_id)
    return inbox.process(session, rules).summary()


def job_radar_scan(session: Session, notifier: Notifier) -> dict:
    _, settings, rules, gate = _ctx(session)
    st = gate.state("search")
    if not st.effective:
        raise JobSkipped(f"búsqueda {st.label}")
    from mototrade.db.models import Deal

    radar = RadarService(session, rules, settings)
    existing = {d.id for d in session.query(Deal).all()}
    deals = radar.scan_opportunities()
    new = [d for d in deals if d.id not in existing]
    extraordinary = 0
    for d in new:
        l = d.listing
        tag = "🚨 EXTRAORDINARIA (verificar)" if d.opportunity_class == "extraordinary" else \
              ("⭐ interesante" if d.opportunity_class == "interesting" else "oportunidad")
        extraordinary += d.opportunity_class == "extraordinary"
        disc = f"{float(d.discount_pct):.0%} bajo comparables" if d.discount_pct is not None else "sin referencia"
        outbound.notify_operator(
            session, f"🏍️ {tag} #{d.id} (score {d.opportunity_score}): {l.make} {l.model} {l.year or ''} "
                     f"{fmt_ars(l.asking_price)} · {disc} · {l.location or 's/ubicación'} · modo {d.opportunity_mode} · "
                     f"máx. compra {fmt_ars(d.score_breakdown.get('max_purchase') or 0)} · riesgo {d.analysis.get('risk_level')}",
            key=f"deal:{d.id}:new")
    return {"scored": len(deals), "new_opportunities": len(new), "extraordinary_new": extraordinary,
            "capital": gate.position.as_dict()}


def job_pricing_review(session: Session, notifier: Notifier) -> dict:
    _, _, rules, gate = _ctx(session)
    if not gate.allows("seller_agent"):
        raise JobSkipped(f"vendedor {gate.state('seller_agent').label}")
    svc = PricingService(session, rules)
    n = 0
    for item in svc.items_for_review():
        rec = svc.recommend_and_request(item)
        if rec:
            n += 1
            outbound.notify_operator(session, f"💲 Recomendación de precio para {item.make} {item.model}: "
                                              f"{fmt_ars(rec.current_price)} → {fmt_ars(rec.recommended_price)} ({rec.reason})",
                                     key=f"pricing:{rec.id}")
    return {"recommendations": n}


def job_stale_followups(session: Session, notifier: Notifier) -> dict:
    crm = CrmService(session)
    stale = crm.stale_conversations(days=3)
    for conv in stale:
        outbound.notify_operator(session, f"⏰ Conversación #{conv.id} ({conv.direction}) sin movimiento hace 3+ días.",
                                 key=f"stale:{conv.id}:{datetime.now(timezone.utc).date()}")
    return {"stale": len(stale)}


def job_outbound_dispatch(session: Session, notifier: Notifier) -> dict:
    return outbound.execute_pending(session, notifier)


JOBS: dict[str, JobFn] = {
    "expire_approvals": job_expire_approvals,
    "telegram_inbox": job_telegram_inbox,
    "radar_scan": job_radar_scan,
    "pricing_review": job_pricing_review,
    "stale_followups": job_stale_followups,
    "outbound_dispatch": job_outbound_dispatch,
}


def run_job(session: Session, name: str, notifier: Notifier | None = None) -> JobRun:
    notifier = notifier or get_notifier()
    run = JobRun(job_name=name, status="running")
    session.add(run)
    session.flush()
    try:
        if name != "outbound_dispatch" and kill_switch.is_engaged(session):
            run.status, run.stats = "skipped", {"reason": "kill switch activo"}
        elif kill_switch.is_worker_paused(session):
            run.status, run.stats = "skipped", {"reason": "worker pausado"}
        else:
            run.stats = JOBS[name](session, notifier)
            run.status = "ok"
    except JobSkipped as exc:
        run.status, run.stats = "skipped", {"reason": str(exc)}
    except Exception as exc:  # el worker nunca muere por un job
        log.exception("job %s falló", name)
        run.status = "error"
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
    run.finished_at = datetime.now(timezone.utc)
    session.flush()
    return run


def run_all(session: Session, notifier: Notifier | None = None) -> list[JobRun]:
    return [run_job(session, name, notifier) for name in JOBS]
