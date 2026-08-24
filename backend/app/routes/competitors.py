import json
from urllib.parse import quote, quote_plus

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.seller_watch_service import SellerWatchService, SOURCING_TAG_BY_TAB
from app.services.scan_coordinator import ScanCoordinator
from app.services.product_repository import ProductRepository
from app.services.fee_engine import FeeEngine

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

# Human-readable labels for BrandScanService's Step 5b filtered_reason
# values -- see brand_scan_service.py for where these get set.
FILTERED_REASON_LABELS = {
    "excluded_category": "Excluded category",
    "dead_listing": "Dead listing (no sales/offers)",
    "unprofitable_ceiling": "Unprofitable even in the UK alone",
    "no_current_price": "No current price",
}

# Ordered so the template can render tabs left-to-right without
# re-deciding the order itself -- most-actionable (live A2A) first,
# least (OA/unclear, needs the most manual digging) last.
TAB_ORDER = ["eu_a2a", "uk_a2a", "wholesale", "oa"]
TAB_LABELS = {
    "eu_a2a": "EU A2A",
    "uk_a2a": "UK A2A",
    "wholesale": "Wholesale",
    "oa": "OA / unclear",
}
DEFAULT_TAB = "eu_a2a"


def _google_search_urls(title: str, ean: str) -> dict:
    """
    Plain search-engine links for a competitor's OA/unclear find --
    title + EAN when Keepa has one (more precise than title alone,
    same reasoning OaLookupService's docstring already gives for why
    title-only search can match the wrong pack size/variant). No
    SerpApi call, no Atlas OA Lookup page involved -- just opens
    Google in a new tab, same as typing the search by hand.
    """
    query = f"{title} {ean}".strip() if ean else title
    encoded = quote_plus(query)

    return {
        "shopping": f"https://www.google.com/search?q={encoded}&tbm=shop",
        "web": f"https://www.google.com/search?q={encoded}",
    }


def build_competitors_context(tab: str = "", buyable_only: bool = False,
                               review_filter: str = "", category: str = "", since_days: int = 0,
                               check_result: str = "") -> dict:
    """Shared with the Leads hub's "Automated" group (leads_hub.py) -- see scan_queue.py's own comment for why."""
    tab = tab if tab in SOURCING_TAG_BY_TAB else DEFAULT_TAB
    sourcing_tag = SOURCING_TAG_BY_TAB[tab]

    # Just for the summary line's counts -- full management (add/pause/
    # remove/check-now) lives on /competitors/sellers now.
    sellers = SellerWatchService.list_tracked_sellers()

    detections = SellerWatchService.list_detections(
        sourcing_tag=sourcing_tag, buyable_only=buyable_only,
        review_filter=review_filter or None, category=category or None,
        since_days=since_days or None,
    )

    counts_by_sourcing_tag = SellerWatchService.get_detection_counts(
        buyable_only=buyable_only, review_filter=review_filter or None,
        category=category or None, since_days=since_days or None,
    )
    # Re-keyed by tab slug (not the raw sourcing_tag string) so the
    # template can do a plain tab_counts.get(t, 0) per tab.
    tab_counts = {slug: counts_by_sourcing_tag.get(tag, 0) for slug, tag in SOURCING_TAG_BY_TAB.items()}
    unscored_count = counts_by_sourcing_tag.get("unscored", 0)

    # Parse each detection's stored JSON up front so the template can
    # show the "Why?" score breakdown and the sourcing reasoning
    # without re-running any logic -- same idea as products.html's
    # parsed_report.
    for entry in detections:
        record = entry["record"]
        entry["parsed_report"] = {}

        if record and record.report_json:
            try:
                entry["parsed_report"] = json.loads(record.report_json)
            except Exception:
                entry["parsed_report"] = {}

        entry["reasoning"] = {}
        listing = entry["listing"]

        if listing.sourcing_reasoning_json:
            try:
                entry["reasoning"] = json.loads(listing.sourcing_reasoning_json)
            except Exception:
                entry["reasoning"] = {}

        # Only the OA tab shows these (see competitors.html), but
        # cheap enough to compute for every row rather than branch here.
        entry["google_urls"] = _google_search_urls(
            record.title if record else "", record.ean if record else ""
        )

        # "What can I pay for this via OA and still make it worth it?"
        # -- also only shown on the OA tab, same reasoning as
        # google_urls above. None when there's no priced record yet
        # (unscored detection) or when Amazon's own fees already
        # exceed the sale price (max_source_cost returns 0.0 for that
        # case -- see FeeEngine.max_source_cost's docstring).
        entry["oa_price_guide"] = None

        if record and record.buy_box_now:
            breakeven = FeeEngine.max_source_cost(
                record.buy_box_now, record.category_name, record.fba_fee, target_roi_pct=0.0,
            )
            if breakeven > 0:
                target = FeeEngine.max_source_cost(
                    record.buy_box_now, record.category_name, record.fba_fee,
                    target_roi_pct=FeeEngine.OA_TARGET_ROI_PCT,
                )
                entry["oa_price_guide"] = {"target": target, "breakeven": breakeven}

    return {
        "sellers": sellers,
        "detections": detections,
        "tab": tab,
        "tab_order": TAB_ORDER,
        "tab_labels": TAB_LABELS,
        "tab_counts": tab_counts,
        "unscored_count": unscored_count,
        "categories": SellerWatchService.list_detection_categories(),
        "category": category,
        "buyable_only": buyable_only,
        "review_filter": review_filter,
        "since_days": since_days,
        "check_result": check_result,
        "watched_asins": ProductRepository.get_watched_asins(),
        "FILTERED_REASON_LABELS": FILTERED_REASON_LABELS,
    }


