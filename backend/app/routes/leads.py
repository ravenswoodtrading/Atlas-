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
from app.services.review_queue_service import ReviewQueueService, apply_lead_decision, clean_atlas_notes
from app.routes.verdict import highlight_figures
from app.services.verdict_service import resolve_source_marketplace

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")
# _verdict_metrics.html (shared with verdict.py's /verdict page) needs
# this filter too -- each route module owns its own Jinja2Templates
# instance in this codebase, so registering it once on verdict.py's
# copy doesn't cover renders that go through this one (was a latent
# 500 on every /review/<id> page, confirmed live 2026-08-23).
templates.env.filters["highlight_figures"] = highlight_figures

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
# Best-effort "where did this cost price come from" for a sheet lead --
# see Lead.source_detail's own docstring for why this is distinct from
# the source="sheet"/"manual" mechanism field. NULL if the VA's sheet
# has no matching column, same graceful-miss behaviour as every other
# alias list here.
SOURCE_DETAIL_ALIASES = ["source", "sourced from", "retailer", "supplier", "store", "url", "link"]
# Which EU Amazon an A2A row would be BOUGHT from, if the sheet says so
# in a column of its own. Usually it does not -- see
# _extract_source_marketplace for the fallbacks that matter more.
SOURCE_MARKETPLACE_ALIASES = [
    "source_marketplace", "source marketplace", "marketplace", "source country",
    "country", "buy from", "source market",
]
# VA's own free-text column -- read-only from Atlas's side, see
# Lead.va_notes's own docstring for why this is never written back to.
VA_NOTES_ALIASES = ["va notes", "va_notes", "notes", "comment", "comments", "summary"]
# Lead Sheet's own repeat-detection column (2026-09-07, Tamara: "the row
# itself has a column ... either says new ASIN or the date it was last
# added") -- read by google_sheets_lead_sync.py to populate
# SheetLeadSyncState.date_last_added, never mapped onto Lead itself.
DATE_LAST_ADDED_ALIASES = ["date last added", "last added", "date added"]
# The VA's decision/rating signal on the real Lead Sheet (confirmed with
# Tamara, 2026-09-05) -- "Avoid" | "Review" | "Ok", mapped in
# _normalize_client_rating below. Distinct from SOURCING_TYPE_ALIASES/
# VA_NOTES_ALIASES, which are different columns entirely.
CLIENT_RATING_ALIASES = ["client rating", "client_rating", "rating"]
# Real gaps found live, 2026-09-06 (Tamara): the sheet's own "Product
# Name" and "Source URL" columns were never captured at all -- a sheet
# lead had no title until Keepa analysis succeeded (which could be a
# while, or never), and the only "source" info kept was a free-text
# retailer NAME (source_detail), never an actual clickable link.
PRODUCT_NAME_ALIASES = ["product name", "title", "name"]
SOURCE_URL_ALIASES = ["source url", "source_url", "url", "link"]


def _normalize_header(text: str) -> str:
    """
    Lowercase + collapse ALL internal whitespace (not just leading/
    trailing) to a single space. Added 2026-09-05 after a real miss:
    Lead Sheet's actual "CoG \n(unit)" header has an embedded newline
    that survived a plain .strip().lower(), so it never matched the
    "cog (unit)" alias -- cost price silently only ever came from
    "Actual CoG" (usually blank pre-purchase). Robust to this and any
    similar future whitespace quirk without hardcoding a literal
    newline sequence.
    """
    return " ".join(str(text).strip().lower().split())


def _extract(payload: dict, aliases: list) -> str | None:
    normalized = {_normalize_header(k): v for k, v in payload.items()}

    for alias in aliases:
        value = normalized.get(_normalize_header(alias))
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


def _extract_source_marketplace(payload: dict) -> str | None:
    """
    Which EU Amazon this row would actually be bought from -- what
    VerdictService.check_source_marketplace needs before it can confirm
    the lead is buyable (Amazon or FBA on the buy box, Amazon in stock)
    rather than just profitable.

    Tried in descending order of trustworthiness: an explicit
    marketplace/country column, then the source/link column, which in
    practice holds the amazon.de|fr|es|it product URL the VA actually
    sourced from. The sheet's "Sourcing Method" column is deliberately
    NOT used as a fallback: its "EUA2A" value says the lead is EU A2A
    but not WHICH EU marketplace, and guessing one would spend a Keepa
    call producing a confident answer about the wrong country.

    None means "skip the check", never "assume DE" -- see
    resolve_source_marketplace.
    """
    return resolve_source_marketplace(
        _extract(payload, SOURCE_MARKETPLACE_ALIASES),
        _extract(payload, SOURCE_DETAIL_ALIASES),
    )


