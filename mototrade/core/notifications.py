"""Centro de notificaciones persistente (canal principal del operador).

- Toda alerta del sistema se guarda acá y se ve en Streamlit (campana + página).
- Telegram es OPCIONAL: si hay token, además se encola una copia por `outbound`. Sin token no pasa nada.
- Idempotente por `dedupe_key`: la misma alerta no se duplica aunque el worker corra varias veces.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mototrade.db.models import Notification


def notify(session: Session, *, key: str, title: str, body: str = "", level: str = "info", category: str = "general",
           entity_type: str | None = None, entity_id=None, also_telegram: bool = True) -> Notification:
    if level not in ("info", "warning", "alert"):
        level = "info"
    row = session.execute(select(Notification).where(Notification.dedupe_key == key)).scalar_one_or_none()
    if row is None:
        row = Notification(level=level, category=category, title=title[:255], body=body, entity_type=entity_type,
                           entity_id=str(entity_id) if entity_id is not None else None, dedupe_key=key)
        session.add(row)
        session.flush()
        if also_telegram:
            from mototrade.config import get_settings

            if get_settings().telegram_enabled:
                from mototrade.core import outbound

                outbound.enqueue(session, kind="notify_operator", channel="telegram",
                                 payload={"text": f"{title}\n{body}".strip()}, idempotency_key=f"notify:{key}")
    return row


def unread_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(Notification).where(Notification.read_at.is_(None))).scalar_one()


def list_notifications(session: Session, *, unread_only: bool = False, limit: int = 100, category: str | None = None) -> list[Notification]:
    q = select(Notification).order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    if category:
        q = q.where(Notification.category == category)
    return list(session.execute(q).scalars())


def mark_read(session: Session, ids: list[int], *, by: str) -> int:
    n = 0
    now = datetime.now(timezone.utc)
    for nid in ids:
        row = session.get(Notification, nid)
        if row and row.read_at is None:
            row.read_at, row.read_by = now, by
            n += 1
    return n


def mark_all_read(session: Session, *, by: str) -> int:
    return mark_read(session, [r.id for r in list_notifications(session, unread_only=True, limit=10_000)], by=by)
