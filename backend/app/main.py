from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder

from app.services.brand_scan_service import BrandScanService
from app.services.product_finder import ProductFinder
from app.services.product_service import ProductService
from app.services.category_survey_service import CategorySurveyService

from app.routes import dashboard, keepa, scan, analyse, opportunities_view

app = FastAPI(title="Atlas")

# NOTE: routes/products.py and routes/add_product.py are NOT registered
# yet -- their Product DB model doesn't match the atlas.db schema, so
# they'll 500 on request. Out of scope until the persistence layer is
# rebuilt; see /areas notes.
app.include_router(dashboard.router)
app.include_router(keepa.router)
app.include_router(scan.router)
app.include_router(analyse.router)
app.include_router(opportunities_view.router)


@app.get("/opportunities/{brand}")
def opportunities_for_brand(brand: str, limit: int = 20):
    """
    Full A2A pipeline: find a brand's ASINs, price them across UK/DE/FR/ES/IT,
    apply fees, score with OpportunityEngine, and return ranked opportunities.
    """
    scanner = BrandScanService()
    return scanner.scan(brand, limit=limit)


@app.get("/categories/{brand}")
def categories_for_brand(brand: str, limit: int = 100):
    """
    Cheap survey of what categories a brand's catalog spans, using
    only UK lookups (1x token cost per ASIN, not 5x). Use this to
    decide what to add to app/config/exclusions.py BEFORE running
    full /opportunities scans.
    """
    survey = CategorySurveyService()
    return survey.survey(brand, limit=limit)


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