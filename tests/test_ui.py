"""Smoke test del panel Streamlit con AppTest: login + render de todas las secciones + acciones clave."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from mototrade.core.money import D
from mototrade.db.models import Deal, InventoryItem
from mototrade.modules.finance import FinanceService
from mototrade.modules.negotiation import NegotiationService
from mototrade.modules.radar import ManualSource, RadarService, RawListing
from mototrade.modules.valuation import ValuationService
from mototrade.security import create_user

APP = str(Path(__file__).resolve().parents[1] / "ui" / "app.py")
VIEWS = ["views/dashboard.py", "views/notificaciones.py", "views/centro_control.py", "views/aprobaciones.py", "views/radar.py", "views/tasaciones.py", "views/inspeccion.py",
         "views/repuestos.py", "views/negociaciones_compra.py", "views/inventario.py", "views/publicaciones.py",
         "views/negociaciones_venta.py", "views/crm.py", "views/finanzas.py", "views/analitica.py", "views/configuracion.py"]


@pytest.fixture()
def seeded(session, rules):
    create_user(session, "admin", "clave-segura-123", "admin")
    fin = FinanceService(session, rules)
    fin.ensure_initial_capital()
    vs = ValuationService(session, rules)
    for p in (2_650_000, 2_700_000, 2_600_000, 2_680_000):
        vs.add_comparable(kind="asking", make="Honda", model="Wave 110", year=2022, kilometers=14000, price=D(p), source="manual")
    radar = RadarService(session, rules)
    radar.ingest(ManualSource([RawListing(source="manual", make="Honda", model="Wave 110", year=2022, kilometers=15000,
                                          asking_price=D(1_500_000), location="Rosario", photos=["a.jpg"], external_id="ui1",
                                          description="rayón en la cacha")]))
    deals = radar.scan_opportunities(available_capital=fin.available_capital())
    deal = deals[0]
    vs.valuate_deal(deal)
    neg = NegotiationService(session, rules)
    neg.authorize_deal_limit(deal, max_price=D(1_300_000), by="admin")
    conv = neg.start_buy_conversation(deal, channel="manual")
    neg.agent_step(conv)
    item = InventoryItem(make="Yamaha", model="YBR 125", year=2020, kilometers=30000, purchase_price=D(1_500_000), status="ready",
                         list_price=D(2_400_000), min_sale_price=D(2_200_000))
    session.add(item)
    session.commit()
    return {"deal_id": deal.id, "conv_id": conv.id, "item_id": item.id}


def _login(at: AppTest) -> AppTest:
    at.run()
    assert not at.exception, at.exception
    at.text_input[0].set_value("admin")
    at.text_input[1].set_value("clave-segura-123")
    at.button[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert "token" in at.session_state
    return at


def test_login_rejects_bad_password(seeded):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.text_input[0].set_value("admin")
    at.text_input[1].set_value("mala")
    at.button[0].click().run()
    assert "token" not in at.session_state
    assert any("incorrectos" in e.value for e in at.error)


def test_all_views_render_without_exceptions(seeded):
    at = _login(AppTest.from_file(APP, default_timeout=60))
    assert at.title[0].value.startswith("📊")  # dashboard por defecto
    for view in VIEWS:
        at.switch_page(view).run()
        assert not at.exception, (view, [e.value for e in at.exception])
        assert at.title, view


def test_dashboard_shows_pending_and_metrics(seeded):
    at = _login(AppTest.from_file(APP, default_timeout=60))
    labels = [m.label for m in at.metric]
    assert "Capital disponible" in labels and "Aprobaciones pendientes" in labels
    pend = next(m for m in at.metric if m.label == "Aprobaciones pendientes")
    assert pend.value == "1"  # el borrador del agente


def test_approve_draft_from_approvals_view(seeded, session):
    at = _login(AppTest.from_file(APP, default_timeout=60))
    at.switch_page("views/aprobaciones.py").run()
    assert not at.exception
    btn = next(b for b in at.button if b.label == "Aprobar")
    btn.click().run()
    assert not at.exception, [e.value for e in at.exception]
    from mototrade.core import approvals

    assert approvals.pending(session) == []


def test_kill_switch_from_config_view(seeded, session):
    at = _login(AppTest.from_file(APP, default_timeout=60))
    at.switch_page("views/configuracion.py").run()
    btn = next(b for b in at.button if "PARADA" in b.label)
    btn.click().run()
    assert not at.exception, [e.value for e in at.exception]
    from mototrade.core import kill_switch

    assert kill_switch.is_engaged(session)
    assert any("ACTIVA" in e.value for e in at.error)


def test_radar_scan_button_and_listing_form(seeded, session):
    at = _login(AppTest.from_file(APP, default_timeout=60))
    at.switch_page("views/radar.py").run()
    assert not at.exception
    btn = next(b for b in at.button if "Analizar" in b.label)
    btn.click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert session.query(Deal).count() == 1


def test_control_center_save_from_ui(seeded, session):
    at = _login(AppTest.from_file(APP, default_timeout=60))
    at.switch_page("views/centro_control.py").run()
    assert not at.exception, [e.value for e in at.exception]
    # el formulario de capital es el primero: cambiar "Máximo de motos simultáneas" y guardar
    inp = next(n for n in at.number_input if n.label.startswith("Máximo de motos"))
    inp.set_value(3)
    btn = next(b for b in at.button if b.label == "Guardar capital")
    btn.click().run()
    assert not at.exception, [e.value for e in at.exception]
    from mototrade.control import ControlCenter

    assert ControlCenter(session).load().capital.max_units == 3
