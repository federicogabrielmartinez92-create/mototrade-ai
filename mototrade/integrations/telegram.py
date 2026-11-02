"""Notificador Telegram (Bot API). Único canal de salida ACTIVO del sistema.

- Con token + chat_id: envía mensajes reales al operador (HTTPS a api.telegram.org).
- Sin credenciales: `ConsoleNotifier` imprime/loguea y deja constancia.
- Los bots de Telegram no pueden iniciar chats: el operador debe escribirle primero al bot.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotifyResult:
    ok: bool
    external_ref: str | None = None
    error: str | None = None
    simulated: bool = False


class Notifier(Protocol):
    name: str

    def send(self, text: str) -> NotifyResult: ...


class ConsoleNotifier:
    name = "console"

    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str) -> NotifyResult:
        self.sent.append(text)
        log.info("[NOTIFICACIÓN SIMULADA] %s", text)
        return NotifyResult(ok=True, external_ref=f"console-{len(self.sent)}", simulated=True)


class TelegramNotifier:
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, transport: httpx.BaseTransport | None = None, timeout: float = 10.0):
        if not bot_token or not chat_id:
            raise ValueError("Telegram requiere bot_token y chat_id.")
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._chat_id = chat_id
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def send(self, text: str) -> NotifyResult:
        try:
            resp = self._client.post(f"{self._base}/sendMessage",
                                     json={"chat_id": self._chat_id, "text": text[:4000], "disable_web_page_preview": True})
            data = resp.json() if resp.content else {}
            if resp.status_code == 200 and data.get("ok"):
                return NotifyResult(ok=True, external_ref=str(data.get("result", {}).get("message_id")))
            return NotifyResult(ok=False, error=f"HTTP {resp.status_code}: {data.get('description') or resp.text[:200]}")
        except httpx.HTTPError as exc:
            return NotifyResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    def get_me(self) -> dict:
        resp = self._client.get(f"{self._base}/getMe")
        return resp.json()


def get_notifier() -> Notifier:
    from mototrade.config import get_settings

    s = get_settings()
    if s.telegram_enabled:
        return TelegramNotifier(s.telegram_bot_token.get_secret_value(), s.telegram_chat_id)
    return ConsoleNotifier()
