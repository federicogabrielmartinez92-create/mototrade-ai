"""Sin credenciales de Telegram: el worker corre, las alertas quedan en el centro de notificaciones de la app."""
from mototrade.config import get_settings
from mototrade.core import notifications as notif, outbound
from mototrade.core.money import D
from mototrade.db.models import Notification, OutboundAction
from mototrade.integrations.telegram import ConsoleNotifier
from mototrade.modules.finance import FinanceService
from mototrade.modules.radar import ManualSource, RadarService, RawListing
from mototrade.modules.valuation import ValuationService
from mototrade.worker import JOBS, run_all


def test_settings_have_no_telegram_and_no_api_key():
    s = get_settings()
    assert not s.telegram_enabled and not s.api_key_enabled or s.api_key.get_secret_value() == "test-api-key-0123456789"


def test_notify_is_persistent_and_idempotent(session):
    a = notif.notify(session, key="k1", title="Hola", body="cuerpo", level="alert", category="opportunity", entity_type="deal", entity_id=1)
    b = notif.notify(session, key="k1", title="otro", body="x")
    assert a.id == b.id and session.query(Notification).count() == 1
    assert notif.unread_count(session) == 1
    # sin Telegram configurado NO se encola nada hacia afuera
    assert session.query(OutboundAction).count() == 0
    assert notif.mark_read(session, [a.id], by="admin") == 1 and notif.unread_count(session) == 0
    assert notif.mark_read(session, [a.id], by="admin") == 0
    notif.notify(session, key="k2", title="dos"); notif.notify(session, key="k3", title="tres")
    assert notif.mark_all_read(session, by="admin") == 2
    assert [r.title for r in notif.list_notifications(session, limit=10)] == ["tres", "dos", "Hola"]


def test_worker_without_telegram_lands_alerts_in_app(session, rules, monkeypatch):
    monkeypatch.setenv("MOTOTRADE_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("MOTOTRADE_TELEGRAM_CHAT_ID", "")
    get_settings.cache_clear()
    FinanceService(session, rules).ensure_initial_capital()
    vs = ValuationService(session, rules)
    for p in (2_650_000, 2_700_000, 2_600_000, 2_680_000, 2_620_000):
        vs.add_comparable(kind="asking", make="Honda", model="Wave 110", year=2022, kilometers=14000, price=D(p), source="manual")
    RadarService(session, rules).ingest(ManualSource([RawListing(source="manual", make="Honda", model="Wave 110", year=2022,
                                                                  kilometers=15000, asking_price=D(1_500_000), location="Rosario",
                                                                  external_id="nt1")]))
    runs = run_all(session, ConsoleNotifier())
    by = {r.job_name: r for r in runs}
    assert by["telegram_inbox"].status == "skipped" and "opcional" in by["telegram_inbox"].stats["reason"]
    assert all(r.status in ("ok", "skipped") for r in runs), [(r.job_name, r.error) for r in runs]
    alerts = notif.list_notifications(session, unread_only=True)
    assert alerts and alerts[0].category == "opportunity" and alerts[0].level == "alert"
    assert session.query(OutboundAction).count() == 0  # nada salió por Telegram
    # segunda corrida: no duplica
    run_all(session, ConsoleNotifier())
    assert session.query(Notification).count() == len(alerts)


def test_notify_enqueues_telegram_copy_only_when_configured(session, monkeypatch):
    monkeypatch.setenv("MOTOTRADE_TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("MOTOTRADE_TELEGRAM_CHAT_ID", "55")
    get_settings.cache_clear()
    try:
        outbound.notify_operator(session, "Título\ncuerpo", key="tg1")
        assert session.query(Notification).count() == 1
        row = session.query(OutboundAction).one()
        assert row.channel == "telegram" and row.payload["text"].startswith("Título")
    finally:
        monkeypatch.setenv("MOTOTRADE_TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setenv("MOTOTRADE_TELEGRAM_CHAT_ID", "")
        get_settings.cache_clear()


def test_api_key_disabled_when_empty(monkeypatch):
    from fastapi.testclient import TestClient
    from mototrade.api import create_app

    monkeypatch.setenv("MOTOTRADE_API_KEY", "")
    get_settings.cache_clear()
    try:
        c = TestClient(create_app())
        assert c.get("/status", headers={"X-API-Key": ""}).status_code == 401
        assert c.get("/health").status_code == 200
    finally:
        monkeypatch.setenv("MOTOTRADE_API_KEY", "test-api-key-0123456789")
        get_settings.cache_clear()
