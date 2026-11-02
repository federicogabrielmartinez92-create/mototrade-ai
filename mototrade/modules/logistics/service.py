"""Módulo 6 — Gestor de visitas y talleres (agenda interna; sin integración de calendario todavía)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from mototrade.core import audit
from mototrade.core.enums import ActorType, VisitStatus
from mototrade.db.models import Visit


class LogisticsService:
    def __init__(self, session: Session):
        self.session = session

    def propose_visit(self, *, kind: str, scheduled_at: datetime, location: str | None = None, deal_id: int | None = None,
                      inventory_item_id: int | None = None, supplier_id: int | None = None, by: str = "operator") -> Visit:
        if kind not in ("inspection", "pickup", "workshop", "delivery"):
            raise ValueError("kind inválido")
        v = Visit(kind=kind, scheduled_at=scheduled_at, location=location, deal_id=deal_id,
                  inventory_item_id=inventory_item_id, supplier_id=supplier_id, status=VisitStatus.PROPOSED)
        self.session.add(v)
        self.session.flush()
        audit.record(self.session, action="visit.proposed", entity_type="visit", entity_id=v.id,
                     details={"kind": kind, "at": scheduled_at.isoformat()}, actor_type=ActorType.HUMAN, actor_id=by)
        return v

    def set_status(self, visit: Visit, status: VisitStatus | str, *, by: str, outcome: str = "") -> None:
        visit.status = str(status)
        if outcome:
            visit.outcome = outcome
        audit.record(self.session, action=f"visit.{status}", entity_type="visit", entity_id=visit.id,
                     details={"outcome": outcome}, actor_type=ActorType.HUMAN, actor_id=by)

    def upcoming(self, now: datetime | None = None) -> list[Visit]:
        now = now or datetime.now(timezone.utc)
        return list(self.session.execute(
            select(Visit).where(Visit.status.in_([VisitStatus.PROPOSED, VisitStatus.CONFIRMED]), Visit.scheduled_at >= now)
            .order_by(Visit.scheduled_at)
        ).scalars())
