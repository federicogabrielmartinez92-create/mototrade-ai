"""Enumeraciones del dominio. Se guardan como texto en la base (portables entre PostgreSQL y SQLite)."""
from enum import StrEnum


class ActorType(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class UserRole(StrEnum):
    ADMIN = "admin"        # todo, incluida configuración y aprobaciones financieras
    OPERATOR = "operator"  # operación diaria, carga de datos, aprobaciones de mensajes
    VIEWER = "viewer"      # solo lectura


class ListingSource(StrEnum):
    MANUAL = "manual"
    CSV_IMPORT = "csv_import"
    MERCADOLIBRE = "mercadolibre"
    FACEBOOK_MARKETPLACE = "facebook_marketplace"
    ROSARIOGARAGE = "rosariogarage"
    OTHER = "other"


class ListingStatus(StrEnum):
    NEW = "new"
    WATCHING = "watching"
    CONTACTED = "contacted"
    DISCARDED = "discarded"
    PURCHASED = "purchased"
    GONE = "gone"          # el anuncio desapareció de la fuente


class DealStage(StrEnum):
    OPPORTUNITY = "opportunity"
    VALUATED = "valuated"
    NEGOTIATING = "negotiating"
    INSPECTION = "inspection"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    PURCHASED = "purchased"
    LOST = "lost"
    DISCARDED = "discarded"


class InventoryStatus(StrEnum):
    ACQUIRED = "acquired"
    RECONDITIONING = "reconditioning"
    READY = "ready"
    PUBLISHED = "published"
    RESERVED = "reserved"
    SOLD = "sold"


class ConversationDirection(StrEnum):
    BUY = "buy"    # nosotros compramos (agente comprador)
    SELL = "sell"  # nosotros vendemos (agente vendedor)


class ConversationStatus(StrEnum):
    OPEN = "open"
    WAITING_COUNTERPART = "waiting_counterpart"
    WAITING_HUMAN = "waiting_human"
    HUMAN_TAKEOVER = "human_takeover"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"


class MessageSender(StrEnum):
    AGENT = "agent"            # borrador/mensaje generado por el agente
    OPERATOR = "operator"      # escrito por el humano
    COUNTERPART = "counterpart"  # la otra parte (texto NO confiable)


class MessageStatus(StrEnum):
    DRAFT = "draft"             # generado, pendiente de aprobación
    APPROVED = "approved"       # aprobado por humano, listo para enviar
    SENT = "sent"               # enviado por un canal autorizado
    RECEIVED = "received"       # recibido de la contraparte
    REJECTED = "rejected"       # el humano descartó el borrador
    BLOCKED = "blocked"         # bloqueado por política (límites, kill switch, horario)


class ApprovalKind(StrEnum):
    SEND_MESSAGE = "send_message"
    MAKE_OFFER = "make_offer"
    ACCEPT_PURCHASE = "accept_purchase"
    ACCEPT_SALE = "accept_sale"
    PUBLISH_LISTING = "publish_listing"
    PRICE_CHANGE = "price_change"
    PAYMENT = "payment"
    RESERVATION = "reservation"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class OutboundStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    BLOCKED = "blocked"


class RepairState(StrEnum):
    SUSPECTED = "suspected"      # hipótesis (p. ej. del inspector visual)
    OBSERVED = "observed"        # observado por humano o foto clara
    QUOTED = "quoted"
    APPROVED = "approved"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    DISCARDED = "discarded"


class FindingKind(StrEnum):
    OBSERVATION = "observation"  # visible y verificable en la foto/descripcion
    HYPOTHESIS = "hypothesis"    # sospecha que requiere inspección


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROVIDED = "provided"        # el vendedor lo entregó / se tiene copia
    VERIFIED_MANUAL = "verified_manual"  # verificado por el operador (no es verificación oficial)
    REJECTED = "rejected"
    NOT_APPLICABLE = "not_applicable"


class PublicationStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    PUBLISHED_MANUAL = "published_manual"   # el operador lo publicó a mano con el texto generado
    PUBLISHED_API = "published_api"         # publicado por integración autorizada (pendiente)
    PAUSED = "paused"
    CLOSED = "closed"


class VisitStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    DONE = "done"
    CANCELLED = "cancelled"


class LedgerKind(StrEnum):
    CAPITAL_IN = "capital_in"
    CAPITAL_OUT = "capital_out"
    PURCHASE = "purchase"
    EXPENSE = "expense"
    SALE = "sale"
    TAX = "tax"
    ADJUSTMENT = "adjustment"


class ExpenseCategory(StrEnum):
    PARTS = "parts"
    LABOR = "labor"
    TRANSFER = "transfer"
    LOGISTICS = "logistics"
    PREPARATION = "preparation"
    MARKETING = "marketing"
    TAX = "tax"
    OTHER = "other"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
