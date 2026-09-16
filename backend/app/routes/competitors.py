import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, quote_plus

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database.database import SessionLocal
from app.database.models import TrackedSeller, SellerNewListing, ProductRecord, OaSourceCandidate
from app.services.seller_watch_service import SellerWatchService, SOURCING_TAG_BY_TAB
from app.services.scan_coordinator import ScanCoordinator
from app.services.product_repository import ProductRepository
from app.services.fee_engine import FeeEngine
from app.services.review_queue_service import ReviewQueueService
from app.services.oa_source_discovery_service import OaSourceDiscoveryService
from app.services import serpapi_client

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


def _days_ago(iso_date: str | None) -> str:
    """
    Presentational only (Competitor Watch redesign, 2026-09-03) --
    identical to review_queue.py's own _days_ago (each route module
    owns its own Jinja2Templates instance in this codebase, so filters
    don't carry over -- see that module's own comment on why this is
    duplicated rather than imported). SourcingClassifier's
    guessed_buy_date is a plain "YYYY-MM-DD" string, not a days-ago
    count -- computed here at display time only.
    """
    if not iso_date:
        return ""
    try:
        parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        return iso_date
    days = (datetime.now() - parsed).days
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


templates.env.filters["days_ago"] = _days_ago


def _google_search_url_variants(title: str, ean: str, asin: str) -> dict:
    """
    Competitor Watch redesign (2026-09-03) -- the Opportunities feed's
    "Google ▾" dropdown on an OA row. Builds three query variants
    rather than the single title(+EAN) query _google_search_urls above
    already used -- EAN is blank on most real detections (confirmed:
    every OA row checked while building this had ean == "", including
    ones with a model number visible right in the title), so a
    title-only and an ASIN+title fallback both matter in practice, not
    just as a nice-to-have. "title_ean" is None (not shown as an
    option) whenever there's no EAN to add -- never fabricated.
    Plain https://www.google.com/search links, same as
    _google_search_urls -- no SerpApi call, opens in a new tab exactly
    like typing the search by hand.
    """
    title = title or ""

    return {
        "title": f"https://www.google.com/search?q={quote_plus(title)}",
        "title_ean": (
            f"https://www.google.com/search?q={quote_plus(f'{title} {ean}'.strip())}" if ean else None
        ),
        "asin_title": f"https://www.google.com/search?q={quote_plus(f'{asin} {title}'.strip())}",
    }


def _oa_search_url_variants(title: str, brand: str, ean: str, mpn: str) -> dict:
    """
    Review Queue OA workbench (2026-09-05) -- the "I'll find it"
    manual-search buttons: EAN/MPN/Product on Google Shopping, plus a
    plain Google Web search on the product, per Tamara's own spec
    ("very obvious button... not hidden inside a dropdown"). Same
    plain-URL, no-API-cost pattern as _google_search_url_variants above
    (tbm=shop is just Google's own Shopping-tab URL parameter, not a
    Shopping API call) -- opens in a new tab exactly like typing the
    search by hand. Each variant is None when its underlying field is
    unknown (never fabricate an EAN/MPN search query), so the template
    only renders buttons for searches that can actually run.
    """
    brand_title = f"{brand} {title}".strip() if brand and not (title or "").lower().startswith(brand.lower()) else (title or "")

    return {
        "shopping_ean": (
            f"https://www.google.com/search?tbm=shop&q={quote_plus(ean)}" if ean else None
        ),
        "shopping_mpn": (
            f"https://www.google.com/search?tbm=shop&q={quote_plus(f'{brand} {mpn}'.strip())}" if mpn else None
        ),
        "shopping_product": (
            f"https://www.google.com/search?tbm=shop&q={quote_plus(brand_title)}" if brand_title else None
        ),
        "web_product": (
            f"https://www.google.com/search?q={quote_plus(brand_title)}" if brand_title else None
        ),
    }


# ---- Competitor Watch redesign (2026-09-03) -- three internal views
# under ONE route, matching the "?view=" pattern the Review Queue
# redesign already established, rather than new top-level sidebar nav
# (see the approved design proposal, section 6/"Navigation"). ----

