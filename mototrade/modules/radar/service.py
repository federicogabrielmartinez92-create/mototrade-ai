"""Radar: ingestión, deduplicación, cambios de precio y oportunidades."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mototrade.config.rules import BusinessRules
from mototrade.core import audit, untrusted
from mototrade.core.enums import ActorType, DealStage, ListingStatus
from mototrade.core.money import D, ONE, ZERO
from mototrade.db.models import Comparable, Deal, Listing, ListingPriceHistory
from mototrade.modules.anomalies import AnomalyDetector, ComparableRecord, OpportunityAnalysis, TargetListing
from mototrade.control.settings_model import ControlSettings
from mototrade.modules.valuation.engine import CostInputs

from .sources import ListingSourceAdapter, RawListing


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", s)


def fingerprint(make: str, model: str, year: int | None, kilometers: int | None, price: Decimal) -> str:
    """Huella difusa para detectar el mismo anuncio en distintas fuentes o reposteado.
    Km se redondea a 1.000 y precio a 50.000 para tolerar pequeñas diferencias."""
    km_bucket = (kilometers // 1000) if kilometers is not None else -1
    price_bucket = int(D(price) // 50_000)
    body = f"{_slug(make)}|{_slug(model)}|{year}|{km_bucket}|{price_bucket}"
    return hashlib.sha1(body.encode()).hexdigest()[:40]


@dataclass
class IngestResult:
    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    price_changes: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    flagged: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {k: (len(v) if isinstance(v, list) else v) for k, v in self.__dict__.items()}


class RadarService:
    def __init__(self, session: Session, rules: BusinessRules, settings: ControlSettings | None = None):
        self.session = session
        self.rules = rules
        if settings is None:
            from mototrade.control import ControlCenter

            settings = ControlCenter(session).load()
        self.settings = settings

    # ---- watchlist -----------------------------------------------------------
    def in_watchlist(self, make: str, model: str) -> bool:
        return any(_slug(w.make) == _slug(make) and _slug(w.model) == _slug(model) for w in self.rules.radar.watchlist)

    def in_target_area(self, location: str | None) -> bool | None:
        if not location:
            return None
        loc = _slug(location)
        return any(_slug(l) in loc or loc in _slug(l) for l in self.rules.radar.locations)

    # ---- ingestión -----------------------------------------------------------
    def ingest(self, source: ListingSourceAdapter, *, by: str = "radar") -> IngestResult:
        result = IngestResult()
        for raw in source.fetch():
            try:
                self._ingest_one(raw, result, by=by)
            except Exception as exc:  # una fila mala no frena el lote
                result.errors.append(f"{raw.external_id or raw.title or '?'}: {exc}")
        audit.record(self.session, action="radar.ingest", entity_type="listing",
                     details={"source": source.name, "simulated": getattr(source, "is_simulated", False),
                              **result.summary()}, actor_type=ActorType.SYSTEM, actor_id=by)
        return result

    def _find_existing(self, raw: RawListing, fp: str) -> tuple[Listing | None, str]:
        if raw.external_id:
            row = self.session.execute(
                select(Listing).where(Listing.source == raw.source, Listing.external_id == raw.external_id)
            ).scalar_one_or_none()
            if row:
                return row, "external_id"
        if raw.source_url:
            row = self.session.execute(select(Listing).where(Listing.source_url == raw.source_url)).scalar_one_or_none()
            if row:
                return row, "url"
        row = self.session.execute(
            select(Listing).where(Listing.fingerprint == fp, Listing.status != ListingStatus.GONE)
        ).scalars().first()
        if row:
            return row, "fingerprint"
        return None, ""

    def _ingest_one(self, raw: RawListing, result: IngestResult, *, by: str) -> Listing:
        if not raw.make or not raw.model:
            raise ValueError("marca y modelo son obligatorios")
        if D(raw.asking_price) <= 0:
            raise ValueError("precio inválido")
        now = raw.observed_at or datetime.now(timezone.utc)
        fp = fingerprint(raw.make, raw.model, raw.year, raw.kilometers, raw.asking_price)
        scan = untrusted.scan(f"{raw.title}\n{raw.description}")
        existing, how = self._find_existing(raw, fp)

        if existing:
            old_price = D(existing.asking_price)
            new_price = D(raw.asking_price)
            existing.last_seen_at = now
            if how == "fingerprint" and existing.source != raw.source:
                result.duplicates.append({"listing_id": existing.id, "source": raw.source, "matched_by": how})
            if new_price != old_price:
                existing.asking_price = new_price
                existing.fingerprint = fingerprint(raw.make, raw.model, raw.year, raw.kilometers, new_price)
                self.session.add(ListingPriceHistory(listing_id=existing.id, price=new_price, observed_at=now))
                change = ((new_price - old_price) / old_price).quantize(D("0.0001")) if old_price else ZERO
                result.price_changes.append({"listing_id": existing.id, "old": str(old_price), "new": str(new_price),
                                             "change_pct": str(change)})
                audit.record(self.session, action="listing.price_changed", entity_type="listing", entity_id=existing.id,
                             details={"old": old_price, "new": new_price, "change_pct": change}, actor_id=by)
            if raw.description and raw.description != existing.description:
                existing.description = scan.text
            existing.injection_flags = scan.flags
            result.updated.append(existing.id)
            return existing

        row = Listing(
            source=raw.source, external_id=raw.external_id, source_url=raw.source_url, fingerprint=fp,
            make=raw.make.strip(), model=raw.model.strip(), version=raw.version or "", year=raw.year,
            kilometers=raw.kilometers, asking_price=D(raw.asking_price), location=raw.location,
            title=untrusted.normalize(raw.title)[:255], description=scan.text, seller_alias=raw.seller_alias,
            photos=list(raw.photos), raw=raw.raw, status=ListingStatus.NEW, first_seen_at=now, last_seen_at=now,
            injection_flags=scan.flags, seller_type=raw.seller_type or "unknown", condition_declared=raw.condition_declared,
            published_at=raw.published_at,
        )
        self.session.add(row)
        self.session.flush()
        self.session.add(ListingPriceHistory(listing_id=row.id, price=row.asking_price, observed_at=now))
        if scan.suspicious:
            result.flagged.append({"listing_id": row.id, "flags": scan.flags})
        audit.record(self.session, action="listing.created", entity_type="listing", entity_id=row.id,
                     details={"source": raw.source, "make": row.make, "model": row.model, "price": row.asking_price,
                              "flags": scan.flags}, actor_id=by)
        result.created.append(row.id)
        return row

    # ---- oportunidades (detector de anomalías, incremento 2) -------------------
    def _comparable_records(self, listing: Listing) -> list[ComparableRecord]:
        v = self.rules.valuation
        since = datetime.now(timezone.utc) - timedelta(days=v.comparables_max_age_days)
        q = select(Comparable).where(func.lower(Comparable.make) == listing.make.strip().lower(),
                                     func.lower(Comparable.model) == listing.model.strip().lower(),
                                     Comparable.observed_at >= since,
                                     (Comparable.listing_id.is_(None)) | (Comparable.listing_id != listing.id))
        rows = list(self.session.execute(q).scalars())
        if listing.year is not None:
            near = [r for r in rows if r.year is not None and abs(r.year - listing.year) <= 1]
            if len(near) < v.min_comparables:
                near = [r for r in rows if r.year is not None and abs(r.year - listing.year) <= 2] or near
            rows = near
        return [ComparableRecord(price=D(r.price), kind=r.kind, observed_at=r.observed_at, year=r.year,
                                 kilometers=r.kilometers, version=r.version or "", seller_type=r.seller_type or "unknown",
                                 condition=r.condition, source=r.source, source_ref=r.source_ref, ref_id=r.id) for r in rows]

    def _target(self, listing: Listing) -> TargetListing:
        hist = [(h.observed_at, D(h.price)) for h in listing.price_history]
        return TargetListing(make=listing.make, model=listing.model, asking_price=D(listing.asking_price),
                             version=listing.version or "", year=listing.year, kilometers=listing.kilometers,
                             location=listing.location, seller_type=listing.seller_type or "unknown",
                             condition_declared=listing.condition_declared, photos_count=len(listing.photos or []),
                             published_at=listing.published_at, first_seen_at=listing.first_seen_at, price_history=hist,
                             injection_flags=list(listing.injection_flags or []), description=listing.description)

    def model_allowed(self, make: str, model: str) -> tuple[bool, str]:
        s = self.settings.search
        if _slug(make) in {_slug(m) for m in s.excluded_makes}:
            return False, "marca excluida"
        if any(_slug(e.make) == _slug(make) and _slug(e.model) == _slug(model) for e in s.excluded_models):
            return False, "modelo excluido"
        return True, ""

    def analyze_listing(self, listing: Listing, *, now: datetime | None = None) -> OpportunityAnalysis:
        """Análisis determinista completo (sin IA): comparables ajustados, descuento, modo A/B, riesgos, puntaje."""
        from mototrade.modules.finance.capital import capital_position, purchase_budget_for_unit
        from mototrade.modules.parts import PartsService

        pos = capital_position(self.session, self.settings.capital)
        budget = purchase_budget_for_unit(pos, self.settings.capital)
        deal = self.session.execute(select(Deal).where(Deal.listing_id == listing.id)).scalars().first()
        ci = PartsService(self.session).cost_inputs_for(deal_id=deal.id) if deal else CostInputs()
        det = AnomalyDetector(self.rules, self.settings.search, self.settings.capital)
        return det.analyze(self._target(listing), self._comparable_records(listing), cost_inputs=ci,
                           purchase_budget=budget if pos.can_buy_another else ZERO, now=now)

    def score_listing(self, listing: Listing, *, available_capital: Decimal | None = None) -> tuple[int, dict]:
        """Puntaje 0–100 con desglose explícito (compatibilidad con incremento 1)."""
        an = self.analyze_listing(listing)
        breakdown = dict(an.breakdown)
        if self.in_watchlist(listing.make, listing.model):
            breakdown["watchlist"] = 10
        area = self.in_target_area(listing.location)
        breakdown["area"] = 5 if area else (2 if area is None else 0)
        score = max(0, min(100, an.score + breakdown.get("watchlist", 0) + breakdown["area"]))
        breakdown.update({"classification": an.classification, "mode": an.mode, "discount_pct": str(an.discount_pct),
                          "market_basis": an.market.basis, "comparables": an.comparables_used,
                          "expected_roi_at_asking": str(an.math.expected_roi) if an.math else None,
                          "max_purchase": str(an.math.max_purchase_price) if an.math else None,
                          "target_price": str(an.target_price) if an.target_price is not None else None,
                          "risk_level": an.risk_level, "risk_flags": an.risk_flags, "action": an.recommended_action})
        return score, breakdown

    def create_opportunity(self, listing: Listing, *, available_capital: Decimal | None = None,
                           by: str = "radar") -> Deal | None:
        """Crea (o re-puntúa) un Deal en etapa 'opportunity' si el anuncio califica. Idempotente."""
        ok, why = self.model_allowed(listing.make, listing.model)
        deal = self.session.execute(select(Deal).where(Deal.listing_id == listing.id)).scalars().first()
        if not ok:
            if deal is None:
                return None
        an = self.analyze_listing(listing)
        score, breakdown = self.score_listing(listing)
        qualifies = ok and (
            an.classification in ("interesting", "extraordinary")
            or (an.mode == "negotiable" and score >= self.rules.radar.opportunity_min_score)
            or score >= self.rules.radar.opportunity_min_score
        )
        if not self.in_watchlist(listing.make, listing.model) and not self.settings.search.allow_any_model:
            qualifies = False
            breakdown["skipped"] = "modelo fuera de la lista prioritaria y allow_any_model=False"
        elif not self.in_watchlist(listing.make, listing.model) and an.classification != "extraordinary" and \
                not (an.mode == "negotiable" and an.math_at_target and an.math_at_target.meets_minimum_roi):
            qualifies = False
            breakdown["skipped"] = "modelo fuera de la lista: solo extraordinarias o negociables rentables"
        if deal is None:
            if not qualifies:
                return None
            deal = Deal(listing_id=listing.id, stage=DealStage.OPPORTUNITY)
            self.session.add(deal)
            listing.status = ListingStatus.WATCHING
            action = "deal.created"
        else:
            action = "deal.rescored"
        deal.opportunity_score = score
        deal.score_breakdown = breakdown
        deal.opportunity_class = an.classification
        deal.opportunity_mode = an.mode
        deal.discount_pct = an.discount_pct
        deal.risk_flags = an.risk_flags
        deal.analysis = an.as_dict()
        deal.analyzed_at = an.analyzed_at
        self.session.flush()
        audit.record(self.session, action=action, entity_type="deal", entity_id=deal.id,
                     details={"listing_id": listing.id, "score": score, "classification": an.classification,
                              "mode": an.mode, "discount_pct": an.discount_pct, "risk": an.risk_flags,
                              "action": an.recommended_action}, actor_id=by, rules_hash=self.rules.rules_hash)
        return deal

    def scan_opportunities(self, *, available_capital: Decimal | None = None, by: str = "radar") -> list[Deal]:
        """Puntúa todos los anuncios activos y crea oportunidades. Idempotente."""
        out: list[Deal] = []
        q = select(Listing).where(Listing.status.in_([ListingStatus.NEW, ListingStatus.WATCHING]))
        for listing in self.session.execute(q).scalars():
            deal = self.create_opportunity(listing, available_capital=available_capital, by=by)
            if deal:
                out.append(deal)
        return out

    def extraordinary_alerts(self) -> list[Deal]:
        return list(self.session.execute(
            select(Deal).where(Deal.opportunity_class == "extraordinary",
                               Deal.stage.in_([DealStage.OPPORTUNITY, DealStage.VALUATED, DealStage.NEGOTIATING]))
            .order_by(Deal.opportunity_score.desc())
        ).scalars())

    def mark_gone(self, seen_external_ids: set[str], source: str, *, by: str = "radar") -> int:
        """Marca como 'gone' los anuncios de una fuente que ya no aparecen en la última pasada."""
        n = 0
        q = select(Listing).where(Listing.source == source, Listing.status.in_([ListingStatus.NEW, ListingStatus.WATCHING]))
        for row in self.session.execute(q).scalars():
            if row.external_id and row.external_id not in seen_external_ids:
                row.status = ListingStatus.GONE
                n += 1
        if n:
            audit.record(self.session, action="listing.marked_gone", entity_type="listing", details={"source": source, "count": n}, actor_id=by)
        return n

    # ---- consultas -----------------------------------------------------------
    def price_drop_alerts(self, result: IngestResult) -> list[dict]:
        threshold = -self.rules.radar.notify_price_drop_pct
        return [c for c in result.price_changes if D(c["change_pct"]) <= threshold]

    def counts(self) -> dict[str, int]:
        rows = self.session.execute(select(Listing.status, func.count()).group_by(Listing.status)).all()
        return {s: n for s, n in rows}
