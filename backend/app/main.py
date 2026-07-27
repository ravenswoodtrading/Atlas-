from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder

from app.services.brand_scan_service import BrandScanService
from app.services.product_finder import ProductFinder
from app.services.product_service import ProductService

app = FastAPI()


@app.get("/scan/{brand}")
def scan_brand(brand: str):
    scanner = BrandScanService()
    return scanner.scan(brand)


@app.get("/debug/{brand}")
def debug_brand(brand: str):
    """
    Returns the raw Keepa product in a JSON-safe format.
    """

    finder = ProductFinder()
    service = ProductService()

    asins = finder.find_brand(brand)

    if not asins:
        return {"error": "No products found"}

    products = service.get_products([asins[0]], "UK")

    if not products:
        return {"error": "No Keepa product returned"}

    return jsonable_encoder(products[0])
