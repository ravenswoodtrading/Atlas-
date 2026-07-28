from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder

from app.services.brand_scan_service import BrandScanService
from app.services.product_finder import ProductFinder
from app.services.product_service import ProductService
from app.services.category_survey_service import CategorySurveyService

from app.database.base import Base
from app.database.database import engine
from app.database import models  # noqa: F401 -- registers ProductRecord with Base.metadata

from app.routes import dashboard, keepa, scan, analyse, opportunities_view, products, watchlist

app = FastAPI(title="Atlas")

# Creates any tables that don't exist yet (e.g. product_records) without
# touching existing ones. The old 'products' table (incompatible legacy
# schema) is left alone -- product_records is a separate table.
Base.metadata.create_all(bind=engine)

# NOTE: routes/add_product.py is still NOT registered -- it targets the
# old incompatible 'products' table. Manual add-a-product isn't wired
# up yet; only scan-based persistence via product_records is.
app.include_router(dashboard.router)
app.include_router(keepa.router)
app.include_router(scan.router)
app.include_router(analyse.router)
app.include_router(opportunities_view.router)
app.include_router(products.router)
app.include_router(watchlist.router)


@app.get("/opportunities/{brand}")
def opportunities_for_brand(brand: str, limit: int = 20, force_rescan: bool = False):
    """
    Full A2A pipeline: find a brand's ASINs, price them across UK/DE/FR/ES/IT,
    apply fees, score with OpportunityEngine, and return ranked opportunities.
    By default, ASINs scanned recently for this brand are skipped to
    save tokens -- pass force_rescan=true to check everything anyway.
    """
    scanner = BrandScanService()
    return scanner.scan(brand, limit=limit, force_rescan=force_rescan)


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

    asins = finder.find_brand(brand, limit=1)

    if not asins:
        return {"error": "No products found"}

    products = service.get_products([asins[0]], "UK")

    if not products:
        return {"error": "No Keepa product returned"}

    return jsonable_encoder(products[0])