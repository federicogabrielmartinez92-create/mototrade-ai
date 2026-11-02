from datetime import datetime, timedelta, timezone
from decimal import Decimal

from mototrade.core.money import D
from mototrade.modules.valuation.engine import (
    ComparablePoint,
    CostInputs,
    compute_deal,
    estimate_market_price,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _cp(price, kind="asking", days=5, km=15000, year=2022):
    return ComparablePoint(price=D(price), kind=kind, observed_at=NOW - timedelta(days=days), year=year,
                           kilometers=km, source="manual")


# ---- estimación de mercado ---------------------------------------------------
def test_asking_prices_are_discounted_never_used_raw(rules):
    comps = [_cp(2_500_000), _cp(2_600_000), _cp(2_400_000), _cp(2_550_000)]
    est = estimate_market_price(comps, rules, now=NOW)
    assert est.basis == "asking_adjusted"
    # mediana 2.525.000 * (1 - 0.06) = 2.373.500
    assert est.market_price == Decimal("2373500.00")
    assert est.comparables_used == 4
    assert est.confidence in ("medium", "high")
    assert any("PEDIDOS" in n for n in est.notes)


def test_sold_prices_take_precedence_over_asking(rules):
    comps = [_cp(2_600_000), _cp(2_700_000), _cp(2_650_000)] + [
        _cp(2_300_000, "sold"), _cp(2_350_000, "sold"), _cp(2_250_000, "sold")
    ]
    est = estimate_market_price(comps, rules, now=NOW)
    assert est.basis == "sold"
    assert est.market_price == Decimal("2300000.00")


def test_stale_comparables_are_ignored(rules):
    comps = [_cp(2_500_000, days=200), _cp(2_600_000, days=100)]
    est = estimate_market_price(comps, rules, now=NOW)
    assert est.basis == "insufficient" and est.market_price == 0 and est.requires_human_review


def test_few_comparables_force_low_confidence(rules):
    est = estimate_market_price([_cp(2_500_000), _cp(2_520_000)], rules, now=NOW)
    assert est.confidence == "low" and est.requires_human_review


def test_high_dispersion_lowers_confidence(rules):
    tight = estimate_market_price([_cp(2_500_000), _cp(2_510_000), _cp(2_490_000), _cp(2_505_000)], rules, now=NOW)
    wide = estimate_market_price([_cp(1_800_000), _cp(3_200_000), _cp(2_000_000), _cp(3_000_000)], rules, now=NOW)
    assert tight.confidence_score > wide.confidence_score


# ---- matemática del negocio --------------------------------------------------
def test_deal_math_all_outputs_present(rules):
    m = compute_deal(D(2_400_000), D(2_000_000), CostInputs(repair_parts=D(50_000), repair_labor=D(30_000)),
                     rules, available_capital=D(4_000_000))
    assert m.quick_sale_price == Decimal("2208000.00")          # 2.4M * 0.92
    assert m.suggested_list_price == Decimal("2496000.00")      # 2.4M * 1.04
    assert m.sale_basis_price == m.quick_sale_price             # deal_basis = quick_sale
    expected_keys = {"repair_parts", "repair_labor", "repair_shipping", "transfer", "logistics", "preparation",
                     "marketing", "contingency", "taxes_estimate"}
    assert set(m.costs) == expected_keys
    assert m.costs["contingency"] == Decimal("60000.00")        # max(3% de 2M, 40k)
    assert m.total_investment == D(2_000_000) + m.total_costs
    assert m.expected_profit == m.sale_basis_price - m.total_investment
    assert set(m.scenarios) == {"base", "market", "adverse", "severe"}
    assert m.scenarios["severe"].profit < m.scenarios["adverse"].profit < m.scenarios["base"].profit
    assert m.rules_hash == rules.rules_hash
    assert m.within_capital_limit is True


def test_max_purchase_guarantees_minimum_roi(rules):
    """Comprando exactamente al máximo, el ROI debe ser >= mínimo; un peso más, y falla."""
    costs = CostInputs(repair_parts=D(80_000), repair_labor=D(40_000))
    m = compute_deal(D(2_400_000), D(2_000_000), costs, rules)
    at_max = compute_deal(D(2_400_000), m.max_purchase_price, costs, rules)
    assert at_max.meets_minimum_roi
    assert at_max.expected_roi >= rules.valuation.minimum_roi
    above = compute_deal(D(2_400_000), m.max_purchase_price + rules.valuation.offer_rounding, costs, rules)
    assert not above.meets_minimum_roi
    assert m.max_purchase_price % rules.valuation.offer_rounding == 0


def test_initial_offer_below_max_and_rounded(rules):
    m = compute_deal(D(2_400_000), D(2_000_000), CostInputs(), rules)
    assert m.initial_offer < m.max_purchase_price
    assert m.initial_offer % rules.valuation.offer_rounding == 0
    assert m.initial_offer <= m.max_purchase_price * rules.negotiation.buyer.opening_offer_pct_of_max


def test_capital_limit_flags(rules):
    m = compute_deal(D(2_400_000), D(2_000_000), CostInputs(), rules, available_capital=D(2_500_000))
    assert m.within_capital_limit is False
    assert any("capital" in w for w in m.warnings)


def test_no_viable_price_when_costs_too_high(rules):
    m = compute_deal(D(1_000_000), D(500_000), CostInputs(repair_parts=D(900_000)), rules)
    assert m.max_purchase_price == 0
    assert any("ROI mínimo" in w for w in m.warnings)


def test_persisted_valuation_via_service(session, rules):
    from mototrade.db.models import Deal, Listing
    from mototrade.modules.valuation import ValuationService

    svc = ValuationService(session, rules)
    for p in (2_500_000, 2_600_000, 2_450_000, 2_550_000):
        svc.add_comparable(kind="asking", make="Honda", model="Wave 110", year=2022, kilometers=12000, price=D(p),
                           source="manual")
    listing = Listing(source="manual", fingerprint="x", make="Honda", model="Wave 110", year=2022, kilometers=15000,
                      asking_price=D(2_400_000))
    session.add(listing)
    session.flush()
    deal = Deal(listing_id=listing.id)
    session.add(deal)
    session.flush()
    deal.listing = listing
    row, est, math = svc.valuate_deal(deal, cost_inputs=CostInputs(repair_parts=D(20_000)))
    session.commit()
    assert row.id and row.basis == "asking_adjusted" and row.comparables_used == 4
    assert row.rules_hash == rules.rules_hash
    assert Decimal(row.max_purchase_price) == math.max_purchase_price
    assert deal.stage == "valuated"
    assert len(row.sources) == 4 and row.scenarios["severe"]["roi"]
