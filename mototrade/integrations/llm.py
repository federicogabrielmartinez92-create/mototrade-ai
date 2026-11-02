"""Cliente de modelo de lenguaje con salida estructurada.

- `FakeLLM`: determinista, sin red. Interpreta mensajes con reglas simples y redacta
  con plantillas. Se usa en tests y cuando no hay API key. Es también el fallback
  si el proveedor real falla.
- `AnthropicLLM`: usa la API de Anthropic con "tool use" forzado para obtener JSON
  validado contra un esquema. Requiere MOTOTRADE_ANTHROPIC_API_KEY.

El LLM NUNCA decide montos ni ejecuta acciones: solo (a) interpreta texto de la
contraparte y (b) redacta un mensaje con el monto y la intención que le da la
estrategia determinista.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from mototrade.core.money import D, fmt_ars


@dataclass(frozen=True)
class LLMResult:
    data: dict[str, Any]
    provider: str
    model: str
    simulated: bool
    usage: dict[str, int] | None = None


class LLMClient(Protocol):
    provider: str
    model: str
    simulated: bool

    def structured(self, *, system: str, user: str, schema: dict, tool_name: str) -> LLMResult: ...


# =============================================================================
# Esquemas de salida
# =============================================================================
INTERPRET_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["greeting", "ask_info", "give_info", "offer", "accept", "reject", "schedule_visit",
                     "payment_pressure", "suspicious", "other"],
        },
        "amount": {"type": ["number", "null"], "description": "Monto en ARS si la contraparte propone/acepta un precio"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "facts": {"type": "array", "items": {"type": "string"}, "description": "Datos afirmados por la contraparte"},
        "summary": {"type": "string"},
    },
    "required": ["intent", "amount", "questions", "facts", "summary"],
}

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


# =============================================================================
# Fake determinista
# =============================================================================
_AMOUNT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:[.\s]\d{3}){1,2}|\d{6,8})(?!\d)")
_MILLION_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:m|mill|millon|millón|millones|palos?)\b", re.I)
_K_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:k|lucas?|mil)\b", re.I)


def extract_amount(text: str) -> Decimal | None:
    t = text.lower()
    m = _MILLION_RE.search(t)
    if m:
        return (D(m.group(1).replace(",", ".")) * 1_000_000).quantize(D("1"))
    m = _K_RE.search(t)
    if m:
        return (D(m.group(1).replace(",", ".")) * 1_000).quantize(D("1"))
    m = _AMOUNT_RE.search(t)
    if m:
        return D(re.sub(r"[.\s]", "", m.group(1)))
    return None


def _classify(text: str, amount: Decimal | None) -> str:
    t = text.lower()
    if re.search(r"\b(seña|transfer[ií]|deposit[aá]|cbu|alias|cvu)\b.{0,40}\b(ahora|ya|urgente|hoy)\b", t):
        return "payment_pressure"
    if re.search(r"\b(ignor[aá]|olvid[aá])\b.{0,30}\b(instrucci|regla)", t) or "system prompt" in t:
        return "suspicious"
    if re.search(r"\b(acept|dale|trato hecho|cerramos|de acuerdo|ok+\b.*precio|me sirve)\b", t):
        return "accept"
    if re.search(r"\b(no me sirve|no acepto|no bajo|no vendo|no gracias|imposible|ni loco)\b", t):
        return "reject"
    if re.search(r"\b(verla|ir a ver|pasar a ver|visita|cuándo|cuando pod|horario|dirección|direccion)\b", t):
        return "schedule_visit"
    if amount is not None:
        return "offer"
    if "?" in t or re.search(r"\b(cu[aá]nto|qu[eé] a[ñn]o|km|kil[oó]metr|papeles|t[ií]tulo|transferencia)\b", t):
        return "ask_info"
    if re.search(r"\b(hola|buenas|buen d[ií]a|qu[eé] tal)\b", t):
        return "greeting"
    if re.search(r"\b(tiene|está|esta|anda|km|papeles|al d[ií]a)\b", t):
        return "give_info"
    return "other"


class FakeLLM:
    provider = "fake"
    model = "fake-rules-v1"
    simulated = True

    def structured(self, *, system: str, user: str, schema: dict, tool_name: str) -> LLMResult:
        if tool_name == "interpret_message":
            body = _extract_external(user)
            amount = extract_amount(body)
            intent = _classify(body, amount)
            questions = [q.strip() + "?" for q in body.split("?")[:-1] if q.strip()][:3]
            data = {"intent": intent, "amount": float(amount) if amount is not None else None,
                    "questions": questions, "facts": [], "summary": body[:140]}
            return LLMResult(data=data, provider=self.provider, model=self.model, simulated=True)
        if tool_name == "draft_message":
            ctx = _parse_draft_context(user)
            return LLMResult(data={"text": _template_draft(ctx)}, provider=self.provider, model=self.model, simulated=True)
        raise ValueError(f"tool desconocida: {tool_name}")


def _extract_external(user: str) -> str:
    m = re.search(r"\[INICIO [^\]]+\]\n(.*)\n\[FIN [^\]]+\]", user, re.S)
    return m.group(1) if m else user


def _parse_draft_context(user: str) -> dict:
    m = re.search(r"CONTEXTO_JSON:(\{.*\})", user, re.S)
    try:
        return json.loads(m.group(1)) if m else {}
    except json.JSONDecodeError:
        return {}


def _template_draft(ctx: dict) -> str:
    role = ctx.get("role", "buyer")
    intent = ctx.get("intent", "ask_info")
    amount = ctx.get("amount")
    name = ctx.get("business_name", "nosotros")
    operator = ctx.get("operator_first_name", "el responsable")
    disclose = ctx.get("disclose_automation", True)
    moto = ctx.get("moto", "la moto")
    amt = fmt_ars(amount) if amount else ""
    intro = f"¡Hola! Te escribo de parte de {name}." if ctx.get("is_opening") else "¡Hola!"
    tag = f" (Soy el asistente automático de {name}; cualquier cierre lo confirma {operator}.)" if (disclose and ctx.get("is_opening")) else ""
    if role == "buyer":
        if intent == "opening_offer":
            return (f"{intro} Vi tu publicación de la {moto} y me interesa. ¿Tiene los papeles al día y está a tu nombre? "
                    f"Si está todo en orden, podría ofrecerte {amt} en el día, con transferencia a tu nombre.{tag}")
        if intent == "counter_offer":
            why = f" Te explico el número: {ctx['repair_costs']}." if ctx.get("repair_costs") else ""
            return (f"{intro} Entiendo.{why} Lo máximo que puedo mejorar hoy es {amt}. Si te sirve, coordinamos "
                    f"para verla y, si está como decís, cerramos.{tag}")
        if intent == "accept_pending_approval":
            return (f"{intro} Me parece bien {amt}. Lo paso a {operator} para que lo confirme y coordinamos "
                    f"día y hora para verla y hacer la transferencia. Te escribo en breve.{tag}")
        if intent == "ask_info":
            return (f"{intro} Para avanzar necesito saber: ¿está a tu nombre, tiene cédula y título, y hay deudas "
                    f"de patente o multas? ¿Cuántos kilómetros reales tiene?{tag}")
        if intent == "escalate":
            return (f"{intro} Gracias por tu tiempo. Lo consulto con {operator} y te confirmo si podemos "
                    f"acercarnos a tu número.{tag}")
        if intent == "decline":
            return f"{intro} Gracias igual, por ahora no llegamos a ese valor. Si bajás el precio, avisame.{tag}"
    else:
        if intent == "counter_offer":
            return (f"{intro} Gracias por el interés en la {moto}. Puedo dejarla en {amt}, está lista para "
                    f"transferir y con service hecho. ¿Querés pasar a verla?{tag}")
        if intent == "accept_pending_approval":
            return (f"{intro} Dale, {amt} me parece bien. Lo confirmo con {operator} y te paso los pasos para "
                    f"la transferencia.{tag}")
        if intent == "ask_info" or intent == "give_info":
            return (f"{intro} Sí, la {moto} tiene papeles al día y está lista para transferir. ¿Querés coordinar "
                    f"para verla?{tag}")
        if intent == "escalate":
            return f"{intro} Lo consulto con {operator} y te confirmo.{tag}"
        if intent == "decline":
            return f"{intro} Gracias, pero por debajo de {amt} no la vendo. Si te interesa, está disponible.{tag}"
    return f"{intro} Gracias por el mensaje, enseguida te respondo con más detalle.{tag}"


# =============================================================================
# Anthropic (real)
# =============================================================================
class AnthropicLLM:
    provider = "anthropic"
    simulated = False

    def __init__(self, api_key: str, model: str, max_tokens: int = 1024):
        from anthropic import Anthropic  # import perezoso: no obliga a tener la lib en tests

        self._client = Anthropic(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens

    def structured(self, *, system: str, user: str, schema: dict, tool_name: str) -> LLMResult:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[{"name": tool_name, "description": "Devuelve el resultado estructurado.", "input_schema": schema}],
            tool_choice={"type": "tool", "name": tool_name},
        )
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use" and block.name == tool_name:
                usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
                return LLMResult(data=dict(block.input), provider=self.provider, model=self.model, simulated=False,
                                 usage=usage)
        raise RuntimeError("El modelo no devolvió la herramienta esperada.")


def get_llm() -> LLMClient:
    from mototrade.config import get_settings

    s = get_settings()
    if s.llm_enabled:
        return AnthropicLLM(api_key=s.anthropic_api_key.get_secret_value(), model=s.llm_model)
    return FakeLLM()
