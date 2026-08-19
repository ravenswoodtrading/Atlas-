import json

from app.services.product_finder import ProductFinder
from app.services.product_service import ProductService
from app.services.product_mapper import ProductMapper
from app.services.product_repository import ProductRepository
from app.services.fee_engine import FeeEngine
from app.services.category_survey_service import get_category_names
from app.config.exclusions import is_excluded, is_gated
from app.config.fees import DEFAULT_REFERRAL_RATE, REFERRAL_RATE_BY_CATEGORY_NAME
from app.services.activity_log import ActivityLog


class SignalService:
    """
    Runs a SignalQuery: finds NEW candidate ASINs matching an event-
    based opportunity pattern (a stock-out, a price spike, or an
    Atlas product that's finally cleared its own profitability
    ceiling) and turns them into lightweight, UNSCORED SignalMatch
    leads worth digging into manually on Keepa/SAS.

    DELIBERATELY MANUAL, not scheduled: run_check() is only ever
    called from the /signals page's "Run now" button (see
    app/routes/signals.py) for now. Two reasons this isn't on an
    automatic scheduler yet:

    1. price_spike relies on a best-guess Keepa Product Finder field
       (deltaPercent90_BUY_BOX_gte) that has not been confirmed live
       from this environment -- see ProductFinder.find_signal_
       candidates' docstring. Running it manually first and
       spot-checking the results against real Keepa/SAS price history
       is the way to build confidence before ever automating it.
    2. Every run here still costs real Keepa tokens (a Product Finder
       call plus one UK-only lookup per NEW candidate, or one UK-only
       lookup per ceiling_recheck candidate) -- deliberately small
       and bounded per click, but worth the user's own judgement on
       when to spend it rather than a background scheduler burning
       through the token budget unattended.

    COST MODEL (the whole point of this feature vs. a normal brand
    scan): stock_out/price_spike NEVER touch a single EU marketplace.
    Only ONE UK-only Keepa lookup is paid per genuinely NEW candidate
    ASIN (diffed against the last run via SignalQuery.
    last_match_snapshot). ceiling_recheck pays one UK-only lookup per
    previously-rejected ASIN it re-checks (bounded by
    CEILING_RECHECK_BATCH_SIZE) and no Product Finder call at all.
    Digging further into any specific match (real EU pricing, a full
    Verdict Check) is left to the user via Keepa/SAS directly, or by
    Watching the ASIN -- which is what triggers Atlas's own full
    5-marketplace pipeline, same as everywhere else in the app.
    """

    # Separate from FeeEngine.OA_TARGET_ROI_PCT (also 25% now, the
    # Competitors page's OA price guide -- previously 18%, coincidence
    # that they now match) -- Signals leads are cheaper/riskier (a
    # single UK-only lookup, no confirmed EU source yet), kept as its
    # own constant rather than merged with FeeEngine's so the two can
    # still diverge again later without one accidentally dragging the
    # other along.
    TARGET_ROI_PCT = 25.0

    # How far above its 90-day typical price today's Buy Box has to
    # sit before Atlas's OWN parsed stats agree a price_spike
    # candidate is real -- checked independently of whatever the
    # (unverified) Keepa Product Finder filter already claimed, same
    # "don't just trust the coarse filter" instinct as the existing
    # dead-listing check. A genuine match has to clear BOTH bars.
    PRICE_SPIKE_CONFIRM_PCT = 15.0

    # Bounds one ceiling_recheck "Run now" click's Keepa cost
    # regardless of how large the ceiling-rejected pool has grown --
    # oldest-checked-first (see ProductRepository.list_ceiling_
    # rejected), so the whole pool cycles through over repeated runs
    # rather than a handful of ASINs hogging every click forever.
    CEILING_RECHECK_BATCH_SIZE = 50

    def __init__(self):
        self.finder = ProductFinder()
        self.product_service = ProductService()

    def run_check(self, signal_query_id: int) -> dict:
        query = ProductRepository.get_signal_query(signal_query_id)

        if query is None:
            return {"error": f"No such signal query: {signal_query_id}"}

        if not query.enabled:
            return {"signal_query_id": query.id, "error": "This signal is disabled."}

        if query.signal_type == "ceiling_recheck":
            result = self._run_ceiling_recheck(query)
        elif query.signal_type in ("stock_out", "price_spike"):
            result = self._run_product_finder_signal(query)
        else:
            return {"signal_query_id": query.id, "error": f"Unknown signal_type: {query.signal_type}"}

        if not result.get("error"):
            ActivityLog.record(
                "signal_check",
                f"{query.name} ({query.signal_type}): {result.get('new_matches', 0)} new match(es)",
            )

        return result

    # ---- stock_out / price_spike (Product Finder-driven) ----

    def _run_product_finder_signal(self, query) -> dict:
        category_ids = [c for c in (query.category_ids or "").split(",") if c]

        candidate_asins = self.finder.find_signal_candidates(
            query.signal_type, category_ids=category_ids or None, limit=100,
        )

        if candidate_asins is None:
            # The Product Finder call itself failed -- must NOT touch
            # last_match_snapshot, or the next successful run would
            # wrongly treat everything as "new" (if we'd cleared it)
            # or silently drop real new candidates (if we'd written a
            # partial/empty list). Same "None vs [] must stay
            # distinguishable" contract as find_brand(). Leave the
            # snapshot untouched and just report the failure.
            return {
                "signal_query_id": query.id,
                "error": "Product Finder request failed -- try again.",
                "candidates_checked": 0, "new_matches": 0,
            }

        try:
            prev_asins = set(json.loads(query.last_match_snapshot or "[]"))
        except (ValueError, TypeError):
            prev_asins = set()

        new_asins = [a for a in candidate_asins if a not in prev_asins]

        result = self._score_and_save_candidates(query, new_asins)

        # Snapshot the FULL candidate list this run saw (not just the
        # newly-surfaced ones) -- next run's diff needs to know
        # everything already seen, whether or not it became a match
        # (e.g. it was gated/excluded/unconfirmed this time).
        ProductRepository.update_signal_query_snapshot(query.id, json.dumps(candidate_asins))

        result["signal_query_id"] = query.id
        result["raw_candidate_count"] = len(candidate_asins)
        return result

    def _score_and_save_candidates(self, query, asins: list) -> dict:
        if not asins:
            return {
                "candidates_checked": 0, "new_matches": 0,
                "skipped_gated": 0, "skipped_excluded": 0, "skipped_unconfirmed": 0,
            }

        keepa_products = self.product_service.get_products(asins, "UK", full=True, stats_days=90)
        category_names = get_category_names(self.finder.api)

        excluded_asins = ProductRepository.get_excluded_asins()
        excluded_category_ids = ProductRepository.get_excluded_category_ids()
        gated_brand_pairs = ProductRepository.get_gated_brand_pairs()

        skipped_gated = 0
        skipped_excluded = 0
        skipped_unconfirmed = 0
        new_matches = 0

        for k in keepa_products:
            asin = k.get("asin") or ""
            if not asin:
                continue

            brand_name = k.get("brand") or ""

            # Same category-tree membership check as BrandScanService
            # Step 3 -- root through leaf, so a narrow excluded
            # subcategory doesn't need its whole parent excluded too.
            category_ids = {
                str(node.get("catId"))
                for node in (k.get("categoryTree") or [])
                if node.get("catId") is not None
            }
            category_ids.update(str(cid) for cid in (k.get("categories") or []))
            root_category = str(k.get("rootCategory") or "")
            if root_category:
                category_ids.add(root_category)

            if asin in excluded_asins or is_excluded(asin, brand_name, category_ids, excluded_category_ids):
                skipped_excluded += 1
                continue

            # Unlike BrandScanService's incidental-find case, gated
            # brands are dropped outright here (not tagged) -- Signals
            # candidates never came from a brand-search Atlas chose to
            # scan, so there's nothing to track by keeping them; see
            # the /signals page's "N skipped: gated/excluded" line.
            if is_gated(brand_name, category_ids, gated_brand_pairs):
                skipped_gated += 1
                continue

            product = ProductMapper.from_keepa(k)

            # Client-side confirmation -- the Product Finder query
            # only narrows the CANDIDATE POOL cheaply; whether the
            # signal genuinely applies is re-checked here against
            # Atlas's own already-trusted, already-parsed stats, the
            # same "verify, don't just trust the coarse filter"
            # instinct as the existing dead-listing check.
            if query.signal_type == "stock_out":
                # No current price/offers at all -- genuinely out of
                # stock right now, not just "had a stock-out at some
                # point in the last 90 days" (which is all the Product
                # Finder field itself confirms).
                confirmed = product.buy_box_now <= 0 or product.offers_now == 0
                reference_price = product.buy_box_90d
            else:  # price_spike
                confirmed = (
                    product.buy_box_90d > 0
                    and product.buy_box_now > product.buy_box_90d * (1 + self.PRICE_SPIKE_CONFIRM_PCT / 100)
                )
                reference_price = product.buy_box_now

            if not confirmed or reference_price <= 0:
                skipped_unconfirmed += 1
                continue

            fba_fee = product.fba_fee if product.fba_fee else FeeEngine.DEFAULT_FBA_FEE
            category_name = category_names.get(product.category, "")

            target_buy_price = FeeEngine.max_source_cost(
                reference_price, category_name, fba_fee, self.TARGET_ROI_PCT
            )

            if target_buy_price <= 0:
                # Even the 25%+ ROI ceiling comes back 0 -- Amazon's
                # own fees already eat the whole reference price, so
                # this isn't a genuine lead either. Feed it into the
                # same ceiling-rejected pool BrandScanService uses, so
                # it's tracked for a later ceiling_recheck rather than
                # just discarded.
                ProductRepository.upsert_ceiling_rejected(
                    asin=asin, title=product.title, brand=product.brand,
                    category=product.category, category_name=category_name,
                    fba_fee=fba_fee, buy_box_at_reject=reference_price,
                )
                skipped_unconfirmed += 1
                continue

            self._save_match(
                query, asin, product, category_name, reference_price,
                target_buy_price, extra_reasoning={
                    "offers_now": product.offers_now,
                    "buy_box_90d": product.buy_box_90d,
                },
            )
            new_matches += 1

        return {
            "candidates_checked": len(keepa_products),
            "new_matches": new_matches,
            "skipped_gated": skipped_gated,
            "skipped_excluded": skipped_excluded,
            "skipped_unconfirmed": skipped_unconfirmed,
        }

    # ---- ceiling_recheck (iterates Atlas's own CeilingRejected pool --
    # no Product Finder call at all) ----

    def _run_ceiling_recheck(self, query) -> dict:
        rejected_rows = ProductRepository.list_ceiling_rejected(limit=self.CEILING_RECHECK_BATCH_SIZE)

        # Always refresh last_checked_at for the "last run X ago"
        # display, even if the pool was empty -- but never touch
        # last_match_snapshot, which this signal type doesn't use.
        ProductRepository.update_signal_query_snapshot(query.id)

        if not rejected_rows:
            return {
                "signal_query_id": query.id, "candidates_checked": 0, "new_matches": 0,
                "still_rejected": 0, "skipped_gated": 0, "skipped_excluded": 0,
            }

        asins = [row.asin for row in rejected_rows]
        rows_by_asin = {row.asin: row for row in rejected_rows}

        keepa_products = self.product_service.get_products(asins, "UK", full=True, stats_days=90)
        category_names = get_category_names(self.finder.api)

        excluded_asins = ProductRepository.get_excluded_asins()
        excluded_category_ids = ProductRepository.get_excluded_category_ids()
        gated_brand_pairs = ProductRepository.get_gated_brand_pairs()

        skipped_gated = 0
        skipped_excluded = 0
        still_rejected = 0
        new_matches = 0

        for k in keepa_products:
            asin = k.get("asin") or ""
            row = rows_by_asin.get(asin)
            if not row:
                continue

            brand_name = k.get("brand") or ""
            category_ids = {
                str(node.get("catId"))
                for node in (k.get("categoryTree") or [])
                if node.get("catId") is not None
            }
            category_ids.update(str(cid) for cid in (k.get("categories") or []))
            root_category = str(k.get("rootCategory") or "")
            if root_category:
                category_ids.add(root_category)

            if asin in excluded_asins or is_excluded(asin, brand_name, category_ids, excluded_category_ids):
                skipped_excluded += 1
                # Now excluded outright -- drop it from the pool
                # rather than re-checking something Atlas will never
                # surface again.
                ProductRepository.remove_ceiling_rejected(asin)
                continue

            if is_gated(brand_name, category_ids, gated_brand_pairs):
                skipped_gated += 1
                # Leave it in the pool -- gating status can change,
                # unlike a hard exclusion.
                continue

            product = ProductMapper.from_keepa(k)
            fba_fee = product.fba_fee if product.fba_fee else FeeEngine.DEFAULT_FBA_FEE
            category_name = category_names.get(product.category, "") or row.category_name

            # Same ceiling math as BrandScanService.scan Step 3b --
            # whichever price is higher, today's or the 90-day
            # typical, so a temporary discount doesn't hide a real
            # graduation.
            best_price = max(product.buy_box_now, product.buy_box_90d)
            referral_rate = REFERRAL_RATE_BY_CATEGORY_NAME.get(category_name.lower(), DEFAULT_REFERRAL_RATE)
            referral_fee = best_price * referral_rate
            ceiling_profit = best_price - fba_fee - referral_fee

            if ceiling_profit <= 0:
                # Still unprofitable -- refresh the pool row with the
                # latest snapshot (price/fee may have moved even
                # though it's still not enough) and move on.
                ProductRepository.upsert_ceiling_rejected(
                    asin=asin, title=product.title or row.title, brand=product.brand or row.brand,
                    category=product.category or row.category, category_name=category_name,
                    fba_fee=fba_fee, buy_box_at_reject=best_price,
                )
                still_rejected += 1
                continue

            # Graduated -- clears the ceiling now. Surface it as a
            # match at the 25%+ ROI target buy price and remove it
            # from the rejected pool (no point re-checking something
            # already surfaced).
            target_buy_price = FeeEngine.max_source_cost(best_price, category_name, fba_fee, self.TARGET_ROI_PCT)

            self._save_match(
                query, asin, product, category_name, best_price, target_buy_price,
                extra_reasoning={
                    "buy_box_at_reject": row.buy_box_at_reject,
                    "first_rejected_at": row.first_rejected_at.isoformat() if row.first_rejected_at else None,
                },
            )
            ProductRepository.remove_ceiling_rejected(asin)
            new_matches += 1

        return {
            "signal_query_id": query.id,
            "candidates_checked": len(keepa_products),
            "new_matches": new_matches,
            "still_rejected": still_rejected,
            "skipped_gated": skipped_gated,
            "skipped_excluded": skipped_excluded,
        }

    # ---- shared ----

    @staticmethod
    def _save_match(query, asin, product, category_name, reference_price, target_buy_price, extra_reasoning=None):
        """
        eu_history_json is a free enrichment from Atlas's OWN past
        scan data (ProductRepository.get_last_eu_check) -- never a
        fresh EU lookup, see SignalMatch's docstring.
        """
        reasoning = {
            "signal_type": query.signal_type,
            "buy_box_now": product.buy_box_now,
            "sales_drops_30d": product.sales_drops_30d,
            "reference_price_used": reference_price,
            "target_roi_pct": SignalService.TARGET_ROI_PCT,
        }
        if extra_reasoning:
            reasoning.update(extra_reasoning)

        eu_history = {}
        last_eu = ProductRepository.get_last_eu_check(asin)
        if last_eu:
            eu_history = {
                "scanned_at": last_eu.scanned_at.isoformat() if last_eu.scanned_at else None,
                "best_source_marketplace": last_eu.best_source_marketplace,
                "best_source_cost_gbp": last_eu.best_source_cost_gbp,
                "roi": last_eu.roi,
                "recommendation": last_eu.recommendation,
            }

        ProductRepository.save_signal_match(
            signal_query_id=query.id, signal_type=query.signal_type, asin=asin,
            title=product.title, brand=product.brand, category_name=category_name,
            buy_box_now=reference_price, monthly_sales=product.monthly_sales,
            sales_drops_30d=product.sales_drops_30d, target_buy_price_gbp=target_buy_price,
            signal_reasoning_json=json.dumps(reasoning), eu_history_json=json.dumps(eu_history),
        )
