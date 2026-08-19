import json

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.services.product_repository import ProductRepository

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

PAGE_SIZE = 25


def _render_products(request: Request, page: int, filter: str, brand: str, sort: str,
                      today_only: bool, base_path: str, view: str):
    filter_value = None
    review_filter = None

    if filter == "profitable":
        filter_value = True
    elif filter == "unprofitable":
        filter_value = False
    elif filter == "unreviewed_notable":
        review_filter = "notable"
    elif filter == "unreviewed_consider":
        review_filter = "consider"
    elif filter == "unreviewed":
        review_filter = "any"

    page = max(page, 1)

    records, total_count = ProductRepository.list_latest(
        page=page, page_size=PAGE_SIZE, profitable_only=filter_value,
        brand=brand or None, sort=sort, today_only=today_only,
        review_filter=review_filter,
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
            "brand": brand,
            "sort": sort,
            "available_brands": ProductRepository.get_distinct_brands(),
            "watched_asins": ProductRepository.get_watched_asins(),
            "excluded_asins": ProductRepository.get_excluded_asins(),
            "base_path": base_path,
            "view": view,
        }
    )


@router.get("/products/today")
def products_today(request: Request, page: int = 1, filter: str = "all",
                    brand: str = "", sort: str = "scanned_desc"):
    return _render_products(request, page, filter, brand, sort,
                             today_only=True, base_path="/products/today", view="today")


@router.get("/products")
def products(request: Request, page: int = 1, filter: str = "all",
             brand: str = "", sort: str = "scanned_desc"):
    """
    Historical scans -- everything whose latest scan happened before
    today. Today's scans live on their own page (/products/today) so
    the two views never overlap.
    """
    return _render_products(request, page, filter, brand, sort,
                             today_only=False, base_path="/products", view="historical")
