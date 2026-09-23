from app.routes.review_validation import validate_rejection_reason
from datetime import datetime, timezone

from urllib.parse import quote

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.product_repository import ProductRepository
from app.services.seller_watch_service import SellerWatchService
from app.services import watchlist_view_service

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

# 2026-09-21 (Tamara: "very slow ... a constant problem"): this page no longer scans anything. It used to run a LIVE
# Keepa scan of every watched ASIN whenever a 5-minute cache had expired (41 minutes for one load at its worst on
# 2026-09-17, queued behind the process-wide scan lock, spending real tokens just to look at the page). It now shows
# each ASIN's latest STORED scan and how old that is; "Refresh prices" runs the scan in the background. See
# watchlist_view_service.


@router.post("/watch/add")
def watch_add(asin: str = Form(...), title: str = Form(""), brand: str = Form(""),
              note: str = Form(""), return_to: str = Form("/products")):
    ProductRepository.add_watch(asin, title=title, brand=brand, note=note)
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/watch/remove")
def watch_remove(asin: str = Form(...), return_to: str = Form("/watchlist")):
    ProductRepository.remove_watch(asin)
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/exclude/add")
def exclude_add(asin: str = Form(...), title: str = Form(""), reason: str = Form(""),
                return_to: str = Form("/products")):
    ProductRepository.add_exclusion(asin.strip().upper(), title=title, reason=reason)
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/exclude/remove")
def exclude_remove(asin: str = Form(...), return_to: str = Form("/exclusions")):
    ProductRepository.remove_exclusion(asin)
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/exclude/category/add")
def exclude_category_add(category_id: str = Form(""), category_name: str = Form(""),
                          reason: str = Form(""), return_to: str = Form("/exclusions")):
    ProductRepository.add_category_exclusion(
        category_id=category_id, category_name=category_name, reason=reason
    )
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/exclude/category/remove")
def exclude_category_remove(exclusion_id: int = Form(...), return_to: str = Form("/exclusions")):
    ProductRepository.remove_category_exclusion(exclusion_id)
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/gate/add")
def gate_add(brand: str = Form(...), category_id: str = Form(""), category_name: str = Form(""),
             reason: str = Form(""), return_to: str = Form("/exclusions")):
    """
    See GatedBrand's docstring -- unlike /exclude/category/add, this
    does NOT drop future matches, it blocks brand-search-driven
    scanning of the brand and tags any incidentally-found ASIN
    "GATED" instead so it's still tracked (see BrandScanService.scan
    and OpportunityEngine.analyse).
    """
    ProductRepository.add_gated_brand(
        brand=brand, category_id=category_id, category_name=category_name, reason=reason
    )
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/gate/remove")
def gate_remove(gated_id: int = Form(...), return_to: str = Form("/exclusions")):
    ProductRepository.remove_gated_brand(gated_id)
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/review/set")
def review_set(asin: str = Form(...), verdict: str = Form(""), listing_id: int = Form(0),
               reason: str = Form(""), reason_category: str = Form(""),
               return_to: str = Form("/products")):
    """
    Canonical "set review" endpoint, used by Products/Discovery/
    Watchlist/Review Queue. listing_id is optional -- only set when
    reviewing a competitor-sourced row from the Review Queue, in which
    case the underlying SellerNewListing gets marked reviewed too, so
    it also clears from the Competitors page's own unreviewed filter
    (and vice versa: reviewing it there already updates the same
    ProductRecord this route updates, via SellerWatchService.set_review).

    A down verdict requires a reason or a recognized reason category.
    Other requires explanatory text. Up/OOS and clearing a review do not.

    reason_category: optional structured reason (atlas-review-queue-
    backend-v1.md section 5, see review_queue_service.
    REVIEW_REASON_CATEGORIES) -- additive alongside `reason`, never a
    replacement for it. Existing callers that don't send it are
    unaffected (stored as NULL, same as any historical row).

    verdict="oos" is a third bucket alongside "up"/"down" -- Amazon is
    out of stock right now, so it's not actionable this instant, but
    worth catching WHEN it restocks rather than losing it entirely to
    a plain "down". It clears the queue the same way "up"/"down" do
    (ProductRecord.review just needs to be truthy -- see
    ProductRepository.list_latest's review_filter), and ADDITIONALLY
    auto-adds the ASIN to the existing Watchlist, reusing its already-
    built re-check machinery (WatchlistService.check_stale runs
    weekly, or visit /watchlist to force an immediate recheck) instead
    of building a parallel monitoring mechanism. Title/brand come from
    Atlas's own last scan record for this ASIN (no extra Keepa lookup
    needed here).
    """
    if verdict == "down":
        reason, reason_category = validate_rejection_reason(reason, reason_category, require_category=False)

    ProductRepository.set_review(asin, verdict or None, reason=reason or None, reason_category=reason_category or None)

    if listing_id:
        SellerWatchService.set_review(
            listing_id, verdict or None, reason=reason or None, reason_category=reason_category or None
        )

    if verdict == "oos":
        record = ProductRepository.get_last_eu_check(asin)
        ProductRepository.add_watch(
            asin,
            title=record.title if record else "",
            brand=record.brand if record else "",
            note="Amazon OOS at review -- watching for restock",
        )

    return RedirectResponse(url=return_to, status_code=303)


