from datetime import date, datetime

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
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


templates.env.filters["days_ago"] = _days_ago

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

# Unified decision vocabulary the UI's Review Actions buttons submit --
# see ReviewQueueService.resolve_item/DECISION_VALUES. Labelled here
# for the button row; the actual values are exactly what resolve_item
# already accepts, nothing invented.
DECISION_BUTTONS = [
    ("approved", "Buy", "success", "bi-check-circle-fill"),
    ("watch", "Watch", "warning", "bi-eye-fill"),
    ("rejected", "Avoid", "danger", "bi-x-circle-fill"),
    ("need_more_info", "Need More Info", "secondary", "bi-question-circle-fill"),
]


@router.get("/review-queue")
def review_queue_page(request: Request, sort: str = "when_desc", view: str = "all", source: str = "all"):
    """
    Unified Review Queue (Command Centre UI build, 2026-09-03) -- one
    ASIN, one row, regardless of how many of Scan/Competitor Watch/VA
    found it (ReviewQueueService.merge_by_asin/list_queue_items,
    already built and tested; nothing about dedup/priority/decision
    logic lives here, this route only filters and displays what that
    service already computed).

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
    """
    view = view if view in VIEW_FILTERS else "all"
    source = source if source in SOURCE_TYPE_LABELS else "all"

    all_items = ReviewQueueService.list_queue_items(sort=sort)
    summary = ReviewQueueService.queue_priority_summary()

    items = all_items
    if view != "all":
        wanted = VIEW_FILTERS[view]
        items = [i for i in items if wanted in i["views"]]
    if source != "all":
        items = [i for i in items if source in i["sources"]]

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
            "watched_asins": ProductRepository.get_watched_asins(),
            "sort": sort,
            "decision_buttons": DECISION_BUTTONS,
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
    fresh from list_queue_items() (not from anything cached client-side)
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
    return_to: str = Form("/review-queue"),
):
    """
    ONE decision resolves every outstanding source/view for this ASIN
    at once (section 13) -- calls ReviewQueueService.resolve_item
    directly, the same function test_unified_review_queue.py already
    exercises against real data. No decision logic of any kind lives
    in this route or in any JavaScript -- this is a thin form-to-
    service call, exactly like every other review action already in
    Atlas (see /review/set, /review/decide).
    """
    ReviewQueueService.resolve_item(asin, decision, reason=reason or None, reason_category=reason_category or None)
    return RedirectResponse(url=return_to, status_code=303)
