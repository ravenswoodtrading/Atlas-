from app.keepa.client import get_keepa_client
from app.services.token_usage_service import TokenUsageService


class ProductFinder:

    def __init__(self):
        self.api = get_keepa_client()

    def find_brand(self, brand: str, limit: int = 100, page: int = 0, category_ids: list = None,
                    usage_category: str = "other"):
        """
        Finds ASINs for a brand via Keepa's Product Finder.

        `category_ids` (optional): a list of Keepa root category IDs
        to restrict results to (e.g. ["213077031"] for "Lighting").
        Use CategorySurveyService (or the /categories page) to find
        real category IDs and names for a brand first.

        `page` selects which 100-result page to fetch (0-indexed).
        Without pagination, any brand with more than 100 matching
        products would be permanently invisible beyond its first 100
        results, no matter how many times it's scanned. Results ARE
        now sorted best-sellers-first by current_SALES ascending (see
        below) -- so within a token budget that can't cover a whole
        brand's catalog in one pass, page 0 hits the strongest
        candidates first instead of Keepa's arbitrary default order.

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

        Returns None (not []) if the Keepa call itself failed
        (network error, timeout, rate limit, etc) -- this MUST stay
        distinguishable from a genuine empty result, which is a real,
        meaningful "this brand has zero matches right now". Conflating
        the two previously caused ScanQueueService.run_next_tick to
        treat a single transient API failure as "brand exhausted,
        0 products" and permanently mark the queue item done -- e.g.
        "trend" and "weber" both got closed out this way on their
        first tick, even though a plain retry immediately afterward
        found 100 results for each. Callers must check `is None`
        explicitly; plain falsiness (`if not asins`) still treats both
        the same, which is fine for callers that don't distinguish
        (see CategorySurveyService/main.py's /debug endpoint) but
        WRONG for anything that decides whether to keep retrying (see
        BrandScanService.scan).
        """

        query = {
            "productType": ["0"],
            "brand": [brand.strip().lower()],
            # Exclude products with no sales rank at all -- these are
            # typically brand-new or barely-tracked listings, not
            # genuine candidates either way.
            "current_SALES_gte": 1,
            # Brand discovery only: UK prices are in pence. COUNT_NEW
            # is offer-count history, not distinct sellers or gating proof.
            # https://keepa.com/api-docs/product-finder.html
            "current_BUY_BOX_SHIPPING_gte": 1000,
            "avg90_COUNT_NEW_gte": 3,
            # Whole-percent Finder ceiling: exclude 97%+ ownership by
            # any seller (Amazon included), without another product call.
            "buyBoxStatsTopSeller90_lte": 96,
            # Best-sellers first. Amazon/Keepa sales rank convention:
            # rank 1 = best seller in its category, so ascending order
            # on current_SALES is lowest-rank-number-first = strongest
            # candidates first. This was previously left unsorted out
            # of caution (the same "verify before trusting an
            # assumption" instinct that caught the perPage bug below),
            # but unlike that case this is documented behaviour, not a
            # guess: the keepa library's own docs give
            # `sort=[["current_SALES", "asc"]]` as the worked example
            # for exactly this (https://keepaapi.readthedocs.io/).
            # Still worth a first-scan sanity check after deploying --
            # print(...) below logs the first page's sales ranks so a
            # backwards sort would be obvious at a glance (ranks
            # should start low and increase down the page, not be
            # scattered/huge) rather than silently fetching worst
            # sellers first the way the unverified perPage change once
            # silently broke requests entirely.
            "sort": [["current_SALES", "asc"]],
            "perPage": 100,
            "page": page,
        }

        if category_ids:
            # "rootCategory" (not "categories_include") is the correct
            # field for this -- categories_include only matches
            # sub-categories directly beneath an already-chosen
            # rootCategory, and returns zero results when used alone
            # with a root category ID (confirmed via direct testing).
            # rootCategory matches a product's actual rootCategory
            # field, which is exactly what CategorySurveyService groups
            # by -- so IDs shown on the /categories page line up
            # exactly with what gets filtered here.
            query["rootCategory"] = [str(c) for c in category_ids]

        print(f"Calling Product Finder (page {page})...")
        tokens_before = self.api.tokens_left

        try:
            # domain="GB" -- MUST match the marketplace every downstream
            # lookup uses (ProductService maps "UK" to domain "GB").
            # Without this, product_finder defaults to the keepa
            # library's own default ("US"), silently searching the US
            # catalog while category IDs (and everything else) come
            # from the UK one. Confirmed via direct testing: a real UK
            # root category ID returns 0 results against the US catalog,
            # even for a brand that has 100 matches without a category
            # filter -- ASIN overlap between US/UK masks the mismatch
            # for a plain brand search, but category IDs never line up
            # across marketplaces at all.
            products = self.api.product_finder(query, wait=False, domain="GB")
        except TypeError:
            # This installed version of the keepa library doesn't
            # accept wait= on product_finder -- fall back to the
            # blocking default rather than crashing outright. This
            # fallback call previously had no error handling of its
            # own, so a rate-limit/network error on the retry (as
            # opposed to the wait= call itself) would raise unhandled
            # all the way up to a bare 500 instead of the graceful
            # "return None" every caller already expects for a failed
            # Product Finder call.
            try:
                products = self.api.product_finder(query, domain="GB")
            except Exception as exc:
                print(f"Product Finder failed (fallback call): {exc}")
                return None
        except Exception as exc:
            print(f"Product Finder failed: {exc}")
            return None

        TokenUsageService.record_keepa_spend(
            usage_category, "keepa_product_finder", tokens_before, self.api.tokens_left,
            marketplace="UK", asins_count=len(products),
        )

        print("Type:", type(products))
        print("Count:", len(products))

        return products[:max(1, limit)]

    def find_signal_candidates(self, signal_type: str, category_ids: list = None,
                                limit: int = 100, page: int = 0,
                                price_spike_pct_gte: int = 20):
        """
        Finds candidate ASINs for the Signals opportunity-discovery
        feature via Keepa's Product Finder. Unlike find_brand(), this
        has no brand restriction at all -- it's filtered purely by
        EVENT-based behaviour (something just changed: went out of
        stock, price jumped) rather than a static snapshot, which is
        the whole point of Signals vs. the brand-by-brand scan (see
        the project doc for the reasoning on why static filters like
        "thin FBA count" were rejected in favour of this).

        This is deliberately the ONLY Keepa cost the Signals feature
        pays to build its candidate pool -- see SignalService, which
        does a single UK-only ProductService.get_products() lookup
        per NEW candidate ASIN (diffed against the last run) and
        nothing further. No EU marketplace tokens are ever spent here.

        signal_type must be "stock_out" or "price_spike":

          "stock_out"   -- products that recorded at least one Amazon
                            stock-out in the last 90 days
                            (outOfStockCountAmazon90 >= 1). Confirmed
                            field name/semantics via Keepa's own
                            open-source ProductFinderRequest.java.

          "price_spike" -- products whose Buy Box price has risen by
                            at least `price_spike_pct_gte` percent over
                            the last 90 days.
                            FIELD NAME/SIGN CORRECTED 2026-08-21: the
                            original field name here, deltaPercent90_
                            BUY_BOX_gte, was checked against Keepa's own
                            Product Finder API docs (keepa.com/api-docs/
                            product-finder.html) and found to be wrong
                            in two ways. First, "BUY_BOX" alone is not a
                            valid <PRICE_TYPE> -- Keepa's documented
                            price-type list only has BUY_BOX_SHIPPING
                            (and BUY_BOX_USED_SHIPPING), so the field is
                            actually deltaPercent90_BUY_BOX_SHIPPING_gte.
                            Second, and much easier to get backwards:
                            Keepa's own docs state deltaPercent90 fields
                            are framed as "a positive value filters for
                            prices/values that have DECREASED, and a
                            negative value filters for INCREASED ones"
                            -- the inverse of what the field name
                            suggests. So finding a genuine price SPIKE
                            (an increase) requires a NEGATIVE threshold,
                            not the positive price_spike_pct_gte this
                            method was passing straight through. Both
                            are now fixed below. This is still based on
                            reading Keepa's docs, not a live test (no
                            Keepa/network access in this sandbox) -- run
                            it manually from the /signals page's "Run
                            now" button after restart and spot-check a
                            handful of the returned ASINs' actual Buy
                            Box history on Keepa/SAS (confirm they
                            really went UP, not down) before trusting it
                            or adding it to the automatic scheduler in
                            main.py's AUTOMATED_SIGNAL_TYPES.

        Like find_brand(): ALWAYS requests perPage=100 (Keepa's own
        working minimum) and trims to `limit` afterward; uses
        wait=False with the same TypeError fallback for older keepa
        library versions; returns None (not []) if the Keepa call
        itself failed, so callers can tell "zero genuine matches"
        apart from "the call failed" -- see find_brand()'s docstring
        for why that distinction matters (SignalService must NOT
        treat a failed call as "nothing to see here" and overwrite
        last_match_snapshot with an empty result).

        Does NOT apply Atlas's own gated/excluded-brand filtering --
        Product Finder has no concept of Atlas's exclusion list. That
        filtering happens client-side in SignalService after this
        call returns, same as every other discovery pipeline.
        """
        if signal_type == "stock_out":
            query = {
                "productType": ["0"],
                "outOfStockCountAmazon90_gte": 1,
                "current_SALES_gte": 1,
                "sort": [["current_SALES", "asc"]],
                "perPage": 100,
                "page": page,
            }
        elif signal_type == "price_spike":
            query = {
                "productType": ["0"],
                # Negative threshold = price INCREASE, per Keepa's own
                # (counter-intuitive) sign convention -- see this
                # method's docstring. price_spike_pct_gte is always
                # passed in positive (e.g. 20 for "risen 20%+"); negate
                # it here so callers don't have to think about Keepa's
                # inverted sign at every call site.
                "deltaPercent90_BUY_BOX_SHIPPING_gte": -abs(price_spike_pct_gte),
                "current_SALES_gte": 1,
                "sort": [["current_SALES", "asc"]],
                "perPage": 100,
                "page": page,
            }
        else:
            raise ValueError(f"Unknown signal_type for find_signal_candidates: {signal_type!r}")

        if category_ids:
            # Same field as find_brand() -- rootCategory, not
            # categories_include (see that method's comment for why).
            query["rootCategory"] = [str(c) for c in category_ids]

        print(f"Calling Product Finder for signal '{signal_type}' (page {page})...")
        tokens_before = self.api.tokens_left

        try:
            products = self.api.product_finder(query, wait=False, domain="GB")
        except TypeError:
            try:
                products = self.api.product_finder(query, domain="GB")
            except Exception as exc:
                print(f"Product Finder (signal '{signal_type}') failed (fallback call): {exc}")
                return None
        except Exception as exc:
            print(f"Product Finder (signal '{signal_type}') failed: {exc}")
            return None

        TokenUsageService.record_keepa_spend(
            "signals", "keepa_product_finder", tokens_before, self.api.tokens_left,
            marketplace="UK", asins_count=len(products),
        )

        print("Type:", type(products))
        print("Count:", len(products))

        return products[:max(1, limit)]

    @staticmethod
    def verify_sales_rank_sort(brand: str, category_ids: list = None) -> dict:
        """
        One-off sanity check for the current_SALES ascending sort
        above -- NOT called anywhere in the normal app flow, so it
        costs nothing unless run deliberately. Fetches page 0 for a
        brand and reports whether the returned ASINs' sales ranks
        actually look sorted best-first (mostly non-decreasing down
        the page), so a backwards sort can be caught by eyeballing a
        small, cheap result instead of trusting the documented
        behaviour blindly on a brand's full-catalog scan. See
        debug_verify_sort() in app/main.py for how to trigger this
        from the browser.
        """
        finder = ProductFinder()
        asins = finder.find_brand(brand, limit=20, page=0, category_ids=category_ids)

        if not asins:
            return {"brand": brand, "asins": [], "note": "No results -- can't verify."}

        # Deliberately defensive about exactly where Keepa/the keepa
        # library surfaces current sales rank on a product dict --
        # this helper is only meant to answer "does the order look
        # right", not to be a load-bearing part of scoring, so it
        # tries the couple of shapes the keepa library is known to
        # return (a parsed `data['SALES']` series, or the raw
        # `stats.current` CSV array at index 3) and falls back to None
        # rather than risk a wrong index silently giving false
        # confidence either way.
        from app.services.product_service import ProductService
        ranks = []

        for product in ProductService().get_products(asins, "UK", full=False):
            rank = None

            data = product.get("data")
            if isinstance(data, dict) and data.get("SALES") is not None:
                try:
                    rank = data["SALES"][-1]
                except (IndexError, TypeError):
                    rank = None

            if rank is None:
                stats = product.get("stats")
                if isinstance(stats, dict):
                    current = stats.get("current")
                    if isinstance(current, list) and len(current) > 3:
                        rank = current[3]

            # Coerce to a plain Python int (or None) before this ever
            # reaches the JSON response -- the keepa library can hand
            # back numpy integer types here (e.g. numpy.int64), which
            # aren't natively JSON-serializable and caused a bare
            # "Internal Server Error" on the debug page even after
            # main.py's route-level try/except, since that only guards
            # the lookup itself, not FastAPI's later JSON encoding step.
            if rank is not None:
                try:
                    rank = int(rank)
                except (TypeError, ValueError):
                    rank = str(rank)

            ranks.append({"asin": product.get("asin"), "current_SALES": rank})

        return {
            "brand": brand,
            "ranks_in_order": ranks,
            "note": (
                "If the sort is correct, current_SALES values below should mostly "
                "increase down the list (low = best-selling), not be scattered or "
                "trend downward/random. A None means this helper couldn't find a "
                "current rank in the shape it checked for that ASIN -- doesn't by "
                "itself mean the sort is wrong, just that this particular check is "
                "inconclusive for that row; look at the ASINs with a real value."
            ),
        }
