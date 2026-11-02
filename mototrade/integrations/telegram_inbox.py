"""Importación asistida por Telegram (incremento 2).

El operador le escribe al bot desde el celular:

    /moto Honda | Wave 110 | 2022 | 15000 | 2300000 | Rosario | https://link.del.anuncio | particular
    (adjuntando fotos en el mismo mensaje o en mensajes siguientes con la misma referencia)

El worker lee los mensajes con `getUpdates(offset)` (long polling, sin webhook: funciona aunque el worker corra
cada 15–30 min por GitHub Actions; Telegram conserva los updates 24 h), crea el anuncio en el radar, descarga las
fotos (`getFile`, ≤ 20 MB) y responde con el resultado. NO es búsqueda automática: es carga manual asistida.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy.orm import Session

from mototrade.core import audit
from mototrade.core.money import D
from mototrade.db.models import Listing, SystemState
from mototrade.modules.radar import ManualSource, RadarService, RawListing

log = logging.getLogger(__name__)
OFFSET_KEY = "telegram_inbox_offset"
HELP = ("Formato: /moto Marca | Modelo | Año | Km | Precio | Ubicación | URL (opcional) | particular/dealer (opcional)\n"
        "Ejemplo: /moto Honda | Wave 110 | 2022 | 15000 | 2300000 | Rosario | https://... | particular\n"
        "Adjuntá fotos al mismo mensaje. Para agregar fotos a un anuncio existente: /fotos <id> con las fotos adjuntas.")


@dataclass
class InboxResult:
    processed: int = 0
    listings_created: list[int] = field(default_factory=list)
    photos_stored: int = 0
    replies: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    last_offset: int | None = None

    def summary(self) -> dict:
        return {"processed": self.processed, "listings_created": len(self.listings_created), "photos": self.photos_stored,
                "errors": len(self.errors), "last_offset": self.last_offset}


def _num(v: str) -> int | None:
    v = re.sub(r"[^\d]", "", v or "")
    return int(v) if v else None


def parse_moto_command(text: str) -> RawListing | None:
    """Parsea '/moto a | b | c ...'. Devuelve None si no es el comando. Lanza ValueError si está mal formado."""
    if not text or not text.strip().lower().startswith("/moto"):
        return None
    body = text.strip()[5:].strip()
    parts = [p.strip() for p in body.split("|")]
    if len(parts) < 5:
        raise ValueError("faltan campos. " + HELP)
    make, model, year, km, price = parts[:5]
    location = parts[5] if len(parts) > 5 and parts[5] else None
    url = parts[6] if len(parts) > 6 and parts[6].startswith("http") else None
    seller = (parts[7] if len(parts) > 7 else "unknown").lower()
    seller = seller if seller in ("particular", "dealer") else "unknown"
    try:
        asking = D(_num(price) or 0)
    except InvalidOperation:
        asking = Decimal(0)
    if asking <= 0:
        raise ValueError("precio inválido. " + HELP)
    return RawListing(source="telegram", make=make, model=model, year=_num(year), kilometers=_num(km), asking_price=asking,
                      location=location, source_url=url, seller_type=seller, observed_at=datetime.now(timezone.utc),
                      external_id=None, raw={"via": "telegram"})


class TelegramInbox:
    def __init__(self, bot_token: str, transport: httpx.BaseTransport | None = None, allowed_chat_id: str | None = None):
        if not bot_token:
            raise ValueError("Telegram inbox requiere token.")
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._file_base = f"https://api.telegram.org/file/bot{bot_token}"
        self._client = httpx.Client(transport=transport, timeout=20.0)
        self.allowed_chat_id = str(allowed_chat_id) if allowed_chat_id else None

    # ---- API ---------------------------------------------------------------
    def get_updates(self, offset: int | None) -> list[dict]:
        params = {"timeout": 0, "limit": 100, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        r = self._client.get(f"{self._base}/getUpdates", params=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"getUpdates falló: {data.get('description')}")
        return data.get("result", [])

    def download_file(self, file_id: str) -> bytes | None:
        r = self._client.get(f"{self._base}/getFile", params={"file_id": file_id})
        data = r.json()
        if not data.get("ok"):
            return None
        path = data["result"].get("file_path")
        if not path:
            return None
        f = self._client.get(f"{self._file_base}/{path}")
        return f.content if f.status_code == 200 else None

    def reply(self, chat_id, text: str) -> None:
        try:
            self._client.post(f"{self._base}/sendMessage", json={"chat_id": chat_id, "text": text[:4000]})
        except httpx.HTTPError as exc:  # la respuesta es cortesía; no frena el procesamiento
            log.warning("no se pudo responder por Telegram: %s", exc)

    # ---- procesamiento -----------------------------------------------------
    def process(self, session: Session, rules, *, by: str = "telegram_inbox") -> InboxResult:
        from mototrade.modules.photos import PhotoError, PhotoService

        res = InboxResult()
        state = session.get(SystemState, OFFSET_KEY)
        offset = int(state.value.get("offset")) if state and state.value.get("offset") is not None else None
        updates = self.get_updates(offset)
        radar = RadarService(session, rules)
        photos = PhotoService(session)
        last_listing_by_chat: dict[str, int] = {}
        for upd in updates:
            res.last_offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if self.allowed_chat_id and chat_id != self.allowed_chat_id:
                res.errors.append(f"chat no autorizado: {chat_id}")
                continue
            text = msg.get("text") or msg.get("caption") or ""
            res.processed += 1
            try:
                listing_id: int | None = None
                if text.strip().lower().startswith("/fotos"):
                    m = re.search(r"/fotos\s+(\d+)", text.lower())
                    listing_id = int(m.group(1)) if m else last_listing_by_chat.get(chat_id)
                    if listing_id is None or session.get(Listing, listing_id) is None:
                        raise ValueError("indicá el id del anuncio: /fotos <id>")
                elif text.strip().lower().startswith("/moto"):
                    raw = parse_moto_command(text)
                    ing = radar.ingest(ManualSource([raw]), by=by)
                    if ing.errors:
                        raise ValueError("; ".join(ing.errors))
                    listing_id = (ing.created or ing.updated)[0]
                    if ing.created:
                        res.listings_created.append(listing_id)
                    last_listing_by_chat[chat_id] = listing_id
                elif text.strip().lower() in ("/start", "/help", "/ayuda"):
                    self.reply(chat_id, HELP)
                    res.replies.append("help")
                    continue
                elif msg.get("photo") and chat_id in last_listing_by_chat:
                    listing_id = last_listing_by_chat[chat_id]  # foto suelta tras un /moto reciente
                else:
                    self.reply(chat_id, "No entendí. " + HELP)
                    res.replies.append("unknown")
                    continue
                n_photos = 0
                if msg.get("photo"):
                    best = max(msg["photo"], key=lambda p: p.get("file_size", 0))
                    data = self.download_file(best["file_id"])
                    if data:
                        try:
                            photos.store(entity_type="listing", entity_id=listing_id, data=data, origin="telegram", by=by)
                            n_photos += 1
                        except PhotoError as exc:
                            res.errors.append(f"foto inválida: {exc}")
                res.photos_stored += n_photos
                l = session.get(Listing, listing_id)
                self.reply(chat_id, f"✅ Anuncio #{listing_id}: {l.make} {l.model} {l.year or ''} · {l.asking_price:,.0f}"
                                    f" · {n_photos} foto(s). Mandá más fotos con /fotos {listing_id}.")
                res.replies.append("ok")
            except ValueError as exc:
                res.errors.append(str(exc))
                self.reply(chat_id, f"⚠️ {exc}")
        if res.last_offset is not None:
            if state is None:
                state = SystemState(key=OFFSET_KEY, value={})
                session.add(state)
            state.value = {"offset": res.last_offset, "updated": datetime.now(timezone.utc).isoformat()}
            state.updated_by = by
        audit.record(session, action="telegram_inbox.processed", entity_type="listing", details=res.summary(), actor_id=by)
        return res
