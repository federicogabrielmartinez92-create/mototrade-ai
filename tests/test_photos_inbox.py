import io
import json

import httpx
import pytest
from PIL import Image

from mototrade.core.money import D
from mototrade.db.models import Inspection, Listing, Photo, Repair, SystemState
from mototrade.integrations.telegram_inbox import TelegramInbox, parse_moto_command
from mototrade.modules.inspection import FakeVisionInspector, InspectionService, estimate_vision_tokens
from mototrade.modules.parts import compare_repair_vs_as_is
from mototrade.modules.photos import LocalPhotoStorage, PhotoError, PhotoService, SupabasePhotoStorage


def _jpeg(w=2400, h=1800, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _listing(session):
    l = Listing(source="manual", fingerprint="p", make="Honda", model="Wave 110", year=2022, asking_price=D(1))
    session.add(l)
    session.flush()
    return l


# ---- fotos ---------------------------------------------------------------------
def test_photo_store_normalizes_dedupes_and_rejects_garbage(session, tmp_path):
    svc = PhotoService(session, LocalPhotoStorage(tmp_path))
    l = _listing(session)
    raw = _jpeg()
    p1 = svc.store(entity_type="listing", entity_id=l.id, data=raw, filename="moto.jpg")
    assert p1.size_bytes < len(raw) and p1.mime_type == "image/jpeg" and p1.origin == "upload"
    img = Image.open(io.BytesIO(svc.read(p1)))
    assert max(img.size) == 1600  # redimensionada
    p2 = svc.store(entity_type="listing", entity_id=l.id, data=raw)  # misma foto → misma fila
    assert p2.id == p1.id and session.query(Photo).count() == 1
    with pytest.raises(PhotoError):
        svc.store(entity_type="listing", entity_id=l.id, data=b"no soy una imagen")
    with pytest.raises(PhotoError):
        svc.store(entity_type="listing", entity_id=l.id, data=b"\xff\xd8\xff" + b"basura")
    assert len(svc.for_entity("listing", l.id)) == 1


def test_supabase_storage_rest(session):
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, req.headers.get("authorization")))
        if req.method == "POST":
            return httpx.Response(200, json={"Key": "fotos/x"})
        return httpx.Response(200, content=b"bytes")

    st = SupabasePhotoStorage("https://proj.supabase.co", "service-key", transport=httpx.MockTransport(handler))
    blob = st.put("listing/1/abc.jpg", b"data", "image/jpeg")
    assert blob.path == "supabase://fotos/listing/1/abc.jpg" and st.get("listing/1/abc.jpg") == b"bytes"
    assert calls[0][1] == "/storage/v1/object/fotos/listing/1/abc.jpg" and "Bearer service-key" in calls[0][2]
    with pytest.raises(PhotoError):
        SupabasePhotoStorage("", "")


# ---- inspector con fotos, caché, presupuesto y reparaciones automáticas --------
def test_inspection_uses_stored_photos_and_caches(session, tmp_path):
    l = _listing(session)
    ps = PhotoService(session, LocalPhotoStorage(tmp_path))
    for c in ((10, 10, 10), (20, 20, 20)):
        ps.store(entity_type="listing", entity_id=l.id, data=_jpeg(800, 600, c))
    svc = InspectionService(session, FakeVisionInspector())
    insp = svc.run(entity_type="listing", entity_id=l.id, description="cacha rota y golpe en el tanque", moto="Honda Wave")
    assert len(insp.photo_ids) == 2 and insp.is_simulated and insp.cache_key
    # desperfectos medium/high generan solicitud de cotización (reparación 'suspected')
    reps = session.query(Repair).filter(Repair.finding_id.is_not(None)).all()
    assert reps and all(r.state == "suspected" for r in reps)
    again = svc.run(entity_type="listing", entity_id=l.id, description="cacha rota y golpe en el tanque", moto="Honda Wave")
    assert again.id == insp.id  # caché: misma entrada → misma inspección, sin nueva llamada
    assert session.query(Inspection).count() == 1
    f = next(f for f in insp.findings if f.severity in ("medium", "high"))
    rep = svc.confirm_finding(f, by="ope")
    assert rep.state == "observed"
    svc.discard_finding(insp.findings[0], by="ope")


