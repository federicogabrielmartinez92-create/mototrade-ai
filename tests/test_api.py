import pytest
from fastapi.testclient import TestClient

from mototrade.api import create_app
from mototrade.api.deps import get_db
from mototrade.core.money import D
from mototrade.modules.valuation import ValuationService
from mototrade.security import create_user

API_KEY = {"X-API-Key": "test-api-key"}


@pytest.fixture()
def client(session):
    app = create_app()

    def _db():
        yield session
        session.flush()

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def test_health_and_auth_required(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/status").status_code == 401
    assert client.get("/status", headers={"X-API-Key": "mala"}).status_code == 401
    r = client.get("/status", headers=API_KEY)
    assert r.status_code == 200 and r.json()["audit_chain_ok"] is True and "integrations" in r.json()


def test_login_token_and_roles(client, session):
    create_user(session, "ope", "clave-segura-123", "operator")
    session.commit()
    r = client.post("/auth/login", json={"username": "ope", "password": "clave-segura-123"})
    assert r.status_code == 200
    tok = r.json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get("/deals", headers=h).status_code == 200
    # operador no puede autorizar límites (admin)
    r = client.post("/deals/1/authorize", json={"max_price": "1000"}, headers=h)
    assert r.status_code == 403
    assert client.post("/auth/login", json={"username": "ope", "password": "x"}).status_code == 401


def test_end_to_end_via_api(client, session, rules):
    from mototrade.modules.finance import FinanceService

    FinanceService(session, rules).ensure_initial_capital()  # sin capital el agente comprador queda pausado
    vs = ValuationService(session, rules)
    for p in (2_650_000, 2_700_000, 2_600_000, 2_680_000):
        vs.add_comparable(kind="asking", make="Honda", model="Wave 110", year=2022, kilometers=14000, price=D(p), source="manual")
    session.commit()
    # 1) alta de anuncio
    r = client.post("/listings", headers=API_KEY, json={"make": "Honda", "model": "Wave 110", "year": 2022, "kilometers": 15000,
                                                        "asking_price": "1500000", "location": "Rosario", "external_id": "api1",
                                                        "photos": ["a.jpg"], "description": "impecable"})
    assert r.status_code == 200 and len(r.json()["created"]) == 1
    listing_id = r.json()["created"][0]
    assert client.get("/listings", headers=API_KEY).json()[0]["id"] == listing_id
    # 2) radar -> oportunidad
    r = client.post("/radar/scan", headers=API_KEY)
    assert r.status_code == 200 and len(r.json()) == 1
    deal_id = r.json()[0]["id"]
    # 3) tasación
    r = client.post(f"/deals/{deal_id}/valuate", headers=API_KEY, json={"repair_parts": "20000"})
    assert r.status_code == 200
    val = r.json()
    assert val["basis"] == "asking_adjusted" and D(val["max_purchase_price"]) > 0 and val["scenarios"]["severe"]
    # 4) autorización de límite por admin (API key = admin)
    r = client.post(f"/deals/{deal_id}/authorize", headers=API_KEY, json={"max_price": val["max_purchase_price"]})
    assert r.status_code == 200 and D(r.json()["authorized_max_price"]) == D(val["max_purchase_price"])
    # 5) conversación de compra y paso del agente
    from mototrade.db.models import Deal
    from mototrade.modules.negotiation import NegotiationService

    deal = session.get(Deal, deal_id)
    conv = NegotiationService(session, rules).start_buy_conversation(deal, channel="manual")
    session.commit()
    r = client.post(f"/conversations/{conv.id}/agent-step", headers=API_KEY)
    assert r.status_code == 200 and not r.json()["blocked"] and r.json()["intent"] == "opening_offer"
    msg = r.json()["message"]
    assert msg["status"] == "draft" and D(msg["proposed_amount"]) <= D(val["max_purchase_price"])
    # 6) aprobación del borrador y marcado como enviado
    pend = client.get("/approvals", headers=API_KEY).json()
    assert any(a["id"] == msg["approval_id"] for a in pend)
    r = client.post(f"/approvals/{msg['approval_id']}/decide", headers=API_KEY, json={"approve": True})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    r = client.post(f"/messages/{msg['id']}/mark-sent", headers=API_KEY)
    assert r.status_code == 200 and r.json()["status"] == "sent"
    # 7) mensaje entrante y nueva ronda
    r = client.post(f"/conversations/{conv.id}/incoming", headers=API_KEY, json={"text": "no, mínimo 2 palos"})
    assert r.status_code == 200
    r = client.post(f"/conversations/{conv.id}/agent-step", headers=API_KEY)
    assert r.status_code == 200 and r.json()["intent"] in ("counter_offer", "escalate")
    msgs = client.get(f"/conversations/{conv.id}/messages", headers=API_KEY).json()
    assert len(msgs) >= 2
    # 8) kill switch por API
    assert client.post("/system/kill-switch", headers=API_KEY, json={"engaged": True, "reason": "x"}).json()["engaged"] is True
    r = client.post(f"/conversations/{conv.id}/agent-step", headers=API_KEY)
    assert r.json()["blocked"] is True
    assert client.get("/analytics/kpis", headers=API_KEY).status_code == 200
    assert client.get("/finance/summary", headers=API_KEY).status_code == 200


def test_csv_import_endpoint(client):
    csv = "make,model,year,kilometers,asking_price\nHonda,Wave 110,2021,20000,2100000\n"
    r = client.post("/listings/import-csv", headers=API_KEY, files={"file": ("x.csv", csv, "text/csv")})
    assert r.status_code == 200 and len(r.json()["created"]) == 1
    r = client.post("/listings/import-csv", headers=API_KEY, files={"file": ("x.csv", "a,b\n1,2\n", "text/csv")})
    assert r.status_code == 400


def test_control_center_endpoints_and_status(client, session):
    r = client.get("/control", headers=API_KEY)
    assert r.status_code == 200 and r.json()["settings"]["capital"]["max_units"] == 2
    r = client.put("/control/capital", headers=API_KEY, json={"values": {"max_units": 3}})
    assert r.status_code == 200 and r.json()["capital"]["max_units"] == 3
    assert client.put("/control/capital", headers=API_KEY, json={"values": {"max_units": -5}}).status_code == 422
    assert client.put("/control/nada", headers=API_KEY, json={"values": {}}).status_code == 422
    st = client.get("/status", headers=API_KEY).json()
    assert st["functions"]["search"]["intended"] is True and st["functions"]["search"]["effective"] is False  # sin capital
    assert "capital libre" in st["functions"]["search"]["reason"]
    f = client.get("/control/functions", headers=API_KEY).json()
    assert f["auto_send"]["effective"] is False


def test_photo_upload_endpoint(client, session, tmp_path, monkeypatch):
    import io
    from PIL import Image
    from mototrade.modules import photos as photos_mod
    from mototrade.modules.photos import LocalPhotoStorage

    monkeypatch.setattr(photos_mod.service, "get_storage", lambda: LocalPhotoStorage(tmp_path))
    r = client.post("/listings", headers=API_KEY, json={"make": "Honda", "model": "Wave 110", "asking_price": "1000000", "external_id": "ph1"})
    lid = r.json()["created"][0]
    buf = io.BytesIO(); Image.new("RGB", (640, 480), (1, 2, 3)).save(buf, format="PNG")
    r = client.post(f"/listings/{lid}/photos", headers=API_KEY, files={"file": ("a.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200 and r.json()["origin"] == "upload"
    r = client.post(f"/listings/{lid}/photos", headers=API_KEY, files={"file": ("a.txt", b"hola", "text/plain")})
    assert r.status_code == 400
    assert client.post("/listings/9999/photos", headers=API_KEY, files={"file": ("a.png", buf.getvalue(), "image/png")}).status_code == 404
