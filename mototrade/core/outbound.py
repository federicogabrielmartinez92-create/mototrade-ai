"""Ejecutor de acciones salientes: idempotente, auditado, con reintentos seguros.

Una `OutboundAction` solo se ejecuta si:
  1. el kill switch NO está activo,
  2. tiene aprobación aprobada (o es una notificación interna al operador),
  3. el canal está autorizado e implementado,
  4. no fue ejecutada antes (idempotency_key único + estado).

Canales implementados: `telegram` (notificaciones al operador). `manual` significa
que el operador envía a mano y registra. WhatsApp / Mercado Libre: NO implementados;
cualquier acción en esos canales queda `blocked` con el motivo.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from mototrade.core import approvals, audit, kill_switch
from mototrade.core.enums import OutboundStatus
from mototrade.db.models import OutboundAction
from mototrade.integrations.telegram import Notifier

IMPLEMENTED_CHANNELS = {"telegram", "manual"}
MAX_ATTEMPTS = 5


def enqueue(session: Session, *, kind: str, channel: str, payload: dict, idempotency_key: str,
            approval_id: int | None = None) -> OutboundAction:
    existing = session.execute(select(OutboundAction).where(OutboundAction.idempotency_key == idempotency_key)).scalar_one_or_none()
    if existing:
        return existing
    row = OutboundAction(kind=kind, channel=channel, payload=payload, idempotency_key=idempotency_key,
                         approval_id=approval_id, status=OutboundStatus.QUEUED)
    session.add(row)
    session.flush()
    audit.record(session, action="outbound.enqueued", entity_type="outbound_action", entity_id=row.id,
                 details={"kind": kind, "channel": channel, "approval_id": approval_id})
    return row


def execute_pending(session: Session, notifier: Notifier, *, by: str = "worker") -> dict:
    stats = {"sent": 0, "blocked": 0, "failed": 0, "skipped": 0}
    rows = list(session.execute(
        select(OutboundAction).where(OutboundAction.status.in_([OutboundStatus.QUEUED, OutboundStatus.FAILED]))
        .order_by(OutboundAction.id)
    ).scalars())
    for row in rows:
        if row.attempts >= MAX_ATTEMPTS:
            stats["skipped"] += 1
            continue
        if kill_switch.is_engaged(session):
            _block(session, row, "kill switch activo")
            stats["blocked"] += 1
            continue
        if row.channel not in IMPLEMENTED_CHANNELS:
            _block(session, row, f"canal '{row.channel}' no implementado/autorizado")
            stats["blocked"] += 1
            continue
        needs_approval = row.kind != "notify_operator"
        if needs_approval and not approvals.is_approved(session, row.approval_id):
            stats["skipped"] += 1
            continue
        if row.channel == "manual":
            stats["skipped"] += 1  # lo envía el humano; se marca desde el panel
            continue
        row.attempts += 1
        res = notifier.send(row.payload.get("text", ""))
        if res.ok:
            row.status = OutboundStatus.SENT
            row.executed_at = datetime.now(timezone.utc)
            row.external_ref = res.external_ref
            stats["sent"] += 1
            audit.record(session, action="outbound.sent", entity_type="outbound_action", entity_id=row.id,
                         details={"channel": row.channel, "ref": res.external_ref, "simulated": res.simulated}, actor_id=by)
        else:
            row.status = OutboundStatus.FAILED
            row.last_error = res.error
            stats["failed"] += 1
            audit.record(session, action="outbound.failed", entity_type="outbound_action", entity_id=row.id,
                         details={"error": res.error, "attempt": row.attempts}, actor_id=by)
    return stats


def _block(session: Session, row: OutboundAction, reason: str) -> None:
    row.status = OutboundStatus.BLOCKED
    row.last_error = reason
    audit.record(session, action="outbound.blocked", entity_type="outbound_action", entity_id=row.id, details={"reason": reason})


def notify_operator(session: Session, text: str, *, key: str) -> OutboundAction:
    """Atajo: encola una notificación interna (no requiere aprobación; no va a terceros)."""
    return enqueue(session, kind="notify_operator", channel="telegram", payload={"text": text}, idempotency_key=f"notify:{key}")
