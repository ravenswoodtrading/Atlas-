from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.services.review_queue_service import ReviewQueueService
from app.services.product_repository import ProductRepository

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.get("/review-queue")
def review_queue_page(request: Request, sort: str = "when_desc"):
    leads = ReviewQueueService.list_leads(sort=sort)

    return templates.TemplateResponse(
        request=request,
        name="review_queue.html",
        context={
            "request": request,
            "leads": leads,
            "watched_asins": ProductRepository.get_watched_asins(),
            "sort": sort,
        }
    )
