from app.keepa.client import get_keepa_client


class ProductFinder:

    def __init__(self):
        self.api = get_keepa_client()

    def find_brand(self, brand: str, limit: int = 100, page: int = 0):
        """
        Finds ASINs for a brand via Keepa's Product Finder.

        `page` selects which 100-result page to fetch (0-indexed) --
        results are sorted best-seller-first, so page=0 is always the
        top 100 by sales rank, page=1 is the next 100, etc. Without
        this, any brand with more than 100 matching products would be
        permanently invisible beyond its top 100 best-sellers, no
        matter how many times it's scanned.

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
            # Exclude products with no sales rank at all. If Keepa
            # represents "unranked" as -1 (their usual convention for
            # missing data elsewhere), sorting current_SALES ascending
            # without this filter would put those at the very front --
            # -1 is a smaller number than any real rank -- meaning
            # every search would show the least-tracked, most obscure
            # listings first instead of genuine best-sellers. This was
            # flagged by seeing real results with no confirmed sales
            # data showing up first.
            "current_SALES_gte": 1,
            "sort": [
                ["current_SALES", "asc"],
                ["monthlySold", "desc"]
            ],
            "perPage": 100,
            "page": page,
        }

        print(f"Calling Product Finder (page {page})...")

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