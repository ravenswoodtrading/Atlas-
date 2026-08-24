from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.services.review_queue_service import ReviewQueueService
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
def review_queue_page(request: Request, sort: str = "when_desc", view: str = "main"):
    """
    Two tabs (2026-08-19): "main" (default) -- star buys, BUY
    recommendations, worth-it PEAK leads, buyable competitor finds --
    still has to be reviewed to leave the list. "consider" -- CONSIDER
    leads that are profitable with some sign of sales but didn't clear
    the stricter bar; deliberately NOT gated on review status, see
    ReviewQueueService.list_consider_leads. Both tabs' counts are
    always computed (cheap, both already capped) so the tab labels can
    show live counts regardless of which tab is active.
    """
    view = view if view in ("main", "consider") else "main"

    main_leads = ReviewQueueService.list_leads(sort=sort)
    consider_leads = ReviewQueueService.list_consider_leads(sort=sort)

    leads = main_leads if view == "main" else consider_leads

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
        }
    )
