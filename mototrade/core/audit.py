"""Registro de auditoría append-only con cadena de hashes (tamper-evident).

Cada fila guarda `prev_hash` (hash de la fila anterior) y `hash` = SHA-256 de
(prev_hash + contenido canónico). Si alguien edita o borra una fila intermedia,
`verify_chain()` lo detecta.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mototrade.core.enums import ActorType
from mototrade.db.models import AuditLog

GENESIS_HASH = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def _compute_hash(prev_hash: str, occurred_at: datetime, actor_type: str, actor_id: str, action: str,
                  entity_type: str, entity_id: str | None, details: dict, rules_hash: str | None) -> str:
    body = _canonical(
        {
            "occurred_at": occurred_at.isoformat(),
            "actor_type": actor_type,
            "actor_id": actor_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "details": details,
            "rules_hash": rules_hash,
        }
    )
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


def record(
    session: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: int | str | None = None,
    details: dict | None = None,
    actor_type: ActorType | str = ActorType.SYSTEM,
    actor_id: str = "system",
    rules_hash: str | None = None,
) -> AuditLog:
    """Agrega una entrada de auditoría dentro de la transacción actual."""
    last = session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(1)).scalar_one_or_none()
    prev_hash = last.hash if last else GENESIS_HASH
    occurred_at = datetime.now(timezone.utc)
    # Normalizamos por JSON para que lo hasheado sea exactamente lo que se persiste.
    details = json.loads(_canonical(details or {}))
    entity_id_s = None if entity_id is None else str(entity_id)
    h = _compute_hash(prev_hash, occurred_at, str(actor_type), actor_id, action, entity_type, entity_id_s,
                      details, rules_hash)
    row = AuditLog(
        occurred_at=occurred_at,
        actor_type=str(actor_type),
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id_s,
        details=details,
        rules_hash=rules_hash,
        prev_hash=prev_hash,
        hash=h,
    )
    session.add(row)
    session.flush()
    return row


def verify_chain(session: Session) -> tuple[bool, int | None]:
    """Recorre toda la cadena. Devuelve (ok, id_de_la_primera_fila_rota)."""
    prev = GENESIS_HASH
    for row in session.execute(select(AuditLog).order_by(AuditLog.id)).scalars():
        occurred = row.occurred_at
        if occurred.tzinfo is None:  # SQLite devuelve naive
            occurred = occurred.replace(tzinfo=timezone.utc)
        expected = _compute_hash(prev, occurred, row.actor_type, row.actor_id, row.action, row.entity_type,
                                 row.entity_id, row.details, row.rules_hash)
        if row.prev_hash != prev or row.hash != expected:
            return False, row.id
        prev = row.hash
    return True, None
