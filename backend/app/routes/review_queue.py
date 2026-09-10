from datetime import date, datetime, timezone
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.services.review_queue_service import (
    ReviewQueueService, SOURCE_FILTERS, SOURCE_FILTER_LABELS,
    REVIEW_REASON_CATEGORIES, REVIEW_REASON_CATEGORY_LABELS,
    QUEUE_PRIORITY_BUY_NOW, QUEUE_PRIORITY_VA_TO_REVIEW,
    QUEUE_PRIORITY_BORDERLINE, QUEUE_PRIORITY_NEEDS_ATTENTION,
    QUEUE_PRIORITY_OA_INVESTIGATE, clean_atlas_notes,
)
from app.services.product_repository import ProductRepository
from app.services.oa_source_discovery_service import OaSourceDiscoveryService
from app.services.seller_watch_service import SellerWatchService, SOURCING_TAG_BY_TAB
from app.database.database import SessionLocal
from app.database.models import ProductRecord, Lead
# Reused, not duplicated (2026-09-05, OA Source Discovery Review Queue
# view) -- the exact same Google-search-link builders the Opportunities
# page's own OA tooling already uses (competitors.py).
from app.routes.competitors import _google_search_url_variants, _oa_search_url_variants
from app.routes.verdict import highlight_figures

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")
# review_queue.html's merged "Why?" panel renders Lead-sourced
# rationale bullets with this filter (same reason leads.py needed it
# registered too -- each route module owns its own Jinja2Templates
# instance in this codebase, see that fix's own comment).
templates.env.filters["highlight_figures"] = highlight_figures


def _days_ago(iso_date: str | None) -> str:
    """
    Presentational only (Command Centre UI build) -- SourcingClassifier's
    guessed_buy_date is stored as a plain "YYYY-MM-DD" string (see its
    own docstring), not a days-ago count; the mock-up's "10 days ago"
    framing is computed here at display time, not invented as a new
    backend field. Falls back to the raw string on anything unparseable
    rather than erroring on old/malformed data.
    """
    if not iso_date:
        return ""
    try:
        parsed = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return iso_date
    days = (date.today() - parsed).days
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def _ago(when: datetime | None) -> str:
    """
    Same "X ago" framing as dashboard.py's _format_ago (duplicated
    rather than imported -- each route module owns its own template
    helpers in this codebase, see _days_ago's own comment just above),
    applied to a merged item's own `when` datetime (already-existing
    field, see _build_merged_item) for the queue rows' "Added" column
    (UI redesign pass, 2026-09-03).
    """
    if not when:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = max(0, (datetime.now(timezone.utc) - when).total_seconds())
    if seconds < 90:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _confidence_label(confidence: int | None) -> str | None:
    """
    Buckets the real ProductRecord.confidence 0-100 int (see
    _scan_lead_dict's own comment on why this now exists on the merged
    item) into the same High/Medium/Low language the mock-up uses --
    thresholds only, never a fabricated value; None stays None (no
    confidence line shown) rather than defaulting to a fake middle
    value.
    """
    if confidence is None:
        return None
    if confidence >= 70:
        return "High"
    if confidence >= 40:
        return "Medium"
    return "Low"


templates.env.filters["days_ago"] = _days_ago
templates.env.filters["ago"] = _ago
templates.env.filters["confidence_label"] = _confidence_label

# Workflow view filter (Command Centre UI build, 2026-09-03) -- the
# PRIMARY filter bar (atlas-review-queue-backend-v1.md's follow-up UI
# brief, section 20). Maps the URL's plain-English `view` value onto
# ReviewQueueService's own QUEUE_PRIORITY_* constants -- presentation-
# layer filtering only (a list comprehension over already-computed
# item["views"]), NOT a new backend decision/dedup mechanism, so this
# stays in the route, not review_queue_service.py (explicitly told not
# to touch that file's architecture for this task).
VIEW_FILTERS = {
    "buy_now": QUEUE_PRIORITY_BUY_NOW,
    "va_to_review": QUEUE_PRIORITY_VA_TO_REVIEW,
    "borderline": QUEUE_PRIORITY_BORDERLINE,
    "needs_attention": QUEUE_PRIORITY_NEEDS_ATTENTION,
    # Fifth view, added 2026-09-05 -- see QUEUE_PRIORITY_OA_INVESTIGATE's
    # own comment in review_queue_service.py.
    "oa_investigate": QUEUE_PRIORITY_OA_INVESTIGATE,
}
VIEW_LABELS = {
    "all": "All to Review",
    "buy_now": "Buy Now",
    "va_to_review": "VA Leads to Decide",
    "borderline": "Verify Before Buying",
    "oa_investigate": "Find an OA Source",
}

