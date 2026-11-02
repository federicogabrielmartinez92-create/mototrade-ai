"""Centro de Control, posición de capital, puerta operativa y presupuesto de IA."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from mototrade.control import ControlCenter, OperationsGate
from mototrade.control.service import ControlError
from mototrade.core import approvals, kill_switch
from mototrade.core.enums import ApprovalKind, UserRole
from mototrade.core.money import D
from mototrade.db.models import AppSetting, AuditLog, Deal, InventoryItem, Listing
from mototrade.integrations.llm import FakeLLM, LLMResult
from mototrade.modules.ai_budget import AIBudget, AIBudgetExceeded, MeteredLLM, estimate_cost_usd
from mototrade.modules.finance import FinanceService
from mototrade.modules.finance.capital import capital_position, purchase_budget_for_unit

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


# ---- persistencia de configuración ------------------------------------------
def test_settings_persist_validate_and_audit(session):
    cc = ControlCenter(session)
    cs = cc.load()
    assert cs.capital.max_units == 2 and cs.search.minimum_roi == Decimal("0.10")
    cc.update_section("capital", {"max_units": 3, "capital_total": "5000000"}, by="admin")
    cc.update_section("search", {"auto_search_enabled": False}, by="admin")
    session.commit()
    # nueva instancia lee lo persistido
    cs2 = ControlCenter(session).load()
    assert cs2.capital.max_units == 3 and cs2.capital.capital_total == D(5_000_000) and not cs2.search.auto_search_enabled
    assert session.get(AppSetting, "capital").version == 1
    cc.update_section("capital", {"max_units": 4}, by="admin")
    assert session.get(AppSetting, "capital").version == 2
    with pytest.raises(ControlError):
        cc.update_section("capital", {"max_units": -1}, by="admin")
    with pytest.raises(ControlError):
        cc.update_section("search", {"target_discount_min": "0.30", "target_discount_max": "0.20"}, by="admin")
    with pytest.raises(ControlError):
        cc.update_section("otra", {}, by="admin")
    logs = [a for a in session.query(AuditLog).all() if a.action == "control.updated"]
    assert len(logs) == 3 and logs[0].details["cambios"]["max_units"] == {"antes": 2, "despues": 3}
    assert logs[2].details["cambios"]["max_units"] == {"antes": 3, "despues": 4}


def test_corrupt_setting_row_is_ignored(session):
    session.add(AppSetting(section="capital", value={"max_units": "no-es-numero"}, version=1))
    session.flush()
    cs = ControlCenter(session).load()
    assert cs.capital.max_units == 2  # vuelve al default sin romper


def test_effective_rules_override_yaml(session, rules):
    cc = ControlCenter(session)
    cc.update_section("search", {"minimum_roi": "0.20", "priority_models": [{"make": "Zanella", "model": "RX 150"}]}, by="admin")
    cc.update_section("agents", {"buyer_max_rounds": 2, "buyer_opening_pct_of_max": "0.80"}, by="admin")
    eff = cc.effective_rules(rules)
    assert eff.valuation.minimum_roi == Decimal("0.20") and eff.negotiation.buyer.max_rounds == 2
    assert eff.negotiation.buyer.opening_offer_pct_of_max == Decimal("0.80")
    assert [w.model for w in eff.radar.watchlist] == ["RX 150"]
    assert eff.rules_hash != rules.rules_hash and len(eff.rules_hash) == 64
    # el hash cambia con la configuración
    cc.update_section("search", {"minimum_roi": "0.21"}, by="admin")
    assert cc.effective_rules(rules).rules_hash != eff.rules_hash


# ---- capital -----------------------------------------------------------------
def _deal(session, asking=1_500_000):
    l = Listing(source="manual", fingerprint="f", make="Honda", model="Wave 110", year=2022, kilometers=10000, asking_price=D(asking))
    session.add(l); session.flush()
    d = Deal(listing_id=l.id); session.add(d); session.flush(); d.listing = l
    return d


def test_capital_position_reserves_pending_purchases(session, rules):
    cc = ControlCenter(session)
    cs = cc.load()
    FinanceService(session, cc.effective_rules(rules)).ensure_initial_capital()  # 4.000.000
    pos = capital_position(session, cs.capital)
    assert pos.ledger_balance == D(4_000_000) and pos.reserve == D(400_000)  # max(300k, 10 %)
    assert pos.free_capital == D(3_600_000) and pos.units_left == 2 and pos.can_buy_another
    deal = _deal(session)
    approvals.request(session, kind=ApprovalKind.ACCEPT_PURCHASE, entity_type="deal", entity_id=deal.id,
                      payload={"p": 1}, summary="x", requested_by="agent", amount=D(1_500_000))
    pos = capital_position(session, cs.capital)
    assert pos.pending_units == 1 and pos.reserved_pending == D(1_725_000)  # 1.5M × 1.15
    assert pos.free_capital == D(4_000_000) - D(400_000) - D(1_725_000) and pos.units_active == 1
    assert purchase_budget_for_unit(pos, cs.capital) == min(pos.free_capital, cs.capital.max_unit_price)


def test_capital_insufficient_blocks_search(session, rules):
    cc = ControlCenter(session)
    cc.update_section("capital", {"capital_total": "1000000", "min_reserve_amount": "100000", "min_unit_budget": "800000"}, by="admin")
    eff = cc.effective_rules(rules)
    FinanceService(session, eff).ensure_initial_capital()  # 1.000.000
    gate = OperationsGate(session)
    assert gate.state("search").effective  # 1M - 100k = 900k ≥ 800k
    deal = _deal(session, 500_000)
    approvals.request(session, kind=ApprovalKind.ACCEPT_PURCHASE, entity_type="deal", entity_id=deal.id, payload={},
                      summary="x", requested_by="agent", amount=D(500_000))
    st = OperationsGate(session).state("search")
    assert st.intended and not st.effective and "capital libre" in st.reason
    assert not OperationsGate(session).allows("buyer_agent")


def test_inventory_full_pauses_search_and_buyer(session, rules):
    cc = ControlCenter(session)
    cc.update_section("capital", {"max_units": 1}, by="admin")
    FinanceService(session, cc.effective_rules(rules)).ensure_initial_capital()
    session.add(InventoryItem(make="Honda", model="Wave 110", purchase_price=D(1_000_000)))
    session.flush()
    gate = OperationsGate(session)
    s = gate.state("search")
    assert s.intended and not s.effective and "inventario completo" in s.reason
    assert not gate.allows("buyer_agent") and gate.allows("seller_agent")
    assert purchase_budget_for_unit(gate.position, cc.load().capital) == 0
    # se libera al subir el máximo
    cc.update_section("capital", {"max_units": 2}, by="admin")
    assert OperationsGate(session).allows("search")


def test_manual_disable_does_not_reactivate(session, rules):
    cc = ControlCenter(session)
    FinanceService(session, cc.effective_rules(rules)).ensure_initial_capital()
    cc.update_section("search", {"auto_search_enabled": False}, by="admin")
    for _ in range(3):
        s = OperationsGate(session).state("search")
        assert not s.intended and not s.effective and "manualmente" in s.reason
    assert not OperationsGate(session).allows("negotiable_analysis")


def test_kill_switch_dominates_gate(session, rules):
    FinanceService(session, rules).ensure_initial_capital()
    kill_switch.engage(session, by="admin")
    g = OperationsGate(session)
    assert all(not g.allows(f) for f in ("search", "buyer_agent", "seller_agent", "ai_calls", "notifications"))


# ---- presupuesto de IA -------------------------------------------------------
def test_ai_cost_estimate():
    prices = {"m": [Decimal("2"), Decimal("10")]}
    assert estimate_cost_usd("m", 1_000_000, 100_000, prices) == Decimal("3")
    assert estimate_cost_usd("desconocido", 1_000_000, 0, prices) == Decimal("3")  # default conservador


def test_ai_budget_limits_and_cache(session):
    cs = ControlCenter(session).load()
    cc = ControlCenter(session)
    cc.update_section("ai_budget", {"monthly_budget_usd": "0.05", "daily_budget_usd": "0.05"}, by="admin")
    cfg = ControlCenter(session).load().ai_budget
    budget = AIBudget(session, cfg)

    class Paid(FakeLLM):
        provider, model, simulated = "anthropic", "claude-sonnet-5-5", False
        calls = 0

        def structured(self, **kw):
            Paid.calls += 1
            res = super().structured(**kw)
            return LLMResult(data=res.data, provider=self.provider, model=self.model, simulated=False,
                             usage={"input_tokens": 10_000, "output_tokens": 1_000})  # ≈ US$0,03

    llm = MeteredLLM(Paid(), budget, entity_type="message", entity_id=1)
    kw = dict(system="s", user="[INICIO X]\nhola, 2.000.000\n[FIN X]", schema={}, tool_name="interpret_message")
    llm.structured(**kw)
    llm.structured(**kw)  # cache: no cuesta ni llama
    assert Paid.calls == 1
    st = budget.status()
    assert st["spent_month_usd"] == Decimal("0.03") and st["within_budget"]
    llm.structured(system="s", user="otro", schema={}, tool_name="draft_message")  # 0,06 acumulado > 0,05
    assert not budget.status()["within_budget"]
    with pytest.raises(AIBudgetExceeded):
        llm.structured(system="s", user="tercero", schema={}, tool_name="draft_message")
    g = OperationsGate(session)
    assert not g.allows("ai_calls") and not g.allows("visual_inspection") and "presupuesto" in g.state("ai_calls").reason
    # lo esencial sigue: inventario/finanzas no dependen de IA
    assert FinanceService(session, ControlCenter(session).effective_rules()).summary()["units_in_stock"] == 0


def test_fake_llm_is_free_and_not_limited(session):
    budget = AIBudget(session, ControlCenter(session).load().ai_budget)
    llm = MeteredLLM(FakeLLM(), budget)
    for _ in range(5):
        llm.structured(system="s", user=f"u{_}", schema={}, tool_name="draft_message")
    assert budget.status()["spent_month_usd"] == 0


def test_visual_quota(session):
    cc = ControlCenter(session)
    cc.update_section("ai_budget", {"max_visual_analyses_per_month": 1}, by="admin")
    b = AIBudget(session, cc.load().ai_budget)
    assert b.check("vision")[0]
    b.record(provider="anthropic", model="claude-sonnet-5-5", purpose="vision", input_tokens=1000)
    ok, reason = b.check("vision")
    assert not ok and "visuales" in reason
    assert not OperationsGate(session).allows("visual_inspection")
