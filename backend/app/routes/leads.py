import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Header, HTTPException, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.database.database import SessionLocal
from app.database.models import Lead
from app.services.product_repository import ProductRepository

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

# Google Apps Script trigger (spec section 6) should send the shared
# secret as a header, matching ATLAS_WEBHOOK_SECRET in Atlas/.env:
#
#   UrlFetchApp.fetch(url, {
#     method: 'post', contentType: 'application/json',
#     headers: { 'X-Webhook-Secret': WEBHOOK_SECRET },
#     payload: JSON.stringify(payload)
#   });

# Best-effort, case-insensitive column-name matching for the VA's Google
# Sheet -- the real header names are still unknown (spec section 8 flags
# this as an open decision to confirm during build). Adjust these once the
# actual sheet is seen; raw_sheet_data always preserves the full row
# regardless, so nothing is lost even if a mapping here is wrong.
ASIN_ALIASES = ["asin"]
# "Sourcing Method" on the real sheet holds values like "EUA2A"/"UKA2A", not
# a strict OA/A2A pair -- normalized in _extract_sourcing_type below rather
# than stored verbatim.
SOURCING_TYPE_ALIASES = ["sourcing_type", "sourcing type", "sourcing method", "type", "oa/a2a"]
VA_ROI_ALIASES = ["roi", "roi%", "roi %"]
VA_PROFIT_ALIASES = ["profit", "net profit", "expected profit"]
VA_COST_PRICE_ALIASES = ["cost_price", "cost", "cost price", "buy cost", "unit cost", "actual cog", "cog (unit)"]
VA_SALE_PRICE_ALIASES = ["sale_price", "sale price", "sell price", "selling price"]


def _extract(payload: dict, aliases: list) -> str | None:
    normalized = {str(k).strip().lower(): v for k, v in payload.items()}

    for alias in aliases:
        value = normalized.get(alias)
        if value not in (None, ""):
            return value

    return None


def _normalize_sourcing_type(value) -> str | None:
    """
    The real sheet's "Sourcing Method" column holds values like "EUA2A" /
    "UKA2A" rather than a strict OA/A2A pair -- fold anything containing
    "A2A" or "OA" down to that, and pass anything else through as-is
    rather than discarding it.
    """
    if value is None:
        return None

    text = str(value).strip()
    upper = text.upper()

    if "A2A" in upper:
        return "A2A"
    if "OA" in upper:
        return "OA"

    return text or None


def _extract_float(payload: dict, aliases: list) -> float | None:
    value = _extract(payload, aliases)

    if value is None:
        return None

    try:
        return float(str(value).replace("%", "").replace(",", "").strip())
    except ValueError:
        return None


@router.post("/api/webhook/sheet-lead")
def sheet_lead_webhook(payload: dict, x_webhook_secret: str = Header(default=None)):
    expected_secret = os.getenv("ATLAS_WEBHOOK_SECRET")

    if not expected_secret or x_webhook_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid or missing webhook secret")

    asin = _extract(payload, ASIN_ALIASES)

    if not asin:
        raise HTTPException(status_code=400, detail="Payload has no recognizable ASIN column")

    db = SessionLocal()
    try:
        lead = Lead(
            asin=str(asin).strip().upper(),
            source="sheet",
            sourcing_type=_normalize_sourcing_type(_extract(payload, SOURCING_TYPE_ALIASES)),
            raw_sheet_data=json.dumps(payload),
            va_roi=_extract_float(payload, VA_ROI_ALIASES),
            va_profit=_extract_float(payload, VA_PROFIT_ALIASES),
            va_cost_price=_extract_float(payload, VA_COST_PRICE_ALIASES),
            va_sale_price=_extract_float(payload, VA_SALE_PRICE_ALIASES),
            status="queued",
        )
        db.add(lead)
        db.commit()
        db.refresh(lead)

        return {"id": lead.id, "status": "queued"}
    finally:
        db.close()


@router.get("/api/leads")
def list_leads_api(status: str | None = None):
    db = SessionLocal()
    try:
        query = db.query(Lead)

        if status:
            query = query.filter(Lead.status == status)

        leads = query.order_by(Lead.added_at.desc()).all()

        return [
            {
                "id": lead.id,
                "asin": lead.asin,
                "source": lead.source,
                "sourcing_type": lead.sourcing_type,
                "status": lead.status,
                "verdict": lead.verdict,
                "rationale": lead.rationale,
                "va_roi": lead.va_roi,
                "va_profit": lead.va_profit,
                "added_at": lead.added_at.isoformat() if lead.added_at else None,
                "analyzed_at": lead.analyzed_at.isoformat() if lead.analyzed_at else None,
            }
            for lead in leads
        ]
    finally:
        db.close()


class ReviewRequest(BaseModel):
    decision: str  # "approved" | "rejected"