COMPETITOR_WATCH_TABS = ("opportunities", "source_finder", "competitors")

OPPORTUNITY_SOURCE_FILTERS = {
    "all": None,
    "eu_a2a": "EU A2A",
    "uk_a2a": "UK A2A",
    "wholesale": "Wholesale (likely)",
    "oa": "OA / unclear",
}
OPPORTUNITY_SOURCE_LABELS = {
    "all": "All sources", "eu_a2a": "EU A2A", "uk_a2a": "UK A2A",
    "wholesale": "Wholesale", "oa": "OA / unclear",
}
OPPORTUNITY_VIEW_LABELS = {
    "all": "All", "buy_now": "Buy Now",
    # Split from buy_now 2026-09-04 (Tamara's own explicit choice --
    # "split into two tabs" -- after a real mismatch surfaced: a
    # CONSIDER-tier listing with 25%+ ROI and confirmed sales evidence
    # showed here as "Buy Now" but wasn't a literal BUY recommendation.
    # See build_opportunities_context's own comment for the split.
    "strong_consider": "Strong Consider",
    "needs_attention": "Needs Attention",
    "oa_investigate": "OA — Worth Investigating", "recent": "Recent Discoveries",
}
OPPORTUNITY_SORT_LABELS = {"newest": "Newest first", "profit_desc": "Highest profit"}


def build_opportunities_context(view: str = "all", source: str = "all", q: str = "",
                                 sort: str = "newest", seller_id: int = 0) -> dict:
    """
    Competitor Watch redesign -- the Opportunities feed. The pool is
    the UNION of the same three already-tested service methods that
    already feed the unified Review Queue (list_notable_buyable /
    list_historical_a2a_not_buyable, both completely unchanged) plus
    the new list_oa_worth_investigating -- this function only filters/
    sorts/labels what those already return. No dedup/priority logic of
    its own: a listing can only ever match ONE of the three (mutually
    exclusive by currently_buyable/sourcing_tag), so there is nothing
    to merge.

    buy_now vs strong_consider (split 2026-09-04, Tamara's own choice):
    list_notable_buyable() itself uses is_notable()'s broader "star"
    bar (a literal BUY, OR CONSIDER-tier with 25%+ ROI and confirmed
    sales evidence -- same bar the unified Review Queue's own BUY_NOW
    view was widened to trust the same day). Splitting the label here
    purely by the record's literal recommendation keeps "Buy Now"
    honest about what's a confirmed BUY vs what's a strong-but-Consider
    lead, without changing what list_notable_buyable() itself returns
    or touching the Review Queue's own (now-matching) definition.
    """
    buy_now_pool = SellerWatchService.list_notable_buyable(limit=300)
    needs_attention = SellerWatchService.list_historical_a2a_not_buyable(limit=300)
    oa_worth = SellerWatchService.list_oa_worth_investigating(limit=300)

    # Real gap found live, 2026-09-16 (Tamara, re: B0F1YS92S2): the
    # unified Review Queue already demotes a "BUY" out of its own
    # BUY_NOW view when TODAY's actual profit is negative (2026-09-12,
    # "Stop Buy Now from surfacing loss-making... leads") -- but that
    # fix only touched review_queue_service._build_merged_item, not
    # this page's own independent buy_now_pool split, so the same
    # loss-making listing kept showing as Buy Now here. buy_now and
    # strong_consider are BOTH drawn from list_notable_buyable() (see
    # this function's own docstring: both map onto the unified queue's
    # one BUY_NOW view), so the exclusion applies to the whole pool
    # before the BUY-vs-CONSIDER label split below, not just the
    # literal "BUY" half of it.
    buy_now_pool = [
        e for e in buy_now_pool
        if not (e["record"] and e["record"].profit is not None and e["record"].profit < 0)
    ]

    buy_now = [e for e in buy_now_pool if e["record"] and e["record"].recommendation == "BUY"]
    strong_consider = [e for e in buy_now_pool if not (e["record"] and e["record"].recommendation == "BUY")]

    for entry in buy_now:
        entry["opp_view"] = "buy_now"
    for entry in strong_consider:
        entry["opp_view"] = "strong_consider"
    for entry in needs_attention:
        entry["opp_view"] = "needs_attention"
    for entry in oa_worth:
        entry["opp_view"] = "oa_investigate"

    pool = buy_now + strong_consider + needs_attention + oa_worth

    counts = {
        "buy_now": len(buy_now),
        "strong_consider": len(strong_consider),
        "needs_attention": len(needs_attention),
        "oa_investigate": len(oa_worth),
        "recent": SellerWatchService.count_recent_detections(days=7),
    }

    items = pool
    if view != "all":
        items = [e for e in items if e["opp_view"] == view]

    wanted_tag = OPPORTUNITY_SOURCE_FILTERS.get(source)
    if wanted_tag:
        items = [e for e in items if e["listing"].sourcing_tag == wanted_tag]

    if seller_id:
        items = [e for e in items if e["listing"].tracked_seller_id == seller_id]

    if q.strip():
        needle = q.strip().lower()
        items = [
            e for e in items
            if needle in e["listing"].asin.lower()
            or (e["record"] and needle in (e["record"].title or "").lower())
        ]

    def _sort_key(entry):
        if sort == "profit_desc":
            record = entry["record"]
            return record.profit if record and record.profit is not None else -1_000_000
        return entry["listing"].detected_at or datetime.min

    items = sorted(items, key=_sort_key, reverse=True)

    for entry in items:
        listing, record = entry["listing"], entry["record"]
        entry["google_urls"] = _google_search_url_variants(
            record.title if record else "", record.ean if record else "", listing.asin,
        )
        reasoning = {}
        if listing.sourcing_reasoning_json:
            try:
                reasoning = json.loads(listing.sourcing_reasoning_json)
            except Exception:
                reasoning = {}
        entry["reasoning"] = reasoning

    sellers = SellerWatchService.list_tracked_sellers()
    seller_names = {s.id: (s.nickname or s.seller_id) for s in sellers}

    return {
        "items": items,
        "counts": counts,
        "view": view,
        "view_labels": OPPORTUNITY_VIEW_LABELS,
        "source": source,
        "source_labels": OPPORTUNITY_SOURCE_LABELS,
        "q": q,
        "sort": sort,
        "sort_labels": OPPORTUNITY_SORT_LABELS,
        "seller_id": seller_id,
        "selected_seller_name": seller_names.get(seller_id) if seller_id else None,
        "watched_asins": ProductRepository.get_watched_asins(),
    }


