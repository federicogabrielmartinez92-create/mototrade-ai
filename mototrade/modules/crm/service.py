"""Módulo 8 — CRM comercial: contactos, pipeline y seguimiento. Datos mínimos (Ley 25.326)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mototrade.core import audit
from mototrade.core.enums import ActorType, ConversationStatus
from mototrade.db.models import Contact, Conversation, Deal, Message


class CrmService:
    def __init__(self, session: Session):
        self.session = session

    def upsert_contact(self, *, alias: str, channel: str, kind: str, channel_handle: str | None = None,
                       opt_in_messaging: bool = False, by: str = "operator") -> Contact:
        q = select(Contact).where(Contact.channel == channel)
        q = q.where(Contact.channel_handle == channel_handle) if channel_handle else q.where(Contact.alias == alias)
        row = self.session.execute(q).scalars().first()
        if row:
            row.alias = alias or row.alias
            row.kind = kind if row.kind == kind else "both"
            row.opt_in_messaging = row.opt_in_messaging or opt_in_messaging
            return row
        row = Contact(alias=alias.strip(), channel=channel, channel_handle=channel_handle, kind=kind,
                      opt_in_messaging=opt_in_messaging)
        self.session.add(row)
        self.session.flush()
        audit.record(self.session, action="contact.created", entity_type="contact", entity_id=row.id,
                     details={"channel": channel, "kind": kind, "opt_in": opt_in_messaging},
                     actor_type=ActorType.HUMAN, actor_id=by)
        return row

    def set_do_not_contact(self, contact: Contact, value: bool, *, by: str) -> None:
        contact.do_not_contact = value
        audit.record(self.session, action="contact.do_not_contact", entity_type="contact", entity_id=contact.id,
                     details={"value": value}, actor_type=ActorType.HUMAN, actor_id=by)

    def pipeline(self) -> dict[str, int]:
        rows = self.session.execute(select(Deal.stage, func.count()).group_by(Deal.stage)).all()
        return {s: n for s, n in rows}

    def conversations_needing_attention(self) -> list[Conversation]:
        return list(self.session.execute(
            select(Conversation).where(Conversation.status.in_([ConversationStatus.WAITING_HUMAN, ConversationStatus.HUMAN_TAKEOVER]))
            .order_by(Conversation.updated_at)
        ).scalars())

    def stale_conversations(self, days: int = 3, now: datetime | None = None) -> list[Conversation]:
        """Conversaciones abiertas sin mensajes en N días: candidatas a seguimiento (humano decide)."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        out = []
        for conv in self.session.execute(select(Conversation).where(Conversation.status.in_(["open", "waiting_counterpart"]))).scalars():
            last = self.session.execute(
                select(func.max(Message.created_at)).where(Message.conversation_id == conv.id)).scalar_one()
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last is None or last < cutoff:
                out.append(conv)
        return out
