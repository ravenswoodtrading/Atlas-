from app.keepa.client import get_keepa_client


class ProductFinder:

    def __init__(self):
        self.api = get_keepa_client()

    def find_brand(self, brand: str, limit: int = 100):
        """
        Finds ASINs for a brand via Keepa's Product Finder.

        `limit` controls how many results are requested (perPage) --
        previously this always requested 100 regardless of what the
        caller actually needed, wasting tokens on a typical small
        scan. Capped at 100 (Keepa's per-page max).

        wait=False is passed so a low token balance returns/raises
        quickly instead of the keepa library silently blocking for
        however long a refill takes (this was previously the one
        Keepa call in the whole pipeline NOT covered by our
        token-budget checks -- everything else in BrandScanService
        checks tokens before calling, but this ran unconditionally
        before that check ever happened).
        """

        per_page = max(1, min(limit, 100))

        query = {
            "productType": ["0"],
            "brand": [brand.strip().lower()],
            "sort": [
                ["current_SALES", "asc"],
                ["monthlySold", "desc"]
            ],
            "perPage": per_page,
            "page": 0
        }

        print("Calling Product Finder...")

        try:
            products = self.api.product_finder(query, wait=False)
        except TypeError:
            # This installed version of the keepa library doesn't
            # accept wait= on product_finder -- fall back to the
            # blocking default rather than crashing outright.
            products = self.api.product_finder(query)
        except Exception as exc:
            print(f"Product Finder failed: {exc}")
            return []

        print("Type:", type(products))
        print("Count:", len(products))

        return products