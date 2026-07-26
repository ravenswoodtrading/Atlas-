from fastapi import APIRouter

from app.services.product_finder import ProductFinder

router = APIRouter(
    prefix="/scan",
    tags=["Scanner"],
)


@router.get("/{brand}")
def scan_brand(brand: str):

    finder = ProductFinder()

    products = finder.find_brand(brand)

    return {
        "count": len(products)
    }