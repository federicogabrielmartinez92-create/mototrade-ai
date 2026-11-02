"""Detector de anomalías: descuentos 15/20/25 %, precios anormalmente bajos, versiones, km, comparables insuficientes,
duplicados, reparaciones imprevistas, escenarios adversos, capital insuficiente, inventario completo."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from mototrade.control import ControlCenter
from mototrade.control.settings_model import CapitalSettings, SearchSettings
from mototrade.core.money import D
from mototrade.db.models import Deal, InventoryItem, Listing
from mototrade.modules.anomalies import AnomalyDetector, ComparableRecord, TargetListing, adjust_comparable
from mototrade.modules.finance import FinanceService
from mototrade.modules.parts import PartsService
from mototrade.modules.radar import ManualSource, RadarService, RawListing
from mototrade.modules.valuation import CostInputs, ValuationService

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _comps(prices, km=15000, year=2022, version="", seller="particular", kind="asking", days=5):
    return [ComparableRecord(price=D(p), kind=kind, observed_at=NOW - timedelta(days=days), year=year, kilometers=km,
                             version=version, seller_type=seller, source="manual", ref_id=i) for i, p in enumerate(prices)]


def _target(asking, **kw):
    base = dict(make="Honda", model="Wave 110", version="", year=2022, kilometers=15000, seller_type="particular", photos_count=3)
    base.update(kw)
    return TargetListing(asking_price=D(asking), **base)


def _det(rules, **search_kw):
    return AnomalyDetector(rules, SearchSettings(**search_kw), CapitalSettings())


def _light(rules, minimum_roi="0.10"):
    """Reglas con estructura de costos liviana (monotributo, sin IVA sobre margen, base mercado) para probar
    los modos A/B con los umbrales comerciales del brief. Con las reglas por defecto (RI + IVA + venta rápida)
    una moto necesita ~30 % de descuento para dejar 10 %: lo documenta el informe."""
    return rules.model_copy(update={
        "taxes": rules.taxes.model_copy(update={"regime": "monotributo", "iva_margin_pct": D("0"), "iibb_pct": D("0.03")}),
        "valuation": rules.valuation.model_copy(update={"deal_basis": "market", "minimum_roi": D(minimum_roi)}),
    })


REF = [2_600_000, 2_650_000, 2_700_000, 2_650_000, 2_600_000]  # mediana 2.650.000


# ---- clasificación por descuento --------------------------------------------
def test_discount_classification_15_20_25(rules):
    det = _det(rules)
    # 10 % → ordinaria
    an = det.analyze(_target(2_385_000), _comps(REF), now=NOW)
    assert an.classification == "ordinary" and an.discount_pct == Decimal("0.1000")
    # 15 % → interesante
    an = det.analyze(_target(2_252_500), _comps(REF), now=NOW)
    assert an.classification == "interesting" and an.discount_pct == Decimal("0.1500")
    # 20 % → interesante
    an = det.analyze(_target(2_120_000), _comps(REF), now=NOW)
    assert an.classification == "interesting" and an.discount_pct == Decimal("0.2000")
    # 25 % → extraordinaria, exige verificación
    an = det.analyze(_target(1_987_500), _comps(REF), now=NOW)
    assert an.classification == "extraordinary" and an.discount_pct == Decimal("0.2500")
    assert "extraordinary_needs_verification" in an.risk_flags and an.recommended_action == "verify"
    assert an.recommended_action != "buy"


def test_thresholds_are_configurable(rules):
    det = _det(rules, target_discount_min="0.10", extraordinary_discount_pct="0.30")
    an = det.analyze(_target(2_385_000), _comps(REF), now=NOW)   # 10 %
    assert an.classification == "interesting"
    an = det.analyze(_target(1_987_500), _comps(REF), now=NOW)   # 25 %
    assert an.classification == "interesting"


def test_abnormally_low_price_flags_scam(rules):
    det = _det(rules)
    an = det.analyze(_target(1_300_000), _comps(REF), now=NOW)  # 51 % abajo
    assert an.classification == "extraordinary"
    assert "too_cheap_possible_scam_or_damage" in an.risk_flags and an.risk_level == "high"
    assert an.recommended_action == "verify"


def test_discount_never_confused_with_profitability(rules):
    """Un 20 % de descuento puede NO ser oportunidad real si los costos se comen el margen."""
    det = _det(rules)
    heavy = CostInputs(repair_parts=D(600_000), repair_labor=D(200_000))
    an = det.analyze(_target(2_120_000), _comps(REF), cost_inputs=heavy, now=NOW)
    assert an.classification == "interesting"
    assert an.math.max_purchase_price < D(2_120_000) and an.mode != "published"
    assert an.recommended_action in ("negotiate_target", "skip", "verify") and an.recommended_action != "negotiate"


# ---- modos A y B --------------------------------------------------------------
def test_mode_a_published_opportunity(rules):
    comps = _comps([4_200_000, 4_300_000, 4_400_000, 4_500_000, 4_350_000])  # ejemplo del brief: FZ 3.0
    target = _target(3_000_000, make="Yamaha", model="FZ 3.0", year=2023)
    # Con las reglas por defecto (RI + IVA s/margen + venta rápida) el ROI al pedido es ~9 % < 10 %: NO es "publicada",
    # el detector la trata como negociable y lo informa en vez de inflar la oportunidad.
    strict = _det(rules).analyze(target, comps, now=NOW)
    assert strict.classification == "extraordinary" and strict.discount_pct > Decimal("0.30")
    assert strict.mode == "negotiable" and not strict.math.meets_minimum_roi
    # Con estructura de costos liviana (monotributo, base mercado) sí es una oportunidad publicada real.
    an = AnomalyDetector(_light(rules), SearchSettings(), CapitalSettings()).analyze(target, comps, now=NOW)
    assert an.mode == "published" and an.target_price == D(3_000_000)
    assert an.math.max_purchase_price >= D(3_000_000) and an.math.meets_minimum_roi
    assert set(an.scenarios) == {"conservador", "central", "adverso"}
    assert an.recommended_action == "verify"  # extraordinaria: nunca "comprar" sin verificar


def test_mode_b_negotiable_target(rules):
    det = AnomalyDetector(_light(rules), SearchSettings(), CapitalSettings())
    an = det.analyze(_target(2_600_000), _comps(REF), now=NOW)  # precio normal
    assert an.classification == "ordinary" and an.mode == "negotiable"
    lo, hi = an.target_discount_range
    assert lo == D(1_950_000) and hi == D(2_210_000)  # 25 % y 15 % de rebaja
    assert lo <= an.target_price <= hi and an.target_price <= an.math.max_purchase_price
    assert an.math_at_target.meets_minimum_roi and an.recommended_action == "negotiate_target"
    assert an.math.initial_offer < an.target_price  # oferta inicial por debajo del objetivo
    assert "accept" not in an.recommended_action  # el sistema propone; nunca afirma que el vendedor aceptará


def test_mode_b_not_viable_when_even_25pct_is_not_enough(rules):
    det = AnomalyDetector(_light(rules), SearchSettings(), CapitalSettings())
    heavy = CostInputs(repair_parts=D(900_000))
    an = det.analyze(_target(2_600_000), _comps(REF), cost_inputs=heavy, now=NOW)
    assert an.mode == "none" and an.recommended_action == "skip"
    assert any("25 %" in n for n in an.notes)


def test_negotiable_mode_can_be_disabled(rules):
    det = AnomalyDetector(_light(rules), SearchSettings(negotiable_mode_enabled=False), CapitalSettings())
    an = det.analyze(_target(2_600_000), _comps(REF), now=NOW)
    assert an.mode == "none"


# ---- versiones, kilometraje, ajustes -----------------------------------------
def test_version_matching_prefers_same_version(rules):
    det = _det(rules)
    comps = _comps([2_600_000, 2_650_000, 2_700_000], version="S") + _comps([3_000_000, 3_100_000, 3_050_000], version="Deluxe")
    an = det.analyze(_target(2_300_000, version="S"), comps, now=NOW)
    assert an.versions_matched and an.comparables_used == 3 and an.reference_asking == D(2_650_000)
    an2 = det.analyze(_target(2_300_000, version="Deluxe"), comps, now=NOW)
    assert an2.reference_asking == D(3_050_000)
    # versión sin suficientes comparables → se amplía y se marca el riesgo
    an3 = det.analyze(_target(2_300_000, version="Rara"), comps, now=NOW)
    assert not an3.versions_matched and "mixed_versions" in an3.risk_flags and an3.comparables_used == 6


def test_km_adjustment_and_high_km_flag(rules):
    det = _det(rules)
    comps = _comps(REF, km=15000)
    low_km = det.analyze(_target(2_300_000, kilometers=5_000), comps, now=NOW)
    high_km = det.analyze(_target(2_300_000, kilometers=80_000), comps, now=NOW)
    # con menos km la referencia sube (comparables con más km se ajustan hacia arriba) → mayor descuento detectado
    assert low_km.reference_asking > high_km.reference_asking
    assert low_km.discount_pct > high_km.discount_pct
    assert "high_km" in high_km.risk_flags and "high_km" not in low_km.risk_flags
    # el ajuste está acotado por km_adjustment_cap
    adj, info = adjust_comparable(comps[0], _target(1, kilometers=200_000), rules)
    assert Decimal(info["km_factor"]) == -rules.anomaly.km_adjustment_cap


def test_dealer_vs_particular_adjustment(rules):
    det = _det(rules)
    dealer_comps = _comps(REF, seller="dealer")
    a = det.analyze(_target(2_300_000, seller_type="particular"), dealer_comps, now=NOW)
    b = det.analyze(_target(2_300_000, seller_type="particular"), _comps(REF, seller="particular"), now=NOW)
    assert a.reference_asking < b.reference_asking  # concesionarias publican más caro: se descuenta


# ---- comparables insuficientes / antiguos ------------------------------------
def test_insufficient_comparables(rules):
    det = _det(rules)
    an = det.analyze(_target(2_000_000), _comps([2_600_000, 2_700_000]), now=NOW)
    assert an.classification == "insufficient" and an.discount_pct is None
    assert "insufficient_comparables" in an.risk_flags and an.recommended_action == "review" and an.mode == "none"
    stale = det.analyze(_target(2_000_000), _comps(REF, days=120), now=NOW)
    assert stale.classification == "insufficient"


def test_sold_comparables_do_not_mix_with_asking(rules):
    det = _det(rules)
    comps = _comps([2_300_000, 2_350_000, 2_250_000], kind="sold") + _comps([2_700_000, 2_750_000, 2_800_000, 2_720_000])
    an = det.analyze(_target(2_400_000), comps, now=NOW)
    assert an.market.basis == "sold" and an.market.market_price == D(2_300_000)
    assert an.reference_asking == D(2_735_000)  # pedido-contra-pedido (mediana de los 4 asking)


# ---- riesgos del anuncio -------------------------------------------------------
def test_listing_risk_flags(rules):
    det = _det(rules)
    hist = [(NOW - timedelta(days=20), D(2_600_000)), (NOW - timedelta(days=10), D(2_500_000)), (NOW, D(2_300_000))]
    an = det.analyze(_target(2_300_000, photos_count=0, year=None, kilometers=None, published_at=NOW - timedelta(days=90),
                             price_history=hist, injection_flags=["payment_request"], condition_declared="regular"),
                     _comps(REF), now=NOW)
    for f in ("no_photos", "unknown_year", "unknown_km", "stale_listing", "repeated_price_drops", "suspicious_text",
              "declared_poor_condition"):
        assert f in an.risk_flags
    assert an.risk_level == "high" and an.score < 50


# ---- integración con radar, capital e inventario ------------------------------
def _seed_comps(session, rules, n=5):
    vs = ValuationService(session, rules)
    for p in REF[:n]:
        vs.add_comparable(kind="asking", make="Honda", model="Wave 110", year=2022, kilometers=14000, price=D(p), source="manual",
                          seller_type="particular")


def test_duplicate_listings_do_not_create_two_deals(session, rules):
    _seed_comps(session, rules)
    radar = RadarService(session, rules)
    radar.ingest(ManualSource([RawListing(source="facebook_marketplace", make="Honda", model="Wave 110", year=2022,
                                          kilometers=15000, asking_price=D(2_100_000), location="Rosario", external_id="fb9")]))
    res = radar.ingest(ManualSource([RawListing(source="mercadolibre", make="Honda", model="Wave 110", year=2022,
                                                kilometers=15400, asking_price=D(2_100_000), location="Rosario", external_id="ml9")]))
    assert res.duplicates and not res.created
    deals = radar.scan_opportunities()
    assert len(deals) == 1 and session.query(Deal).count() == 1


def test_unexpected_repairs_lower_max_purchase(session, rules):
    _seed_comps(session, rules)
    radar = RadarService(session, rules)
    res = radar.ingest(ManualSource([RawListing(source="manual", make="Honda", model="Wave 110", year=2022, kilometers=15000,
                                                asking_price=D(2_200_000), location="Rosario", external_id="r1")]))
    listing = session.get(Listing, res.created[0])
    deal = radar.create_opportunity(listing)
    before = D(deal.analysis["at_asking"]["max_purchase_price"])
    parts = PartsService(session)
    rep = parts.add_repair(issue="Motor con ruido", deal_id=deal.id)
    parts.add_quote(rep, option="repair", labor_cost=D(250_000), source="taller", availability_verified=True)
    radar.create_opportunity(listing)  # re-análisis
    after = D(deal.analysis["at_asking"]["max_purchase_price"])
    assert after < before and after <= before - D(250_000) + rules.valuation.offer_rounding


def test_adverse_sale_scenarios_present(session, rules):
    _seed_comps(session, rules)
    radar = RadarService(session, rules)
    res = radar.ingest(ManualSource([RawListing(source="manual", make="Honda", model="Wave 110", year=2022, kilometers=15000,
                                                asking_price=D(2_000_000), location="Rosario", external_id="s1")]))
    an = radar.analyze_listing(session.get(Listing, res.created[0]), now=NOW)
    c, ce, a = an.scenarios["conservador"], an.scenarios["central"], an.scenarios["adverso"]
    assert Decimal(a["profit"]) < Decimal(c["profit"]) < Decimal(ce["profit"])


def test_capital_insufficient_marks_exceeds_budget_and_no_mode(session, rules):
    cc = ControlCenter(session)
    cc.update_section("capital", {"capital_total": "600000", "min_reserve_amount": "100000", "min_unit_budget": "800000"}, by="admin")
    FinanceService(session, cc.effective_rules(rules)).ensure_initial_capital()
    _seed_comps(session, rules)
    radar = RadarService(session, rules)
    res = radar.ingest(ManualSource([RawListing(source="manual", make="Honda", model="Wave 110", year=2022, kilometers=15000,
                                                asking_price=D(2_000_000), location="Rosario", external_id="c1")]))
    an = radar.analyze_listing(session.get(Listing, res.created[0]), now=NOW)
    assert "exceeds_budget" in an.risk_flags and an.math.within_capital_limit is False


def test_inventory_full_still_analyzes_but_flags_budget(session, rules):
    cc = ControlCenter(session)
    cc.update_section("capital", {"max_units": 1}, by="admin")
    FinanceService(session, cc.effective_rules(rules)).ensure_initial_capital()
    session.add(InventoryItem(make="Honda", model="Wave 110", purchase_price=D(1_000_000)))
    session.flush()
    _seed_comps(session, rules)
    radar = RadarService(session, rules)
    res = radar.ingest(ManualSource([RawListing(source="manual", make="Honda", model="Wave 110", year=2022, kilometers=15000,
                                                asking_price=D(2_000_000), location="Rosario", external_id="i1")]))
    an = radar.analyze_listing(session.get(Listing, res.created[0]), now=NOW)
    assert "exceeds_budget" in an.risk_flags


def test_excluded_and_any_model_rules(session, rules):
    cc = ControlCenter(session)
    cc.update_section("search", {"excluded_makes": ["Zanella"], "allow_any_model": False}, by="admin")
    vs = ValuationService(session, rules)
    for p in (3_000_000, 3_100_000, 3_050_000, 3_000_000):
        vs.add_comparable(kind="asking", make="Kawasaki", model="Ninja 400", year=2021, kilometers=10000, price=D(p), source="manual")
    radar = RadarService(session, rules)
    res = radar.ingest(ManualSource([
        RawListing(source="manual", make="Zanella", model="RX 150", year=2021, kilometers=1000, asking_price=D(500_000), external_id="z"),
        RawListing(source="manual", make="Kawasaki", model="Ninja 400", year=2021, kilometers=9000, asking_price=D(2_000_000), external_id="k"),
    ]))
    assert radar.scan_opportunities() == []  # marca excluida + modelo fuera de lista con allow_any_model=False
    cc.update_section("search", {"allow_any_model": True}, by="admin")
    radar = RadarService(session, rules)
    deals = radar.scan_opportunities()
    assert [d.listing.make for d in deals] == ["Kawasaki"] and deals[0].opportunity_class == "extraordinary"
