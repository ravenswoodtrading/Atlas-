from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database.dependencies import get_db
from app.models.product import Product

router = APIRouter()

templates = Jinja2Templates(directory="backend/app/templates")


@router.get("/products/add")
def add_product_page(request: Request):
    return templates.TemplateResponse(
        request,
        "add_product.html",
        {}
    )


@router.post("/products/add")
def add_product(
    asin: str = Form(...),
    title: str = Form(...),
    brand: str = Form(...),
    buy_price: float = Form(...),
    sell_price: float = Form(...),
    db: Session = Depends(get_db),
):
    profit = sell_price - buy_price
    roi = (profit / buy_price) * 100 if buy_price else 0

    product = Product(
        asin=asin,
        title=title,
        brand=brand,
        buy_price=buy_price,
        sell_price=sell_price,
        profit=profit,
        roi=roi,
    )

    db.add(product)
    db.commit()

    return RedirectResponse("/products", status_code=303)