def build_source_finder_context(asin: str = "") -> dict:
    """
    Competitor Watch redesign -- Source Finder tab. Reuses
    OaSourceDiscoveryService/OaSourceCandidate exactly as /oa-discovery
    already does (see that route) -- a differently-framed VIEW onto
    the SAME pipeline, not a second sourcing engine. Standalone
    (asin="") shows a preview list of OA opportunities worth
    investigating (reuses list_oa_worth_investigating); pre-loaded
    (asin=...) shows that one ASIN's existing evidence plus any
    OaSourceCandidate rows a PAST run has already produced for it --
    never runs a new search itself (see the find-source POST route,
    the only place that spends tokens).
    """
    serpapi_configured = bool(os.getenv("SERPAPI_API_KEY"))
    serpapi_searches_left = (
        serpapi_client.get_account_status().get("plan_searches_left") if serpapi_configured else None
    )

    if not asin:
        return {
            "asin": "",
            "loaded_item": None,
            "preview": SellerWatchService.list_oa_worth_investigating(limit=30),
            "candidates": [],
            "serpapi_configured": serpapi_configured,
            "serpapi_searches_left": serpapi_searches_left,
        }

    db = SessionLocal()
    try:
        row = (
            db.query(SellerNewListing, TrackedSeller, ProductRecord)
            .join(TrackedSeller, SellerNewListing.tracked_seller_id == TrackedSeller.id)
            .outerjoin(ProductRecord, SellerNewListing.product_record_id == ProductRecord.id)
            .filter(SellerNewListing.asin == asin)
            .order_by(SellerNewListing.detected_at.desc())
            .first()
        )
        candidates = (
            db.query(OaSourceCandidate)
            .filter(OaSourceCandidate.asin == asin)
            .order_by(OaSourceCandidate.id.desc())
            .all()
        )
    finally:
        db.close()

    loaded_item = None
    if row:
        listing, seller, record = row
        reasoning = {}
        if listing.sourcing_reasoning_json:
            try:
                reasoning = json.loads(listing.sourcing_reasoning_json)
            except Exception:
                reasoning = {}

        oa_price_guide = None
        if record and record.buy_box_now:
            breakeven = FeeEngine.max_source_cost(
                record.buy_box_now, record.category_name, record.fba_fee, target_roi_pct=0.0,
            )
            if breakeven > 0:
                target = FeeEngine.max_source_cost(
                    record.buy_box_now, record.category_name, record.fba_fee,
                    target_roi_pct=FeeEngine.OA_TARGET_ROI_PCT,
                )
                oa_price_guide = {"target": target, "breakeven": breakeven}

        loaded_item = {
            "listing": listing, "seller": seller, "record": record,
            "reasoning": reasoning, "oa_price_guide": oa_price_guide,
            "google_urls": _google_search_url_variants(
                record.title if record else "", record.ean if record else "", asin,
            ),
        }

    return {
        "asin": asin,
        "loaded_item": loaded_item,
        "preview": [],
        "candidates": candidates,
        "serpapi_configured": serpapi_configured,
        "serpapi_searches_left": serpapi_searches_left,
    }


