from app.keepa.client import get_keepa_client


class ProductFinder:

    def __init__(self):
        self.api = get_keepa_client()

    def find_brand(self, brand: str, limit: int = 100, page: int = 0):
        """
        Finds ASINs for a brand via Keepa's Product Finder.

        `page` selects which 100-result page to fetch (0-indexed).
        Without pagination, any brand with more than 100 matching
        products would be permanently invisible beyond its first 100
        results, no matter how many times it's scanned. Results are
        NOT deliberately sorted by rank (see below) -- just walked
        through page by page in whatever order Keepa returns.

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
            # Exclude products with no sales rank at all -- these are
            # typically brand-new or barely-tracked listings, not
            # genuine candidates either way.
            #
            # NOTE: deliberately NOT sorting by current_SALES here.
            # Ascending should mean "best rank first" (rank 1 = best
            # seller), but that direction hasn't been verified against
            # a live response -- the same category of assumption that
            # broke the perPage parameter earlier. Rather than risk it
            # being backwards and silently fetching the worst sellers
            # first every time, pagination now just walks through
            # Keepa's own (effectively arbitrary, from our side) order
            # instead. Every page still only contains genuinely ranked
            # products, just not deliberately best-to-worst.
            "current_SALES_gte": 1,
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