def _split_selection(selected: list[str]) -> tuple[list[str], list[int]]:
    """
    Selection checkboxes carry "ASIN::listing_id" (listing_id is "0"
    for scan-sourced leads, matching the single-item /review/set
    route's own listing_id=0 default for "not a competitor row").
    """
    asins = []
    listing_ids = []

    for item in selected:
        asin, _, listing_id = item.partition("::")

        if asin:
            asins.append(asin)

        if listing_id and listing_id != "0":
            try:
                listing_ids.append(int(listing_id))
            except ValueError:
                pass

    return asins, listing_ids


@router.post("/review/set-bulk")
def review_set_bulk(selected: list[str] = Form(...), verdict: str = Form(""),
                     return_to: str = Form("/review-queue")):
    """Bulk version of /review/set -- see review_queue.html's checkbox toolbar."""
    asins, listing_ids = _split_selection(selected)

    ProductRepository.set_review_bulk(asins, verdict or None)

    if listing_ids:
        SellerWatchService.set_review_bulk(listing_ids, verdict or None)

    return RedirectResponse(url=return_to, status_code=303)


@router.post("/exclude/add-bulk")
def exclude_add_bulk(selected: list[str] = Form(...), reason: str = Form("Bulk excluded from review queue"),
                      return_to: str = Form("/review-queue")):
    """
    Bulk exclude from the Review Queue's checkbox toolbar. Unlike
    /review/set-bulk this doesn't touch SellerNewListing.review --
    ExcludedProduct is a separate, permanent "never surface this ASIN
    again" mechanism (checked before spending Keepa tokens on future
    scans), not the same thing as marking today's rows reviewed.
    """
    asins, _ = _split_selection(selected)

    ProductRepository.add_exclusion_bulk(asins, reason=reason)

    return RedirectResponse(url=return_to, status_code=303)


@router.post("/watchlist/refresh")
def watchlist_refresh(force: bool = Form(False), profitable_only: bool = Form(True)):
    """Start a background re-price of every watched ASIN (the old inline scan). Never blocks; one at a time."""
    asins = [w.asin for w in ProductRepository.list_watched()]
    started = watchlist_view_service.start_refresh(asins, force=force)
    if not asins:
        message = "Nothing on the watchlist to refresh."
    elif started:
        message = "Refreshing prices in the background -- this page keeps showing the last results until it finishes."
    else:
        message = "A refresh is already running."
    return RedirectResponse(url=f"/watchlist?profitable_only={str(profitable_only).lower()}&message={quote(message)}",
                            status_code=303)


