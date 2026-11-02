"""Módulo 4 — Auditor documental.

El sistema NO verifica nada oficialmente (no existe API pública de DNRPA). Lo que
hace: mantiene el checklist de requisitos para transferir una moto usada en
Argentina/Santa Fe, registra evidencia cargada por el operador y BLOQUEA la compra
hasta que los ítems bloqueantes estén marcados como verificados manualmente.

Requisitos de referencia (11/2026; confirmar con el Registro Seccional):
- Título del automotor y cédula (o título digital).
- Informe de dominio (solicitud online en www2.jus.gov.ar → "Informes Online").
- Formulario 08 / 08D precargado y firmas certificadas.
- Verificación física (F.12) para modelos 2004 en adelante (vigencia limitada).
- Identidad del titular (DNI) coincidente con el título.
- Prendas / inhibiciones: surgen del informe de dominio.
- Deudas de patentes e infracciones: ya no bloquean la transferencia, pero conviene conocerlas.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from mototrade.core import audit
from mototrade.core.enums import ActorType, DocumentStatus
from mototrade.db.models import Deal, DocumentRequirement

PURCHASE_CHECKLIST: list[dict] = [
    {"code": "titulo", "label": "Título del automotor (físico o digital) a nombre del vendedor", "blocking": True},
    {"code": "dni_titular", "label": "DNI del titular coincide con el título", "blocking": True},
    {"code": "informe_dominio", "label": "Informe de dominio sin prendas, embargos ni inhibiciones", "blocking": True},
    {"code": "verificacion_fisica", "label": "Verificación física (F.12) vigente, si el modelo lo requiere", "blocking": True},
    {"code": "numeros_coinciden", "label": "Número de motor y chasis coinciden con el título", "blocking": True},
    {"code": "formulario_08", "label": "Formulario 08/08D firmado y certificado", "blocking": True},
    {"code": "cedula", "label": "Cédula verde/azul (si existe)", "blocking": False},
    {"code": "libre_deuda_patentes", "label": "Deuda de patentes conocida (no bloquea; negociar)", "blocking": False},
    {"code": "libre_deuda_infracciones", "label": "Infracciones conocidas (no bloquea; negociar)", "blocking": False},
    {"code": "service_manual", "label": "Manual / service / llaves de repuesto", "blocking": False},
]


class DocumentsService:
    def __init__(self, session: Session):
        self.session = session

    def init_checklist(self, *, deal_id: int | None = None, inventory_item_id: int | None = None,
                       by: str = "system") -> list[DocumentRequirement]:
        existing = self._rows(deal_id=deal_id, inventory_item_id=inventory_item_id)
        if existing:
            return existing
        rows = []
        for item in PURCHASE_CHECKLIST:
            rows.append(DocumentRequirement(deal_id=deal_id, inventory_item_id=inventory_item_id, code=item["code"],
                                            label=item["label"], required=True, blocking=item["blocking"],
                                            status=DocumentStatus.PENDING))
        self.session.add_all(rows)
        self.session.flush()
        audit.record(self.session, action="documents.checklist_created", entity_type="deal" if deal_id else "inventory_item",
                     entity_id=deal_id or inventory_item_id, details={"items": len(rows)}, actor_id=by)
        return rows

    def _rows(self, *, deal_id=None, inventory_item_id=None) -> list[DocumentRequirement]:
        q = select(DocumentRequirement)
        q = q.where(DocumentRequirement.deal_id == deal_id) if deal_id is not None else q.where(DocumentRequirement.inventory_item_id == inventory_item_id)
        return list(self.session.execute(q.order_by(DocumentRequirement.id)).scalars())

    def set_status(self, req: DocumentRequirement, status: DocumentStatus | str, *, by: str, notes: str = "",
                   evidence_path: str | None = None) -> None:
        if str(status) not in {s.value for s in DocumentStatus}:
            raise ValueError(f"Estado inválido: {status}")
        req.status = str(status)
        req.notes = notes
        if evidence_path:
            req.evidence_path = evidence_path
        if status == DocumentStatus.VERIFIED_MANUAL:
            req.verified_by = by
            req.verified_at = datetime.now(timezone.utc)
        audit.record(self.session, action="document.status", entity_type="document_requirement", entity_id=req.id,
                     details={"code": req.code, "status": str(status)}, actor_type=ActorType.HUMAN, actor_id=by)
        # Sincroniza la bandera del deal
        if req.deal_id:
            deal = self.session.get(Deal, req.deal_id)
            if deal:
                deal.documents_ok = self.is_clear(deal_id=req.deal_id)

    def is_clear(self, *, deal_id=None, inventory_item_id=None) -> bool:
        rows = self._rows(deal_id=deal_id, inventory_item_id=inventory_item_id)
        if not rows:
            return False
        return all(r.status in (DocumentStatus.VERIFIED_MANUAL, DocumentStatus.NOT_APPLICABLE)
                   for r in rows if r.blocking)

    def blockers(self, *, deal_id=None, inventory_item_id=None) -> list[str]:
        rows = self._rows(deal_id=deal_id, inventory_item_id=inventory_item_id)
        return [r.label for r in rows if r.blocking and r.status not in (DocumentStatus.VERIFIED_MANUAL, DocumentStatus.NOT_APPLICABLE)]
