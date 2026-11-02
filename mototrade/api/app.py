"""API HTTP (FastAPI). Uso interno y para integraciones futuras (webhooks de canales autorizados)."""
from __future__ import annotations

from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile, File
from sqlalchemy import select
from sqlalchemy.orm import Session

from mototrade import __version__
from mototrade.config import BusinessRules, get_settings
from mototrade.control import ControlCenter, OperationsGate
from mototrade.control.service import ControlError
from mototrade.core import approvals, audit, kill_switch
from mototrade.core.enums import ApprovalStatus
from mototrade.db.models import ApprovalRequest, Conversation, Deal, InventoryItem, Listing, Valuation
from mototrade.modules.analytics import AnalyticsService
from mototrade.modules.finance import FinanceService
from mototrade.modules.negotiation import NegotiationService
from mototrade.modules.negotiation.service import NegotiationError
from mototrade.modules.radar import CsvSource, ManualSource, RadarService, RawListing
from mototrade.modules.valuation import CostInputs, ValuationService
from mototrade.security import AuthError, authenticate, issue_token
from mototrade.security.auth import Principal

from . import schemas as S
from .deps import current_principal, get_business_rules, get_db, require_role


def create_app() -> FastAPI:
    app = FastAPI(title="MotoTrade AI API", version=__version__, docs_url="/docs")

    # ---- sistema ---------------------------------------------------------
    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/status")
    def status_(db: Session = Depends(get_db), p: Principal = Depends(current_principal), rules: BusinessRules = Depends(get_business_rules)):
        s = get_settings()
        ok, broken = audit.verify_chain(db)
        gate = OperationsGate(db)
        return {"env": s.env, "kill_switch": kill_switch.is_engaged(db), "worker_paused": kill_switch.is_worker_paused(db),
                "rules": rules.summary(), "integrations": s.integration_status(), "security_warnings": s.security_warnings(),
                "audit_chain_ok": ok, "audit_broken_at": broken,
                "functions": {k: {"intended": v.intended, "effective": v.effective, "reason": v.reason}
                              for k, v in gate.snapshot().items()},
                "capital": gate.position.as_dict(), "ai_budget": {k: str(v) for k, v in gate.ai_budget.items()}}

    # ---- Centro de Control ------------------------------------------------
    @app.get("/control")
    def control_get(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
        cc = ControlCenter(db)
        return {"settings": cc.load().model_dump(mode="json"), "versions": cc.versions()}

    @app.put("/control/{section}")
    def control_update(section: str, body: S.SettingsUpdateIn, db: Session = Depends(get_db),
                       p: Principal = Depends(require_role("admin"))):
        try:
            new = ControlCenter(db).update_section(section, body.values, by=p.username)
        except ControlError as e:
            raise HTTPException(422, str(e))
        return new.model_dump(mode="json")

    @app.get("/control/functions")
    def control_functions(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
        g = OperationsGate(db)
        return {k: {"intended": v.intended, "effective": v.effective, "label": v.label, "reason": v.reason}
                for k, v in g.snapshot().items()}

    @app.post("/auth/login", response_model=S.TokenOut)
    def login(body: S.LoginIn, db: Session = Depends(get_db)):
        try:
            p = authenticate(db, body.username, body.password)
        except AuthError as e:
            raise HTTPException(401, str(e))
        return S.TokenOut(token=issue_token(p), username=p.username, role=p.role)

    @app.post("/system/kill-switch")
    def set_kill_switch(body: S.KillSwitchIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin", "operator"))):
        if body.engaged:
            kill_switch.engage(db, by=p.username, reason=body.reason)
        else:
            if p.role != "admin":
                raise HTTPException(403, "Solo un admin puede liberar la parada de emergencia.")
            kill_switch.release(db, by=p.username)
        return {"engaged": kill_switch.is_engaged(db)}

    # ---- radar -----------------------------------------------------------
    @app.get("/listings", response_model=list[S.ListingOut])
    def list_listings(status: str | None = None, limit: int = Query(100, le=500), db: Session = Depends(get_db),
                      p: Principal = Depends(current_principal)):
        q = select(Listing).order_by(Listing.id.desc()).limit(limit)
        if status:
            q = q.where(Listing.status == status)
        return list(db.execute(q).scalars())

    @app.post("/listings", response_model=S.IngestOut)
    def create_listing(body: S.ListingIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin", "operator")),
                       rules: BusinessRules = Depends(get_business_rules)):
        raw = RawListing(**body.model_dump())
        res = RadarService(db, rules).ingest(ManualSource([raw]), by=p.username)
        return S.IngestOut(**res.__dict__)

    @app.post("/listings/import-csv", response_model=S.IngestOut)
    async def import_csv(file: UploadFile = File(...), source: str = "csv_import", db: Session = Depends(get_db),
                         p: Principal = Depends(require_role("admin", "operator")), rules: BusinessRules = Depends(get_business_rules)):
        content = await file.read()
        try:
            res = RadarService(db, rules).ingest(CsvSource(content, source), by=p.username)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return S.IngestOut(**res.__dict__)

    @app.post("/radar/scan", response_model=list[S.DealOut])
    def radar_scan(db: Session = Depends(get_db), p: Principal = Depends(require_role("admin", "operator")),
                   rules: BusinessRules = Depends(get_business_rules)):
        return RadarService(db, rules).scan_opportunities(by=p.username)

    @app.get("/deals/{deal_id}/analysis")
    def deal_analysis(deal_id: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal),
                      rules: BusinessRules = Depends(get_business_rules)):
        deal = db.get(Deal, deal_id)
        if not deal or not deal.listing:
            raise HTTPException(404, "Deal inexistente")
        return RadarService(db, rules).analyze_listing(deal.listing).as_dict()

    @app.post("/listings/{listing_id}/photos", response_model=S.PhotoOut)
    async def upload_photo(listing_id: int, file: UploadFile = File(...), db: Session = Depends(get_db),
                           p: Principal = Depends(require_role("admin", "operator"))):
        from mototrade.modules.photos import PhotoError, PhotoService

        if db.get(Listing, listing_id) is None:
            raise HTTPException(404, "Anuncio inexistente")
        try:
            return PhotoService(db).store(entity_type="listing", entity_id=listing_id, data=await file.read(),
                                          filename=file.filename or "", by=p.username)
        except PhotoError as e:
            raise HTTPException(400, str(e))

    # ---- deals / tasación ------------------------------------------------
    @app.get("/deals", response_model=list[S.DealOut])
    def list_deals(stage: str | None = None, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
        q = select(Deal).order_by(Deal.id.desc())
        if stage:
            q = q.where(Deal.stage == stage)
        return list(db.execute(q).scalars())

    @app.post("/deals/{deal_id}/authorize", response_model=S.DealOut)
    def authorize_deal(deal_id: int, body: S.AuthorizeIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin")),
                       rules: BusinessRules = Depends(get_business_rules)):
        deal = db.get(Deal, deal_id)
        if not deal:
            raise HTTPException(404, "Deal inexistente")
        NegotiationService(db, rules).authorize_deal_limit(deal, max_price=body.max_price, by=p.username)
        return deal

    @app.post("/deals/{deal_id}/valuate", response_model=S.ValuationOut)
    def valuate(deal_id: int, body: S.ValuateIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin", "operator")),
                rules: BusinessRules = Depends(get_business_rules)):
        deal = db.get(Deal, deal_id)
        if not deal:
            raise HTTPException(404, "Deal inexistente")
        ci = CostInputs(repair_parts=body.repair_parts, repair_labor=body.repair_labor, transfer=body.transfer,
                        logistics=body.logistics, preparation=body.preparation, marketing=body.marketing)
        cap = FinanceService(db, rules).available_capital()
        row, _, _ = ValuationService(db, rules).valuate_deal(deal, cost_inputs=ci, purchase_price=body.purchase_price,
                                                             manual_market_price=body.manual_market_price,
                                                             available_capital=cap, by=p.username, notes=body.notes)
        return row

    @app.get("/deals/{deal_id}/valuations", response_model=list[S.ValuationOut])
    def deal_valuations(deal_id: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
        return list(db.execute(select(Valuation).where(Valuation.deal_id == deal_id).order_by(Valuation.id.desc())).scalars())

    # ---- aprobaciones ----------------------------------------------------
    @app.get("/approvals", response_model=list[S.ApprovalOut])
    def list_approvals(status: str = "pending", db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
        return list(db.execute(select(ApprovalRequest).where(ApprovalRequest.status == status).order_by(ApprovalRequest.id)).scalars())

    @app.post("/approvals/{approval_id}/decide", response_model=S.ApprovalOut)
    def decide(approval_id: int, body: S.DecideIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin", "operator"))):
        try:
            row = approvals.decide(db, approval_id, approve=body.approve, by=p.username, role=p.role, note=body.note)
        except approvals.ApprovalError as e:
            raise HTTPException(409, str(e))
        # sincroniza el borrador asociado si es un mensaje
        if row.entity_type == "message":
            from mototrade.db.models import Message

            msg = db.get(Message, int(row.entity_id))
            if msg:
                NegotiationService(db, ControlCenter(db).effective_rules()).apply_approval(msg)
        return row

    # ---- negociación -----------------------------------------------------
    @app.get("/conversations", response_model=list[S.ConversationOut])
    def list_conversations(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
        return list(db.execute(select(Conversation).order_by(Conversation.id.desc())).scalars())

    @app.get("/conversations/{conv_id}/messages", response_model=list[S.MessageOut])
    def conv_messages(conv_id: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal),
                      rules: BusinessRules = Depends(get_business_rules)):
        conv = db.get(Conversation, conv_id)
        if not conv:
            raise HTTPException(404, "Conversación inexistente")
        return NegotiationService(db, rules).messages(conv)

    @app.post("/conversations/{conv_id}/incoming", response_model=S.MessageOut)
    def incoming(conv_id: int, body: S.IncomingIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin", "operator")),
                 rules: BusinessRules = Depends(get_business_rules)):
        conv = db.get(Conversation, conv_id)
        if not conv:
            raise HTTPException(404, "Conversación inexistente")
        return NegotiationService(db, rules).record_incoming(conv, body.text, external_message_id=body.external_message_id)

    @app.post("/conversations/{conv_id}/agent-step", response_model=S.AgentStepOut)
    def agent_step(conv_id: int, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin", "operator")),
                   rules: BusinessRules = Depends(get_business_rules)):
        conv = db.get(Conversation, conv_id)
        if not conv:
            raise HTTPException(404, "Conversación inexistente")
        from mototrade.integrations.llm import get_llm

        try:
            r = NegotiationService(db, rules, get_llm()).agent_step(conv)
        except NegotiationError as e:
            raise HTTPException(409, str(e))
        return S.AgentStepOut(blocked=r.blocked, intent=r.decision.intent, amount=r.decision.amount,
                              rationale=r.decision.rationale, verdict=r.verdict,
                              message=S.MessageOut.model_validate(r.message) if r.message else None,
                              approval_id=r.approval_id, deal_approval_id=r.deal_approval_id)

    @app.post("/messages/{message_id}/mark-sent", response_model=S.MessageOut)
    def mark_sent(message_id: int, channel_ref: str | None = None, db: Session = Depends(get_db),
                  p: Principal = Depends(require_role("admin", "operator")), rules: BusinessRules = Depends(get_business_rules)):
        from mototrade.db.models import Message

        msg = db.get(Message, message_id)
        if not msg:
            raise HTTPException(404, "Mensaje inexistente")
        svc = NegotiationService(db, rules)
        svc.apply_approval(msg)
        try:
            svc.mark_sent(msg, by=p.username, channel_ref=channel_ref)
        except NegotiationError as e:
            raise HTTPException(409, str(e))
        return msg

    # ---- inventario / finanzas / analítica --------------------------------
    @app.get("/inventory", response_model=list[S.InventoryOut])
    def inventory(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
        return list(db.execute(select(InventoryItem).order_by(InventoryItem.id.desc())).scalars())

    @app.get("/finance/summary")
    def finance_summary(db: Session = Depends(get_db), p: Principal = Depends(current_principal), rules: BusinessRules = Depends(get_business_rules)):
        return FinanceService(db, rules).summary()

    @app.get("/analytics/kpis")
    def kpis(db: Session = Depends(get_db), p: Principal = Depends(current_principal), rules: BusinessRules = Depends(get_business_rules)):
        return AnalyticsService(db, rules).kpis()

    return app


app = create_app()
