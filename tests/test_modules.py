"""Tests de los módulos: precios dinámicos, permutas, analítica, inspección, repuestos, documentos, publicaciones, CRM."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mototrade.core import approvals
from mototrade.core.enums import ApprovalKind, DocumentStatus, UserRole
from mototrade.core.money import D
from mototrade.db.models import Deal, InventoryItem, Listing, Publication
from mototrade.modules.analytics import AnalyticsService
from mototrade.modules.crm import CrmService
from mototrade.modules.documents import DocumentsService, PURCHASE_CHECKLIST
from mototrade.modules.finance import FinanceService
from mototrade.modules.inspection import FakeVisionInspector, InspectionService
from mototrade.modules.logistics import LogisticsService
from mototrade.modules.parts import PartsService, compare_quotes
from mototrade.modules.pricing import PricingService, recommend_price
from mototrade.modules.publications import PublicationsService, generate_ad_copy
from mototrade.modules.tradein import TradeInService
from mototrade.modules.valuation import ValuationService

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _item(session, days_ago=0, **kw):
    base = dict(make="Honda", model="Wave 110", year=2022, kilometers=12000, purchase_price=D(1_500_000),
                acquired_at=NOW - timedelta(days=days_ago), status="ready", list_price=D(2_400_000),
                min_sale_price=D(2_100_000))
    base.update(kw)
    item = InventoryItem(**base)
    session.add(item)
    session.flush()
    return item


# ---- precios dinámicos -------------------------------------------------------
def test_pricing_rules(rules):
    a = recommend_price(current_price=D(2_400_000), original_price=D(2_400_000), min_price=D(2_100_000), days_in_stock=5,
                        inquiries_last_7d=5, market_price=None, rules=rules)
    assert not a.should_change
    b = recommend_price(current_price=D(2_400_000), original_price=D(2_400_000), min_price=D(2_100_000), days_in_stock=16,
                        inquiries_last_7d=0, market_price=None, rules=rules)
    assert b.should_change and b.recommended_price == D(2_300_000)  # 2 % + escalón extra por baja demanda = 4 %
    c = recommend_price(current_price=D(2_400_000), original_price=D(2_400_000), min_price=D(2_350_000), days_in_stock=60,
                        inquiries_last_7d=0, market_price=None, rules=rules)
    assert c.recommended_price == D(2_350_000) and "mínimo autorizado" in c.reason
    d = recommend_price(current_price=D(2_200_000), original_price=D(2_400_000), min_price=D(1_000_000), days_in_stock=90,
                        inquiries_last_7d=0, market_price=None, rules=rules)
    assert d.recommended_price >= D(2_400_000) * (1 - rules.pricing.max_total_cut_pct)  # tope acumulado 10 %


def test_pricing_service_requests_approval(session, rules):
    item = _item(session, days_ago=20)
    svc = PricingService(session, rules)
    rec = svc.recommend_and_request(item, now=NOW)
    assert rec is not None and rec.status == "pending" and rec.approval_id
    assert not svc.apply_if_approved(rec, by="ope")  # todavía no aprobado
    approvals.decide(session, rec.approval_id, approve=True, by="ope", role=UserRole.OPERATOR)
    assert svc.apply_if_approved(rec, by="ope") and item.list_price == rec.recommended_price
    assert rec.status == "applied"


# ---- permutas ----------------------------------------------------------------
def test_tradein_evaluation(session, rules):
    vs = ValuationService(session, rules)
    for p in (1_300_000, 1_350_000, 1_250_000, 1_320_000):
        vs.add_comparable(kind="asking", make="Gilera", model="Smash 110", year=2020, kilometers=20000, price=D(p), source="manual")
    item = _item(session)
    svc = TradeInService(session, rules)
    row, res = svc.evaluate(item, sale_price_a=D(2_300_000), offered_make="Gilera", offered_model="Smash 110",
                            offered_year=2020, offered_km=25000)
    assert row.id and res.tradein_value > 0 and res.cash_difference == D(2_300_000) - res.tradein_value
    assert res.tradein_value <= res.b_math.max_purchase_price
    assert res.combined_profit == res.profit_a + res.expected_profit_b
    assert row.verdict in ("accept", "counter", "reject")
    # sin comparables para B -> revisión humana
    row2, res2 = svc.evaluate(item, sale_price_a=D(2_300_000), offered_make="Zanella", offered_model="RX 150",
                              offered_year=2019, offered_km=30000)
    assert res2.verdict == "reject" and res2.tradein_value == 0


# ---- inspección --------------------------------------------------------------
def test_fake_inspection_marks_hypotheses_and_simulated(session):
    svc = InspectionService(session, FakeVisionInspector())
    listing = Listing(source="manual", fingerprint="z", make="Honda", model="Wave 110", asking_price=D(1))
    session.add(listing)
    session.flush()
    insp = svc.run(entity_type="listing", entity_id=listing.id, description="Tiene un rayón en la cacha y la cadena estirada",
                   moto="Honda Wave 110")
    assert insp.is_simulated and insp.provider == "fake"
    assert len(insp.findings) >= 3  # 2 por texto + 1 por falta de fotos
    assert all(f.kind == "hypothesis" and f.requires_human_check for f in insp.findings)
    repair = svc.confirm_finding(insp.findings[0], by="ope")
    assert repair is not None and repair.state == "observed" and insp.findings[0].kind == "observation"


# ---- repuestos ---------------------------------------------------------------
def test_parts_quotes_comparison_and_cost_inputs(session):
    svc = PartsService(session)
    sup = svc.add_supplier(name="Repuestos Pérez", kind="parts")
    item = _item(session)
    rep = svc.add_repair(issue="Cacha lateral rota", inventory_item_id=item.id)
    q1 = svc.add_quote(rep, option="replace", part_cost=D(45_000), labor_cost=D(10_000), supplier_id=sup.id,
                       source="whatsapp proveedor", availability_verified=True)
    q2 = svc.add_quote(rep, option="repair", part_cost=D(0), labor_cost=D(30_000), source="estimación", availability_verified=False)
    q3 = svc.add_quote(rep, option="replace", part_cost=D(20_000), labor_cost=D(5_000), source="simulado", is_simulated=True)
    cmp = compare_quotes([q1, q2, q3])
    assert cmp.best_quote_id == q1.id  # el único verificado y no simulado gana aunque no sea el más barato
    ci = svc.cost_inputs_for(inventory_item_id=item.id)
    assert ci.repair_parts == D(45_000) and ci.repair_labor == D(10_000)
    svc.choose_quote(rep, q2, by="ope")
    ci = svc.cost_inputs_for(inventory_item_id=item.id)
    assert ci.repair_labor == D(30_000) and rep.state == "approved"
    with pytest.raises(ValueError):
        svc.add_quote(rep, option="otra")


# ---- documentos --------------------------------------------------------------
def test_documents_checklist_blocks_until_verified(session):
    listing = Listing(source="manual", fingerprint="d", make="Honda", model="Wave 110", asking_price=D(1))
    session.add(listing)
    session.flush()
    deal = Deal(listing_id=listing.id)
    session.add(deal)
    session.flush()
    svc = DocumentsService(session)
    rows = svc.init_checklist(deal_id=deal.id)
    assert len(rows) == len(PURCHASE_CHECKLIST)
    assert svc.init_checklist(deal_id=deal.id) == rows  # idempotente
    assert not svc.is_clear(deal_id=deal.id) and len(svc.blockers(deal_id=deal.id)) == 6
    for r in rows:
        if r.blocking:
            svc.set_status(r, DocumentStatus.VERIFIED_MANUAL, by="ope")
    assert svc.is_clear(deal_id=deal.id) and deal.documents_ok
    svc.set_status(rows[0], DocumentStatus.REJECTED, by="ope", notes="título a nombre de otra persona")
    assert not svc.is_clear(deal_id=deal.id) and not deal.documents_ok


# ---- publicaciones -----------------------------------------------------------
def test_ad_copy_uses_only_real_data(session):
    item = _item(session, color="rojo")
    title, body = generate_ad_copy(item, repairs_done=["kit de transmisión"], price=D(2_400_000), business_name="MT")
    assert title == "Honda Wave 110 2022" and "12.000 km" in body and "rojo" in body and "kit de transmisión" in body
    assert "garantía" not in body.lower()


def test_publication_flow_requires_approval(session, rules):
    item = _item(session)
    svc = PublicationsService(session, business_name="MT")
    with pytest.raises(ValueError):
        svc.draft(item, channel="manual", price=D(2_000_000))  # debajo del mínimo
    pub = svc.draft(item, channel="mercadolibre_manual")
    assert pub.status == "pending_approval" and pub.price == D(2_400_000)
    with pytest.raises(ValueError):
        svc.mark_published_manual(pub, by="ope", external_url="https://x")
    req = next(a for a in approvals.pending(session) if a.kind == ApprovalKind.PUBLISH_LISTING)
    approvals.decide(session, req.id, approve=True, by="ope", role=UserRole.OPERATOR)
    svc.mark_published_manual(pub, by="ope", external_url="https://x")
    assert pub.status == "published_manual" and item.status == "published"


# ---- CRM / logística / analítica ---------------------------------------------
def test_crm_contacts_and_pipeline(session):
    crm = CrmService(session)
    c = crm.upsert_contact(alias="Juan", channel="whatsapp", kind="seller", channel_handle="+549341...")
    c2 = crm.upsert_contact(alias="Juan P.", channel="whatsapp", kind="buyer", channel_handle="+549341...")
    assert c.id == c2.id and c2.kind == "both"
    crm.set_do_not_contact(c, True, by="ope")
    assert c.do_not_contact
    assert crm.pipeline() == {}


def test_logistics_visits(session):
    lg = LogisticsService(session)
    v = lg.propose_visit(kind="inspection", scheduled_at=NOW + timedelta(days=1), location="Funes")
    assert v.status == "proposed" and lg.upcoming(now=NOW)[0].id == v.id
    lg.set_status(v, "done", by="ope", outcome="moto ok")
    assert lg.upcoming(now=NOW) == []
    with pytest.raises(ValueError):
        lg.propose_visit(kind="party", scheduled_at=NOW)


def test_analytics_kpis_with_sales(session, rules):
    fin = FinanceService(session, rules)
    fin.ensure_initial_capital()
    sold = _item(session, days_ago=30, status="sold", sold_price=D(2_300_000), sold_at=NOW - timedelta(days=10))
    _item(session, days_ago=5)
    an = AnalyticsService(session, rules)
    k = an.kpis(now=NOW)
    assert k["units_sold"] == 1 and k["units_in_stock"] == 1
    assert k["avg_days_in_stock_sold"] == 20 and k["realized_profit"] > 0
    assert k["annualized_return_on_capital"] is not None
    by_model = an.performance_by_model(now=NOW)
    assert by_model[0]["model"] == "Wave 110" and by_model[0]["sold"] == 1
    assert an.valuation_accuracy()["samples"] == 0
