"""Módulo 7 — Administrador de publicaciones.

FUNCIONA: generación de texto de aviso (plantilla determinista, sin inventar datos),
borradores por canal, aprobación PUBLISH_LISTING y registro de publicación manual.
PENDIENTE: publicación automática por API (Mercado Libre requiere paquete de
publicación contratado con ejecutivo de cuenta; Marketplace y RosarioGarage no
tienen API). Solo se usan fotos reales (`Photo.is_authentic`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from mototrade.core import approvals, audit
from mototrade.core.enums import ActorType, ApprovalKind, InventoryStatus, PublicationStatus
from mototrade.core.money import D, fmt_ars
from mototrade.db.models import InventoryItem, Photo, Publication, Repair


def generate_ad_copy(item: InventoryItem, *, repairs_done: list[str], price: Decimal, extras: list[str] | None = None,
                     business_name: str = "") -> tuple[str, str]:
    """Texto del aviso a partir de datos REALES de la ficha. No agrega atributos no cargados."""
    year = f" {item.year}" if item.year else ""
    title = f"{item.make} {item.model}{year}".strip()
    lines = [f"{title} — {fmt_ars(price)}"]
    if item.kilometers is not None:
        lines.append(f"• {item.kilometers:,} km".replace(",", "."))
    if item.color:
        lines.append(f"• Color {item.color}")
    if repairs_done:
        lines.append("• Reacondicionada: " + ", ".join(repairs_done))
    for e in extras or []:
        lines.append(f"• {e}")
    lines.append("• Lista para transferir. Documentación en orden para firmar 08.")
    lines.append("• Se puede ver en Rosario con cita previa. Consultá por permuta.")
    if business_name:
        lines.append(f"{business_name}")
    return title, "\n".join(lines)


class PublicationsService:
    def __init__(self, session: Session, business_name: str = ""):
        self.session = session
        self.business_name = business_name

    def draft(self, item: InventoryItem, *, channel: str, price: Decimal | None = None, extras: list[str] | None = None,
              by: str = "operator") -> Publication:
        if item.status == InventoryStatus.SOLD:
            raise ValueError("La moto ya está vendida.")
        price = D(price if price is not None else item.list_price or 0)
        if price <= 0:
            raise ValueError("Precio de publicación inválido.")
        if item.min_sale_price is not None and price < D(item.min_sale_price):
            raise ValueError("El precio de publicación no puede ser menor al mínimo autorizado.")
        done = [r.issue for r in self.session.execute(
            select(Repair).where(Repair.inventory_item_id == item.id, Repair.state == "done")).scalars()]
        photos = [p.id for p in self.session.execute(
            select(Photo).where(Photo.entity_type == "inventory_item", Photo.entity_id == item.id, Photo.is_authentic.is_(True))
        ).scalars()]
        title, body = generate_ad_copy(item, repairs_done=done, price=price, extras=extras, business_name=self.business_name)
        pub = Publication(inventory_item_id=item.id, channel=channel, title=title, body=body, price=price,
                          photo_ids=photos, status=PublicationStatus.PENDING_APPROVAL)
        self.session.add(pub)
        self.session.flush()
        req = approvals.request(self.session, kind=ApprovalKind.PUBLISH_LISTING, entity_type="publication", entity_id=pub.id,
                                payload={"channel": channel, "price": str(price), "title": title},
                                summary=f"Publicar {title} en {channel} a {fmt_ars(price)}", requested_by=by, amount=price,
                                actor_type=ActorType.HUMAN)
        audit.record(self.session, action="publication.drafted", entity_type="publication", entity_id=pub.id,
                     details={"channel": channel, "price": price, "photos": len(photos), "approval_id": req.id},
                     actor_type=ActorType.HUMAN, actor_id=by)
        return pub

    def mark_published_manual(self, pub: Publication, *, by: str, external_url: str | None = None) -> None:
        """El operador publicó a mano el texto aprobado en el canal y registra la URL."""
        from mototrade.db.models import ApprovalRequest

        ok = self.session.execute(
            select(ApprovalRequest).where(ApprovalRequest.entity_type == "publication", ApprovalRequest.entity_id == str(pub.id),
                                          ApprovalRequest.status == "approved")
        ).scalars().first()
        if ok is None:
            raise ValueError("La publicación no tiene aprobación PUBLISH_LISTING aprobada.")
        pub.status = PublicationStatus.PUBLISHED_MANUAL
        pub.external_url = external_url
        pub.published_at = datetime.now(timezone.utc)
        item = self.session.get(InventoryItem, pub.inventory_item_id)
        if item and item.status in (InventoryStatus.READY, InventoryStatus.RECONDITIONING, InventoryStatus.ACQUIRED):
            item.status = InventoryStatus.PUBLISHED
        audit.record(self.session, action="publication.published_manual", entity_type="publication", entity_id=pub.id,
                     details={"url": external_url}, actor_type=ActorType.HUMAN, actor_id=by)

    def record_inquiry(self, pub: Publication, n: int = 1) -> None:
        pub.inquiries_count += n

    def close(self, pub: Publication, *, by: str) -> None:
        pub.status = PublicationStatus.CLOSED
        audit.record(self.session, action="publication.closed", entity_type="publication", entity_id=pub.id,
                     actor_type=ActorType.HUMAN, actor_id=by)
