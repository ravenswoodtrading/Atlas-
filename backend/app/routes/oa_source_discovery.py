import json
import os

from fastapi import APIRouter, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse

from app.database.database import SessionLocal
from app.database.models import OaSourceRun, OaSourceCandidate
from app.services.oa_source_discovery_service import OaSourceDiscoveryService
from app.services.seller_watch_service import SellerWatchService
from app.services import serpapi_client

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.get("/oa-discovery")
def oa_discovery_page(request: Request, run_id: int = 0):
    db = SessionLocal()

    try:
        runs = db.query(OaSourceRun).order_by(OaSourceRun.started_at.desc()).limit(20).all()

        selected_run = db.get(OaSourceRun, run_id) if run_id else (runs[0] if runs else None)

        candidates = []
        if selected_run:
            candidates = (
                db.query(OaSourceCandidate)
                .filter(OaSourceCandidate.run_id == selected_run.id)
                .order_by(
                    OaSourceCandidate.estimated_profit_gbp.desc().nullslast(),
                    OaSourceCandidate.match_confidence_pct.desc(),
                )
                .all()
            )
    finally:
        db.close()

    # How many distinct tracked competitors have ever been detected
    # selling each candidate ASIN -- computed live (not persisted on
    # OaSourceCandidate) so it always reflects Competitor Watch's
    # current state, even when viewing an older run. Attached directly
    # onto each candidate object for the template to read as
    # c.competitor_count -- see SellerWatchService.distinct_seller_counts.
    if candidates:
        competitor_counts = SellerWatchService.distinct_seller_counts([c.asin for c in candidates])
        for c in candidates:
            c.competitor_count = competitor_counts.get(c.asin, 0)
            # Every Google Shopping result considered for this ASIN,
            # not just the auto-picked winner -- parsed here (not in
            # the template, which can't run json.loads) so the results
            # page can show "other retailers found" and let the user
            # pick a different one via update_candidate. [] if no
            # shopping search ran or it found nothing.
            try:
                c.shopping_candidates = json.loads(c.shopping_candidates_json) if c.shopping_candidates_json else []
            except (ValueError, TypeError):
                c.shopping_candidates = []

    preview_candidates = OaSourceDiscoveryService.preview_candidates(limit=100)
    excluded_asins = OaSourceDiscoveryService.list_excluded()

    # Shown as a page-level banner when missing -- SerpApi is what
    # actually finds+prices retailers automatically now (2026-08-19);
    # without a key, every run silently falls back to the older,
    # slower, no-auto-price Brave-only pipeline.
    serpapi_configured = bool(os.getenv("SERPAPI_API_KEY"))
    serpapi_searches_left = serpapi_client.get_account_status().get("plan_searches_left") if serpapi_configured else None

    return templates.TemplateResponse(
        request=request,
        name="oa_source_discovery.html",
        context={
            "request": request,
            "runs": runs,
            "selected_run": selected_run,
            "candidates": candidates,
            "preview_candidates": preview_candidates,
            "excluded_asins": excluded_asins,
            "serpapi_configured": serpapi_configured,
            "serpapi_searches_left": serpapi_searches_left,
        }
    )


@router.post("/oa-discovery/exclude")
def oa_discovery_exclude(asin: str = Form(...), title: str = Form(""), reason: str = Form("")):
    """
    Module-scoped skip -- see OaSourceExcludedAsin's docstring. Does
    NOT touch Atlas's app-wide Exclusions list; the ASIN stays fully
    visible everywhere else in Atlas, it's just left out of future OA
    Source Discovery batches/previews.
    """
    OaSourceDiscoveryService.exclude_asin(asin, title=title, reason=reason)
    return RedirectResponse(url="/oa-discovery", status_code=303)


@router.post("/oa-discovery/unexclude")
def oa_discovery_unexclude(asin: str = Form(...)):
    OaSourceDiscoveryService.unexclude_asin(asin)
    return RedirectResponse(url="/oa-discovery", status_code=303)


@router.post("/oa-discovery/run")
def oa_discovery_run(limit: int = Form(20)):
    """
    Runs synchronously -- same "block until done" convention as
    Watchlist's force-rescan and other manual Atlas actions. A large
    batch (the spec's own ~100-ASIN test protocol) can take several
    minutes (one Brave call per query, several queries per ASIN) --
    the page explains this before the button is clicked.
    """
    result = OaSourceDiscoveryService.run_batch(limit=limit)
    run_id = result.get("run_id", 0)
    return RedirectResponse(url=f"/oa-discovery?run_id={run_id}", status_code=303)


@router.post("/oa-discovery/update-candidate")
def oa_discovery_update_candidate(candidate_id: int = Form(...), run_id: int = Form(...),
                                   retailer_domain: str = Form(""), retailer_url: str = Form(""),
                                   retailer_title: str = Form(""), retailer_price_gbp: float = Form(...),
                                   retailer_stock_text: str = Form("")):
    """
    Replaces the old confirm-price endpoint (2026-08-19) -- takes the
    full retailer identity, not just a price, so the user can swap in
    a different, better retailer they found themselves (e.g. a
    genuinely cheaper price at a retailer Atlas didn't auto-pick),
    not only confirm the one Atlas found. See
    OaSourceDiscoveryService.update_candidate for the full behaviour,
    including automatic promotion to the Review Queue when the
    confirmed price clears both the match-confidence and ROI bars.
    """
    OaSourceDiscoveryService.update_candidate(
        candidate_id, retailer_domain, retailer_url, retailer_price_gbp,
        retailer_title=retailer_title, retailer_stock_text=retailer_stock_text,
    )
    return RedirectResponse(url=f"/oa-discovery?run_id={run_id}", status_code=303)