def build_competitor_analytics_context(seller_id: int = 0) -> dict:
    """Competitor Watch redesign -- Competitors tab (seller list + drill-down)."""
    empty_breakdown = {
        "total": 0, "eu_a2a": 0, "uk_a2a": 0, "wholesale": 0, "oa": 0,
        "unscored": 0, "last_7d": 0, "last_30d": 0, "buy_opportunities": 0,
    }

    sellers = SellerWatchService.list_tracked_sellers()
    breakdown = SellerWatchService.get_seller_breakdown()

    seller_rows = [
        {"seller": s, **breakdown.get(s.id, empty_breakdown)}
        for s in sellers
    ]
    seller_rows.sort(key=lambda r: r["total"], reverse=True)

    selected = None
    if seller_id:
        selected_seller = next((s for s in sellers if s.id == seller_id), None)
        if selected_seller:
            selected = {
                "seller": selected_seller,
                **breakdown.get(seller_id, empty_breakdown),
                "marketplace_pattern": SellerWatchService.get_seller_marketplace_pattern(seller_id),
                "oa_retailer_patterns": SellerWatchService.get_seller_oa_retailer_patterns(seller_id),
            }

    return {
        "seller_rows": seller_rows,
        "selected": selected,
        "seller_id": seller_id,
    }


@router.get("/competitors")
def competitors_page(request: Request, tab: str = "opportunities", view: str = "all",
                      source: str = "all", q: str = "", sort: str = "newest",
                      seller_id: int = 0, asin: str = "", check_result: str = ""):
    """
    Competitor Watch, redesigned 2026-09-03 -- "turn competitor
    activity into sourcing opportunities" rather than a flat detections
    table. Three internal views via `tab` (see COMPETITOR_WATCH_TABS) --
    no SEPARATE top-level nav entries for them (Opportunities/Source
    Finder/Competitors all stay inside this one route); this page as a
    whole got its first real sidebar entry ("Opportunities", under
    Find) in the Navigation redesign, 2026-09-04.

    NOTE: this route used to share its `tab` param name with a second,
    differently-scoped `tab` on the now-removed /competitors-legacy
    page (EU A2A/UK A2A/Wholesale/OA -- see build_competitors_context,
    deleted 2026-09-04 navigation cleanup alongside that route). To
    avoid exactly that ambiguity within THIS route, the equivalent
    sourcing-tag filter here is named `source` instead (see
    OPPORTUNITY_SOURCE_FILTERS).
    """
    tab = tab if tab in COMPETITOR_WATCH_TABS else "opportunities"

    if tab == "source_finder":
        context = build_source_finder_context(asin=asin)
        content_template = "_competitors_source_finder.html"
    elif tab == "competitors":
        context = build_competitor_analytics_context(seller_id=seller_id)
        content_template = "_competitors_analytics.html"
    else:
        context = build_opportunities_context(view=view, source=source, q=q, sort=sort, seller_id=seller_id)
        content_template = "_competitors_opportunities.html"

    return templates.TemplateResponse(
        request=request,
        name="competitors.html",
        context={
            "request": request,
            "tab": tab,
            "content_template": content_template,
            "check_result": check_result,
            **context,
        }
    )


