from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.services.brand_scan_service import BrandScanService

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.get("/discovery")
def discovery(request: Request, brand: str = "", limit: int = 20):
    result = None

    if brand:
        scanner = BrandScanService()
        result = scanner.scan(brand, limit=limit)

    return templates.TemplateResponse(
        request=request,
        name="discovery.html",
        context={
            "request": request,
            "brand": brand,
            "limit": limit,
            "result": result,
        }
    )