def _stringify(value) -> str | None:
    """A sheet cell can come through as any JSON type -- coerce to a plain string for a String column."""
    return str(value).strip() or None if value is not None else None


def _extract_float(payload: dict, aliases: list) -> float | None:
    """
    Real bug found live, 2026-09-06: Lead Sheet's real price cells come
    through as "£65.00"/"£37.99" -- the £ symbol was never stripped,
    so float() raised on every single one and va_sale_price/va_cost_
    price silently ended up None for every real lead. Strips every
    currency symbol Atlas deals with elsewhere (GBP/EUR/USD), not just
    £, since a VA/Sourcing Method column can reference an EU A2A buy.
    """
    value = _extract(payload, aliases)

    if value is None:
        return None

    cleaned = str(value)
    for symbol in ("£", "€", "$", "%", ","):
        cleaned = cleaned.replace(symbol, "")

    try:
        return float(cleaned.strip())
    except ValueError:
        return None


def normalize_client_rating(value) -> str | None:
    """
    Lead Sheet's real "Client Rating" column is the VA/client's decision
    signal. First checked with Tamara 2026-09-05 against a small sample
    (Avoid/Review/Ok) -- widened the same day after a real gap was found
    live: a full column scan of all 720 rows turned up a richer real
    vocabulary (Avoid 330, Good 120, Review 88, Ok 84, OOS 44, Already
    bought 21, blank 18, No purchase (other reason) 14) that this
    function didn't recognize, so genuinely already-decided leads
    (Good/OOS in particular) were being left stuck pending in Atlas.

    "Already bought" -> approved and "No purchase (other reason)" ->
    rejected are reasonable-but-not-explicitly-confirmed interpretations
    (flagged to Tamara) -- everything else here was either directly
    confirmed or is an unambiguous synonym (Good ~ Ok, OOS -> Atlas's
    own "oos" decision). "Review" and anything unrecognized/blank
    deliberately return None -- left pending for a human on either
    side, never guessed at.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("avoid", "no purchase (other reason)"):
        return "rejected"
    if text in ("ok", "good", "already bought"):
        return "approved"
    if text == "oos":
        return "oos"
    return None


def ingest_sheet_lead_row(payload: dict, db, *, existing_lead=None, new_submission=False) -> Lead:
    """
    Single shared entry point for "a VA sheet row became/updated an
    Atlas Lead" -- used by BOTH the webhook below (unchanged external
    behavior, kept alive in case Apps Script is ever revived) and the
    pull-based google_sheets_lead_sync.pull_and_ingest_va_leads
    (2026-09-05). Extracted so this mapping logic exists in exactly one
    place -- see that module's own docstring for why the transport
    changed but this logic didn't need to.

    Raises ValueError if the payload has no recognizable ASIN -- the
    caller decides what to do with that (webhook: 400; poller: skip
    and log).
    """
    asin = _extract(payload, ASIN_ALIASES)
    if not asin:
        raise ValueError("Payload has no recognizable ASIN column")
    asin = str(asin).strip().upper()

    # The poller passes the matched submission explicitly. Webhook callers
    # also match reviewed leads so a retrospective edit cannot reopen them.
    lead = existing_lead
    if lead is None and not new_submission:
        candidates = db.query(Lead).filter(Lead.asin == asin, Lead.source == 'sheet').all()
        submitted = str(_extract(payload, ['date']) or '').strip()
        if submitted:
            matched = []
            for candidate in candidates:
                try:
                    original = json.loads(candidate.raw_sheet_data or '{}')
                except (ValueError, TypeError):
                    continue
                if str(_extract(original, ['date']) or '').strip() == submitted:
                    matched.append(candidate)
            candidates = matched
        if len(candidates) > 1:
            raise ValueError('Ambiguous sheet submission; use the sheet sync to match the original row')
        lead = candidates[0] if candidates else None

    is_new = lead is None
    if is_new:
        lead = Lead(asin=asin, source="sheet")
        db.add(lead)

    lead.sourcing_type = _normalize_sourcing_type(_extract(payload, SOURCING_TYPE_ALIASES))
    lead.raw_sheet_data = json.dumps(payload)
    lead.va_roi = _extract_float(payload, VA_ROI_ALIASES)
    lead.va_profit = _extract_float(payload, VA_PROFIT_ALIASES)
    lead.va_cost_price = _extract_float(payload, VA_COST_PRICE_ALIASES)
    lead.va_sale_price = _extract_float(payload, VA_SALE_PRICE_ALIASES)
    lead.source_detail = _stringify(_extract(payload, SOURCE_DETAIL_ALIASES))
    lead.source_marketplace = _extract_source_marketplace(payload)
    lead.va_notes = _stringify(_extract(payload, VA_NOTES_ALIASES))
    lead.title = _stringify(_extract(payload, PRODUCT_NAME_ALIASES))
    lead.source_url = _stringify(_extract(payload, SOURCE_URL_ALIASES))
    if is_new:
        lead.status = "queued"
        lead.verdict = None
        lead.rationale = None
        lead.keepa_metrics = None
        lead.analysis_attempts = 0

    db.flush()
    return lead


@router.post("/api/webhook/sheet-lead")
def sheet_lead_webhook(payload: dict, x_webhook_secret: str = Header(default=None)):
    expected_secret = os.getenv("ATLAS_WEBHOOK_SECRET")

    if not expected_secret or x_webhook_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid or missing webhook secret")

    db = SessionLocal()
    try:
        try:
            lead = ingest_sheet_lead_row(payload, db)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        db.commit()
        db.refresh(lead)

        return {"id": lead.id, "status": lead.status}
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
    """
    Atlas nav consolidation Phase 1 (2026-08-24) -- the Lead Queue
    merged into Review Queue (see ReviewQueueService._lead_dict/
    _pending_leads). This page's own live queue view is gone; redirect
    anything bookmarked or Discord-linked to the merged page rather
    than 404ing. review_lead.html is now unused -- deleted alongside
    this change.
    """
    return RedirectResponse(url="/review-queue", status_code=302)


@router.get("/review/history")
def review_history_page(request: Request, decision: str = ""):
    """
    Merged reviewed history (nav consolidation fast-follow, 2026-08-24)
    -- was Lead-only; now also covers Products/Discovery/Watchlist's
    own reviewed scan records and buyable competitor detections, the
    same merge the live queue got in Phase 1. See
    ReviewQueueService.list_reviewed_history for the actual query/merge.
    """
    history = ReviewQueueService.list_reviewed_history(decision_filter=decision)

    return templates.TemplateResponse(
        request=request,
        name="reviewed_leads.html",
        context={
            "request": request,
            "rows": history["rows"],
            "decision": decision,
            "approved_count": history["approved_count"],
            "rejected_count": history["rejected_count"],
            "oos_count": history["oos_count"],
            "total_count": history["total_count"],
        }
    )


@router.post("/review/decide")
def review_decide(
    lead_id: int = Form(...),
    decision: str = Form(...),
    reason: str = Form(""),
    reason_category: str = Form(""),
    purchased_qty: str = Form(""),
    atlas_notes: str = Form(""),
    return_to: str = Form("/review"),
):
    db = SessionLocal()
    is_sheet_lead = False
    try:
        lead = db.get(Lead, lead_id)

        if lead is not None:
            is_sheet_lead = lead.source == "sheet"
            qty = None
            if is_sheet_lead and decision == "approved" and purchased_qty.strip():
                try:
                    qty = float(purchased_qty.strip())
                except ValueError:
                    qty = None
            cleaned_atlas_notes = clean_atlas_notes(atlas_notes)
            if cleaned_atlas_notes:
                lead.atlas_notes = cleaned_atlas_notes
            apply_lead_decision(lead, decision, reason, reason_category or None, purchased_qty=qty)
            db.commit()
    finally:
        db.close()

    # Push to Lead Sheet immediately -- best-effort, never blocks the
    # Atlas-side decision already committed above.
    if is_sheet_lead:
        try:
            from app.services.google_sheets_lead_sync import push_decision_to_sheet
            push_decision_to_sheet(lead_id)
        except Exception as exc:
            print(f"push_decision_to_sheet failed for lead {lead_id}: {exc}")

    return RedirectResponse(url=return_to, status_code=303)


class SheetLeadDecisionRequest(BaseModel):
    asin: str
    decision: str  # "approved" | "rejected" | "oos"
    reason: str | None = None


@router.post("/api/webhook/sheet-lead-decision")
def sheet_lead_decision_webhook(body: SheetLeadDecisionRequest, x_webhook_secret: str = Header(default=None)):
    """
    VA sheet -> Atlas half of the bidirectional review sync (2026-08-24)
    -- fires when a VA marks a lead's status/comment directly on the
    sheet (their thumbs up/down + "why not" column), instead of Atlas's
    own Review Queue UI. Applies the exact same apply_lead_decision the
    UI path uses, so a sheet-side review and an Atlas-side review are
    indistinguishable once recorded -- same OOS-auto-watch behavior,
    same Reviewed History bucket.

    Only ever matches the most recent UNDECIDED sheet-sourced lead for
    this ASIN -- if nothing matches (already decided by someone, on
    either side, or this ASIN was never sent to Atlas at all), this is
    a deliberate no-op rather than an error: "first decision wins" is
    the whole point of this sync (see the user's own "don't want to
    review the same lead twice" requirement), so a sheet edit arriving
    after Atlas already decided it should do nothing, not overwrite it.

    synced_to_sheet_at is stamped immediately, not left for the
    Atlas->sheet pull to find -- this decision's origin IS the sheet,
    it already has this information, there's nothing to push back.
    """
    expected_secret = os.getenv("ATLAS_WEBHOOK_SECRET")

    if not expected_secret or x_webhook_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid or missing webhook secret")

    if body.decision not in ("approved", "rejected", "oos"):
        raise HTTPException(status_code=400, detail="decision must be 'approved', 'rejected', or 'oos'")

    asin = body.asin.strip().upper()

    db = SessionLocal()
    try:
        lead = (
            db.query(Lead)
            .filter(Lead.asin == asin, Lead.source == "sheet", Lead.decision.is_(None))
            .order_by(Lead.added_at.desc())
            .first()
        )

        if lead is None:
            return {"updated": False, "reason": "No pending sheet-sourced lead found for this ASIN"}

        apply_lead_decision(lead, body.decision, body.reason)
        lead.synced_to_sheet_at = datetime.now(timezone.utc)
        db.commit()

        return {"updated": True, "lead_id": lead.id}
    finally:
        db.close()


@router.get("/api/leads/pending-sheet-sync")
def pending_sheet_sync(x_webhook_secret: str = Header(default=None)):
    """
    Atlas -> VA sheet half of the bidirectional review sync (2026-08-24)
    -- polled periodically by an Apps Script time trigger. Returns
    every sheet-sourced lead that was decided in Atlas's OWN Review
    Queue UI (not via the sheet, which stamps synced_to_sheet_at itself
    at decide-time -- see sheet_lead_decision_webhook) and hasn't been
    pushed back to the sheet yet.

    Deliberately does NOT return leads that only have an AI verdict
    (BUY/WATCH/AVOID) with no human decision yet -- decision IS NOT
    NULL is the filter, not verdict. The user was explicit about this:
    only a real human decision should ever reach the sheet, never
    Atlas's own unreviewed AI judgment.

    Marks every returned lead's synced_to_sheet_at immediately, in the
    same request, rather than waiting for a separate "ack" call from
    the Apps Script side -- simpler, and consistent with every other
    best-effort integration in this codebase. The tradeoff: if the
    Apps Script fetch succeeds but then fails to actually write the
    row before finishing, that lead won't be retried on the next poll.
    Acceptable here (low-stakes internal tool, small batches, visible
    failures) -- a two-phase ack would close that gap at real added
    complexity for a lot of code that's likely to run more or less
    unattended.
    """
    expected_secret = os.getenv("ATLAS_WEBHOOK_SECRET")

    if not expected_secret or x_webhook_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid or missing webhook secret")

    db = SessionLocal()
    try:
        leads = (
            db.query(Lead)
            .filter(Lead.source == "sheet", Lead.decision.isnot(None), Lead.synced_to_sheet_at.is_(None))
            .order_by(Lead.reviewed_at.asc())
            .all()
        )

        results = [
            {"asin": lead.asin, "decision": lead.decision, "decision_reason": lead.decision_reason}
            for lead in leads
        ]

        now = datetime.now(timezone.utc)
        for lead in leads:
            lead.synced_to_sheet_at = now

        db.commit()

        return {"leads": results}
    finally:
        db.close()


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
