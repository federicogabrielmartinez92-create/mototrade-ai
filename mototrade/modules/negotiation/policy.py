"""Guardia de políticas del negociador: la última palabra antes de que un borrador exista.

Verifica, en código y sin IA:
- límites autorizados por el humano (precio máximo de compra / mínimo de venta),
- kill switch, agente habilitado en la conversación, rondas máximas,
- tope diario de mensajes por conversación y horario de silencio,
- intenciones prohibidas para un agente (pagar, reservar, aceptar) → siempre a aprobación humana.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mototrade.config.rules import BusinessRules
from mototrade.core import kill_switch
from mototrade.core.enums import ConversationDirection, MessageSender
from mototrade.core.money import D
from mototrade.db.models import Conversation, Message

AR_TZ = ZoneInfo("America/Argentina/Cordoba")

FORBIDDEN_AGENT_INTENTS = {"pay", "reserve", "accept_final", "close_deal", "share_bank_details"}


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "reasons": self.reasons, "checks": self.checks}


class NegotiationGuard:
    def __init__(self, session: Session, rules: BusinessRules):
        self.session = session
        self.rules = rules

    def _in_quiet_hours(self, now: datetime) -> bool:
        local = now.astimezone(AR_TZ).time()
        start = time.fromisoformat(self.rules.negotiation.quiet_hours_start)
        end = time.fromisoformat(self.rules.negotiation.quiet_hours_end)
        if start <= end:
            return start <= local < end
        return local >= start or local < end  # rango que cruza medianoche

    def _agent_messages_last_24h(self, conversation_id: int, now: datetime) -> int:
        since = now - timedelta(hours=24)
        return self.session.execute(
            select(func.count()).select_from(Message).where(
                Message.conversation_id == conversation_id, Message.sender == MessageSender.AGENT,
                Message.created_at >= since,
            )
        ).scalar_one()

    def check(self, conversation: Conversation, *, intent: str, amount: Decimal | None,
              authorized_limit: Decimal | None, round_number: int, now: datetime | None = None) -> GuardVerdict:
        now = now or datetime.now(timezone.utc)
        reasons: list[str] = []
        checks: dict = {}

        checks["kill_switch"] = kill_switch.is_engaged(self.session)
        if checks["kill_switch"]:
            reasons.append("Parada de emergencia activa.")

        checks["agent_enabled"] = conversation.agent_enabled
        if not conversation.agent_enabled:
            reasons.append("La conversación está en manos del operador (agente deshabilitado).")

        checks["forbidden_intent"] = intent in FORBIDDEN_AGENT_INTENTS
        if checks["forbidden_intent"]:
            reasons.append(f"Intención '{intent}' prohibida para un agente: requiere humano.")

        max_rounds = (self.rules.negotiation.buyer.max_rounds if conversation.direction == ConversationDirection.BUY
                      else self.rules.negotiation.seller.max_rounds)
        checks["rounds"] = {"current": round_number, "max": max_rounds}
        if intent in ("opening_offer", "counter_offer") and round_number > max_rounds:
            reasons.append(f"Se superaron las {max_rounds} rondas permitidas: escalar a humano.")

        if intent in ("opening_offer", "counter_offer", "accept_pending_approval"):
            if amount is None or D(amount) <= 0:
                reasons.append("Monto inválido o ausente para una oferta.")
            elif authorized_limit is None:
                reasons.append("No hay límite autorizado por un humano para esta conversación.")
            elif conversation.direction == ConversationDirection.BUY and D(amount) > D(authorized_limit):
                reasons.append(f"Oferta {amount} supera el máximo autorizado {authorized_limit}.")
            elif conversation.direction == ConversationDirection.SELL and D(amount) < D(authorized_limit):
                reasons.append(f"Precio {amount} es menor al mínimo autorizado {authorized_limit}.")
            checks["amount"] = str(amount) if amount is not None else None
            checks["authorized_limit"] = str(authorized_limit) if authorized_limit is not None else None

        cap = self.rules.negotiation.daily_message_cap_per_conversation
        sent = self._agent_messages_last_24h(conversation.id, now)
        checks["daily_cap"] = {"sent_24h": sent, "cap": cap}
        if sent >= cap:
            reasons.append(f"Tope diario de {cap} mensajes del agente alcanzado en esta conversación.")

        checks["quiet_hours"] = self._in_quiet_hours(now)
        if checks["quiet_hours"]:
            reasons.append("Horario de silencio: el borrador se genera pero no se envía hasta la mañana.")

        # El horario de silencio no bloquea el BORRADOR, solo el envío; se informa como razón "soft".
        hard = [r for r in reasons if not r.startswith("Horario de silencio")]
        return GuardVerdict(allowed=not hard, reasons=reasons, checks=checks)
