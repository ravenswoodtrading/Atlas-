from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.services.product_repository import ProductRepository

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.get("/")
def dashboard(request: Request):
    stats = ProductRepository.get_summary_stats()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "request": request,
            "stats": stats,
        }
    )