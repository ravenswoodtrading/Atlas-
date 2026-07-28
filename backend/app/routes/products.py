import json

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.services.product_repository import ProductRepository

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

PAGE_SIZE = 25


@router.get("/products")
def products(request: Request, page: int = 1, filter: str = "all"):
    filter_value = None

    if filter == "profitable":
        filter_value = True
    elif filter == "unprofitable":
        filter_value = False

    page = max(page, 1)

    records, total_count = ProductRepository.list_latest(
        page=page, page_size=PAGE_SIZE, profitable_only=filter_value
    )

    # Parse the stored report JSON so the template can show the same
    # score/confidence breakdown that was computed at scan time,
    # without needing to re-run scoring logic.
    for record in records:
        try:
            record.parsed_report = json.loads(record.report_json) if record.report_json else {}
        except Exception:
            record.parsed_report = {}

    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)

    return templates.TemplateResponse(
        request=request,
        name="products.html",
        context={
            "request": request,
            "products": records,
            "page": page,
            "total_pages": total_pages,
            "total_count": total_count,
            "filter": filter,
            "watched_asins": ProductRepository.get_watched_asins(),
            "excluded_asins": ProductRepository.get_excluded_asins(),
        }
    )