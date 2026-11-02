"""Presupuesto y medición de IA.

- Cada llamada (real o simulada) se registra en `ai_usage` con tokens y costo estimado.
- Antes de llamar, `AIBudget.check(purpose)` verifica límites mensual/diario y cupos por tipo.
- `MeteredLLM` envuelve cualquier `LLMClient`: cachea por clave (misma entrada → misma salida, sin costo) y registra.
- Si se agota el presupuesto, las operaciones NO esenciales (análisis en profundidad, visión, redacción con IA)
  se detienen; consultar inventario, finanzas y conversaciones nunca depende de la IA.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mototrade.control.settings_model import AIBudgetSettings
from mototrade.core.money import D, ZERO
from mototrade.db.models import AIUsage
from mototrade.integrations.llm import LLMClient, LLMResult

ESSENTIAL_PURPOSES = {"interpret"}  # interpretar un mensaje entrante es barato y necesario; igual tiene tope mensual


class AIBudgetExceeded(RuntimeError):
    pass


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int, prices: dict[str, list[Decimal]]) -> Decimal:
    p_in, p_out = prices.get(model) or prices.get("default") or [Decimal("3"), Decimal("15")]
    return (D(input_tokens) * p_in + D(output_tokens) * p_out) / Decimal(1_000_000)


@dataclass(frozen=True)
class BudgetStatus:
    spent_month_usd: Decimal
    spent_today_usd: Decimal
    visual_this_month: int
    deep_today: int
    within_budget: bool
    reason: str


class AIBudget:
    def __init__(self, session: Session, settings: AIBudgetSettings):
        self.session = session
        self.cfg = settings

    def _sum(self, since: datetime, purpose: str | None = None) -> Decimal:
        q = select(func.coalesce(func.sum(AIUsage.est_cost_usd), 0)).where(AIUsage.occurred_at >= since, AIUsage.cache_hit.is_(False))
        if purpose:
            q = q.where(AIUsage.purpose == purpose)
        return D(self.session.execute(q).scalar_one())

    def _count(self, since: datetime, purpose: str) -> int:
        return self.session.execute(
            select(func.count()).select_from(AIUsage).where(AIUsage.occurred_at >= since, AIUsage.purpose == purpose,
                                                           AIUsage.cache_hit.is_(False))
        ).scalar_one()

    def status(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month = self._sum(month_start)
        today = self._sum(day_start)
        visual = self._count(month_start, "vision")
        deep = self._count(day_start, "deep_analysis")
        reason = ""
        if month >= self.cfg.monthly_budget_usd:
            reason = f"presupuesto mensual de IA agotado (US$ {month:.2f} / {self.cfg.monthly_budget_usd})"
        elif today >= self.cfg.daily_budget_usd:
            reason = f"límite diario de IA alcanzado (US$ {today:.2f} / {self.cfg.daily_budget_usd})"
        return {"spent_month_usd": month, "spent_today_usd": today, "visual_this_month": visual, "deep_today": deep,
                "within_budget": not reason, "reason": reason, "monthly_budget_usd": self.cfg.monthly_budget_usd,
                "daily_budget_usd": self.cfg.daily_budget_usd}

    def check(self, purpose: str, now: datetime | None = None) -> tuple[bool, str]:
        st = self.status(now)
        if not st["within_budget"]:
            return False, st["reason"]
        if purpose == "vision" and st["visual_this_month"] >= self.cfg.max_visual_analyses_per_month:
            return False, "máximo de análisis visuales del mes alcanzado"
        if purpose == "deep_analysis" and st["deep_today"] >= self.cfg.max_deep_analyses_per_day:
            return False, "máximo de análisis en profundidad del día alcanzado"
        return True, ""

    def record(self, *, provider: str, model: str, purpose: str, input_tokens: int = 0, output_tokens: int = 0,
               simulated: bool = False, cache_hit: bool = False, cache_key: str | None = None,
               entity_type: str | None = None, entity_id: str | None = None) -> AIUsage:
        cost = ZERO if (simulated or cache_hit) else estimate_cost_usd(model, input_tokens, output_tokens, self.cfg.model_prices)
        row = AIUsage(provider=provider, model_name=model, purpose=purpose, entity_type=entity_type,
                      entity_id=str(entity_id) if entity_id is not None else None, input_tokens=input_tokens,
                      output_tokens=output_tokens, est_cost_usd=cost, simulated=simulated, cache_hit=cache_hit,
                      cache_key=cache_key)
        self.session.add(row)
        self.session.flush()
        return row

    def cached(self, cache_key: str) -> AIUsage | None:
        return self.session.execute(
            select(AIUsage).where(AIUsage.cache_key == cache_key, AIUsage.cache_hit.is_(False)).order_by(AIUsage.id.desc()).limit(1)
        ).scalar_one_or_none()


def cache_key_for(tool_name: str, system: str, user: str, model: str) -> str:
    return hashlib.sha256(f"{tool_name}|{model}|{system}|{user}".encode()).hexdigest()[:64]


class MeteredLLM:
    """Envuelve un LLMClient: verifica presupuesto, cachea resultados idénticos y registra uso."""

    PURPOSE_BY_TOOL = {"interpret_message": "interpret", "draft_message": "draft", "deep_analysis": "deep_analysis"}

    def __init__(self, inner: LLMClient, budget: AIBudget, *, entity_type: str | None = None, entity_id=None,
                 cache: dict[str, dict] | None = None):
        self.inner = inner
        self.budget = budget
        self.provider = inner.provider
        self.model = inner.model
        self.simulated = inner.simulated
        self.entity_type, self.entity_id = entity_type, entity_id
        self._cache = cache if cache is not None else {}

    def structured(self, *, system: str, user: str, schema: dict, tool_name: str) -> LLMResult:
        purpose = self.PURPOSE_BY_TOOL.get(tool_name, "other")
        key = cache_key_for(tool_name, system, user, self.model)
        if key in self._cache:
            self.budget.record(provider=self.provider, model=self.model, purpose=purpose, cache_hit=True, cache_key=key,
                               entity_type=self.entity_type, entity_id=self.entity_id)
            return LLMResult(data=self._cache[key], provider=self.provider, model=self.model, simulated=self.simulated)
        if not self.simulated:
            ok, reason = self.budget.check(purpose)
            if not ok:
                raise AIBudgetExceeded(reason)
        res = self.inner.structured(system=system, user=user, schema=schema, tool_name=tool_name)
        usage = res.usage or {}
        self.budget.record(provider=res.provider, model=res.model, purpose=purpose,
                           input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
                           simulated=res.simulated, cache_key=key, entity_type=self.entity_type, entity_id=self.entity_id)
        self._cache[key] = dict(res.data)
        return res