# Items with these Lens actions are useful signals, but are not decisions a
# person can make today. Keep them in Atlas for its monitoring workflows,
# rather than letting them make the Review Queue look like an ever-growing
# unresolved to-do list. VA and OA items remain reviewable regardless: they
# each have their own human workflow.
MONITOR_ONLY_ACTIONS = {"WATCH", "HISTORICAL_RECURRING", "BLOCKED"}
_REVIEW_QUEUE_CACHE = {}
_REVIEW_QUEUE_CACHE_TTL = 8


def _clear_review_queue_cache():
    _REVIEW_QUEUE_CACHE.clear()


def _is_monitor_only(item: dict) -> bool:
    return (
        item.get("action") in MONITOR_ONLY_ACTIONS
        and "lead" not in item.get("sources", [])
        and "oa_investigate" not in item.get("sources", [])
        and not item.get("conflict")
    )


def _matches_workflow_view(item: dict, view: str) -> bool:
    """Whether a merged item belongs in one of the four human workflows."""
    if _is_monitor_only(item):
        return False
    views = set(item.get("views", []))
    sources = set(item.get("sources", []))
    if view == "buy_now":
        return QUEUE_PRIORITY_BUY_NOW in views
    if view == "va_to_review":
        return "lead" in sources
    if view == "oa_investigate":
        return "oa_investigate" in sources
    if view == "borderline":
        # A conflict or any former Borderline/Needs Attention item is a
        # verification task, unless it is a monitor-only signal above.
        return (
            item.get("conflict")
            or QUEUE_PRIORITY_BORDERLINE in views
            or QUEUE_PRIORITY_NEEDS_ATTENTION in views
        )
    return False


def _requires_human_review(item: dict) -> bool:
    return any(_matches_workflow_view(item, view) for view in VIEW_FILTERS)

# Source-type filter (secondary, per section 20) -- reuses the exact
# "scan"/"competitor"/"lead" values already on every merged item's
# `sources` list (ReviewQueueService.merge_by_asin), just labelled with
# the brief's own source-type names for display. "oa_investigate" added
# 2026-09-05 -- ReviewQueueService._oa_investigate_lead_dict's own
# distinct source value, so an OA item's Source badge/filter reads
# "OA Source Discovery", never "Competitor" (it has no confirmed
# source, unlike a real competitor find).
SOURCE_TYPE_LABELS = {
    "scan": "Atlas", "competitor": "Competitor", "lead": "VA",
    "oa_investigate": "OA Source Discovery",
}

# Source-store (marketplace) filter, added 2026-09-04 -- every merged
# item already carries best_source_marketplace (review_queue.html's
# flag emoji row uses the same 5 values), just exposed as a filter too.
MARKETPLACE_LABELS = {"UK": "UK", "DE": "Germany", "FR": "France", "ES": "Spain", "IT": "Italy"}

# Competitor filter, added 2026-09-04 -- filters to items whose
# competitor_info.seller matches one of the sellers actually present
# in the CURRENT unfiltered queue (not every TrackedSeller ever added,
# which would list sellers with nothing outstanding right now).

# Sort control (UI redesign pass, 2026-09-03) -- exposes
# ReviewQueueService.SORT_OPTIONS in the UI itself; the values are
# passed straight through to list_queue_items(sort=...), nothing new
# computed here.
SORT_LABELS = {
    "when_desc": "Newest first",
    "score_desc": "Highest score",
    "profit_desc": "Highest profit",
    "roi_desc": "Highest ROI",
}

# Unified decision vocabulary the UI's Review Actions buttons submit --
# see ReviewQueueService.resolve_item/DECISION_VALUES. Labelled here
# for the button row; the actual values are exactly what resolve_item
# already accepts, nothing invented.
DECISION_BUTTONS = [
    ("approved", "Buy", "success", "bi-check-circle-fill"),
    ("watch", "Watch", "warning", "bi-eye-fill"),
    ("rejected", "Avoid", "danger", "bi-x-circle-fill"),
    # Renamed from "Need More Info" 2026-09-04 -- that label read as an
    # action that would SHOW you more info (open something), when it's
    # actually a fourth resolution decision (same "leaves the queue"
    # behaviour as Buy/Watch/Avoid) for "I don't have enough to decide
    # either way yet". Stored value unchanged (still "need_more_info",
    # same DECISION_VALUES/dashboard breakdown), only the button text.
    ("need_more_info", "Unsure", "secondary", "bi-question-circle-fill"),
]