@router.get("/watchlist")
def watchlist_page(request: Request, profitable_only: bool = True, message: str = ""):
    watched = ProductRepository.list_watched()
    result = None
    hidden_count = 0
    visible_opportunities = []

    # "Actionable" reuses the exact same recommendation vocabulary the
    # table's own badges already use (see the BUY/CONSIDER/PEAK_WINDOW/
    # GATED/else-IGNORE chain in watchlist.html) -- NOT the cruder
    # profit<=0-and-profit_90d<=0 test this used to be computed with
    # (that test was written before PEAK_WINDOW existed as its own
    # category, and would have hidden exactly the "don't rule out,
    # profitable at a real recent peak" rows that feature was later
    # built to surface). Wired up 2026-08-21 -- profitable_only/
    # hidden_count were both already accepted here but never actually
    # filtered anything in the template until now.
    ACTIONABLE_RECOMMENDATIONS = {"BUY", "CONSIDER", "PEAK_WINDOW", "GATED"}

    if watched:
        result = watchlist_view_service.stored_result([w.asin for w in watched])

        hidden_count = sum(
            1 for o in result["opportunities"]
            if o["report"]["recommendation"] not in ACTIONABLE_RECOMMENDATIONS
        )
        visible_opportunities = (
            [
                o for o in result["opportunities"]
                if o["report"]["recommendation"] in ACTIONABLE_RECOMMENDATIONS
            ]
            if profitable_only else result["opportunities"]
        )

    reviews = {}
    if result and not result.get("error"):
        reviews = ProductRepository.get_reviews([o["product"]["asin"] for o in result["opportunities"]])

    # {asin: note} -- lets the per-row table (built from scan results,
    # not WatchedProduct directly) show whether/why an item was
    # auto-added by WatchlistService (see the "Auto-added:" prefix
    # convention) without a schema change.
    notes = {w.asin: w.note for w in watched if w.note}

    # {asin: viable_days_90d} -- same shape as `notes` above, for the
    # same reason (per-row table built from scan results, not
    # WatchedProduct directly). 2026-09-03: how many of the last 90
    # days this ASIN's EU source cost actually cleared the viability
    # floor -- Tamara's own explicit requirement that this be a real,
    # visible factor, not just prose buried in the note's hover text.
    viable_days = {w.asin: w.viable_days_90d for w in watched if w.viable_days_90d}

    return templates.TemplateResponse(
        request=request,
        name="watchlist.html",
        context={
            "request": request,
            "watched": watched,
            "result": result,
            "viable_days": viable_days,
            "visible_opportunities": visible_opportunities,
            "hidden_count": hidden_count,
            "profitable_only": profitable_only,
            "reviews": reviews,
            "notes": notes,
            "refresh": watchlist_view_service.refresh_state(),
            "stale_after_hours": watchlist_view_service.STALE_AFTER_HOURS,
            "message": message,
        }
    )


@router.get("/exclusions")
def exclusions_page(request: Request):
    exclusions = ProductRepository.list_exclusions()
    category_exclusions = ProductRepository.list_category_exclusions()
    gated_brands = ProductRepository.list_gated_brands()
    excluded_brands = ProductRepository.list_excluded_brands()
    catalog_stats = ProductRepository.get_known_products_catalog_stats()

    # Computed here rather than in the template -- Jinja has no built-in
    # date-math filter, and "days ago" is far more readable at a glance
    # than a raw timestamp for judging whether the imported catalog data
    # (which silently pre-filters ASINs before any Keepa token is spent)
    # is stale enough to warrant a fresh import.
    oldest_imported_at = catalog_stats.get("oldest_imported_at")
    catalog_stats["oldest_days_ago"] = (
        (datetime.now(timezone.utc).replace(tzinfo=None) - oldest_imported_at).days
        if oldest_imported_at else None
    )

    return templates.TemplateResponse(
        request=request,
        name="exclusions.html",
        context={
            "request": request,
            "exclusions": exclusions,
            "category_exclusions": category_exclusions,
            "gated_brands": gated_brands,
            "excluded_brands": excluded_brands,
            "catalog_stats": catalog_stats,
        }
    )


@router.post("/exclude/brand/add")
def exclude_brand_add(brand: str = Form(...), reason: str = Form(""), return_to: str = Form("/exclusions")):
    """
    See ExcludedBrand's own docstring for how this differs from
    /gate/add: unconditional, brand-wide, no category scoping, and
    immediately removes any existing Scan Queue rows for the brand
    (ProductRepository.add_brand_exclusion) rather than leaving them to
    go stale in place the way a gated brand does.
    """
    ProductRepository.add_brand_exclusion(brand=brand, reason=reason)
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/exclude/brand/remove")
def exclude_brand_remove(exclusion_id: int = Form(...), return_to: str = Form("/exclusions")):
    ProductRepository.remove_brand_exclusion(exclusion_id)
    return RedirectResponse(url=return_to, status_code=303)


@router.get("/gated-opportunities")
def gated_opportunities_page(request: Request):
    """
    The case for pursuing ungating on a specific brand: every real,
    scored A2A opportunity Atlas has found for a currently-gated brand
    (see GatedBrand / Product.gated / OpportunityEngine's "GATED"
    recommendation), most commonly surfaced via Competitor Watch --
    Atlas doesn't deliberately search for MORE of a gated brand (see
    BrandScanService.scan Step 1), so these are all incidental finds.
    """
    opportunities = ProductRepository.list_gated_opportunities()
    summary = ProductRepository.get_gated_brand_summary()

    return templates.TemplateResponse(
        request=request,
        name="gated_opportunities.html",
        context={
            "request": request,
            "opportunities": opportunities,
            "summary": summary,
        }
    )
