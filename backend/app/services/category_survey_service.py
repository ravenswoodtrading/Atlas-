from collections import defaultdict

from app.services.product_finder import ProductFinder
from app.services.product_service import ProductService

# Cached {category_id_str: name} lookup for root categories -- built
# once per app run (category_lookup(0) returns ALL root categories in
# one call) and reused after that, since root categories almost never
# change. Avoids repeating this call on every single survey.
_category_name_cache = None


def get_category_names(api, domain="GB"):
    """
    Public so routes (e.g. the Discovery page filter chips) can show
    human-readable names for category IDs already selected, without
    re-running a full brand survey just to label them.
    """
    global _category_name_cache

    if _category_name_cache is not None:
        return _category_name_cache

    try:
        categories = api.category_lookup(0, domain=domain)
        _category_name_cache = {
            str(cat_id): (data.get("name") or "Unknown")
            for cat_id, data in categories.items()
        }
    except Exception as exc:
        print(f"Category name lookup failed: {exc}")
        _category_name_cache = {}

    return _category_name_cache


class CategorySurveyService:
    """
    Cheap way to see what categories a brand's catalog actually spans,
    WITHOUT paying for the 4 EU marketplace lookups per ASIN --  only
    the UK lookup is needed to see title/brand/category. Use this to
    build up app/config/exclusions.py before running full opportunity
    scans, or to find real category IDs for a category-filtered
    brand search.
    """

    def __init__(self):
        self.finder = ProductFinder()
        self.product_service = ProductService()

    def survey(self, brand: str, limit: int = 100):
        asins = self.finder.find_brand(brand, limit=limit)

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

        category_names = get_category_names(self.product_service.api)

        categories = [
            {
                "category_id": cat,
                "category_name": category_names.get(cat, "Unknown"),
                "count": data["count"],
                "sample_titles": data["sample_titles"],
            }
            for cat, data in sorted(by_category.items(), key=lambda kv: -kv[1]["count"])
        ]

        return {
            "brand": brand,
            "asins_scanned": len(uk_products),
            "tokens_remaining": self.product_service.api.tokens_left,
            "categories": categories,
        }