# Quick Reject menu (UI redesign pass, 2026-09-03) -- one-click reasons
# for the row-level "no detail panel needed" reject path ("if you
# already know you're gated on this, Reject -> Gated in seconds").
# Each maps onto an EXISTING REVIEW_REASON_CATEGORIES value (see that
# tuple's own comments) -- resolve_item is called with
# decision="rejected" and this reason_category, exactly the same call
# the detail panel's own Avoid button makes with a category attached;
# no new decision pathway, just a faster way to reach it. Labels here
# are the quick-menu's own short wording and may read differently from
# REVIEW_REASON_CATEGORY_LABELS' fuller text used elsewhere (e.g.
# reviewed history) -- same underlying stored value either way.
QUICK_REJECT_REASONS = [
    ("GATED", "Gated"),
    ("ALREADY_BOUGHT", "Already bought"),
    ("PRICE_CHANGED", "Price changed"),
    # Added 2026-09-08, Tamara -- see REVIEW_REASON_CATEGORIES' own
    # comment in review_queue_service.py for the full reasoning.
    ("RISKY_PRICE_DROP", "Risky price drop"),
    ("INSUFFICIENT_PROFIT", "Margin too low"),
    # Added 2026-09-04, user-requested -- all four map onto EXISTING
    # REVIEW_REASON_CATEGORIES values (OUT_OF_STOCK is the one genuinely
    # new category, added there; the other three already existed but
    # weren't reachable from this quick menu, only the detail panel's
    # full dropdown).
    ("OUT_OF_STOCK", "Out of stock"),
    ("PRODUCT_RISK", "Risky listing"),
    ("INSUFFICIENT_SALES", "Doesn't sell enough"),
    ("TOO_MUCH_STOCK", "Too much FBA stock"),
    ("NOT_INTERESTED", "Don't want"),
    ("OTHER", "Other..."),
]


@router.get("/review-queue/oa-investigate/export.xlsx")
def review_queue_oa_investigate_export():
    """
    "Download Excel" button on the OA to Investigate view (2026-09-07,
    Tamara: "lets give me a button on the App to download the full
    list"). Reuses oa_export_service's own row-building logic verbatim
    -- the exact same query/certainty computation the OA to Investigate
    view itself uses, so the download always matches what's on screen.
    Read-only, no Keepa cost.
    """
    from app.services.oa_export_service import build_oa_investigate_workbook

    buf, _ = build_oa_investigate_workbook()
    filename = f"oa_to_investigate_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/review-queue")
