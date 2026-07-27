from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
import traceback

from app.keepa.client import get_keepa_client

router = APIRouter(
    prefix="/keepa",
    tags=["Keepa"]
)

templates = Jinja2Templates(directory="app/templates")


@router.get("/test")
def test_keepa():
    try:
        api = get_keepa_client()

        return {
            "status": "connected",
            "tokens_left": api.tokens_left
        }

    except Exception:
        traceback.print_exc()
        raise


@router.get("/product/{asin}")
def get_product(request: Request, asin: str):
    try:
        api = get_keepa_client()

        products = api.query(asin, domain="GB")

        if not products:
            return {
                "error": "Product not found"
            }

        product = products[0]

        # Build a simple view model instead of passing the whole Keepa object
        view_model = {
            "asin": product.get("asin"),
            "title": product.get("title"),
            "brand": product.get("brand"),
            "manufacturer": product.get("manufacturer"),
        }

        return templates.TemplateResponse(
            request=request,
            name="product.html",
            context={
                "product": view_model
            }
        )

    except Exception:
        traceback.print_exc()
        raise