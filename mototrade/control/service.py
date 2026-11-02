"""Persistencia auditada del Centro de Control y cálculo de reglas efectivas."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from mototrade.config import BusinessRules, get_rules
from mototrade.config.rules import WatchlistEntry
from mototrade.core import audit
from mototrade.core.enums import ActorType
from mototrade.db.models import AppSetting

from .settings_model import ControlSettings


class ControlError(RuntimeError):
    pass


def _dump(section_model) -> dict:
    return json.loads(section_model.model_dump_json())


class ControlCenter:
    def __init__(self, session: Session):
        self.session = session

    # ---- lectura -------------------------------------------------------------
    def load(self) -> ControlSettings:
        """Devuelve la configuración efectiva: valores por defecto + lo persistido (validado)."""
        data: dict[str, Any] = {}
        for sec in ControlSettings.SECTIONS:
            row = self.session.get(AppSetting, sec)
            if row:
                data[sec] = row.value
        try:
            return ControlSettings(**data)
        except ValidationError as exc:  # una fila corrupta no tumba el sistema: se ignora y se avisa
            audit.record(self.session, action="control.invalid_row", entity_type="app_settings",
                         details={"error": str(exc)[:500]})
            good = {k: v for k, v in data.items() if self._valid(k, v)}
            return ControlSettings(**good)

    @staticmethod
    def _valid(section: str, value: dict) -> bool:
        try:
            ControlSettings(**{section: value})
            return True
        except ValidationError:
            return False

    def versions(self) -> dict[str, int]:
        return {sec: (self.session.get(AppSetting, sec).version if self.session.get(AppSetting, sec) else 0)
                for sec in ControlSettings.SECTIONS}

    # ---- escritura -----------------------------------------------------------
    def update_section(self, section: str, values: dict, *, by: str) -> ControlSettings:
        if section not in ControlSettings.SECTIONS:
            raise ControlError(f"Sección desconocida: {section}")
        current = self.load()
        merged = {**_dump(getattr(current, section)), **values}
        try:
            new_settings = ControlSettings(**{**{s: _dump(getattr(current, s)) for s in ControlSettings.SECTIONS}, section: merged})
        except ValidationError as exc:
            raise ControlError(f"Configuración inválida: {exc.errors()[0].get('msg', exc)}") from exc
        row = self.session.get(AppSetting, section)
        before = _dump(getattr(current, section))  # valor efectivo previo (default si no había fila)
        after = _dump(getattr(new_settings, section))
        if row is None:
            row = AppSetting(section=section, value=after, version=1, updated_by=by)
            self.session.add(row)
        else:
            row.value = after
            row.version += 1
            row.updated_by = by
        self.session.flush()
        changed = {k: {"antes": before.get(k), "despues": after.get(k)} for k in after if before.get(k) != after.get(k)}
        audit.record(self.session, action="control.updated", entity_type="app_settings", entity_id=section,
                     details={"version": row.version, "cambios": changed}, actor_type=ActorType.HUMAN, actor_id=by)
        return new_settings

    def ensure_defaults(self, *, by: str = "bootstrap") -> ControlSettings:
        """Persiste los valores por defecto la primera vez (para que el panel muestre versión 1)."""
        current = self.load()
        for sec in ControlSettings.SECTIONS:
            if self.session.get(AppSetting, sec) is None:
                self.session.add(AppSetting(section=sec, value=_dump(getattr(current, sec)), version=1, updated_by=by))
        self.session.flush()
        return current

    # ---- reglas efectivas ----------------------------------------------------
    def effective_rules(self, base: BusinessRules | None = None) -> BusinessRules:
        """BusinessRules del YAML con los parámetros operativos sobreescritos por el Centro de Control.
        El `rules_hash` resultante combina YAML + configuración, para trazabilidad exacta."""
        base = base or get_rules()
        cs = self.load()
        watch = [WatchlistEntry(make=m.make, model=m.model) for m in cs.search.priority_models] or base.radar.watchlist
        updated = base.model_copy(update={
            "capital": base.capital.model_copy(update={
                "initial_capital": cs.capital.capital_total,
                "min_cash_reserve_pct": cs.capital.min_reserve_pct,
            }),
            "valuation": base.valuation.model_copy(update={"minimum_roi": cs.search.minimum_roi}),
            "negotiation": base.negotiation.model_copy(update={
                "buyer": base.negotiation.buyer.model_copy(update={
                    "opening_offer_pct_of_max": cs.agents.buyer_opening_pct_of_max,
                    "step_pct": cs.agents.buyer_step_pct, "max_rounds": cs.agents.buyer_max_rounds}),
                "seller": base.negotiation.seller.model_copy(update={
                    "step_pct": cs.agents.seller_step_pct, "max_rounds": cs.agents.seller_max_rounds}),
                "daily_message_cap_per_conversation": cs.agents.daily_message_cap_per_conversation,
            }),
            "radar": base.radar.model_copy(update={"watchlist": watch,
                                                   "notify_price_drop_pct": base.radar.notify_price_drop_pct}),
        })
        combo = hashlib.sha256((base.rules_hash + cs.model_dump_json()).encode()).hexdigest()
        return updated.model_copy(update={"rules_hash": combo})
