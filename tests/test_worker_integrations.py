import json

import httpx
import pytest

from mototrade.core import approvals, kill_switch, outbound
from mototrade.core.enums import ApprovalKind, UserRole
from mototrade.core.money import D
from mototrade.db.models import JobRun, OutboundAction
from mototrade.integrations.telegram import ConsoleNotifier, TelegramNotifier
from mototrade.modules.finance import FinanceService
from mototrade.modules.radar import ManualSource, RadarService, RawListing
from mototrade.modules.valuation import ValuationService
from mototrade.worker import JOBS, run_all, run_job


# ---- Telegram con transporte simulado ---------------------------------------
def _mock_transport(status=200, body=None, raise_exc=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc:
            raise raise_exc
        assert request.url.path.endswith("/sendMessage")
        payload = json.loads(request.content)
        assert payload["chat_id"] == "123" and "text" in payload
        return httpx.Response(status, json=body or {"ok": True, "result": {"message_id": 42}})
    return httpx.MockTransport(handler)


def test_telegram_notifier_success_and_failure():
    n = TelegramNotifier("tok", "123", transport=_mock_transport())
    r = n.send("hola")
    assert r.ok and r.external_ref == "42" and not r.simulated
    n = TelegramNotifier("tok", "123", transport=_mock_transport(401, {"ok": False, "description": "Unauthorized"}))
    r = n.send("hola")
    assert not r.ok and "Unauthorized" in r.error
    n = TelegramNotifier("tok", "123", transport=_mock_transport(raise_exc=httpx.ConnectError("boom")))
    r = n.send("hola")
    assert not r.ok and "ConnectError" in r.error
    with pytest.raises(ValueError):
        TelegramNotifier("", "")


# ---- outbound ----------------------------------------------------------------
def test_outbound_idempotent_and_requires_approval(session):
    n = ConsoleNotifier()
    a = outbound.enqueue(session, kind="send_message", channel="telegram", payload={"text": "x"}, idempotency_key="k1")
    b = outbound.enqueue(session, kind="send_message", channel="telegram", payload={"text": "x"}, idempotency_key="k1")
    assert a.id == b.id
    stats = outbound.execute_pending(session, n)
    assert stats["skipped"] == 1 and a.status == "queued"  # sin aprobación no se envía
    req = approvals.request(session, kind=ApprovalKind.SEND_MESSAGE, entity_type="message", entity_id=1,
                            payload={"t": 1}, summary="s", requested_by="agent")
    a.approval_id = req.id
    approvals.decide(session, req.id, approve=True, by="ope", role=UserRole.OPERATOR)
    stats = outbound.execute_pending(session, n)
    assert stats["sent"] == 1 and a.status == "sent" and n.sent == ["x"]
    assert outbound.execute_pending(session, n)["sent"] == 0  # no se reenvía


def _tg(session, text, key):
    """Encola una copia a Telegram directamente (notify_operator solo lo hace si Telegram está configurado)."""
    return outbound.enqueue(session, kind="notify_operator", channel="telegram", payload={"text": text}, idempotency_key=f"notify:{key}")


def test_outbound_blocked_by_kill_switch_and_unknown_channel(session):
    n = ConsoleNotifier()
    _tg(session, "aviso", "a1")
    wa = outbound.enqueue(session, kind="send_message", channel="whatsapp", payload={"text": "y"}, idempotency_key="wa1")
    stats = outbound.execute_pending(session, n)
    assert stats["sent"] == 1 and stats["blocked"] == 1 and wa.status == "blocked" and "whatsapp" in wa.last_error
    _tg(session, "aviso2", "a2")
    kill_switch.engage(session, by="admin")
    stats = outbound.execute_pending(session, n)
    assert stats["blocked"] == 1 and stats["sent"] == 0


def test_outbound_retries_and_gives_up(session):
    class Flaky:
        name = "flaky"
        calls = 0

        def send(self, text):
            self.calls += 1
            from mototrade.integrations.telegram import NotifyResult
            return NotifyResult(ok=False, error="down")

    f = Flaky()
    row = _tg(session, "x", "r1")
    for _ in range(outbound.MAX_ATTEMPTS + 2):
        outbound.execute_pending(session, f)
    assert row.attempts == outbound.MAX_ATTEMPTS and row.status == "failed" and f.calls == outbound.MAX_ATTEMPTS


# ---- worker ------------------------------------------------------------------
def test_worker_runs_all_jobs_and_records(session, rules):
    fin = FinanceService(session, rules)
    fin.ensure_initial_capital()
    vs = ValuationService(session, rules)
    for p in (2_650_000, 2_700_000, 2_600_000, 2_680_000):
        vs.add_comparable(kind="asking", make="Honda", model="Wave 110", year=2022, kilometers=14000, price=D(p), source="manual")
    RadarService(session, rules).ingest(ManualSource([RawListing(source="manual", make="Honda", model="Wave 110", year=2022,
                                                                  kilometers=15000, asking_price=D(1_500_000), location="Rosario",
                                                                  photos=["a.jpg"], external_id="w1")]))
    n = ConsoleNotifier()
    runs = run_all(session, n)
    session.commit()
    assert [r.job_name for r in runs] == list(JOBS)
    by = {r.job_name: r for r in runs}
    assert by["telegram_inbox"].status == "skipped"  # sin token: se informa, no falla
    assert all(r.status == "ok" for r in runs if r.job_name != "telegram_inbox"), [(r.job_name, r.error) for r in runs]
    radar = by["radar_scan"]
    assert radar.stats["new_opportunities"] == 1 and radar.stats["extraordinary_new"] == 1
    from mototrade.core import notifications as notif

    alerts = notif.list_notifications(session, unread_only=True)
    assert any("EXTRAORDINARIA" in a.title and "score" in a.title for a in alerts)  # quedó en el centro de notificaciones
    assert n.sent == []  # sin Telegram configurado no sale nada
    # segunda pasada: idempotente, sin nuevas oportunidades ni reenvíos
    n2 = ConsoleNotifier()
    runs2 = run_all(session, n2)
    assert next(r for r in runs2 if r.job_name == "radar_scan").stats["new_opportunities"] == 0
    assert n2.sent == [] and len(notif.list_notifications(session)) == len(alerts)
    assert session.query(JobRun).count() == 2 * len(JOBS)


def test_worker_job_error_is_captured(session, monkeypatch):
    def boom(session, notifier):
        raise RuntimeError("explotó")
    monkeypatch.setitem(JOBS, "radar_scan", boom)
    run = run_job(session, "radar_scan", ConsoleNotifier())
    assert run.status == "error" and "explotó" in run.error


def test_worker_respects_kill_switch_and_pause(session):
    kill_switch.engage(session, by="admin")
    run = run_job(session, "radar_scan", ConsoleNotifier())
    assert run.status == "skipped" and "kill" in run.stats["reason"]
    kill_switch.release(session, by="admin")
    kill_switch.set_worker_paused(session, True, by="admin")
    run = run_job(session, "radar_scan", ConsoleNotifier())
    assert run.status == "skipped" and "pausado" in run.stats["reason"]


def test_worker_suspends_and_recovers_with_capital_and_inventory(session, rules):
    """Suspensión y recuperación de tareas programadas: sin capital → radar 'skipped' con motivo; con capital → corre;
    inventario completo → se pausa otra vez; desactivación manual → no se reactiva sola."""
    from mototrade.control import ControlCenter
    from mototrade.db.models import InventoryItem

    n = ConsoleNotifier()
    r = run_job(session, "radar_scan", n)
    assert r.status == "skipped" and "capital libre" in r.stats["reason"]
    FinanceService(session, rules).ensure_initial_capital()
    assert run_job(session, "radar_scan", n).status == "ok"
    ControlCenter(session).update_section("capital", {"max_units": 1}, by="admin")
    session.add(InventoryItem(make="Honda", model="Wave 110", purchase_price=D(1_000_000)))
    session.flush()
    r = run_job(session, "radar_scan", n)
    assert r.status == "skipped" and "inventario completo" in r.stats["reason"]
    ControlCenter(session).update_section("capital", {"max_units": 3}, by="admin")
    assert run_job(session, "radar_scan", n).status == "ok"
    ControlCenter(session).update_section("search", {"auto_search_enabled": False}, by="admin")
    for _ in range(2):
        r = run_job(session, "radar_scan", n)
        assert r.status == "skipped" and "manualmente" in r.stats["reason"]
    # el despacho de notificaciones y el vendedor siguen funcionando con la búsqueda pausada
    assert run_job(session, "outbound_dispatch", n).status == "ok"
    assert run_job(session, "pricing_review", n).status == "ok"