@router.get("/competitors/opportunity/{asin}")
def competitors_opportunity_detail(request: Request, asin: str):
    """Use the canonical review panel, including sourcing tools and actions."""
    from app.routes.review_queue import review_queue_item_detail

    return review_queue_item_detail(request, asin)


@router.post("/competitors/find-source")
def competitors_find_source(asin: str = Form(...)):
    """
    Explicit, single-ASIN OA Source Finder run (Competitor Watch
    redesign, 2026-09-03) -- a thin wrapper around the EXISTING batch
    pipeline (OaSourceDiscoveryService.run_batch), scoped to one ASIN
    via test_asins. test_mode is left at its default False, so this is
    a REAL run: real Keepa lookup, real SerpApi/Brave search, real
    promotion to Review Queue if a result clears the trust+ROI bar --
    see run_batch's own docstring for exactly what test_asins does
    when test_mode is NOT set. NOT a second sourcing engine -- the
    exact same function the "Run batch" button on /oa-discovery calls.

    Only ever reached from Source Finder's own explicit "Run Source
    Search" button (a POST) -- never automatically, never from a GET/
    page load, matching the standing Keepa-discipline rule.
    """
    OaSourceDiscoveryService.run_batch(limit=1, test_asins=[asin])
    return RedirectResponse(url=f"/competitors?tab=source_finder&asin={quote(asin)}", status_code=303)


def build_competitors_sellers_context(check_result: str = "") -> dict:
    """
    Kept as its own function even though /competitors/sellers is its
    only caller now -- Leads Hub used to be a second caller (removed
    2026-09-04, Navigation redesign).
    """
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
    # Redirect target changed 2026-09-04 (navigation cleanup) from the
    # now-removed /competitors-legacy to /competitors/sellers -- same
    # target check-now above already uses. Kept the route/service call
    # itself untouched (real maintenance capability, not legacy-page
    # cruft) even though no current page links to this button; only
    # /competitors-legacy's own template did.
    return RedirectResponse(url=f"/competitors/sellers?check_result={quote(message)}", status_code=303)


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
    # See rescan-unscored's own comment on this same change.
    return RedirectResponse(url=f"/competitors/sellers?check_result={quote(message)}", status_code=303)


@router.post("/competitors/reclassify-oa-queue")
def competitors_reclassify_oa_queue():
    """
    Same idea as reclassify-all just above, but scoped to exactly the
    current OA to Investigate population (2026-09-07, Tamara: "I want
    everything in the OA investigation queues reclassified and details
    of how many were reclassified") -- see SellerWatchService.
    reclassify_oa_investigate_queue's own docstring for why this is a
    separate, narrower method rather than reclassify_all with a bigger
    cap. Bounded per click by the Keepa token budget, same as
    reclassify-all -- click again once tokens refill to keep going.
    """
    ScanCoordinator.acquire_for_manual_scan()

    try:
        result = SellerWatchService.reclassify_oa_investigate_queue()
    finally:
        ScanCoordinator.release_after_manual_scan()

    flips = ", ".join(f"{count} -> {tag}" for tag, count in result["flipped_to"].items()) or "none"
    message = (
        f"OA queue reclassify: {result['processed']}/{result['queue_size']} ASIN(s) checked, "
        f"{result['flipped']} reclassified ({flips}), {result['stayed_oa']} confirmed still OA/unclear."
        + (" Stopped early -- low on tokens, click again once they refill." if result["stopped_early"] else "")
    )
    return RedirectResponse(url=f"/competitors/sellers?check_result={quote(message)}", status_code=303)
