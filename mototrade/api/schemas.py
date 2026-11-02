from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    token: str
    username: str
    role: str


class ListingIn(BaseModel):
    seller_type: str = "unknown"
    condition_declared: str | None = None
    published_at: datetime | None = None
    source: str = "manual"
    make: str
    model: str
    asking_price: Decimal = Field(gt=0)
    version: str = ""
    year: int | None = None
    kilometers: int | None = Field(default=None, ge=0)
    location: str | None = None
    source_url: str | None = None
    external_id: str | None = None
    title: str = ""
    description: str = ""
    seller_alias: str | None = None
    photos: list[str] = []


class ListingOut(ORM):
    id: int
    source: str
    make: str
    model: str
    year: int | None
    kilometers: int | None
    asking_price: Decimal
    location: str | None
    status: str
    injection_flags: list
    first_seen_at: datetime
    last_seen_at: datetime


class IngestOut(BaseModel):
    created: list[int]
    updated: list[int]
    price_changes: list[dict]
    duplicates: list[dict]
    flagged: list[dict]
    errors: list[str]


class DealOut(ORM):
    id: int
    listing_id: int | None
    stage: str
    opportunity_score: int | None
    score_breakdown: dict
    opportunity_class: str | None = None
    opportunity_mode: str | None = None
    discount_pct: Decimal | None = None
    risk_flags: list = []
    analyzed_at: datetime | None = None
    authorized_max_price: Decimal | None
    current_offer: Decimal | None
    documents_ok: bool
    mechanic_ok: bool


class AuthorizeIn(BaseModel):
    max_price: Decimal = Field(gt=0)


class ValuateIn(BaseModel):
    purchase_price: Decimal | None = None
    manual_market_price: Decimal | None = None
    repair_parts: Decimal = Decimal(0)
    repair_labor: Decimal = Decimal(0)
    transfer: Decimal | None = None
    logistics: Decimal | None = None
    preparation: Decimal | None = None
    marketing: Decimal | None = None
    notes: str = ""


class ValuationOut(ORM):
    id: int
    market_price: Decimal
    quick_sale_price: Decimal
    suggested_list_price: Decimal
    initial_offer: Decimal
    max_purchase_price: Decimal
    costs: dict
    total_costs: Decimal
    expected_profit: Decimal
    expected_roi: Decimal
    scenarios: dict
    confidence: str
    confidence_score: Decimal
    basis: str
    comparables_used: int
    sources: list
    rules_hash: str
    notes: str
    created_at: datetime


class ApprovalOut(ORM):
    id: int
    kind: str
    entity_type: str
    entity_id: str | None
    summary: str
    amount: Decimal | None
    requested_by: str
    status: str
    created_at: datetime
    expires_at: datetime | None


class DecideIn(BaseModel):
    approve: bool
    note: str = ""


class IncomingIn(BaseModel):
    text: str
    external_message_id: str | None = None


class MessageOut(ORM):
    id: int
    sender: str
    status: str
    text: str
    proposed_amount: Decimal | None
    intent: str | None
    created_at: datetime
    approval_id: int | None
    injection_flags: list


class ConversationOut(ORM):
    id: int
    direction: str
    channel: str
    deal_id: int | None
    inventory_item_id: int | None
    status: str
    rounds: int
    agent_enabled: bool
    last_agent_offer: Decimal | None
    last_counterpart_offer: Decimal | None


class AgentStepOut(BaseModel):
    blocked: bool
    intent: str
    amount: Decimal | None
    rationale: str
    verdict: dict
    message: MessageOut | None
    approval_id: int | None
    deal_approval_id: int | None


class InventoryOut(ORM):
    id: int
    make: str
    model: str
    year: int | None
    kilometers: int | None
    purchase_price: Decimal
    status: str
    list_price: Decimal | None
    min_sale_price: Decimal | None
    sold_price: Decimal | None
    acquired_at: datetime


class KillSwitchIn(BaseModel):
    engaged: bool
    reason: str = ""


class SettingsUpdateIn(BaseModel):
    values: dict


class PhotoOut(ORM):
    id: int
    entity_type: str
    entity_id: int
    sha256: str | None
    size_bytes: int | None
    origin: str
    caption: str
