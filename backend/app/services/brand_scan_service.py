from dataclasses import asdict

from app.services.product_finder import ProductFinder
from app.services.product_service import ProductService
from app.services.product_mapper import ProductMapper
from app.services.fee_engine import FeeEngine
from app.services.opportunity_engine import OpportunityEngine
from app.config.exclusions import is_excluded

EU_MARKETPLACES = ["DE", "FR", "ES", "IT"]

# Safety margin -- don't spend right down to 0, leave headroom for
# whatever else might use the account (e.g. another tab open).
MIN_TOKEN_BUFFER = 5


class BrandScanService:

    def __init__(self):
        self.finder = ProductFinder()
        self.product_service = ProductService()

    def scan(self, brand: str, limit: int = 20):
        """
        Full A2A pipeline for a brand, with token-budget awareness so
        a scan never silently hangs waiting for Keepa tokens to
        refill. Instead: measure the real token cost of the UK batch,
        use that measured rate to decide -- BEFORE each EU
        marketplace call -- whether there's enough budget left, and
        skip (with a clear note in the response) rather than block.
        """

        # Step 1 - Find ASINs
        asins = self.finder.find_brand(brand)
        asins = asins[:limit]

        if not asins:
            return {
                "brand": brand, "count": 0, "opportunities": [],
                "skipped_excluded": 0, "marketplaces_skipped_low_tokens": [],
                "tokens_remaining": None,
            }

        tokens_before_uk = self.product_service.api.tokens_left

        if tokens_before_uk is not None and tokens_before_uk <= MIN_TOKEN_BUFFER:
            return {
                "brand": brand, "count": 0, "opportunities": [],
                "skipped_excluded": 0, "marketplaces_skipped_low_tokens": [],
                "tokens_remaining": tokens_before_uk,
                "error": (
                    f"Only {tokens_before_uk} Keepa tokens left -- not enough "
                    f"to safely start a scan. Wait for your account to refill."
                ),
            }

        # Step 2 - Load UK data first (this is the only lookup we pay
        # for regardless of exclusions -- we need it to know category)
        uk_products = self.product_service.get_products(asins, "UK")

        tokens_after_uk = self.product_service.api.tokens_left

        # Measure the REAL per-ASIN token cost from what we just spent,
        # rather than guessing -- Keepa's cost per call depends on the
        # options requested (history, offers, stats, etc.) which we
        # don't want to hardcode assumptions about.
        if tokens_before_uk is not None and tokens_after_uk is not None:
            uk_cost = max(tokens_before_uk - tokens_after_uk, 0)
            per_asin_cost_estimate = (uk_cost / len(asins)) if asins else 0
        else:
            per_asin_cost_estimate = None

        # Step 3 - Filter out excluded categories/ASINs/gated brands
        # BEFORE spending tokens on the 4 EU marketplaces.
        included_uk_products = []
        skipped_excluded = 0

       for uk_product in uk_products:
            asin = uk_product.get("asin") or ""
            brand_name = uk_product.get("brand") or ""
            category = str(uk_product.get("rootCategory") or "")

            if is_excluded(asin, brand_name, category):
                skipped_excluded += 1
                continue

            included_uk_products.append(uk_product)

        included_asins = [p.get("asin") for p in included_uk_products]

        # Step 4 - Pull EU data one marketplace at a time, checking
        # measured token budget before each one.
        eu_lookups = {}
        marketplaces_skipped_low_tokens = []

        for marketplace in EU_MARKETPLACES:
            if not included_asins:
                eu_lookups[marketplace] = {}
                continue

            tokens_now = self.product_service.api.tokens_left

            if per_asin_cost_estimate is not None and tokens_now is not None:
                estimated_cost = per_asin_cost_estimate * len(included_asins)

                if tokens_now < estimated_cost + MIN_TOKEN_BUFFER:
                    marketplaces_skipped_low_tokens.append(marketplace)
                    eu_lookups[marketplace] = {}
                    continue

            eu_lookups[marketplace] = {
                p.get("asin"): p
                for p in self.product_service.get_products(included_asins, marketplace)
            }

        opportunities = []

        # Step 5 - Map, price, and score each remaining ASIN
        for uk_product in included_uk_products:
            asin = uk_product.get("asin")

            eu_products = {
                marketplace: lookup.get(asin)
                for marketplace, lookup in eu_lookups.items()
            }

            product = ProductMapper.from_keepa_multi(uk_product, eu_products)

            if not product.buy_box_now:
                continue

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
            "marketplaces_skipped_low_tokens": marketplaces_skipped_low_tokens,
            "tokens_remaining": self.product_service.api.tokens_left,
            "opportunities": opportunities,
        }
