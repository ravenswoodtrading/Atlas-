from datetime import date, datetime, timezone

from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.services.review_queue_service import (
    ReviewQueueService, SOURCE_FILTERS, SOURCE_FILTER_LABELS,
    REVIEW_REASON_CATEGORIES, REVIEW_REASON_CATEGORY_LABELS,
    QUEUE_PRIORITY_BUY_NOW, QUEUE_PRIORITY_VA_TO_REVIEW,
    QUEUE_PRIORITY_BORDERLINE, QUEUE_PRIORITY_NEEDS_ATTENTION,
)
from app.services.product_repository import ProductRepository
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
}
VIEW_LABELS = {
    "all": "All",
    "buy_now": "Buy Now",
    "va_to_review": "VA to Review",
    "borderline": "Borderline",
    "needs_attention": "Needs Attention",
}

# Source-type filter (secondary, per section 20) -- reuses the exact
# "scan"/"competitor"/"lead" values already on every merged item's
# `sources` list (ReviewQueueService.merge_by_asin), just labelled with
# the brief's own source-type names for display.
SOURCE_TYPE_LABELS = {"scan": "Atlas", "competitor": "Competitor", "lead": "VA"}

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


@router.get("/review-queue")
def review_queue_page(
    request: Request, sort: str = "when_desc", view: str = "all", source: str = "all", q: str = "",
    marketplace: str = "all", competitor: str = "all",
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

    # Shared once (2026-09-04 perf fix) -- list_queue_items() and
    # queue_priority_summary() each need the same "latest ProductRecord
    # per ASIN" snapshot; computing it once here instead of letting each
    # reload the whole table independently roughly halves this route's
    # remaining query cost. See ProductRepository.get_latest_per_asin's
    # own docstring for the full history.
    latest_records = ProductRepository.get_latest_per_asin()
    all_items = ReviewQueueService.list_queue_items(sort=sort, latest_records=latest_records)
    summary = ReviewQueueService.queue_priority_summary(latest_records=latest_records)

    competitor_names = sorted({
        (i.get("competitor_info") or {}).get("seller").nickname
        for i in all_items
        if (i.get("competitor_info") or {}).get("seller")
    })
    competitor = competitor if competitor in competitor_names else "all"

    items = all_items
    if view != "all":
        wanted = VIEW_FILTERS[view]
        items = [i for i in items if wanted in i["views"]]
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

    return templates.TemplateResponse(
        request=request,
        name="review_queue.html",
        context={
            "request": request,
            "items": items,
            "unique_total": len(all_items),
            "summary": summary,
            "view": view,
            "view_labels": VIEW_LABELS,
            "source": source,
            "source_type_labels": SOURCE_TYPE_LABELS,
            "marketplace": marketplace,
            "marketplace_labels": MARKETPLACE_LABELS,
            "competitor": competitor,
            "competitor_names": competitor_names,
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

    return templates.TemplateResponse(
        request=request,
        name="_review_queue_item_detail.html",
        context={
            "request": request,
            "item": item,
            "asin": asin,
            "source_type_labels": SOURCE_TYPE_LABELS,
            "decision_buttons": DECISION_BUTTONS,
            "review_reason_categories": REVIEW_REASON_CATEGORIES,
            "review_reason_category_labels": REVIEW_REASON_CATEGORY_LABELS,
        }
    )


@router.post("/review-queue/resolve")
def review_queue_resolve(
    asin: str = Form(...),
    decision: str = Form(...),
    reason: str = Form(""),
    reason_category: str = Form(""),
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
    """
    resolved = ReviewQueueService.resolve_item(asin, decision, reason=reason or None, reason_category=reason_category or None)
    return JSONResponse({"ok": True, "asin": asin, "resolved": resolved})


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
    return JSONResponse({"ok": True})
