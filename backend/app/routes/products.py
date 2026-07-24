from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter()

templates = Jinja2Templates(directory="backend/app/templates")


@router.get("/products")
def products(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="products.html",
        context={"request": request}
    )