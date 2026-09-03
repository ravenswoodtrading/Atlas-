from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.services.review_queue_service import (
    ReviewQueueService, SOURCE_FILTERS, SOURCE_FILTER_LABELS,
    REVIEW_REASON_CATEGORIES, REVIEW_REASON_CATEGORY_LABELS,
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


@router.get("/review-queue")
def review_queue_page(request: Request, sort: str = "when_desc", view: str = "main", source: str = ""):
    """
    Two tabs (2026-08-19): "main" (default) -- star buys, BUY
    recommendations, worth-it PEAK leads, buyable competitor finds --
    still has to be reviewed to leave the list. "consider" -- CONSIDER
    leads that are profitable with some sign of sales but didn't clear
    the stricter bar; deliberately NOT gated on review status, see
    ReviewQueueService.list_consider_leads. Both tabs' counts are
    always computed (cheap, both already capped) so the tab labels can
    show live counts regardless of which tab is active.

    source (2026-08-26): optional origin filter -- see
    review_queue_service.SOURCE_FILTERS -- so e.g. every VA Sheet lead
    can be reviewed as one batch instead of interleaved with scan/
    competitor/other Lead rows. Applied AFTER the tab split, so
    main_count/consider_count (the tab badges) stay the true unfiltered
    totals for that tab -- only the visible table narrows.
    """
    view = view if view in ("main", "consider") else "main"

    main_leads = ReviewQueueService.list_leads(sort=sort)
    consider_leads = ReviewQueueService.list_consider_leads(sort=sort)

    leads = main_leads if view == "main" else consider_leads
    leads = ReviewQueueService.filter_by_source(leads, source)

    return templates.TemplateResponse(
        request=request,
        name="review_queue.html",
        context={
            "request": request,
            "leads": leads,
            "view": view,
            "main_count": len(main_leads),
            "consider_count": len(consider_leads),
            "watched_asins": ProductRepository.get_watched_asins(),
            "sort": sort,
            "source": source,
            "source_filters": SOURCE_FILTERS,
            "source_filter_labels": SOURCE_FILTER_LABELS,
            "review_reason_categories": REVIEW_REASON_CATEGORIES,
            "review_reason_category_labels": REVIEW_REASON_CATEGORY_LABELS,
        }
    )