@router.get("/competitors")
def competitors_page(request: Request, tab: str = "", buyable_only: bool = False,
                      review_filter: str = "", category: str = "", since_days: int = 0,
                      check_result: str = ""):
    """
    Detections feed -- the day-to-day working page. Tracked-seller
    management (add/pause/remove, "Check now") lives on its own page,
    /competitors/sellers, linked from the summary line below -- this
    page was getting long with both on it at once, and the two are
    genuinely different tasks (set up sellers occasionally vs. work
    through detections often).
    """
    return templates.TemplateResponse(
        request=request,
        name="competitors.html",
        context={"request": request, **build_competitors_context(
            tab, buyable_only, review_filter, category, since_days, check_result
        )}
    )


def build_competitors_sellers_context(check_result: str = "") -> dict:
    """Shared with the Leads hub's "Automated" group (leads_hub.py)."""
    return {
        "sellers": SellerWatchService.list_tracked_sellers(),
        "stats": SellerWatchService.get_seller_stats(),
        "check_result": check_result,
    }


@router.get("/competitors/sellers")
def competitors_sellers_page(request: Request, check_result: str = ""):
    """
    Tracked-seller management -- add/pause/resume/remove and "Check
    now", split out from the detections feed (see competitors_page's
    docstring) since this is an occasional setup task, not something
    you look at every session the way the detections feed is.
    """
    return templates.TemplateResponse(
        request=request,
        name="competitors_sellers.html",
        context={"request": request, **build_competitors_sellers_context(check_result)}
    )


@router.post("/competitors/add")
def competitors_add(seller_id: str = Form(...), nickname: str = Form("")):
    SellerWatchService.add_tracked_seller(seller_id, nickname)
    return RedirectResponse(url="/competitors/sellers", status_code=303)


@router.post("/competitors/pause")
def competitors_pause(tracked_seller_id: int = Form(...)):
    SellerWatchService.set_active(tracked_seller_id, False)
    return RedirectResponse(url="/competitors/sellers", status_code=303)


@router.post("/competitors/resume")
def competitors_resume(tracked_seller_id: int = Form(...)):
    SellerWatchService.set_active(tracked_seller_id, True)
    return RedirectResponse(url="/competitors/sellers", status_code=303)


@router.post("/competitors/remove")
def competitors_remove(tracked_seller_id: int = Form(...)):
    SellerWatchService.remove_tracked_seller(tracked_seller_id)
    return RedirectResponse(url="/competitors/sellers", status_code=303)


@router.post("/competitors/dismiss")
def competitors_dismiss(listing_id: int = Form(...), return_to: str = Form("/competitors")):
    SellerWatchService.dismiss_detection(listing_id)
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/competitors/review")
def competitors_review(listing_id: int = Form(...), verdict: str = Form(""),
                        return_to: str = Form("/competitors")):
    SellerWatchService.set_review(listing_id, verdict or None)
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/competitors/check-now")
def competitors_check_now():
    """
    Manual "check now" -- runs a check immediately rather than waiting
    for the background scheduler (every 2h, see main.py), same "Run
    one now" idea as the Scan Queue page. Not in the original spec's
    UI list, added because the page would otherwise have no way to do
    anything until the scheduler's next tick.

    Manual, user-initiated scan -- takes priority over the automated
    scheduler for its whole duration (see ScanCoordinator), same
    pattern Discovery/Watchlist/Replen's "check now" already use, so
    the scheduler can't sneak a tick in mid-check and start competing
    for tokens.
    """
    ScanCoordinator.acquire_for_manual_scan()

    try:
        result = SellerWatchService.run_check()
    finally:
        ScanCoordinator.release_after_manual_scan()

    message = f"Checked {result['checked']} seller(s), found {result['new_listings']} new listing(s)."
    return RedirectResponse(url=f"/competitors/sellers?check_result={quote(message)}", status_code=303)


@router.post("/competitors/rescan-unscored")
def competitors_rescan_unscored():
    """
    Backfill button for detections that only ever recorded a bare
    ASIN (product_record_id NULL) -- most commonly ones dropped for
    having no EU A2A source before scan() gained include_no_eu_source.
    Re-scans exactly those ASINs so they pick up real title/price/rank
    data and a sourcing tag. Same manual-scan priority pattern as
    check-now, since this also spends Keepa tokens.
    """
    ScanCoordinator.acquire_for_manual_scan()

    try:
        result = SellerWatchService.rescan_unscored()
    finally:
        ScanCoordinator.release_after_manual_scan()

    message = f"Rescanned {result['rescanned']} ASIN(s), {result['updated']} now have data."
    return RedirectResponse(url=f"/competitors?check_result={quote(message)}", status_code=303)


@router.post("/competitors/reclassify-all")
def competitors_reclassify_all():
    """
    One-off backfill button: re-runs SourcingClassifier against a
    fresh Keepa fetch for EVERY existing detection (not just unscored
    ones -- see rescan-unscored above), so historical detections pick
    up classifier rule changes made after they were first tagged.
    Bounded per click by the Keepa token budget -- click again once
    tokens refill to keep going until "0 remaining".
    """
    ScanCoordinator.acquire_for_manual_scan()

    try:
        result = SellerWatchService.reclassify_all()
    finally:
        ScanCoordinator.release_after_manual_scan()

    message = (
        f"Reclassified {result['processed']} ASIN(s), {result['flipped']} tag(s) changed. "
        f"{result['remaining']} remaining"
        + (" (stopped early -- low on tokens, click again once they refill)." if result['stopped_early'] else ".")
    )
    return RedirectResponse(url=f"/competitors?check_result={quote(message)}", status_code=303)