@router.post("/api/leads/{lead_id}/review")
def review_lead_api(lead_id: int, body: ReviewRequest):
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'rejected'")

    db = SessionLocal()
    try:
        lead = db.get(Lead, lead_id)

        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")

        lead.decision = body.decision
        lead.status = "reviewed"
        lead.reviewed_at = datetime.now(timezone.utc)
        db.commit()

        return {"id": lead.id, "status": lead.status, "decision": lead.decision}
    finally:
        db.close()


@router.get("/review")
def review_page(request: Request):
    db = SessionLocal()
    try:
        leads = (
            db.query(Lead)
            .filter(Lead.status == "analyzed")
            .order_by(Lead.analyzed_at.desc())
            .all()
        )

        rows = []
        for lead in leads:
            metrics = {}
            if lead.keepa_metrics:
                try:
                    metrics = json.loads(lead.keepa_metrics)
                except Exception:
                    metrics = {}
            rows.append({"lead": lead, "metrics": metrics})
    finally:
        db.close()

    return templates.TemplateResponse(
        request=request,
        name="review_lead.html",
        context={"request": request, "rows": rows},
    )


@router.get("/review/history")
def review_history_page(request: Request, decision: str = ""):
    """
    Browsable history of every reviewed lead (approved AND rejected),
    newest-decided first -- previously reviewing a lead just made it
    vanish from /review with no way to look back at what was decided
    or why. Same Product/Category/Price/Sales-evidence enrichment as
    the Lead Queue table itself, built from the same keepa_metrics
    JSON every analyzed lead already carries (see VerdictService/
    LeadAnalysisService) -- no new Keepa lookups here, this is pure
    history browsing.
    """
    db = SessionLocal()
    try:
        base_query = db.query(Lead).filter(Lead.status == "reviewed")

        approved_count = base_query.filter(Lead.decision == "approved").count()
        rejected_count = base_query.filter(Lead.decision == "rejected").count()
        oos_count = base_query.filter(Lead.decision == "oos").count()

        query = base_query
        if decision in ("approved", "rejected", "oos"):
            query = query.filter(Lead.decision == decision)

        leads = query.order_by(Lead.reviewed_at.desc()).limit(300).all()

        rows = []
        for lead in leads:
            metrics = {}
            if lead.keepa_metrics:
                try:
                    metrics = json.loads(lead.keepa_metrics)
                except Exception:
                    metrics = {}
            rows.append({"lead": lead, "metrics": metrics})
    finally:
        db.close()

    return templates.TemplateResponse(
        request=request,
        name="reviewed_leads.html",
        context={
            "request": request,
            "rows": rows,
            "decision": decision,
            "approved_count": approved_count,
            "rejected_count": rejected_count,
            "oos_count": oos_count,
            "total_count": approved_count + rejected_count + oos_count,
        }
    )


@router.post("/review/decide")
def review_decide(
    lead_id: int = Form(...),
    decision: str = Form(...),
    return_to: str = Form("/review"),
):
    """
    decision: "approved" | "rejected" | "oos" (Amazon out of stock
    right now -- not actionable this instant, but worth catching WHEN
    it restocks rather than losing it entirely to a plain reject).
    All three clear the lead from the pending Lead Queue the same way
    (status="reviewed") -- "oos" is just a third bucket in Reviewed
    History (see reviewed_leads.html) rather than a real approve/
    reject verdict.

    "oos" ALSO auto-adds the ASIN to the existing Watchlist, reusing
    its already-built re-check machinery (WatchlistService.check_stale
    runs weekly, or visit /watchlist to force an immediate recheck)
    instead of building a parallel monitoring mechanism -- title/brand
    come from the lead's own keepa_metrics (see VerdictService/
    LeadAnalysisService), so no extra Keepa lookup is needed here.
    """
    db = SessionLocal()
    try:
        lead = db.get(Lead, lead_id)

        if lead is not None:
            lead.decision = decision
            lead.status = "reviewed"
            lead.reviewed_at = datetime.now(timezone.utc)
            db.commit()

            if decision == "oos":
                title, brand = "", ""

                if lead.keepa_metrics:
                    try:
                        metrics = json.loads(lead.keepa_metrics)
                        title = metrics.get("title") or ""
                        brand = metrics.get("brand") or ""
                    except Exception:
                        pass

                ProductRepository.add_watch(
                    lead.asin, title=title, brand=brand,
                    note="Amazon OOS at review -- watching for restock",
                )
    finally:
        db.close()

    return RedirectResponse(url=return_to, status_code=303)


@router.get("/review/{lead_id}")
def review_detail(request: Request, lead_id: int):
    db = SessionLocal()
    try:
        lead = db.get(Lead, lead_id)
        metrics = {}

        if lead and lead.keepa_metrics:
            try:
                metrics = json.loads(lead.keepa_metrics)
            except Exception:
                metrics = {}
    finally:
        db.close()

    return templates.TemplateResponse(
        request=request,
        name="review_lead_detail.html",
        context={"request": request, "lead": lead, "metrics": metrics},
    )
