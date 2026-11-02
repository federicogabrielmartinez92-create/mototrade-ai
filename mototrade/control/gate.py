"""Puerta operativa: intención configurada vs. estado EFECTIVO de cada función.

Toda automatización (worker, API, agentes) consulta `OperationsGate.allows(<función>)` antes de actuar.
Ejemplo: búsqueda habilitada pero sin capital libre → efectivo = pausada por capital.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from mototrade.core import kill_switch
from mototrade.modules.finance.capital import CapitalPosition, capital_position

from .service import ControlCenter
from .settings_model import ControlSettings

FUNCTIONS = ("search", "negotiable_analysis", "buyer_agent", "seller_agent", "visual_inspection", "parts_search",
             "ai_calls", "auto_send", "notifications")


@dataclass(frozen=True)
class FunctionState:
    name: str
    intended: bool
    effective: bool
    reason: str = ""
    details: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.effective:
            return "activo"
        if not self.intended:
            return f"desactivado ({self.reason})" if self.reason else "desactivado"
        return f"pausado: {self.reason}"


class OperationsGate:
    def __init__(self, session: Session, settings: ControlSettings | None = None, now: datetime | None = None):
        self.session = session
        self.cs = settings or ControlCenter(session).load()
        self.now = now or datetime.now(timezone.utc)
        self._pos: CapitalPosition | None = None
        self._ai: dict | None = None

    @property
    def position(self) -> CapitalPosition:
        if self._pos is None:
            self._pos = capital_position(self.session, self.cs.capital)
        return self._pos

    @property
    def ai_budget(self) -> dict:
        if self._ai is None:
            from mototrade.modules.ai_budget import AIBudget

            self._ai = AIBudget(self.session, self.cs.ai_budget).status(self.now)
        return self._ai

    def state(self, name: str) -> FunctionState:
        cs = self.cs
        ks = kill_switch.is_engaged(self.session)
        if name == "search":
            intended = cs.search.auto_search_enabled
            if ks:
                return FunctionState(name, intended, False, "parada de emergencia")
            if not intended:
                return FunctionState(name, False, False, "desactivada manualmente")
            if not self.position.can_buy_another:
                return FunctionState(name, True, False, "; ".join(self.position.reasons), self.position.as_dict())
            return FunctionState(name, True, True, details=self.position.as_dict())
        if name == "negotiable_analysis":
            base = self.state("search")
            intended = cs.search.negotiable_mode_enabled and base.intended
            return FunctionState(name, intended, intended and base.effective, base.reason if not base.effective else
                                 ("desactivado manualmente" if not cs.search.negotiable_mode_enabled else ""))
        if name == "buyer_agent":
            intended = cs.agents.buyer_agent_enabled
            if ks:
                return FunctionState(name, intended, False, "parada de emergencia")
            if not intended:
                return FunctionState(name, False, False, "desactivado manualmente")
            if not self.position.can_buy_another:
                return FunctionState(name, True, False, "sin capacidad de compra: " + "; ".join(self.position.reasons))
            return FunctionState(name, True, True)
        if name == "seller_agent":
            intended = cs.agents.seller_agent_enabled
            if ks:
                return FunctionState(name, intended, False, "parada de emergencia")
            return FunctionState(name, intended, intended, "" if intended else "desactivado manualmente")
        if name == "ai_calls":
            b = self.ai_budget
            if ks:
                return FunctionState(name, True, False, "parada de emergencia")
            if not b["within_budget"]:
                return FunctionState(name, True, False, b["reason"], b)
            return FunctionState(name, True, True, details=b)
        if name == "visual_inspection":
            intended = cs.agents.visual_inspection_enabled
            if not intended:
                return FunctionState(name, False, False, "desactivada manualmente")
            ai = self.state("ai_calls")
            if not ai.effective:
                return FunctionState(name, True, False, ai.reason)
            if self.ai_budget["visual_this_month"] >= cs.ai_budget.max_visual_analyses_per_month:
                return FunctionState(name, True, False, "máximo de análisis visuales del mes alcanzado")
            return FunctionState(name, True, True)
        if name == "parts_search":
            intended = cs.agents.parts_search_enabled
            return FunctionState(name, intended, False, "sin fuente automática autorizada (solo carga manual)" if intended
                                 else "desactivada manualmente")
        if name == "auto_send":
            intended = cs.agents.auto_send_enabled
            return FunctionState(name, intended, False, "ningún canal autorizado para envío automático" if intended
                                 else "desactivado")
        if name == "notifications":
            from mototrade.config import get_settings

            s = get_settings()
            if ks:
                return FunctionState(name, True, False, "parada de emergencia")
            return FunctionState(name, True, True, "" if s.telegram_enabled else "modo consola (sin token de Telegram)")
        raise ValueError(f"función desconocida: {name}")

    def allows(self, name: str) -> bool:
        return self.state(name).effective

    def snapshot(self) -> dict[str, FunctionState]:
        return {n: self.state(n) for n in FUNCTIONS}
