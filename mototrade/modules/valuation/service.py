"""Servicio de tasación: une comparables de la base, motor determinista y persistencia."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mototrade.config.rules import BusinessRules
from mototrade.core import audit
from mototrade.core.enums import ActorType
from mototrade.core.money import D
from mototrade.db.models import Comparable, Deal, InventoryItem, Listing, Valuation

from .engine import ComparablePoint, CostInputs, DealMath, MarketEstimate, compute_deal, estimate_market_price, \
    manual_market_estimate


class ValuationService:
    def __init__(self, session: Session, rules: BusinessRules):
        self.session = session
        self.rules = rules

    # ---- comparables ---------------------------------------------------------
    def fetch_comparables(self, make: str, model: str, year: int | None, *, exclude_listing_id: int | None = None,
                          year_tolerance: int = 1) -> list[ComparablePoint]:
        v = self.rules.valuation
        since = datetime.now(timezone.utc) - timedelta(days=v.comparables_max_age_days)
        q = select(Comparable).where(
            func.lower(Comparable.make) == make.strip().lower(),
            func.lower(Comparable.model) == model.strip().lower(),
            Comparable.observed_at >= since,
        )
        if year is not None:
            q = q.where(Comparable.year.between(year - year_tolerance, year + year_tolerance))
        if exclude_listing_id is not None:
            q = q.where((Comparable.listing_id.is_(None)) | (Comparable.listing_id != exclude_listing_id))
        rows = list(self.session.execute(q).scalars())
        # Si hay pocos, ampliamos la tolerancia de año una vez (y lo informamos vía notes del estimador).
        if len(rows) < v.min_comparables and year is not None and year_tolerance < 2:
            return self.fetch_comparables(make, model, year, exclude_listing_id=exclude_listing_id, year_tolerance=2)
        return [
            ComparablePoint(price=D(r.price), kind=r.kind, observed_at=r.observed_at, year=r.year,
                            kilometers=r.kilometers, source=r.source, source_ref=r.source_ref, ref_id=r.id)
            for r in rows
        ]

    def add_comparable(self, *, kind: str, make: str, model: str, year: int | None, kilometers: int | None,
                       price: Decimal, source: str, source_ref: str | None = None, location: str | None = None,
                       observed_at: datetime | None = None, listing_id: int | None = None, version: str = "",
                       seller_type: str = "unknown", condition: str | None = None,
                       by: str = "operator") -> Comparable:
        if kind not in ("asking", "sold"):
            raise ValueError("kind debe ser 'asking' o 'sold'")
        if seller_type not in ("particular", "dealer", "unknown"):
            raise ValueError("seller_type debe ser particular, dealer o unknown")
        row = Comparable(kind=kind, make=make.strip(), model=model.strip(), version=(version or "").strip(), year=year,
                         kilometers=kilometers, price=D(price), source=source, source_ref=source_ref, location=location,
                         seller_type=seller_type, condition=condition,
                         observed_at=observed_at or datetime.now(timezone.utc), listing_id=listing_id)
        self.session.add(row)
        self.session.flush()
        audit.record(self.session, action="comparable.added", entity_type="comparable", entity_id=row.id,
                     details={"kind": kind, "make": make, "model": model, "year": year, "price": price},
                     actor_type=ActorType.HUMAN, actor_id=by)
        return row

    # ---- tasación ------------------------------------------------------------
    def estimate(self, make: str, model: str, year: int | None, *, exclude_listing_id: int | None = None,
                 manual_market_price: Decimal | None = None) -> MarketEstimate:
        if manual_market_price is not None:
            return manual_market_estimate(D(manual_market_price))
        comps = self.fetch_comparables(make, model, year, exclude_listing_id=exclude_listing_id)
        return estimate_market_price(comps, self.rules)

    def valuate_deal(self, deal: Deal, *, cost_inputs: CostInputs | None = None,
                     purchase_price: Decimal | None = None, manual_market_price: Decimal | None = None,
                     available_capital: Decimal | None = None, by: str = "valuation_engine",
                     notes: str = "") -> tuple[Valuation, MarketEstimate, DealMath]:
        listing = deal.listing
        if listing is None:
            raise ValueError("El deal no tiene anuncio asociado.")
        estimate = self.estimate(listing.make, listing.model, listing.year, exclude_listing_id=listing.id,
                                 manual_market_price=manual_market_price)
        price = D(purchase_price if purchase_price is not None else listing.asking_price)
        math = compute_deal(estimate.market_price, price, cost_inputs or CostInputs(), self.rules,
                            available_capital=available_capital)
        row = self._persist(estimate, math, make=listing.make, model=listing.model, year=listing.year,
                            km=listing.kilometers, deal_id=deal.id, by=by, notes=notes)
        if deal.stage == "opportunity":
            deal.stage = "valuated"
        return row, estimate, math

    def valuate_item(self, item: InventoryItem, *, cost_inputs: CostInputs | None = None,
                     manual_market_price: Decimal | None = None, by: str = "valuation_engine",
                     notes: str = "") -> tuple[Valuation, MarketEstimate, DealMath]:
        estimate = self.estimate(item.make, item.model, item.year, manual_market_price=manual_market_price)
        math = compute_deal(estimate.market_price, D(item.purchase_price), cost_inputs or CostInputs(), self.rules)
        row = self._persist(estimate, math, make=item.make, model=item.model, year=item.year, km=item.kilometers,
                            inventory_item_id=item.id, by=by, notes=notes)
        return row, estimate, math

    def _persist(self, estimate: MarketEstimate, math: DealMath, *, make: str, model: str, year: int | None,
                 km: int | None, deal_id: int | None = None, inventory_item_id: int | None = None,
                 by: str, notes: str) -> Valuation:
        all_notes = list(estimate.notes) + list(math.warnings)
        if notes:
            all_notes.append(notes)
        row = Valuation(
            deal_id=deal_id, inventory_item_id=inventory_item_id, make=make, model=model, year=year, kilometers=km,
            market_price=math.market_price, quick_sale_price=math.quick_sale_price,
            suggested_list_price=math.suggested_list_price, initial_offer=math.initial_offer,
            max_purchase_price=math.max_purchase_price, costs=math.costs_as_str(), total_costs=math.total_costs,
            expected_profit=math.expected_profit, expected_roi=math.expected_roi, scenarios=math.scenarios_as_dict(),
            confidence=estimate.confidence, confidence_score=estimate.confidence_score, basis=estimate.basis,
            comparables_used=estimate.comparables_used, sources=estimate.sources, rules_hash=self.rules.rules_hash,
            computed_by=by, notes="\n".join(all_notes),
        )
        self.session.add(row)
        self.session.flush()
        audit.record(
            self.session, action="valuation.computed", entity_type="valuation", entity_id=row.id,
            details={"deal_id": deal_id, "inventory_item_id": inventory_item_id, "market_price": math.market_price,
                     "max_purchase": math.max_purchase_price, "roi": math.expected_roi, "confidence": estimate.confidence,
                     "basis": estimate.basis, "comparables": estimate.comparables_used},
            actor_type=ActorType.SYSTEM, actor_id=by, rules_hash=self.rules.rules_hash,
        )
        return row
