import re
from dataclasses import replace

from app.services.product_service import ProductService
from app.services.product_mapper import ProductMapper
from app.services.category_survey_service import get_category_names
from app.services.fee_engine import FeeEngine
from app.services import serpapi_client

# Words too generic to help narrow a Google Shopping search -- stripped
# out of the naive query suggestion so it doesn't waste words on noise
# like "the"/"with" that Amazon titles are full of.
STOPWORDS = {
    "the", "a", "an", "with", "for", "and", "of", "in", "to", "on",
    "experience", "perfect", "ideal", "premium", "new",
}

# How many significant words to keep from the title for the suggested
# query -- a starting point only, the route/template exposes this as
# an EDITABLE field since title-cleaning is inherently imprecise
# (confirmed empirically: a hand-cleaned query found real matches,
# the raw 150-character Amazon title almost certainly wouldn't).
SUGGESTED_QUERY_WORD_COUNT = 10


class OaLookupService:

    @staticmethod
    def get_baseline(asin: str):
        """
        UK-side Keepa data for the given ASIN -- title, EAN, current
        price, category. Returns (product, category_name) or
        (None, "") if Keepa has nothing for this ASIN. No EU calls at
        all -- OA doesn't need a cross-border cost comparison.
        """
        service = ProductService()
        products = service.get_products([asin], "UK", full=True)

        if not products:
            return None, ""

        product = ProductMapper.from_keepa(products[0])

        if not product.title:
            return None, ""

        category_names = get_category_names(service.api)
        category_name = category_names.get(product.category, "")

        return product, category_name

    @staticmethod
    def suggest_query(title: str) -> str:
        words = re.findall(r"[A-Za-z0-9&']+", title)
        significant = [w for w in words if w.lower() not in STOPWORDS]
        return " ".join(significant[:SUGGESTED_QUERY_WORD_COUNT])

    @staticmethod
    def search_candidates(product, category_name: str, query: str) -> list:
        """
        Searches UK retailers via SerpApi and prices each result as if
        it were the product's A2A source -- FeeEngine already handles
        this correctly with NO new fee logic: best_source_marketplace
        set to a label not in EU_VAT_RATE_BY_MARKETPLACE falls back to
        the UK VAT rate, which is exactly right for a domestic UK
        retailer purchase (input VAT reclaimed, same as the revenue
        side). Sorted by profit descending.

        NOT auto-matched -- these are candidates for a human to visually
        confirm (title, thumbnail, retailer, a link to investigate
        further), since title-based search returns same-brand results
        that can be the wrong pack size/variant (confirmed during
        testing, not hypothetical).
        """
        results = serpapi_client.search_uk_shopping(query)
        candidates = []

        for result in results:
            hypothetical = replace(
                product,
                best_source_marketplace="UK-OA",
                best_source_cost_gbp=result["extracted_price"],
            )
            fees = FeeEngine.calculate(hypothetical, category_name=category_name)

            candidates.append({
                **result,
                "profit": fees.profit,
                "roi": fees.roi,
                "profit_90d": fees.profit_90d,
                "roi_90d": fees.roi_90d,
            })

        candidates.sort(key=lambda c: c["profit"], reverse=True)

        return candidates