def review_queue_page(
    request: Request, sort: str = "when_desc", view: str = "all", source: str = "all", q: str = "",
    marketplace: str = "all", competitor: str = "all", reason: str = "all", page: int = 1,
):
    """
    Unified Review Queue (Command Centre UI build, 2026-09-03; refined
    2026-09-03 pass 2 -- search box + sort control added, row-level
    quick actions) -- one ASIN, one row, regardless of how many of
    Scan/Competitor Watch/VA found it (ReviewQueueService.merge_by_asin/
    list_queue_items, already built and tested; nothing about dedup/
    priority/decision logic lives here, this route only filters and
    displays what that service already computed).

    view: "all" (default) | "buy_now" | "va_to_review" | "borderline" |
    "needs_attention" -- the PRIMARY filter (section 20). An item can
    belong to more than one view at once (section 2/3) -- filtering to
    one view shows every item that INCLUDES it, not just items whose
    single strongest view happens to match. Any unrecognised value
    (including the pre-unification "main"/"consider" query values some
    old bookmarks/links may still carry) falls back to "all" rather
    than erroring, so nothing that used to work 404s or 500s.

    source: "all" (default) | "scan" | "competitor" | "lead" --
    secondary filter (section 20), same values already on every merged
    item's `sources` list.

    marketplace: "all" (default) | "UK" | "DE" | "FR" | "ES" | "IT" --
    filters on best_source_marketplace, added 2026-09-04.

    competitor: "all" (default) | a seller nickname -- filters to items
    whose competitor_info.seller matches, added 2026-09-04. Only sellers
    with something outstanding right now appear in the dropdown (built
    off all_items below, before any filter is applied).

    q: free-text search (UI redesign pass, 2026-09-03) -- matched
    case-insensitively against title/ASIN on the already-fetched items
    list, no new query/index. Purely a display filter, same as view/
    source above.
    """
    view = view if view in VIEW_FILTERS else "all"
    source = source if source in SOURCE_TYPE_LABELS else "all"
    sort = sort if sort in SORT_LABELS else "when_desc"
    marketplace = marketplace if marketplace in MARKETPLACE_LABELS else "all"
    reason_options = {
        "price_drop": "Price drop", "low_sales": "Low sales evidence",
        "low_roi": "Low ROI", "needs_review": "Needs review", "missing_data": "Missing data",
    }
    reason = reason if reason in reason_options else "all"

    def item_reason(item):
        # A peak-price opportunity takes precedence over the current ROI
        # badge: it is specifically profitable if the Buy Box recovers.
        if item.get("recommendation") == "PEAK_WINDOW" or (
            (item.get("roi_90d") or 0) >= 25 and (item.get("roi") or 0) < 25
        ):
            return "price_drop"
        if not ProductRepository.has_verify_sales_evidence(item.get("monthly_sales") or 0, item.get("sales_drops_30d") or 0):
            return "missing_data"
        if (item.get("monthly_sales") or 0) < 1 and (item.get("sales_drops_30d") or 0) < 12:
            return "low_sales"
        if item.get("recommendation") in ("LOW_CONFIDENCE", "LOW_SCORE"):
            return "needs_review"
        if max(item.get("roi") or 0, item.get("roi_90d") or 0) < 25:
            return "low_roi"
        return "needs_review"

    # One unified queue build per request.  list_queue_items() already
    # provides every item's view membership, so the tab counts below can
    # be derived from that result.  Calling queue_priority_summary() here
    # used to rebuild and merge the whole queue a second time just to get
    # those counts; with a large scan history that made opening the page
    # look like it had stalled.
    cached_queue = _REVIEW_QUEUE_CACHE.get(sort)
    now = time.monotonic()
    if cached_queue and now - cached_queue[0] < _REVIEW_QUEUE_CACHE_TTL:
        all_items = cached_queue[1]
    else:
        latest_records = ProductRepository.get_latest_per_asin()
        all_items = ReviewQueueService.list_queue_items(sort=sort, latest_records=latest_records)
        _REVIEW_QUEUE_CACHE[sort] = (now, all_items)
    review_items = [item for item in all_items if _requires_human_review(item)]
    monitor_count = len(all_items) - len(review_items)
    summary = {
        "unique_items": len(review_items),
        # The exact number of merged raw rows requires the same second
        # full queue rebuild. It is explanatory rather than actionable,
        # so omit it from this fast path instead of delaying the page.
        "duplicates_merged": 0,
        **{
            "buy_now": sum(1 for item in review_items if _matches_workflow_view(item, "buy_now")),
            "va_to_review": sum(1 for item in review_items if _matches_workflow_view(item, "va_to_review")),
            "borderline": sum(1 for item in review_items if _matches_workflow_view(item, "borderline")),
            "oa_investigate": sum(1 for item in review_items if _matches_workflow_view(item, "oa_investigate")),
        },
    }

    competitor_names = sorted({
        (i.get("competitor_info") or {}).get("seller").nickname
        for i in review_items
        if (i.get("competitor_info") or {}).get("seller")
    })
    competitor = competitor if competitor in competitor_names else "all"

    items = review_items
    if view != "all":
        items = [item for item in items if _matches_workflow_view(item, view)]
    if source != "all":
        items = [i for i in items if source in i["sources"]]
    if marketplace != "all":
        items = [i for i in items if i.get("best_source_marketplace") == marketplace]
    if competitor != "all":
        items = [
            i for i in items
            if (i.get("competitor_info") or {}).get("seller")
            and i["competitor_info"]["seller"].nickname == competitor
        ]
    if q.strip():
        needle = q.strip().lower()
        items = [
            i for i in items
            if needle in (i.get("asin") or "").lower() or needle in (i.get("title") or "").lower()
        ]
    if reason != "all":
        items = [i for i in items if item_reason(i) == reason]

    page_size = 50
    page_count = max(1, (len(items) + page_size - 1) // page_size)
    page = max(1, min(page, page_count))
    first = (page - 1) * page_size
    page_items = items[first:first + page_size]
    query = dict(view=view, source=source, sort=sort, q=q,
        marketplace=marketplace, competitor=competitor, reason=reason)
    previous_url = '/review-queue?' + urlencode({**query, 'page': page - 1}) if page > 1 else None
    next_url = '/review-queue?' + urlencode({**query, 'page': page + 1}) if page < page_count else None

    return templates.TemplateResponse(
        request=request,
        name="review_queue.html",
        context={
            "request": request,
            "items": page_items,
            "page": page,
            "page_count": page_count,
            "page_start": first + 1 if items else 0,
            "page_end": min(first + page_size, len(items)),
            "filtered_total": len(items),
            "previous_url": previous_url,
            "next_url": next_url,
            "unique_total": len(review_items),
            "monitor_count": monitor_count,
            "summary": summary,
            "view": view,
            "view_labels": VIEW_LABELS,
            "source": source,
            "source_type_labels": SOURCE_TYPE_LABELS,
            "marketplace": marketplace,
            "marketplace_labels": MARKETPLACE_LABELS,
            "competitor": competitor,
            "competitor_names": competitor_names,
            "reason": reason,
            "reason_options": reason_options,
            "item_reason": item_reason,
            "watched_asins": ProductRepository.get_watched_asins(),
            "sort": sort,
            "sort_labels": SORT_LABELS,
            "q": q,
            "decision_buttons": DECISION_BUTTONS,
            "quick_reject_reasons": QUICK_REJECT_REASONS,
            "review_reason_categories": REVIEW_REASON_CATEGORIES,
            "review_reason_category_labels": REVIEW_REASON_CATEGORY_LABELS,
        }
    )


@router.get("/review-queue/item/{asin}")
def review_queue_item_detail(request: Request, asin: str):
    """
    Detail panel content for ONE unified item (section 14), fetched via
    a small AJAX call and injected into the page's shared offcanvas
    (see review_queue.html's own script) rather than rendering every
    row's full detail inline on the list page. Re-derives the item
    fresh from get_queue_item() (not from anything cached client-side)
    so the panel is never stale relative to whatever's actually in the
    database right now.
    """
    item = ReviewQueueService.get_queue_item(asin)

    # OA Source Discovery workbench (2026-09-05, redesigned from the
    # original "just another lead" OA card per Tamara's own review of
    # the live screenshot -- see save_manual_source's own docstring for
    # the underlying persistence). Computed only when this item
    # actually has an OA source (oa_price_guide is None otherwise),
    # since none of this is relevant for an item with a confirmed
    # source already. EAN read straight off ProductRecord -- it's not a
    # field on the merged Review Queue item (never needed there before
    # now). MPN only ever comes from a PRIOR automated OA Source
    # Discovery run (OaSourceCandidate.mpn) -- there is no free way to
    # get it otherwise (Keepa-only field), so the MPN search button
    # simply doesn't render rather than spending a token just to
    # populate a search box (see safety constraints on this build).
    google_urls = None
    oa_search_urls = None
    source_finder_url = None
    manual_candidate = None
    ean = ""
    if item and item.get("oa_price_guide"):
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

        manual_candidate = OaSourceDiscoveryService.get_manual_candidate(asin)
        mpn = manual_candidate.mpn if manual_candidate else ""

        google_urls = _google_search_url_variants(item.get("title", ""), ean, asin)
        oa_search_urls = _oa_search_url_variants(item.get("title", ""), item.get("brand", ""), ean, mpn)
        source_finder_url = f"/competitors?tab=source_finder&asin={asin}"

    return templates.TemplateResponse(
        request=request,
        name="_review_queue_item_detail.html",
        context={
            "request": request,
            "item": item,
            "asin": asin,
            "oa_search_urls": oa_search_urls,
            "manual_candidate": manual_candidate,
            "ean": ean,
            "mpn": manual_candidate.mpn if manual_candidate else "",
            "source_type_labels": SOURCE_TYPE_LABELS,
            "decision_buttons": DECISION_BUTTONS,
            "review_reason_categories": REVIEW_REASON_CATEGORIES,
            "review_reason_category_labels": REVIEW_REASON_CATEGORY_LABELS,
            "google_urls": google_urls,
            "source_finder_url": source_finder_url,
        }
    )


@router.post("/review-queue/oa/save-source")
def review_queue_oa_save_source(
    asin: str = Form(...),
    retailer_domain: str = Form(""),
    retailer_url: str = Form(""),
    retailer_price_gbp: float = Form(...),
    delivery_gbp: float = Form(0.0),
    notes: str = Form(""),
    source_confidence: str = Form(""),
):
    """
    OA workbench "Save Source" (2026-09-05) -- persists a human-found
    retail source and returns the computed economics, WITHOUT resolving
    the item or making any Buy/Reject decision (see OaSourceDiscoveryService.
    save_manual_source's own docstring: Tamara's explicit instruction is
    that entering a source price must never automatically become a BUY).
    The detail panel re-fetches itself after this call (same pattern the
    resolve/undo toast already uses) so the "Source Found" status and the
    existing Review Actions form both reflect the freshly-saved source.

    source_confidence: "High"/"Medium"/"Low"/"" (2026-09-07, Tamara --
    see save_manual_source's own docstring for the full reasoning).
    """
    result = OaSourceDiscoveryService.save_manual_source(
        asin, retailer_domain, retailer_url, retailer_price_gbp, delivery_gbp, notes, source_confidence,
    )
    return JSONResponse({"ok": True, "asin": asin, **result})


@router.post("/review-queue/save-note")
def review_queue_save_note(asin: str = Form(...), atlas_notes: str = Form(...)):
    """
    Save an Atlas comment WITHOUT making a Buy/Reject/etc decision yet
    (2026-09-05) -- Tamara asked for a way to write a note back to the
    VA independent of deciding the lead. Applies to every still-pending
    lead for this ASIN regardless of source (same reasoning as Pass 2's
    reconciliation in pull_and_ingest_va_leads: Lead Sheet is the
    team's single master sheet), then pushes immediately via
    push_decision_to_sheet -- which (see its own docstring) writes ONLY
    the Atlas Notes column here, since no decision was made.
    """
    db = SessionLocal()
    lead_ids = []
    try:
        for lead in db.query(Lead).filter(Lead.asin == asin, Lead.decision.is_(None)).all():
            lead.atlas_notes = clean_atlas_notes(atlas_notes)
            lead_ids.append(lead.id)
        db.commit()
    finally:
        db.close()

    pushed = 0
    for lead_id in lead_ids:
        try:
            from app.services.google_sheets_lead_sync import push_decision_to_sheet
            if push_decision_to_sheet(lead_id):
                pushed += 1
        except Exception as exc:
            print(f"push_decision_to_sheet (note-only) failed for lead {lead_id}: {exc}")

    return JSONResponse({"ok": True, "asin": asin, "leads_updated": len(lead_ids), "pushed_to_sheet": pushed})


@router.post("/review-queue/resolve")
def review_queue_resolve(
    asin: str = Form(...),
    decision: str = Form(...),
    reason: str = Form(""),
    reason_category: str = Form(""),
    purchased_qty: str = Form(""),
    atlas_notes: str = Form(""),
    also_exclude: str = Form(""),
):
    """
    ONE decision resolves every outstanding source/view for this ASIN
    at once (section 13) -- calls ReviewQueueService.resolve_item
    directly, the same function test_unified_review_queue.py already
    exercises against real data. No decision logic of any kind lives
    in this route or in any JavaScript -- this is a thin form-to-
    service call, exactly like every other review action already in
    Atlas (see /review/set, /review/decide).

    Returns JSON (UI redesign pass, 2026-09-03 -- was a redirecting
    form post before) rather than redirecting, so both the row-level
    quick actions AND the detail panel's Review Actions can call this
    via fetch(), show a small "Reviewed -- Undo" toast instead of a
    full page reload, and hand the returned `resolved` shape straight
    to /review-queue/undo if the user clicks Undo.

    `already_resolved` (2026-09-04 fix) -- True when every source
    resolve_item checked was ALREADY reviewed/dismissed before this
    call (nothing outstanding left to touch): resolve_item itself only
    ever acts on rows it finds genuinely outstanding right now (see its
    own docstring), so this click did nothing new. Real, reproduced bug:
    a row can still be showing in a page the user has had open for a
    while after something else (another tab, an automated recheck)
    already resolved the same ASIN -- clicking Buy on it used to come
    back "ok": true with an empty `resolved` and the UI showed a normal
    success/Undo toast anyway, implying the click had just bought
    something it hadn't. The frontend uses this flag to tell the two
    cases apart honestly instead of always claiming success.
    """
    # Atlas Notes (2026-09-05, VA Lead Sheet sync) -- written directly
    # onto any outstanding sheet-sourced lead(s) for this ASIN BEFORE
    # resolving, so push_decision_to_sheet (called inside resolve_item
    # for source="sheet" leads) has the note to push out in the same
    # write as the decision itself.
    cleaned_atlas_notes = clean_atlas_notes(atlas_notes)
    if cleaned_atlas_notes:
        db = SessionLocal()
        try:
            for lead in db.query(Lead).filter(Lead.asin == asin, Lead.source == "sheet", Lead.decision.is_(None)).all():
                lead.atlas_notes = cleaned_atlas_notes
            db.commit()
        finally:
            db.close()

    qty = None
    if purchased_qty.strip():
        try:
            qty = float(purchased_qty.strip())
        except ValueError:
            qty = None

    resolved = ReviewQueueService.resolve_item(
        asin, decision, reason=reason or None, reason_category=reason_category or None, purchased_qty=qty,
    )

    # "Also exclude from future scans" (2026-09-07, Tamara) -- an explicit
    # opt-in checkbox in the detail panel's Review Actions form, offered
    # alongside Avoid but never automatic: choosing a reason_category like
    # GATED is only a reporting label (see REVIEW_REASON_CATEGORIES' own
    # comment) and was never wired to any exclusion logic -- real bug
    # Tamara caught (a hard-gated Philips product kept resurfacing after
    # being rejected as "Gated"). This reuses the exact same ExcludedProduct
    # table/check the Exclusions page's own "Exclude ASIN" form writes to
    # (see ProductRepository.add_exclusion) -- checked before ANY future
    # Keepa spend on this ASIN, unlike reason_category.
    if also_exclude and decision == "rejected":
        ProductRepository.add_exclusion(
            asin.strip().upper(),
            reason=reason.strip() or REVIEW_REASON_CATEGORY_LABELS.get(reason_category, "") or "Excluded from Review Queue",
        )

    already_resolved = not resolved["scan"] and not resolved["competitor"] and not resolved["lead"]
    _clear_review_queue_cache()
    return JSONResponse({"ok": True, "asin": asin, "resolved": resolved, "already_resolved": already_resolved})


@router.post("/review-queue/reclassify")
def review_queue_reclassify(asin: str = Form(...), sourcing_tag: str = Form(...)):
    """
    Manual sourcing-tag correction (2026-09-07, Tamara: found a real
    "OA / unclear" item that was actually EU A2A -- see SellerNewListing.
    manually_classified's own docstring). Only meant for the OA to
    Investigate detail panel, where "Atlas got the classification wrong"
    is exactly the thing being reported -- not exposed as a bulk action
    anywhere else.
    """
    if sourcing_tag not in SOURCING_TAG_BY_TAB.values():
        return JSONResponse({"ok": False, "error": "Unknown sourcing tag."}, status_code=400)

    updated = SellerWatchService.set_manual_sourcing_tag(asin, sourcing_tag)
    _clear_review_queue_cache()
    return JSONResponse({"ok": True, "asin": asin, "sourcing_tag": sourcing_tag, "updated": updated})


@router.post("/review-queue/undo")
def review_queue_undo(asin: str = Form(...), scan: str = Form(""), competitor: str = Form(""), lead: str = Form("")):
    """
    Undo for the toast /review-queue/resolve's JS shows immediately
    after a review action (UI redesign pass, 2026-09-03). `scan`/
    `competitor`/`lead` are exactly the `resolved` fields the preceding
    resolve call returned (competitor/lead as comma-separated ids,
    scan as "1"/"" ) -- see ReviewQueueService.unresolve_item for what
    this does and its one documented edge case.
    """
    resolved = {
        "scan": scan == "1",
        "competitor": [int(x) for x in competitor.split(",") if x],
        "lead": [int(x) for x in lead.split(",") if x],
    }
    ReviewQueueService.unresolve_item(asin, resolved)
    _clear_review_queue_cache()
    return JSONResponse({"ok": True})
