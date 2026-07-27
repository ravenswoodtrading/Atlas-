from dataclasses import asdict

from app.services.product_finder import ProductFinder
from app.services.product_service import ProductService
from app.services.product_mapper import ProductMapper
from app.services.fee_engine import FeeEngine
from app.services.opportunity_engine import OpportunityEngine

EU_MARKETPLACES = ["DE", "FR", "ES", "IT"]


class BrandScanService:

    def __init__(self):
        self.finder = ProductFinder()
        self.product_service = ProductService()

    def scan(self, brand: str, limit: int = 20):
        """
        Full A2A pipeline for a brand:
        find ASINs -> pull UK + EU Keepa data -> map costs -> apply
        fees -> score with OpportunityEngine -> rank by score.
        """

        # Step 1 - Find ASINs
        asins = self.finder.find_brand(brand)

        # Keep the list bounded -- Keepa calls (and tokens) scale with this
        asins = asins[:limit]

        if not asins:
            return {"brand": brand, "count": 0, "opportunities": []}

        # Step 2 - Load UK data (selling side) and each EU marketplace