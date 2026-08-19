from dataclasses import asdict

from app.services.product_finder import ProductFinder
from app.services.product_service import ProductService
from app.services.product_mapper import ProductMapper
from app.services.fee_engine import FeeEngine
from app.services.opportunity_engine import OpportunityEngine
from app.services.product_repository import ProductRepository
from app.services.sourcing_classifier import SourcingClassifier
from app.config.exclusions import is_excluded, is_excluded_by_name, is_gated
from app.services.category_survey_service import get_category_names
from app.config.fees import DEFAULT_REFERRAL_RATE, REFERRAL_RATE_BY_CATEGORY_NAME
from app.services.keepa_priority import KeepaPriority
from app.services.discord_notifier import DiscordNotifier

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

    def _current_tokens(self):
        """
        Returns the live Keepa token balance -- refreshing it first if
        the cached value looks at/below the safety buffer.

        tokens_left only updates when a real Keepa call succeeds and
        parses the response, but a gate check that returns early
        BECAUSE the balance looks too low is exactly what stops any
        such call from ever happening again. Without this refresh, a
        cached value that dips to the buffer once would look
        permanently stuck there forever, even hours after the real
        account balance has actually recovered -- update_status() is a
        free balance check (spends no tokens) either way, so this
        costs nothing even when the answer is still "still too low".
        """
        tokens = self.product_service.api.tokens_left

        if tokens is not None and tokens <= MIN_TOKEN_BUFFER:
            try:
                self.product_service.api.update_status()
                tokens = self.product_service.api.tokens_left
            except Exception as exc:
                print(f"Token status refresh failed: {exc}")

        return tokens

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

            # Priority-yield: a manual verdict check or queued lead analysis
            # (see KeepaPriority) always wins the Keepa token budget over
            # this bulk A2A pipeline -- stop here exactly like a low-token
            # pause so ScanQueueService retries this page next tick instead
            # of skipping ahead. Checked every chunk (<=10 ASINs), not just
            # once per call, so a scan yields within one more Keepa request.
            if KeepaPriority.has_pending():
                ran_out = True
                break

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

    def scan(self, brand: str, limit: int = 20, force_rescan: bool = False,
             asins: list = None, category_ids: list = None, page: int = 0,
             include_no_eu_source: bool = False):
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

        Pass `category_ids` (a list of Keepa root category IDs) to
        restrict the brand finder to specific categories -- ignored
        entirely if `asins` is also provided. See the /categories
        page or CategorySurveyService to find real category IDs and
        names for a brand.

        `page` selects which 100-result Product Finder page to fetch
        (see ProductFinder.find_brand) -- ignored if `asins` is
        provided. This is what lets a caller (e.g. ScanQueueService)
        walk deeper into a brand's catalog across repeated calls
        instead of only ever seeing the same first ~100 results.

        include_no_eu_source: by default a product with no A2A source
        found in any of the 4 EU marketplaces is dropped entirely
        (Steps 1-4 already spent tokens confirming it clears the
        dead-listing/ceiling/exclusion bars, but with no EU cost there's
        no A2A margin to score). Pass True to keep these instead --
        they still get a ProductRecord (profit/roi come out 0, since
        FeeEngine has no cost to subtract) and a SourcingClassifier tag
        ("OA / unclear" in practice, since there's no EU spread to
        match). Used by SellerWatchService so a competitor's UK-only
        listing still surfaces with real title/price/rank data as a
        manual-OA-research lead, instead of vanishing into a bare-ASIN
        detection -- other callers (Discovery/Watchlist/Scan Queue)
        leave this off, since flooding those with zero-ROI IGNORE
        entries would just dilute real A2A opportunities.
        """

        # Normalized once here (not just inside find_brand's own Keepa
        # query) so the PERSISTED brand_query label is consistent too --
        # confirmed via direct inspection of real scan history that
        # "PLAYMOBIL", "playmobil", and "playmobil " (trailing space)
        # were being stored as three different labels for what's
        # obviously the same brand, cluttering the Products page brand
        # filter and (before the cooldown was made global) even
        # defeating same-brand cooldown matching.
        brand = brand.strip().lower()

        raw_page_count = None

        # Step 1 - Find ASINs (or use the explicit list if provided).
        # Always requests the FULL page (limit=100) here, regardless of
        # the caller's `limit` -- the final `asins[:limit]` trim below
        # happens AFTER exclusion/cooldown filtering, so a filtered-out
        # ASIN gets backfilled from later in the same page instead of
        # just shrinking the batch. raw_page_count (the untrimmed page
        # size) tells a pagination-aware caller whether this was the
        # last page (fewer than 100 = nothing more beyond it).
        if asins is not None:
            asins = list(asins)
        else:
            # Check the token buffer BEFORE calling find_brand, not just
            # before the UK fetch below -- otherwise a near-empty balance
            # still burns a Product Finder call and comes back with
            # raw_page_count=0, which is indistinguishable from a
            # genuinely-exhausted catalog (see ScanQueueService, which
            # relies on that distinction to know whether to stop
            # advancing through a brand's pages or retry the same page
            # once tokens refill).
            tokens_before_search = self._current_tokens()

            if tokens_before_search is not None and tokens_before_search <= MIN_TOKEN_BUFFER:
                return {
                    "brand": brand, "count": 0, "opportunities": [],
                    "skipped_excluded": 0, "skipped_user_excluded": 0,
                    "skipped_known_excluded": 0, "skipped_recently_scanned": 0,
                    "skipped_unprofitable_ceiling": 0, "skipped_dead_listing": 0,
                    "marketplaces_skipped_low_tokens": [], "marketplaces_partial_low_tokens": {},
                    "tokens_remaining": tokens_before_search,
                    "asins_scanned": 0, "raw_page_count": None, "uk_ran_out": False,
                    "error": (
                        f"Only {tokens_before_search} Keepa tokens left -- not enough "
                        f"to safely start a scan. Wait for your account to refill."
                    ),
                }

            # Gated-brand pre-token block -- a brand-search-driven scan
            # (this branch only; an explicit `asins` list from
            # Competitor Watch/an upload/Replen/a Scan Queue rescan of
            # already-tracked ASINs still goes through normally, since
            # those aren't Atlas deliberately hunting for MORE of a
            # brand it can't sell) never spends a single Keepa token on
            # a whole-brand gate. Category-scoped gates can't be
            # checked yet (no category known until the UK lookup), so
            # those are only caught later, at Step 3.
            gated_brand_names = ProductRepository.get_whole_gated_brand_names()

            if brand in gated_brand_names:
                return {
                    "brand": brand, "count": 0, "opportunities": [],
                    "skipped_excluded": 0, "skipped_user_excluded": 0,
                    "skipped_known_excluded": 0, "skipped_recently_scanned": 0,
                    "skipped_unprofitable_ceiling": 0, "skipped_dead_listing": 0,
                    "marketplaces_skipped_low_tokens": [], "marketplaces_partial_low_tokens": {},
                    "tokens_remaining": tokens_before_search,
                    "asins_scanned": 0, "raw_page_count": None, "uk_ran_out": False,
                    "gated_brand_skip": True,
                    "error": (
                        f"'{brand}' is on the gated brands list -- skipping brand-search "
                        f"scanning to avoid spending tokens hunting for more of a brand "
                        f"you can't currently sell. Remove it from Gated Brands on the "
                        f"Exclusions page if that changes."
                    ),
                }

            asins = self.finder.find_brand(brand, limit=100, page=page, category_ids=category_ids)

            if asins is None:
                # The Product Finder call itself failed (network/API
                # error, NOT a genuine "zero matches" -- see
                # ProductFinder.find_brand's docstring). Must NOT be
                # treated as raw_page_count=0, or ScanQueueService.
                # run_next_tick would mark this brand "done, exhausted"
                # off a single transient failure. Same error-shaped
                # response as the token-buffer guard above -- caller
                # leaves the item untouched and just retries next tick.
                return {
                    "brand": brand, "count": 0, "opportunities": [],
                    "skipped_excluded": 0, "skipped_user_excluded": 0,
                    "skipped_known_excluded": 0, "skipped_recently_scanned": 0,
                    "skipped_unprofitable_ceiling": 0, "skipped_dead_listing": 0,
                    "marketplaces_skipped_low_tokens": [], "marketplaces_partial_low_tokens": {},
                    "tokens_remaining": self._current_tokens(),
                    "asins_scanned": 0, "raw_page_count": None, "uk_ran_out": False,
                    "error": "Product Finder request failed -- will retry.",
                }

            raw_page_count = len(asins)

        # Step 1a - Skip user-excluded ASINs (marked via the Exclude
        # button on the results page) BEFORE spending any tokens --
        # the cheapest possible check, just a set membership test.
        user_excluded_asins = ProductRepository.get_excluded_asins()
        skipped_user_excluded = 0

        if user_excluded_asins:
            before_count = len(asins)
            asins = [a for a in asins if a not in user_excluded_asins]
            skipped_user_excluded = before_count - len(asins)

        # User-editable category exclusions (Exclusions page) --
        # fetched once here, reused below for both the known_products
        # name-based check and Step 3's live category-ID check, rather
        # than a DB query per ASIN. See ExcludedCategory/is_excluded's
        # docstrings for why ID and name are two separate sets.
        excluded_category_ids = ProductRepository.get_excluded_category_ids()
        excluded_category_names = ProductRepository.get_excluded_category_names()

        # Gated brands (DB-backed GatedBrand rows, plus the static
        # GATED_BRAND_CATEGORIES set -- see is_gated()) -- fetched once
        # here, same convention as the exclusion sets above, and reused
        # in Step 3 below to TAG (not drop) an incidentally-found ASIN.
        gated_brand_pairs = ProductRepository.get_gated_brand_pairs()

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

            if known and is_excluded_by_name(
                asin, known.brand, known.category_root, excluded_category_names
            ):
                skipped_known_excluded += 1
                continue

            remaining_asins.append(asin)

        asins = remaining_asins

        skipped_recently_scanned = 0

        if not force_rescan:
            recently_scanned = ProductRepository.get_recently_scanned_asins(
                RESCAN_COOLDOWN_HOURS
            )
            before_count = len(asins)
            asins = [a for a in asins if a not in recently_scanned]
            skipped_recently_scanned = before_count - len(asins)

        asins = asins[:limit]

        empty_response = {
            "brand": brand, "count": 0, "opportunities": [],
            "skipped_excluded": 0,
            "skipped_user_excluded": skipped_user_excluded,
            "skipped_known_excluded": skipped_known_excluded,
            "skipped_recently_scanned": skipped_recently_scanned,
            "skipped_unprofitable_ceiling": 0,
            "skipped_dead_listing": 0,
            "marketplaces_skipped_low_tokens": [],
            "marketplaces_partial_low_tokens": {},
            "tokens_remaining": None,
            "asins_scanned": 0,
            "raw_page_count": raw_page_count,
            "uk_ran_out": False,
        }

        if not asins:
            return empty_response

        tokens_before_uk = self._current_tokens()

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

        # Keepa root category ID -> human-readable Amazon category name,
        # used below (and in Step 5) to look up a category-specific
        # referral rate instead of assuming a flat one -- see
        # app/config/fees.py for why that matters. Cached across the
        # whole process, so this is a no-op after the first call.
        category_names = get_category_names(self.product_service.api)

        # Step 3 - Filter out excluded categories/ASINs/gated brands
        # BEFORE spending tokens on the 4 EU marketplaces.
        included_uk_products = []
        skipped_excluded = 0

        # ASINs found incidentally (not via a brand-search this scan
        # already refused to run -- see Step 1) that belong to a gated
        # brand. NOT dropped like a real exclusion -- carried through
        # the normal pipeline so they still get fully priced/scored,
        # and Step 5 sets product.gated=True on each before scoring so
        # OpportunityEngine tags them "GATED" instead of BUY/CONSIDER/
        # IGNORE. See GatedBrand's docstring for why.
        gated_asins = set()

        # Everything Step 3/3b/5 filters out below, paired with WHY --
        # only collected when include_no_eu_source (Competitors), since
        # that's the one caller that wants a bare Keepa-fetched ASIN to
        # still show real title/price/rank instead of vanishing. The
        # UK fetch above already paid the tokens for this data
        # regardless of what happens next -- keeping it here costs
        # nothing extra, it's purely "don't throw away what we already
        # have". See the mapping loop after Step 5 for how these become
        # lightweight, unscored ProductRecords (recommendation=IGNORE,
        # never auto-watched, never counted in `count`).
        filtered_uk_products = []

        for uk_product in uk_products:
            asin = uk_product.get("asin") or ""
            brand_name = uk_product.get("brand") or ""

            # Every category ID this product belongs to, root through
            # leaf -- categoryTree walks the full ancestor chain (with
            # names, though we only need the IDs here), categories is
            # Keepa's flat list of every node it's directly listed in
            # (can include ones outside the primary tree), and
            # rootCategory is included too in case a product somehow
            # has neither of the others populated. Checking the whole
            # set (not just rootCategory) is what lets EXCLUDED_CATEGORIES
            # target a narrow subcategory without also excluding
            # everything else under its parent.
            category_ids = {
                str(node.get("catId"))
                for node in (uk_product.get("categoryTree") or [])
                if node.get("catId") is not None
            }
            category_ids.update(str(cid) for cid in (uk_product.get("categories") or []))

            root_category = str(uk_product.get("rootCategory") or "")
            if root_category:
                category_ids.add(root_category)

            if is_excluded(asin, brand_name, category_ids, excluded_category_ids):
                skipped_excluded += 1
                if include_no_eu_source:
                    filtered_uk_products.append((uk_product, "excluded_category"))
                continue

            if is_gated(brand_name, category_ids, gated_brand_pairs):
                gated_asins.add(asin)

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
                if include_no_eu_source:
                    filtered_uk_products.append((uk_product, "dead_listing"))
                continue

            fba_fee = quick_product.fba_fee if quick_product.fba_fee else FeeEngine.DEFAULT_FBA_FEE

            # Use whichever price is higher -- today's or the 90-day
            # typical. A temporary Amazon-driven discount on today's
            # price shouldn't disqualify a product that's normally
            # profitable at its typical price, before we've even
            # checked what it costs to source.
            best_price_for_ceiling = max(quick_product.buy_box_now, quick_product.buy_box_90d)
            ceiling_referral_rate = REFERRAL_RATE_BY_CATEGORY_NAME.get(
                category_names.get(quick_product.category, "").lower(), DEFAULT_REFERRAL_RATE
            )
            referral_fee = best_price_for_ceiling * ceiling_referral_rate
            ceiling_profit = best_price_for_ceiling - fba_fee - referral_fee

            if ceiling_profit <= 0:
                skipped_unprofitable_ceiling += 1
                if include_no_eu_source:
                    filtered_uk_products.append((uk_product, "unprofitable_ceiling"))

                # Persist so the Signals page's "ceiling_recheck" can
                # periodically ask "has this graduated since we last
                # said no" using a price a normal scan already paid
                # tokens for -- see CeilingRejected/SignalService.
                # This runs for EVERY caller of scan() (Discovery, Scan
                # Queue, Watchlist, Replen, Competitor Watch, an
                # uploaded list), costing nothing extra.
                ProductRepository.upsert_ceiling_rejected(
                    asin=asin,
                    title=quick_product.title,
                    brand=quick_product.brand,
                    category=quick_product.category,
                    category_name=category_names.get(quick_product.category, ""),
                    fba_fee=fba_fee,
                    buy_box_at_reject=best_price_for_ceiling,
                )
                continue

            # Cleared the ceiling this time -- if it was previously
            # sitting in the ceiling-rejected pool, it's graduated, so
            # drop it there. Harmless no-op if it was never in the
            # pool (the common case).
            ProductRepository.remove_ceiling_rejected(asin)

            ceiling_checked_products.append(uk_product)

        included_uk_products = ceiling_checked_products
        included_asins = [p.get("asin") for p in included_uk_products]

        # Step 4 - Pull EU data one marketplace at a time, in chunks,
        # carrying the measured cost estimate forward each time.
        eu_lookups = {}
        marketplaces_skipped_low_tokens = []
        marketplaces_partial_low_tokens = {}

        # EU calls now request full=True too (stats=90, not just
        # current price) -- CONFIRMED via direct token measurement that
        # this costs the SAME as full=False on this account (the charge
        # comes from history=True/buybox=True, stats is free on top).
        # Needed so ProductMapper can read each marketplace's 90-day low
        # for WatchlistService's EU-dip auto-watch check. Cost estimate
        # is still reset and measured fresh here rather than inheriting
        # UK's -- real per-request cost can still differ by marketplace
        # (offer volume, price history depth, etc.), so this stays a
        # genuine fresh measurement, not just a leftover from when the
        # shapes differed.
        eu_cost_estimate = None

        for marketplace in EU_MARKETPLACES:
            if not included_asins:
                eu_lookups[marketplace] = {}
                continue

            products, fetched, ran_out, eu_cost_estimate = self._fetch_in_chunks(
                included_asins, marketplace, eu_cost_estimate, full=True
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
            product.gated = asin in gated_asins

            if not product.buy_box_now:
                if include_no_eu_source:
                    filtered_uk_products.append((uk_product, "no_current_price"))
                continue

            if not product.best_source_marketplace and not include_no_eu_source:
                continue

            category_name = category_names.get(product.category, "")
            fees = FeeEngine.calculate(product, category_name=category_name)
            product.fba_fee = fees.fba_fee
            product.referral_fee = fees.referral_fee
            product.profit = fees.profit
            product.roi = fees.roi
            product.margin = fees.margin
            product.profit_90d = fees.profit_90d
            product.roi_90d = fees.roi_90d
            product.margin_90d = fees.margin_90d
            product.profit_peak = fees.profit_peak
            product.roi_peak = fees.roi_peak
            product.margin_peak = fees.margin_peak
            product.category_name = category_name
            product.referral_rate_used = fees.referral_rate_used
            product.uk_vat_rate_used = fees.uk_vat_rate_used
            product.eu_vat_rate_used = fees.eu_vat_rate_used

            # Recent-window (last SourcingClassifier.RECENT_WINDOW_DAYS
            # days) EU-margin / UK-dip evidence for SourcingClassifier --
            # computed here, not inside ProductMapper, since this is the
            # one place both the raw UK/EU Keepa dicts (uk_product,
            # eu_products -- needed for day-by-day price reconstruction)
            # and the resolved category/fee context (category_name,
            # product.fba_fee, product.eu_vat_rate_used, just set above)
            # are in scope together. No extra Keepa tokens -- pure
            # Python over data already fetched this scan. See
            # SourcingClassifier.compute_recent_evidence and Product's
            # own field docstrings for what each value means.
            recent_evidence = SourcingClassifier.compute_recent_evidence(
                uk_product, eu_products.get(product.best_source_marketplace),
                product, category_name,
            )
            for field_name, value in recent_evidence.items():
                setattr(product, field_name, value)

            # Local import -- WatchlistService imports BrandScanService
            # itself (for its weekly check_stale rescan), so a
            # module-level import here would be circular.
            #
            # Never auto-watch a gated product -- the Watchlist is for
            # things worth actively re-checking to buy, and a gated
            # brand isn't sourceable right now regardless of how the
            # numbers look. It's still saved/scored below (Gated Brand
            # Opportunities needs that), just not added here.
            if not product.gated:
                from app.services.watchlist_service import WatchlistService
                WatchlistService.maybe_auto_watch(product, category_name)

            report = OpportunityEngine.analyse(product)

            product_dict = asdict(product)
            report_dict = asdict(report)

            ProductRepository.save_opportunity(product_dict, report_dict, brand)

            # Discord ping -- same "worth a look" bar the Review Queue
            # itself uses (is_notable), so a good find gets surfaced
            # immediately instead of waiting for the next time the
            # Review Queue happens to be checked. No-ops quietly if
            # DISCORD_WEBHOOK_URL isn't configured -- see
            # DiscordNotifier's docstring.
            if ProductRepository.is_notable(
                report_dict.get("recommendation") or "",
                product_dict.get("monthly_sales") or 0,
                product_dict.get("roi") or 0,
                product_dict.get("roi_90d") or 0,
                product_dict.get("sales_drops_30d") or 0,
            ):
                DiscordNotifier.notify_opportunity(product_dict, report_dict, source_label=brand)

            opportunities.append({
                "product": product_dict,
                "report": report_dict,
            })

        # Step 5b - Competitors only (include_no_eu_source): map and
        # save a LIGHTWEIGHT record for everything Step 3/3b/5 just
        # filtered out, using the UK data already fetched above rather
        # than re-spending tokens. No EU lookup, no FeeEngine/trend/
        # scoring pass (nothing to score -- these were dropped BEFORE
        # profitability was ever assessed against a real EU cost), no
        # WatchlistService.maybe_auto_watch call. Still appended to
        # `opportunities` (score 0, so it naturally sorts last) since
        # that's what SellerWatchService reads to link a ProductRecord
        # and run SourcingClassifier against -- the whole point is
        # giving the Competitors page a real title/price/rank instead
        # of a bare ASIN, with filtered_reason explaining why it's not
        # a live opportunity. With no EU source data, SourcingClassifier
        # will tag these "OA / unclear", which is an honest label here.
        # Never runs for other callers (Discovery/Watchlist/Replen/Scan
        # Queue all leave include_no_eu_source at its False default),
        # so this can't dilute those pages with zero-score noise.
        for uk_product, reason in filtered_uk_products:
            product = ProductMapper.from_keepa(uk_product)
            product.category_name = category_names.get(product.category, "")

            product_dict = asdict(product)
            report_dict = {
                "asin": product.asin,
                "title": product.title,
                "brand": product.brand,
                "score": 0,
                "confidence": 0,
                "recommendation": "IGNORE",
                "trend": {},
                "score_breakdown": [],
                "confidence_breakdown": [],
                "filtered_reason": reason,
            }

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
            "skipped_user_excluded": skipped_user_excluded,
            "skipped_known_excluded": skipped_known_excluded,
            "skipped_recently_scanned": skipped_recently_scanned,
            "skipped_unprofitable_ceiling": skipped_unprofitable_ceiling,
            "skipped_dead_listing": skipped_dead_listing,
            "marketplaces_skipped_low_tokens": marketplaces_skipped_low_tokens,
            "marketplaces_partial_low_tokens": marketplaces_partial_low_tokens,
            "tokens_remaining": self.product_service.api.tokens_left,
            "opportunities": opportunities,
            # Real count of ASINs actually looked up this call (i.e. UK
            # tokens genuinely spent on them) -- distinct from `count`
            # (final opportunities, after profitability/dead-listing
            # filtering). This is what ScanQueueService tracks progress
            # against, since it matches token spend and the user's own
            # mental model of "products scanned" regardless of whether
            # they turned out to be good opportunities.
            "asins_scanned": len(fetched_uk_asins),
            "raw_page_count": raw_page_count,
            # True if the UK fetch stopped partway through this page due
            # to low tokens -- ScanQueueService uses this to avoid
            # advancing to the next page (which would permanently skip
            # whatever was left unscanned on this one) and to avoid
            # mistaking a token-starved partial page for a genuinely
            # exhausted catalog.
            "uk_ran_out": uk_ran_out,
        }