from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database.dependencies import get_db
from app.models.product import Product

router = APIRouter()

templates = Jinja2Templates(directory="backend/app/templates")


@router.get("/products")
def products(
    request: Request,
    db: Session = Depends(get_db)
):
    products = db.query(Product).order_by(Product.title).all()

    return templates.TemplateResponse(
        request=request,
        name="products.html",
        context={
            "request": request,
            "products": products
        }
    )