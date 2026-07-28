from dataclasses import asdict

from app.services.product_finder import ProductFinder
from app.services.product_service import ProductService
from app.services.product_mapper import ProductMapper
from app.services.fee_engine import FeeEngine
from app.services.opportunity_engine import OpportunityEngine
from app.services.product_repository import ProductRepository
from app.config.exclusions import is_excluded, is_excluded_by_name

EU_MARKETPLACES = ["DE", "FR", "ES", "IT"]

# Safety margin -- don't spend right down to 0, leave headroom for
# whatever else might use the account (e.g. another tab open).
MIN_TOKEN_BUFFER = 5

# How many ASINs to request per Keepa call. Keepa allows a single
# request to push your token balance NEGATIVE if it doesn't have
# enough for the whole batch, rather than refusing it upfront -- so
# a large batch can overshoot badly. Chunking bounds the overshoot to
# roughly one chunk's worth of tokens instead of the whole scan.
CHUNK_SIZE = 10

# How long a scanned ASIN is considered "fresh enough" to skip
# re-scanning by default. This is what stops repeated scans of the
# same brand from re-spending tokens on the exact same top-ranked
# products every time -- once they're on cooldown, the next scan
# naturally falls through to the next tier down instead.
RESCAN_COOLDOWN_HOURS = 24


