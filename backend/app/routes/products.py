from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.services.product_repository import ProductRepository

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


@router.get("/products")
def products(request: Request):
    records = ProductRepository.list_latest(limit=200)

    return templates.TemplateResponse(
        request=request,
        name="products.html",
        context={
            "request": request,
            "products": records,
        }
    )
