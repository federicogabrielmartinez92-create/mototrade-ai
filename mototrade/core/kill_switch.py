"""Botón de parada de emergencia y pausas globales.

Toda acción con efecto externo (envíos, publicaciones, ofertas) y todo ciclo del
worker consultan `is_engaged()` antes de actuar.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from mototrade.core import audit
from mototrade.core.enums import ActorType
from mototrade.db.models import SystemState

KILL_SWITCH_KEY = "kill_switch"
WORKER_PAUSED_KEY = "worker_paused"


class KillSwitchEngaged(RuntimeError):
    pass


def _get(session: Session, key: str) -> SystemState | None:
    return session.get(SystemState, key)


def is_engaged(session: Session) -> bool:
    row = _get(session, KILL_SWITCH_KEY)
    return bool(row and row.value.get("engaged"))


def engage(session: Session, *, by: str, reason: str = "") -> None:
    row = _get(session, KILL_SWITCH_KEY) or SystemState(key=KILL_SWITCH_KEY, value={})
    row.value = {"engaged": True, "reason": reason}
    row.updated_by = by
    session.add(row)
    audit.record(session, action="kill_switch.engage", entity_type="system", details={"reason": reason},
                 actor_type=ActorType.HUMAN, actor_id=by)


def release(session: Session, *, by: str) -> None:
    row = _get(session, KILL_SWITCH_KEY) or SystemState(key=KILL_SWITCH_KEY, value={})
    row.value = {"engaged": False, "reason": ""}
    row.updated_by = by
    session.add(row)
    audit.record(session, action="kill_switch.release", entity_type="system", actor_type=ActorType.HUMAN,
                 actor_id=by)


def assert_not_engaged(session: Session) -> None:
    if is_engaged(session):
        raise KillSwitchEngaged("Parada de emergencia activa: no se ejecutan acciones automáticas.")


def is_worker_paused(session: Session) -> bool:
    row = _get(session, WORKER_PAUSED_KEY)
    return bool(row and row.value.get("paused"))


def set_worker_paused(session: Session, paused: bool, *, by: str) -> None:
    row = _get(session, WORKER_PAUSED_KEY) or SystemState(key=WORKER_PAUSED_KEY, value={})
    row.value = {"paused": paused}
    row.updated_by = by
    session.add(row)
    audit.record(session, action="worker.pause" if paused else "worker.resume", entity_type="system",
                 actor_type=ActorType.HUMAN, actor_id=by)
