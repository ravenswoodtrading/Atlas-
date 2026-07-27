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

        # Step 2 - Load UK data (selling side) and each EU marketplace (buying side)
        uk_products = self.product_service.get_products(asins, "UK")

        eu_lookups = {
            marketplace: {
                p.get("asin"): p
                for p in self.product_service.get_products(asins, marketplace)
            }
            for marketplace in EU_MARKETPLACES
        }

        opportunities = []

        # Step 3 - Map, price, and score each ASIN
        for uk_product in uk_products:
            asin = uk_product.get("asin")

            eu_products = {
                marketplace: lookup.get(asin)
                for marketplace, lookup in eu_lookups.items()
            }

            product = ProductMapper.from_keepa_multi(uk_product, eu_products)

            # Skip anything with no EU source at all -- there's no A2A deal here
            if not product.best_source_marketplace:
                continue

            fees = FeeEngine.calculate(product)
            product.fba_fee = fees.fba_fee
            product.referral_fee = fees.referral_fee
            product.profit = fees.profit
            product.roi = fees.roi

            report = OpportunityEngine.analyse(product)

            opportunities.append({
                "product": asdict(product),
                "report": asdict(report),
            })

        # Step 4 - Rank best opportunities first
        opportunities.sort(key=lambda o: o["report"]["score"], reverse=True)

        return {
            "brand": brand,
            "count": len(opportunities),
            "opportunities": opportunities,
        }
