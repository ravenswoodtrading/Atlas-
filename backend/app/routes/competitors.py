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


def build_competitors_context(tab: str = "", buyable_only: bool = False,
                               review_filter: str = "", category: str = "", since_days: int = 0,
                               check_result: str = "") -> dict:
    """
    Kept as its own function (not inlined into competitors_legacy_page)
    even though /competitors-legacy is its only caller now -- Leads Hub
    used to be a second caller (removed 2026-09-04, Navigation redesign:
    it duplicated this page's own, better-maintained detections feed).
    """
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


@router.get("/competitors-legacy")
def competitors_legacy_page(request: Request, tab: str = "", buyable_only: bool = False,
                             review_filter: str = "", category: str = "", since_days: int = 0,
                             check_result: str = ""):
    """
    The PRE-redesign detections-table page (Competitor Watch redesign,
    2026-09-03) -- kept reachable at its own URL, unchanged, purely as
    a fallback/reference while the new /competitors Opportunities feed
    beds in. Not linked from the sidebar (Navigation redesign,
    2026-09-04) -- direct URL only. Candidate for removal alongside
    build_competitors_context/_competitors_content.html once the new
    Opportunities feed is fully trusted.
    """
    return templates.TemplateResponse(
        request=request,
        name="competitors_legacy.html",
        context={"request": request, **build_competitors_context(
            tab, buyable_only, review_filter, category, since_days, check_result
        )}
    )


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
    "all": "All", "buy_now": "Buy Now", "needs_attention": "Needs Attention",
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
    """
    buy_now = SellerWatchService.list_notable_buyable(limit=300)
    needs_attention = SellerWatchService.list_historical_a2a_not_buyable(limit=300)
    oa_worth = SellerWatchService.list_oa_worth_investigating(limit=300)

    for entry in buy_now:
        entry["opp_view"] = "buy_now"
    for entry in needs_attention:
        entry["opp_view"] = "needs_attention"
    for entry in oa_worth:
        entry["opp_view"] = "oa_investigate"

    pool = buy_now + needs_attention + oa_worth

    counts = {
        "buy_now": len(buy_now),
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

    IMPORTANT: this is a DIFFERENT `tab` than build_competitors_context's
    own `tab` param (EU A2A/UK A2A/Wholesale/OA) -- that one still
    exists, unchanged, on /competitors-legacy (no longer linked from the
    sidebar, direct URL only). To avoid exactly this ambiguity within
    THIS route, the equivalent sourcing-tag filter here is named
    `source` instead (see OPPORTUNITY_SOURCE_FILTERS).
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
    """
    Detail drawer content for ONE Competitor Watch opportunity --
    reuses ReviewQueueService.get_queue_item(asin) UNCHANGED (the exact
    same merged-item shape /review-queue/item/{asin} already returns;
    no new merge/priority logic here, Review Queue's own service/
    template are untouched). Rendered through a Competitor-Watch-
    specific partial (not Review Queue's own detail partial) so its
    Next Action buttons can differ -- Find Source/Google for an
    unconfirmed OA item vs Buy/Watch for a currently-buyable one --
    without adding OA-specific branching into Review Queue's template.
    """
    item = ReviewQueueService.get_queue_item(asin)

    competitor_source = None
    if item:
        competitor_source = next(
            (s for s in item["source_items"] if s["source"] == "competitor"), None,
        )

    # EAN isn't a field on the merged Review Queue item (never needed
    # there) -- read straight off the ProductRecord, same cheap lookup
    # build_opportunities_context already does per-row.
    db = SessionLocal()
    try:
        record = (
            db.query(ProductRecord)
            .filter(ProductRecord.asin == asin)
            .order_by(ProductRecord.scanned_at.desc())
            .first()
        )
        ean = record.ean if record else ""
    finally:
        db.close()

    google_urls = _google_search_url_variants(item.get("title", "") if item else "", ean, asin)

    return templates.TemplateResponse(
        request=request,
        name="_competitor_opportunity_detail.html",
        context={
            "request": request,
            "item": item,
            "asin": asin,
            "competitor_source": competitor_source,
            "google_urls": google_urls,
        }
    )


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


@router.post("/competitors/dismiss")
def competitors_dismiss(listing_id: int = Form(...), return_to: str = Form("/competitors-legacy")):
    SellerWatchService.dismiss_detection(listing_id)
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/competitors/review")
def competitors_review(listing_id: int = Form(...), verdict: str = Form(""),
                        return_to: str = Form("/competitors-legacy")):
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
    return RedirectResponse(url=f"/competitors-legacy?check_result={quote(message)}", status_code=303)


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
    return RedirectResponse(url=f"/competitors-legacy?check_result={quote(message)}", status_code=303)
