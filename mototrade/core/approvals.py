"""Cola de aprobaciones humanas.

Regla de oro: ningún agente paga, recibe dinero, reserva, acepta una compra ni
cierra una venta. Lo único que puede hacer es CREAR una solicitud acá. Un humano
con rol suficiente la aprueba o rechaza; recién entonces un ejecutor idempotente
realiza la acción (si el canal está autorizado).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from mototrade.core import audit
from mototrade.core.enums import ActorType, ApprovalKind, ApprovalStatus, UserRole
from mototrade.db.models import ApprovalRequest

# Qué rol mínimo puede aprobar cada tipo de solicitud.
REQUIRED_ROLE: dict[str, str] = {
    ApprovalKind.SEND_MESSAGE: UserRole.OPERATOR,
    ApprovalKind.MAKE_OFFER: UserRole.OPERATOR,
    ApprovalKind.PUBLISH_LISTING: UserRole.OPERATOR,
    ApprovalKind.PRICE_CHANGE: UserRole.OPERATOR,
    ApprovalKind.ACCEPT_PURCHASE: UserRole.ADMIN,
    ApprovalKind.ACCEPT_SALE: UserRole.ADMIN,
    ApprovalKind.PAYMENT: UserRole.ADMIN,
    ApprovalKind.RESERVATION: UserRole.ADMIN,
}
_ROLE_RANK = {UserRole.VIEWER: 0, UserRole.OPERATOR: 1, UserRole.ADMIN: 2}


class ApprovalError(RuntimeError):
    pass


def make_idempotency_key(kind: str, entity_type: str, entity_id, payload: dict) -> str:
    body = json.dumps({"k": kind, "t": entity_type, "i": str(entity_id), "p": payload},
                      sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:64]


def request(
    session: Session,
    *,
    kind: ApprovalKind | str,
    entity_type: str,
    entity_id: int | str | None,
    payload: dict,
    summary: str,
    requested_by: str,
    amount: Decimal | None = None,
    ttl_hours: int = 72,
    rules_hash: str | None = None,
    actor_type: ActorType = ActorType.AGENT,
) -> ApprovalRequest:
    """Crea (o devuelve, si ya existe) una solicitud de aprobación. Idempotente por contenido."""
    key = make_idempotency_key(str(kind), entity_type, entity_id, payload)
    existing = session.execute(
        select(ApprovalRequest).where(ApprovalRequest.idempotency_key == key)
    ).scalar_one_or_none()
    if existing:
        return existing
    row = ApprovalRequest(
        kind=str(kind),
        entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        payload=payload,
        summary=summary,
        amount=amount,
        requested_by=requested_by,
        status=ApprovalStatus.PENDING,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
        idempotency_key=key,
        rules_hash=rules_hash,
    )
    session.add(row)
    session.flush()
    audit.record(session, action="approval.requested", entity_type="approval_request", entity_id=row.id,
                 details={"kind": str(kind), "summary": summary, "amount": amount, "target": f"{entity_type}:{entity_id}"},
                 actor_type=actor_type, actor_id=requested_by, rules_hash=rules_hash)
    return row


def _check_role(kind: str, role: str) -> None:
    needed = REQUIRED_ROLE.get(kind, UserRole.ADMIN)
    if _ROLE_RANK.get(role, -1) < _ROLE_RANK[needed]:
        raise ApprovalError(f"Se requiere rol '{needed}' para aprobar '{kind}' (tenés '{role}').")


def decide(
    session: Session,
    approval_id: int,
    *,
    approve: bool,
    by: str,
    role: str,
    note: str = "",
) -> ApprovalRequest:
    row = session.get(ApprovalRequest, approval_id)
    if row is None:
        raise ApprovalError("Solicitud inexistente.")
    if row.status != ApprovalStatus.PENDING:
        raise ApprovalError(f"La solicitud ya está en estado '{row.status}'.")
    expires = row.expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires is not None and expires < datetime.now(timezone.utc):
        row.status = ApprovalStatus.EXPIRED
        session.flush()
        raise ApprovalError("La solicitud expiró; generá una nueva.")
    _check_role(row.kind, role)
    row.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    row.decided_by = by
    row.decided_at = datetime.now(timezone.utc)
    row.decision_note = note
    session.flush()
    audit.record(session, action="approval.approved" if approve else "approval.rejected",
                 entity_type="approval_request", entity_id=row.id,
                 details={"kind": row.kind, "note": note, "amount": row.amount},
                 actor_type=ActorType.HUMAN, actor_id=by, rules_hash=row.rules_hash)
    return row


def pending(session: Session) -> list[ApprovalRequest]:
    return list(session.execute(
        select(ApprovalRequest).where(ApprovalRequest.status == ApprovalStatus.PENDING).order_by(ApprovalRequest.id)
    ).scalars())


def expire_stale(session: Session) -> int:
    now = datetime.now(timezone.utc)
    n = 0
    for row in pending(session):
        exp = row.expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp is not None and exp < now:
            row.status = ApprovalStatus.EXPIRED
            n += 1
    if n:
        audit.record(session, action="approval.expired_batch", entity_type="approval_request", details={"count": n})
    return n


def is_approved(session: Session, approval_id: int | None) -> bool:
    if approval_id is None:
        return False
    row = session.get(ApprovalRequest, approval_id)
    return bool(row and row.status == ApprovalStatus.APPROVED)
