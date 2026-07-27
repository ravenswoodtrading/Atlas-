from collections import defaultdict

from app.services.product_finder import ProductFinder
from app.services.product_service import ProductService


class CategorySurveyService:
    """
    Cheap way to see what categories a brand's catalog actually spans,
    WITHOUT paying for the 4 EU marketplace lookups per ASIN --  only
    the UK lookup is needed to see title/brand/category. Use this to
    build up app/config/exclusions.py before running full opportunity
    scans.
    """

    def __init__(self):
        self.finder = ProductFinder()
        self.product_service = ProductService()

    def survey(self, brand: str, limit: int = 100):
        asins = self.finder.find_brand(brand)
        asins = asins[:limit]

        if not asins:
            return {"brand": brand, "asins_scanned": 0, "categories": []}

        uk_products = self.product_service.get_products(asins, "UK")

        by_category = defaultdict(lambda: {"count": 0, "sample_titles": []})

        for p in uk_products:
            category = str(p.get("rootCategory") or "unknown")
            entry = by_category[category]
            entry["count"] += 1

            if len(entry["sample_titles"]) < 3:
                title = p.get("title") or p.get("asin") or "(no title)"
                entry["sample_titles"].append(title)

        categories = [
            {"category": cat, "count": data["count"], "sample_titles": data["sample_titles"]}
            for cat, data in sorted(by_category.items(), key=lambda kv: -kv[1]["count"])
        ]

        return {
            "brand": brand,
            "asins_scanned": len(uk_products),
            "tokens_remaining": self.product_service.api.tokens_left,
            "categories": categories,
        }