from dataclasses import asdict

from app.services.product_finder import ProductFinder
from app.services.product_service import ProductService
from app.services.product_mapper import ProductMapper
from app.services.fee_engine import FeeEngine
from app.services.opportunity_engine import OpportunityEngine
from app.config.exclusions import is_excluded

EU_MARKETPLACES = ["DE", "FR", "ES", "IT"]


class BrandScanService:

    def __init__(self):
        self.finder = ProductFinder()
        self.product_service = ProductService()

    def scan(self, brand: str, limit: int = 20):
        """
        Full A2A pipeline for a brand:
        find ASINs -> pull UK data -> filter out excluded
        categories/ASINs/gated brands BEFORE spending tokens on EU
        marketplaces -> pull EU data for what's left -> map costs ->
        apply fees -> score with OpportunityEngine -> rank by score.
        """

        # Step 1 - Find ASINs
        asins = self.finder.find_brand(brand)

        # Keep the list bounded -- Keepa calls (and tokens) scale with this
        asins = asins[:limit]

        if not asins:
            return {"brand": brand, "count": 0, "opportunities": [], "skipped_excluded": 0}

        # Step 2 - Load UK data first (this is the only lookup we pay
        # for regardless of exclusions -- we need it to know category)
        uk_products = self.product_service.get_products(asins, "UK")

        # Step 3 - Filter out excluded categories/ASINs/gated brands
        # BEFORE spending tokens on the 4 EU marketplaces. This is
        # where the token savings actually happen.
        included_uk_products = []
        skipped_excluded = 0

        for uk_product in uk_products:
            asin = uk_product.get("asin", "")
            brand_name = uk_product.get("brand", "")
            category = str(uk_product.get("rootCategory", ""))

            if is_excluded(asin, brand_name, category):
                skipped_excluded += 1
                continue

            included_uk_products.append(uk_product)

        included_asins = [p.get("asin") for p in included_uk_products]

        # Step 4 - Only now pull EU data, and only for ASINs that survived filtering
        eu_lookups = {
            marketplace: {
                p.get("asin"): p
                for p in self.product_service.get_products(included_asins, marketplace)
            }
            for marketplace in EU_MARKETPLACES
        } if included_asins else {marketplace: {} for marketplace in EU_MARKETPLACES}

        opportunities = []

        # Step 5 - Map, price, and score each remaining ASIN
        for uk_product in included_uk_products:
            asin = uk_product.get("asin")

            eu_products = {
                marketplace: lookup.get(asin)
                for marketplace, lookup in eu_lookups.items()
            }

            product = ProductMapper.from_keepa_multi(uk_product, eu_products)

            # No live UK buy box price -- can't calculate a real profit,
            # so this isn't a usable opportunity (was previously producing
            # nonsense negative profit/ROI numbers off a 0 sell price).
            if not product.buy_box_now:
                continue

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

        # Step 6 - Rank best opportunities first
        opportunities.sort(key=lambda o: o["report"]["score"], reverse=True)

        return {
            "brand": brand,
            "count": len(opportunities),
            "skipped_excluded": skipped_excluded,
            "opportunities": opportunities,
        }
