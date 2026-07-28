from app.keepa.client import get_keepa_client


class ProductFinder:

    def __init__(self):
        self.api = get_keepa_client()

    def find_brand(self, brand: str, limit: int = 100):
        """
        Finds ASINs for a brand via Keepa's Product Finder.

        IMPORTANT: always requests perPage=100, Keepa's max, REGARDLESS
        of `limit` -- confirmed via direct testing that Keepa's Product
        Finder rejects smaller values (e.g. perPage=20 returns
        REQUEST_REJECTED) while perPage=100 succeeds, with an otherwise
        byte-for-byte identical request. This was a real bug introduced
        by an earlier "only request what we need" optimization -- it
        broke the endpoint rather than saving tokens. `limit` is now
        applied by trimming the returned list afterward instead.

        wait=False is passed so a low token balance returns/raises
        quickly instead of the keepa library silently blocking for
        however long a refill takes.
        """

        query = {
            "productType": ["0"],
            "brand": [brand.strip().lower()],
            "sort": [
                ["current_SALES", "asc"],
                ["monthlySold", "desc"]
            ],
            "perPage": 100,
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

        return products[:max(1, limit)]