class BrandScanService:

    def __init__(self):
        self.finder = ProductFinder()
        self.product_service = ProductService()

    def _fetch_in_chunks(self, asins, marketplace, cost_estimate, full=True):
        """
        Fetches `asins` from `marketplace` in CHUNK_SIZE pieces,
        checking measured token budget before each chunk. Returns
        (products, fetched_asins, ran_out: bool, updated_cost_estimate).

        cost_estimate: current best guess at per-ASIN token cost for
        this call shape, or None if not yet known. Updated after each
        chunk from real measured spend, so later chunks (and later
        marketplaces) use an increasingly accurate figure.

        full=False requests a slimmed-down query (see
        ProductService.get_products) -- used for EU marketplaces,
        where only the current price is ever read.
        """
        products = []
        fetched_asins = []
        ran_out = False

        for i in range(0, len(asins), CHUNK_SIZE):
            chunk = asins[i:i + CHUNK_SIZE]

            tokens_now = self.product_service.api.tokens_left

            if tokens_now is not None:
                if cost_estimate is not None:
                    estimated_cost = cost_estimate * len(chunk)
                    if tokens_now < estimated_cost + MIN_TOKEN_BUFFER:
                        ran_out = True
                        break
                elif tokens_now <= MIN_TOKEN_BUFFER:
                    # No cost estimate yet, but already critically low --
                    # don't risk even the first (measuring) chunk.
                    ran_out = True
                    break

            tokens_before = self.product_service.api.tokens_left
            chunk_products = self.product_service.get_products(chunk, marketplace, full=full)
            tokens_after = self.product_service.api.tokens_left

            products.extend(chunk_products)
            fetched_asins.extend(chunk)

            if tokens_before is not None and tokens_after is not None and chunk:
                measured = max(tokens_before - tokens_after, 0) / len(chunk)
                cost_estimate = measured

        return products, fetched_asins, ran_out, cost_estimate

    def scan(self, brand: str, limit: int = 20, force_rescan: bool = False, asins: list = None):
        """
        Full A2A pipeline for a brand, with chunked token-budget
        awareness so a scan never silently hangs OR blows through its
        token budget in one oversized request. Measures real per-ASIN
        cost as it goes and checks it before every chunk, not just
        before every marketplace.

        By default, ASINs already scanned for this brand within
        RESCAN_COOLDOWN_HOURS are skipped BEFORE any tokens are spent
        -- this is what stops repeated scans from re-paying for the
        same top-ranked products, and lets later scans naturally
        reach further into the catalog instead. Pass force_rescan=True
        to check everything regardless of when it was last scanned.

        Pass an explicit `asins` list (e.g. from an uploaded file) to
        scan exactly those ASINs instead of using Keepa's brand
        finder. `brand` is still used as a label for the
        recently-scanned cooldown and saved records either way.
        """

        # Step 1 - Find ASINs (or use the explicit list if provided)
        if asins is not None:
            asins = list(asins)
        else:
            asins = self.finder.find_brand(brand, limit=limit)

        # Step 1b - Check imported static catalog data (known_products)
        # for category/brand exclusions BEFORE spending ANY tokens --
        # not just before the EU calls like the exclusions.py check
        # further down, which needs a live UK lookup first to learn
        # category. This only helps for ASINs that have been imported
        # via import_known_products.py; anything not imported still
        # needs the UK lookup to learn its category.
        known_products = ProductRepository.get_known_products(asins)
        skipped_known_excluded = 0
        remaining_asins = []

        for asin in asins:
            known = known_products.get(asin)

            if known and is_excluded_by_name(asin, known.brand, known.category_root):
                skipped_known_excluded += 1
                continue

            remaining_asins.append(asin)

        asins = remaining_asins

        skipped_recently_scanned = 0

        if not force_rescan:
            recently_scanned = ProductRepository.get_recently_scanned_asins(
                brand, RESCAN_COOLDOWN_HOURS
            )
            before_count = len(asins)
            asins = [a for a in asins if a not in recently_scanned]
            skipped_recently_scanned = before_count - len(asins)

        asins = asins[:limit]

        empty_response = {
            "brand": brand, "count": 0, "opportunities": [],
            "skipped_excluded": 0,
            "skipped_known_excluded": skipped_known_excluded,
            "skipped_recently_scanned": skipped_recently_scanned,
            "skipped_unprofitable_ceiling": 0,
            "skipped_dead_listing": 0,
            "marketplaces_skipped_low_tokens": [],
            "marketplaces_partial_low_tokens": {},
            "tokens_remaining": None,
        }

        if not asins:
            return empty_response

        tokens_before_uk = self.product_service.api.tokens_left

        if tokens_before_uk is not None and tokens_before_uk <= MIN_TOKEN_BUFFER:
            return {
                **empty_response,
                "tokens_remaining": tokens_before_uk,
                "error": (
                    f"Only {tokens_before_uk} Keepa tokens left -- not enough "
                    f"to safely start a scan. Wait for your account to refill."
                ),
            }

        # Step 2 - Load UK data first, in chunks (this is the only
        # lookup we pay for regardless of exclusions -- we need it to
        # know category). No cost estimate yet on the first call.
        uk_products, fetched_uk_asins, uk_ran_out, cost_estimate = self._fetch_in_chunks(
            asins, "UK", cost_estimate=None
        )

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

        # Step 3b - Dead-listing and ceiling checks using UK data alone
        # (no extra tokens -- pure computation on data we already
        # have). Skip the EU lookups entirely for these.
        ceiling_checked_products = []
        skipped_unprofitable_ceiling = 0
        skipped_dead_listing = 0

        for uk_product in included_uk_products:
            quick_product = ProductMapper.from_keepa(uk_product)

            # Dead listing: zero sales rank drops AND zero current
            # offers together mean this genuinely never sells and
            # nobody's even listing it right now. This is NOT the same
            # as a temporarily out-of-stock item that normally sells
            # well -- that would still show sales_drops_30d > 0 from
            # its sales history before going out of stock, and stays
            # in as a legitimate opportunity.
            if quick_product.sales_drops_30d == 0 and quick_product.offers_now == 0:
                skipped_dead_listing += 1
                continue

            fba_fee = quick_product.fba_fee if quick_product.fba_fee else FeeEngine.DEFAULT_FBA_FEE

            # Use whichever price is higher -- today's or the 90-day
            # typical. A temporary Amazon-driven discount on today's
            # price shouldn't disqualify a product that's normally
            # profitable at its typical price, before we've even
            # checked what it costs to source.
            best_price_for_ceiling = max(quick_product.buy_box_now, quick_product.buy_box_90d)
            referral_fee = best_price_for_ceiling * FeeEngine.DEFAULT_REFERRAL_RATE
            ceiling_profit = best_price_for_ceiling - fba_fee - referral_fee

            if ceiling_profit <= 0:
                skipped_unprofitable_ceiling += 1
                continue

            ceiling_checked_products.append(uk_product)

        included_uk_products = ceiling_checked_products
        included_asins = [p.get("asin") for p in included_uk_products]

        # Step 4 - Pull EU data one marketplace at a time, in chunks,
        # carrying the measured cost estimate forward each time.
        eu_lookups = {}
        marketplaces_skipped_low_tokens = []
        marketplaces_partial_low_tokens = {}

        # EU calls use a slimmed-down request (full=False) -- reset the
        # cost estimate so it's measured fresh for that cheaper shape,
        # rather than inheriting the UK full-request estimate (which
        # would overestimate EU cost and could cause an overly
        # cautious skip).
        eu_cost_estimate = None

        for marketplace in EU_MARKETPLACES:
            if not included_asins:
                eu_lookups[marketplace] = {}
                continue

            products, fetched, ran_out, eu_cost_estimate = self._fetch_in_chunks(
                included_asins, marketplace, eu_cost_estimate, full=False
            )

            eu_lookups[marketplace] = {p.get("asin"): p for p in products}

            if ran_out:
                if fetched:
                    marketplaces_partial_low_tokens[marketplace] = {
                        "fetched": len(fetched), "total": len(included_asins),
                    }
                else:
                    marketplaces_skipped_low_tokens.append(marketplace)

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
            product.profit_90d = fees.profit_90d
            product.roi_90d = fees.roi_90d

            report = OpportunityEngine.analyse(product)

            product_dict = asdict(product)
            report_dict = asdict(report)

            ProductRepository.save_opportunity(product_dict, report_dict, brand)

            opportunities.append({
                "product": product_dict,
                "report": report_dict,
            })

        # Step 6 - Rank best opportunities first
        opportunities.sort(key=lambda o: o["report"]["score"], reverse=True)

        return {
            "brand": brand,
            "count": len(opportunities),
            "skipped_excluded": skipped_excluded,
            "skipped_known_excluded": skipped_known_excluded,
            "skipped_recently_scanned": skipped_recently_scanned,
            "skipped_unprofitable_ceiling": skipped_unprofitable_ceiling,
            "skipped_dead_listing": skipped_dead_listing,
            "marketplaces_skipped_low_tokens": marketplaces_skipped_low_tokens,
            "marketplaces_partial_low_tokens": marketplaces_partial_low_tokens,
            "tokens_remaining": self.product_service.api.tokens_left,
            "opportunities": opportunities,
        }