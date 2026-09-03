import json
import os

from fastapi import APIRouter, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse

from app.database.database import SessionLocal
from app.database.models import OaSourceRun, OaSourceCandidate
from app.services.oa_source_discovery_service import (
    OaSourceDiscoveryService,
    DEFAULT_DETECTION_WINDOW_DAYS,
)
from app.services.seller_watch_service import SellerWatchService
from app.services import serpapi_client

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

# Options in the "Detected" dropdown on the "next up to scan" table.
# 0 = no window (every eligible ASIN, the pre-2026-08-28 behaviour) --
# kept as an explicit choice so an old-but-high-value ASIN is never
# permanently unreachable, just not the default.
DETECTION_WINDOW_OPTIONS = [
    (3, "Last 3 days"),
    (7, "Last 7 days"),
    (14, "Last 14 days"),
    (30, "Last 30 days"),
    (0, "All time"),
]


def build_oa_discovery_context(run_id: int = 0,
                                since_days: int = DEFAULT_DETECTION_WINDOW_DAYS) -> dict:
    """Shared with the Leads hub's "Web-Sourced" group (leads_hub.py) -- see scan_queue.py's own comment for why."""
    db = SessionLocal()

    try:
        # Excludes test-harness runs (see OaSourceRun.is_test's docstring
        # in app/database/models.py) -- a step-3 SerpApi-vs-Serper
        # comparison run shouldn't clutter this page's run picker or
        # silently become the default-selected run.
        runs = (
            db.query(OaSourceRun)
            .filter(OaSourceRun.is_test == False)  # noqa: E712
            .order_by(OaSourceRun.started_at.desc())
            .limit(20)
            .all()
        )

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

    # Rows + the two pool counts behind the window filter's
    # "N of M" line -- one pass, see preview_context.
    preview = OaSourceDiscoveryService.preview_context(limit=100, since_days=since_days)
    excluded_asins = OaSourceDiscoveryService.list_excluded()

    # Shown as a page-level banner when missing -- SerpApi is what
    # actually finds+prices retailers automatically now (2026-08-19);
    # without a key, every run silently falls back to the older,
    # slower, no-auto-price Brave-only pipeline.
    serpapi_configured = bool(os.getenv("SERPAPI_API_KEY"))
    serpapi_searches_left = serpapi_client.get_account_status().get("plan_searches_left") if serpapi_configured else None

    return {
        "runs": runs,
        "selected_run": selected_run,
        "candidates": candidates,
        "preview_candidates": preview["candidates"],
        "preview_window_matched": preview["window_matched"],
        "preview_pool_total": preview["pool_total"],
        "since_days": since_days,
        "window_options": DETECTION_WINDOW_OPTIONS,
        "excluded_asins": excluded_asins,
        "serpapi_configured": serpapi_configured,
        "serpapi_searches_left": serpapi_searches_left,
    }


@router.get("/oa-discovery")
def oa_discovery_page(request: Request, run_id: int = 0,
                       since_days: int = DEFAULT_DETECTION_WINDOW_DAYS):
    return templates.TemplateResponse(
        request=request,
        name="oa_source_discovery.html",
        context={"request": request, **build_oa_discovery_context(run_id, since_days)}
    )


@router.post("/oa-discovery/exclude")
def oa_discovery_exclude(asin: str = Form(...), title: str = Form(""), reason: str = Form(""),
                          since_days: int = Form(DEFAULT_DETECTION_WINDOW_DAYS)):
    """
    Module-scoped skip -- see OaSourceExcludedAsin's docstring. Does
    NOT touch Atlas's app-wide Exclusions list; the ASIN stays fully
    visible everywhere else in Atlas, it's just left out of future OA
    Source Discovery batches/previews.
    """
    OaSourceDiscoveryService.exclude_asin(asin, title=title, reason=reason)
    # Preserve the window the user was looking at across the redirect --
    # excluding a row shouldn't silently snap the table back to the
    # default 7 days and lose their place. Same for every redirect below.
    return RedirectResponse(url=f"/oa-discovery?since_days={since_days}", status_code=303)


@router.post("/oa-discovery/unexclude")
def oa_discovery_unexclude(asin: str = Form(...),
                            since_days: int = Form(DEFAULT_DETECTION_WINDOW_DAYS)):
    OaSourceDiscoveryService.unexclude_asin(asin)
    return RedirectResponse(url=f"/oa-discovery?since_days={since_days}", status_code=303)


@router.post("/oa-discovery/run")
def oa_discovery_run(limit: int = Form(20),
                      since_days: int = Form(DEFAULT_DETECTION_WINDOW_DAYS)):
    """
    Runs synchronously -- same "block until done" convention as
    Watchlist's force-rescan and other manual Atlas actions. A large
    batch (the spec's own ~100-ASIN test protocol) can take several
    minutes (one Brave call per query, several queries per ASIN) --
    the page explains this before the button is clicked.

    `since_days` is posted by the form as a hidden field mirroring the
    "Detected" dropdown, so the run searches exactly the rows the
    preview was showing rather than silently re-deriving its own set.
    """
    result = OaSourceDiscoveryService.run_batch(limit=limit, since_days=since_days)
    run_id = result.get("run_id", 0)
    return RedirectResponse(
        url=f"/oa-discovery?run_id={run_id}&since_days={since_days}", status_code=303,
    )


@router.post("/oa-discovery/update-candidate")
def oa_discovery_update_candidate(candidate_id: int = Form(...), run_id: int = Form(...),
                                   retailer_domain: str = Form(""), retailer_url: str = Form(""),
                                   retailer_title: str = Form(""), retailer_price_gbp: float = Form(...),
                                   retailer_stock_text: str = Form(""),
                                   since_days: int = Form(DEFAULT_DETECTION_WINDOW_DAYS)):
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
    return RedirectResponse(
        url=f"/oa-discovery?run_id={run_id}&since_days={since_days}", status_code=303,
    )