def test_vision_token_estimate():
    assert estimate_vision_tokens(1600, 1200) == 58 * 43


def test_repair_vs_as_is():
    r = compare_repair_vs_as_is(market_price=D(2_400_000), repair_cost=D(80_000), as_is_haircut_pct=D("0.10"))
    assert r.recommendation == "repair" and r.profit_delta == D(160_000)
    r = compare_repair_vs_as_is(market_price=D(2_400_000), repair_cost=D(300_000), as_is_haircut_pct=D("0.10"))
    assert r.recommendation == "sell_as_is" and r.profit_delta < 0


# ---- Telegram inbox --------------------------------------------------------------
def test_parse_moto_command():
    raw = parse_moto_command("/moto Honda | Wave 110 | 2022 | 15.000 | 2.300.000 | Rosario | https://x.com/a | particular")
    assert raw.make == "Honda" and raw.model == "Wave 110" and raw.year == 2022 and raw.kilometers == 15000
    assert raw.asking_price == D(2_300_000) and raw.source_url == "https://x.com/a" and raw.seller_type == "particular"
    assert parse_moto_command("hola") is None
    with pytest.raises(ValueError):
        parse_moto_command("/moto Honda | Wave")
    with pytest.raises(ValueError):
        parse_moto_command("/moto Honda | Wave | 2022 | 100 | cero")


def _updates_transport(updates, photo_bytes, sent):
    def handler(req: httpx.Request):
        path = req.url.path
        if path.endswith("/getUpdates"):
            off = req.url.params.get("offset")
            batch = [u for u in updates if off is None or u["update_id"] >= int(off)]
            return httpx.Response(200, json={"ok": True, "result": batch})
        if path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/f1.jpg"}})
        if "/file/bot" in path:
            return httpx.Response(200, content=photo_bytes)
        if path.endswith("/sendMessage"):
            sent.append(json.loads(req.content)["text"])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_telegram_inbox_creates_listing_with_photo_and_persists_offset(session, rules, tmp_path, monkeypatch):
    from mototrade.modules import photos as photos_mod

    monkeypatch.setattr(photos_mod.service, "get_storage", lambda: LocalPhotoStorage(tmp_path))
    sent: list[str] = []
    updates = [
        {"update_id": 100, "message": {"chat": {"id": 555}, "text": "/start"}},
        {"update_id": 101, "message": {"chat": {"id": 555}, "caption": "/moto Gilera | Smash 110 | 2021 | 22000 | 1500000 | Funes",
                                        "photo": [{"file_id": "small", "file_size": 100}, {"file_id": "big", "file_size": 900}]}},
        {"update_id": 102, "message": {"chat": {"id": 555}, "text": "/moto Honda | Wave"}},
        {"update_id": 103, "message": {"chat": {"id": 999}, "text": "/moto Yamaha | YBR | 2020 | 1 | 2000000"}},
    ]
    inbox = TelegramInbox("tok", transport=_updates_transport(updates, _jpeg(900, 700), sent), allowed_chat_id="555")
    res = inbox.process(session, rules)
    assert res.processed == 3 and len(res.listings_created) == 1 and res.photos_stored == 1
    assert any("chat no autorizado" in e for e in res.errors) and any("faltan campos" in e for e in res.errors)
    l = session.get(Listing, res.listings_created[0])
    assert l.source == "telegram" and l.make == "Gilera" and l.asking_price == D(1_500_000)
    assert len(PhotoService(session, LocalPhotoStorage(tmp_path)).for_entity("listing", l.id)) == 1
    assert session.get(SystemState, "telegram_inbox_offset").value["offset"] == 104
    assert any(t.startswith("✅ Anuncio") for t in sent) and any(t.startswith("⚠️") for t in sent)
    # segunda pasada: usa el offset guardado → no reprocesa
    res2 = inbox.process(session, rules)
    assert res2.processed == 0 and session.query(Listing).count() == 1
