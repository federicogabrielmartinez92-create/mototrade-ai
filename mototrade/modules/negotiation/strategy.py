"""Estrategias deterministas de negociación. Deciden QUÉ monto e intención proponer; nunca la IA.

Comprador: arranca en `opening_offer_pct_of_max` del máximo autorizado y mejora de a
`step_pct` por ronda, sin superar nunca el máximo. Si la contraparte pide un valor
dentro del límite → "accept_pending_approval" (el humano confirma).

Vendedor: arranca en el precio de lista, cede de a `step_pct` del precio de lista por
ronda, nunca por debajo del mínimo autorizado. Si la contraparte ofrece ≥ mínimo →
"accept_pending_approval".
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from mototrade.config.rules import BusinessRules
from mototrade.core.money import D, ZERO, round_down_to


@dataclass(frozen=True)
class StrategyDecision:
    intent: str  # opening_offer | counter_offer | accept_pending_approval | ask_info | escalate | decline | hold
    amount: Decimal | None
    rationale: str


class BuyerStrategy:
    def __init__(self, rules: BusinessRules):
        self.rules = rules
        self.b = rules.negotiation.buyer
        self.rounding = rules.valuation.offer_rounding

    def decide(self, *, authorized_max: Decimal, last_our_offer: Decimal | None, counterpart_amount: Decimal | None,
               counterpart_intent: str, round_number: int, suggested_opening: Decimal | None = None) -> StrategyDecision:
        authorized_max = D(authorized_max)
        if counterpart_intent in ("payment_pressure", "suspicious"):
            return StrategyDecision("escalate", None, "Mensaje sospechoso o presión de pago: revisar humano.")
        if counterpart_intent == "reject" and round_number >= self.b.max_rounds:
            return StrategyDecision("decline", last_our_offer, "Rechazo tras rondas máximas.")
        if counterpart_intent == "ask_info" or counterpart_intent == "greeting":
            if last_our_offer is None:
                opening = suggested_opening if suggested_opening is not None else authorized_max * self.b.opening_offer_pct_of_max
                opening = min(round_down_to(opening, self.rounding), authorized_max)
                return StrategyDecision("opening_offer", opening, "Primer contacto con consulta: oferta de apertura.")
            return StrategyDecision("ask_info", None, "Responder consulta sin mover el precio.")
        if counterpart_intent == "schedule_visit":
            return StrategyDecision("ask_info", None, "Coordinar visita: responde el operador con agenda.")

        if last_our_offer is None:
            opening = suggested_opening if suggested_opening is not None else authorized_max * self.b.opening_offer_pct_of_max
            opening = min(round_down_to(opening, self.rounding), authorized_max)
            return StrategyDecision("opening_offer", opening, "Oferta de apertura según reglas.")

        if counterpart_amount is not None:
            cp = D(counterpart_amount)
            if cp <= authorized_max:
                return StrategyDecision("accept_pending_approval", cp,
                                        f"Contraparte pide {cp} ≤ máximo {authorized_max}: aceptar sujeto a aprobación.")
            if counterpart_intent == "accept":
                return StrategyDecision("accept_pending_approval", D(last_our_offer),
                                        "Contraparte aceptó nuestra última oferta.")
        elif counterpart_intent == "accept":
            return StrategyDecision("accept_pending_approval", D(last_our_offer), "Contraparte aceptó nuestra oferta.")

        if round_number >= self.b.max_rounds:
            return StrategyDecision("escalate", None, "Rondas máximas alcanzadas sin acuerdo.")
        nxt = D(last_our_offer) + authorized_max * self.b.step_pct
        nxt = min(round_down_to(nxt, self.rounding), authorized_max)
        if nxt <= D(last_our_offer):
            return StrategyDecision("escalate", None, "No hay margen para mejorar la oferta dentro del límite.")
        return StrategyDecision("counter_offer", nxt, f"Mejora de ronda {round_number}: {nxt}.")


class SellerStrategy:
    def __init__(self, rules: BusinessRules):
        self.rules = rules
        self.s = rules.negotiation.seller
        self.rounding = rules.valuation.offer_rounding

    def decide(self, *, list_price: Decimal, min_price: Decimal, last_our_price: Decimal | None,
               counterpart_amount: Decimal | None, counterpart_intent: str, round_number: int) -> StrategyDecision:
        list_price, min_price = D(list_price), D(min_price)
        if counterpart_intent in ("payment_pressure", "suspicious"):
            return StrategyDecision("escalate", None, "Mensaje sospechoso o presión de pago: revisar humano.")
        if counterpart_intent in ("greeting", "ask_info", "give_info"):
            return StrategyDecision("ask_info", last_our_price or list_price, "Responder consulta; precio de lista.")
        if counterpart_intent == "schedule_visit":
            return StrategyDecision("ask_info", last_our_price or list_price, "Coordinar visita: agenda del operador.")
        current = D(last_our_price) if last_our_price is not None else list_price
        if counterpart_amount is not None:
            cp = D(counterpart_amount)
            if cp >= min_price:
                return StrategyDecision("accept_pending_approval", cp, f"Oferta {cp} ≥ mínimo {min_price}.")
        elif counterpart_intent == "accept":
            return StrategyDecision("accept_pending_approval", current, "Contraparte aceptó el precio.")
        if round_number >= self.s.max_rounds:
            return StrategyDecision("escalate", None, "Rondas máximas alcanzadas sin acuerdo.")
        nxt = current - list_price * self.s.step_pct
        nxt = max(round_down_to(nxt, self.rounding), min_price)
        if nxt >= current:
            if counterpart_amount is not None and D(counterpart_amount) < min_price:
                return StrategyDecision("decline", min_price, "Sin margen: mantener mínimo.")
            return StrategyDecision("escalate", None, "Sin margen para ceder.")
        return StrategyDecision("counter_offer", nxt, f"Concesión de ronda {round_number}: {nxt}.")
