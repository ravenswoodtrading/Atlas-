import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.database.database import SessionLocal
from app.database.models import TrackedSeller, SellerNewListing, ProductRecord, OaSourceCandidate
from app.keepa.client import get_keepa_client
from app.models.product import Product
from app.services.brand_scan_service import BrandScanService
from app.services.product_repository import ProductRepository
from app.services.sourcing_classifier import SourcingClassifier, BRAND_PATTERN_MIN_SAMPLE
from app.services.activity_log import ActivityLog
from app.services.token_usage_service import TokenUsageService
from app.services.fee_engine import FeeEngine

# How many DISTINCT ASINs to send to BrandScanService.scan() per Keepa
# call within one reclassify_all() run -- same chunking spirit as
# ReplenService.CHECK_BATCH_SIZE, sized so a single low-token stop
# doesn't waste a huge partially-fetched batch.
RECLASSIFY_BATCH_SIZE = 15

# Daily cap for the SCHEDULED reclassify tick (2026-09-05) -- passed as
# reclassify_all's max_asins, not a change to that function's own
# manual-admin-route default (None -- no cap). Same value as
# ReviewQueueService.MAX_STALE_RECHECK_PER_RUN, for the same reason:
# real tokens, no free recompute path (see reclassify_all's own
# docstring), so a large backlog (1,178 never-reclassified ASINs
# confirmed live the day this was added) clears gradually rather than
# in one uncapped daily run.
RECLASSIFY_DAILY_CAP = 50

# A "hot" lead -- the same ASIN independently listed by 2+ distinct
# tracked competitors within this many days. Corroboration from
# multiple sellers is a stronger signal than any one of them alone
# (e.g. several competitors all catching the same live spread/sale),
# but it's still just a research signal, not a costed live opportunity
# -- see list_notable_buyable for the bar that actually feeds Review
# Queue, which this deliberately does NOT affect.
HOT_WINDOW_DAYS = 14
MIN_HOT_COMPETITORS = 2

# competitors.html tab slug -> the sourcing_tag value it filters to.
SOURCING_TAG_BY_TAB = {
    "eu_a2a": "EU A2A",
    "uk_a2a": "UK A2A",
    "wholesale": "Wholesale (likely)",
    "oa": "OA / unclear",
}


class SellerWatchService:
    """
    Detects new ASINs listed by tracked competitor sellers (see
    TrackedSeller) via Keepa's seller storefront lookup, and hands
    each newly-detected ASIN off to the EXISTING pricing/scoring
    pipeline (BrandScanService.scan) exactly as an ASIN-list upload
    would -- no separate pipeline, just a new trigger for it. That
    call already applies the same zero-token exclusion pre-checks
    (user-excluded ASINs, known_products category/brand exclusions)
    that Discovery/brand scans get, for free.

    Each newly-detected ASIN that the pipeline actually scores (i.e.
    produces a ProductRecord -- some get filtered out first, e.g. dead
    listings or no EU source at all) is then run through
    SourcingClassifier using that same already-fetched data, no extra
    tokens. An ASIN that never made it into a ProductRecord is still
    recorded as a detection, just with sourcing_tag left NULL --
    there's no real data to classify it against, and guessing off
    empty/zero fields would be worse than admitting we don't know yet.

    Background-thread scheduling is not wired in yet -- run_check() is
    a plain callable for now.
    """

    @staticmethod
    def _fetch_storefronts(seller_ids: list) -> dict:
        """
        One Keepa call PER seller ID. Returns {seller_id:
        seller_info_dict} for whichever sellers Keepa actually
        returned data for -- a missing entry means no data was
        available this run (not an error), same "some IDs just don't
        come back" shape as a product query.

        NOT batched, despite seller_query's own docstring saying up to
        100 IDs are allowed per call -- CONFIRMED via a live request
        that Keepa's server rejects anything more than 1 seller ID
        per call whenever storefront=True (HTTP 405: "Maximum allowed
        sellerId batch size for this request is 1 if storefront is
        requested"). That 100-ID limit only applies to a plain
        (non-storefront) seller lookup. Same total token cost either
        way (1 token per seller) -- this just costs one HTTP
        round-trip per seller instead of a single batched one.

        wait=True (2026-09-04, real bug found live: 5 of 19 tracked
        sellers went 20-27+ HOURS without a single successful check).
        Was wait=False -- any seller whose turn came up when tokens
        happened to be momentarily low failed outright with no retry,
        and since this loop always walks sellers in the SAME fixed
        order, the SAME sellers at the end of the list lost out every
        time, not a fair/rotating subset. Competitor Watch is Tamara's
        own highest-priority lead source and this list is cheap (at
        most ~1 token per seller, ~19 total for a full pass) -- letting
        each call wait for its own token to refill (22/min on this
        plan, so worst case here is under a minute) reliably completes
        every seller instead of silently dropping some. This still
        runs inside asyncio.to_thread (see main.py's scheduler), so
        blocking here never blocks the actual web server.
        """
        api = get_keepa_client()
        results = {}
        tokens_before = api.tokens_left

        for seller_id in seller_ids:
            try:
                try:
                    response = api.seller_query(seller_id, domain="GB", storefront=True, wait=True)
                except TypeError:
                    # Installed keepa version doesn't accept wait= here --
                    # same fallback ProductFinder.find_brand already uses
                    # for product_finder().
                    response = api.seller_query(seller_id, domain="GB", storefront=True)
            except Exception as exc:
                print(f"Seller storefront lookup failed for {seller_id}: {exc}")
                continue

            results.update(response or {})

        # One aggregate row for the whole batch rather than one per
        # seller_id -- a run_check tick can cover dozens of tracked
        # sellers, and the Token Usage page cares about "how much did
        # this feature cost this run", not a row per HTTP call.
        TokenUsageService.record_keepa_spend(
            "competitor_watch", "keepa_seller_query", tokens_before, api.tokens_left,
            marketplace="UK", asins_count=len(seller_ids),
        )

        return results

    @staticmethod
    def _brand_repeat_count(db, tracked_seller_id: int, brand: str) -> int:
        """
        How many of this tracked seller's PAST detections (prior
        committed runs -- SessionLocal is autoflush=False, so sibling
        detections from the same run never count each other, keeping
        this independent of processing order) were the same brand.
        Feeds SourcingClassifier's "same brand recurring" wholesale
        signal. Case-insensitive: Keepa brand casing is inconsistent
        across listings from the same real brand.
        """
        if not brand:
            return 0

        return (
            db.query(SellerNewListing)
            .join(ProductRecord, SellerNewListing.product_record_id == ProductRecord.id)
            .filter(SellerNewListing.tracked_seller_id == tracked_seller_id)
            .filter(func.lower(ProductRecord.brand) == brand.lower())
            .count()
        )

    @staticmethod
    def brand_sourcing_pattern(brand: str) -> dict | None:
        """
        Historical sourcing_tag distribution for `brand` across EVERY
        non-dismissed detection Atlas has ever made (any seller, any
        time) -- 2026-09-07, Tamara's own point: "WORX often comes up
        via EU A2A [as does] Makita, but ... Philips ... UK retailers
        often have Philips sales too, so if there is genuinely no EU
        price drops then more likely to be OA". Feeds SourcingClassifier.
        assess_certainty's brand-history fact -- deliberately a SEPARATE,
        broader signal from _brand_repeat_count above (that one asks "has
        THIS SELLER listed this brand repeatedly", a wholesale signal;
        this asks "across ALL sellers, how does this brand usually get
        classified", a sourcing-pattern signal).

        Returns None for an empty/unknown brand or a sample smaller than
        SourcingClassifier.BRAND_PATTERN_MIN_SAMPLE (assess_certainty
        applies that same floor again itself, but returning None here
        lets a caller skip the query's own cost -- N/A -- entirely, and
        keeps "no real pattern yet" and "0% EU A2A" visibly distinct).
        """
        if not brand:
            return None

        db = SessionLocal()
        try:
            rows = (
                db.query(SellerNewListing.sourcing_tag, func.count(SellerNewListing.id))
                .join(ProductRecord, SellerNewListing.product_record_id == ProductRecord.id)
                .filter(func.lower(ProductRecord.brand) == brand.lower())
                .filter(SellerNewListing.dismissed == False)  # noqa: E712
                .filter(SellerNewListing.sourcing_tag.isnot(None))
                .group_by(SellerNewListing.sourcing_tag)
                .all()
            )
        finally:
            db.close()

        total = sum(count for _, count in rows)
        if total < BRAND_PATTERN_MIN_SAMPLE:
            return None

        eu_a2a_count = next((count for tag, count in rows if tag == "EU A2A"), 0)
        return {"sample_size": total, "eu_a2a_pct": (eu_a2a_count / total) * 100.0}

    @staticmethod
    def _persist_classification(listing: SellerNewListing, classification, recommendation: str | None) -> None:
        """
        Writes a fresh SourcingClassifier result onto a listing --
        shared by run_check/rescan_unscored/reclassify_all so all three
        call sites go through SourcingClassifier.merge_evidence rather
        than each doing its own json.dumps(classification.reasoning)
        (which is what silently discarded historical A2A evidence
        before atlas-competitor-watch-classification-v1.md's first
        fix: a later reclassify blindly overwrote sourcing_reasoning_json
        with only THIS round's findings, losing whatever real EU/UK
        A2A evidence a previous round had recorded).

        sourcing_tag is still overwritten outright every call -- see
        merge_evidence's own docstring for why that's correct: the
        CURRENT classification is meant to change freely as evidence
        ages out of the window. Only the evidence archive inside
        sourcing_reasoning_json is preserve-on-miss.

        recommendation (2026-09-03 follow-up fix): the LINKED
        ProductRecord's own OpportunityEngine recommendation for
        TODAY's actual best source across all 4 marketplaces --
        "BUY" | "CONSIDER" | "WATCH" | "IGNORE" | etc, or None if
        there's no linked record at all. currently_buyable is derived
        from this DIRECTLY -- ONLY "BUY" counts, matching the locked
        definition (CONSIDER/WATCH/anything else is explicitly NOT a
        confirmed buying lead, per Tamara's own instruction). This
        used to be computed independently inside SourcingClassifier
        (a profit>0/recovered-price check tied to the HISTORICAL
        marketplace) -- that was a second, parallel profitability
        signal duplicating what OpportunityEngine already decides, and
        could disagree with it (e.g. still say "buyable" via the
        historical DE evidence when DE itself is no longer viable
        today and a DIFFERENT marketplace, ES, is the real current
        opportunity). "Can we buy this now" and "how did they source
        it" are answered by two genuinely independent computations now
        -- see SourcingClassification's own docstring.
        """
        previous_reasoning = None
        if listing.sourcing_reasoning_json:
            try:
                previous_reasoning = json.loads(listing.sourcing_reasoning_json)
            except Exception:
                previous_reasoning = None

        # Skip the automated tag once a human has manually corrected it
        # (2026-09-07, Tamara) -- see SellerNewListing.manually_classified's
        # own docstring for the real case this fixes: Atlas confidently
        # re-tagging a manually-corrected listing back to its own (wrong)
        # answer on the very next scheduled recheck. currently_buyable and
        # the evidence archive still update either way -- both are
        # independent of which sourcing_tag is currently displayed.
        if not listing.manually_classified:
            listing.sourcing_tag = classification.sourcing_tag
        listing.currently_buyable = recommendation == "BUY"
        listing.sourcing_reasoning_json = json.dumps(
            SourcingClassifier.merge_evidence(previous_reasoning, classification)
        )

    @staticmethod
    def set_manual_sourcing_tag(asin: str, sourcing_tag: str) -> int:
        """
        Human correction of sourcing_tag (Review Queue's OA to Investigate
        detail panel, 2026-09-07) -- for when Atlas's own classifier got
        it wrong, e.g. a real EU A2A opportunity tagged "OA / unclear"
        because SourcingClassifier only found supporting price evidence
        outside its own recent-window check (see SellerNewListing.
        manually_classified's own docstring for the real case this fixes).

        Applies to EVERY non-dismissed listing for this ASIN, not just
        one -- sourcing_tag describes how the PRODUCT is sourced, not
        anything seller-specific, so a correction should hold regardless
        of which competitor's detection happens to be showing it. Sets
        manually_classified=True on each so _persist_classification's
        next automated pass (run_check/rescan_unscored/reclassify_all)
        never silently reverts this. Returns how many rows were updated.

        sourcing_tag must be one of SOURCING_TAG_BY_TAB's values (EU A2A/
        UK A2A/Wholesale (likely)/OA / unclear) -- the caller (the route)
        validates this before calling in, this method trusts it.
        """
        db = SessionLocal()
        try:
            rows = (
                db.query(SellerNewListing)
                .filter(SellerNewListing.asin == asin)
                .filter(SellerNewListing.dismissed == False)  # noqa: E712
                .all()
            )
            for row in rows:
                row.sourcing_tag = sourcing_tag
                row.manually_classified = True
            db.commit()
            return len(rows)
        finally:
            db.close()

    @staticmethod
    def run_check() -> dict:
        """
        One full pass over every active tracked seller. Returns a
        small summary dict for logging/status, same convention as
        ScanQueueService.run_next_tick.

        Ordered oldest-checked-first (2026-09-04, alongside the
        _fetch_storefronts wait=True fix -- see its own docstring for
        the real starvation bug this closes). Belt-and-suspenders: with
        wait=True this loop should now complete every seller most
        ticks anyway, but if tokens ever DO run out mid-pass (a very
        long drought), whoever's waited longest gets checked first, so
        a shortfall is self-correcting next tick rather than always
        landing on the same sellers at the end of a fixed list.
        """
        db = SessionLocal()

        try:
            tracked = (
                db.query(TrackedSeller)
                .filter(TrackedSeller.active == True)
                .order_by(TrackedSeller.last_checked_at.asc().nulls_first())
                .all()
            )

            if not tracked:
                return {"checked": 0, "new_listings": 0}

            storefronts = SellerWatchService._fetch_storefronts(
                [t.seller_id for t in tracked]
            )

            scanner = BrandScanService(usage_category="competitor_watch")
            checked = 0
            total_new = 0

            for seller in tracked:
                info = storefronts.get(seller.seller_id)

                # No data back for this seller_id this run (not found,
                # or Keepa has nothing collected yet) -- leave its
                # snapshot and last_checked_at untouched rather than
                # overwriting with an empty list, which would make
                # EVERY one of its real listings look "new" the next
                # time data actually comes back.
                if info is None or "asinList" not in info:
                    print(f"No storefront data for seller {seller.seller_id} this run -- skipping.")
                    continue

                current_asins = set(info.get("asinList") or [])

                if seller.last_checked_at is None:
                    # First-ever check for this seller: nothing to diff
                    # against yet. Record the baseline WITHOUT treating
                    # their entire existing catalog as "new listings" --
                    # detection starts from the next check onward.
                    new_asins = set()
                else:
                    previous_asins = (
                        set(json.loads(seller.last_asin_snapshot))
                        if seller.last_asin_snapshot else set()
                    )
                    new_asins = current_asins - previous_asins

                if new_asins:
                    new_asins_list = sorted(new_asins)
                    label = f"competitor:{seller.nickname or seller.seller_id}"

                    # limit defaults to 20 in scan() -- pass the real
                    # count so a competitor listing more than that in
                    # one check window doesn't silently lose detections.
                    # include_no_eu_source=True: unlike Discovery/Watchlist,
                    # a competitor listing with no EU A2A source is still a
                    # useful lead here -- it may well be an OA find, sourced
                    # outside the EU-arbitrage pipeline entirely. Keeping it
                    # gives it a real ProductRecord (title/price/rank) and a
                    # SourcingClassifier tag instead of vanishing into a
                    # bare-ASIN detection with nothing to go on.
                    scan_result = scanner.scan(
                        brand=label,
                        asins=new_asins_list,
                        limit=len(new_asins_list),
                        include_no_eu_source=True,
                    )

                    # Only ASINs that made it all the way through
                    # scoring appear here (see BrandScanService.scan
                    # Step 5) -- e.g. a dead listing, excluded category,
                    # or one with no current UK buy-box price at all
                    # still won't have an opportunity to classify
                    # against (missing EU source alone no longer drops
                    # it, see include_no_eu_source above). Keeps the
                    # whole opp (product + report) -- report["recommendation"]
                    # is what currently_buyable now derives from (see
                    # _persist_classification's own docstring), not
                    # just the product dict.
                    scored_by_asin = {
                        opp["product"]["asin"]: opp
                        for opp in scan_result.get("opportunities", [])
                    }
                    record_ids = ProductRepository.get_latest_record_ids(new_asins_list)

                    for asin in new_asins_list:
                        opp = scored_by_asin.get(asin)

                        listing = SellerNewListing(
                            tracked_seller_id=seller.id,
                            asin=asin,
                            product_record_id=record_ids.get(asin),
                        )

                        if opp:
                            product = Product(**opp["product"])
                            brand_repeat = SellerWatchService._brand_repeat_count(
                                db, seller.id, product.brand
                            )
                            classification = SourcingClassifier.classify(
                                product, brand_repeat_count=brand_repeat
                            )
                            recommendation = (opp.get("report") or {}).get("recommendation")
                            # No previous stored reasoning to preserve --
                            # a brand-new detection (see
                            # _persist_classification's own docstring
                            # for what this does on an existing listing
                            # being reclassified instead).
                            SellerWatchService._persist_classification(listing, classification, recommendation)

                        db.add(listing)

                    total_new += len(new_asins_list)

                seller.last_asin_snapshot = json.dumps(sorted(current_asins))
                seller.last_checked_at = datetime.now(timezone.utc)
                checked += 1

            db.commit()

            ActivityLog.record(
                "competitor_check",
                f"{checked} seller(s) checked, {total_new} new listing(s)",
            )

            return {"checked": checked, "new_listings": total_new}

        finally:
            db.close()

    @staticmethod
    def rescan_unscored() -> dict:
        """
        One-off backfill for detections stuck with sourcing_tag=NULL
        and no linked ProductRecord -- e.g. everything recorded before
        scan()'s include_no_eu_source flag existed, back when a
        no-EU-source ASIN was dropped entirely instead of scored as a
        possible OA lead. Re-scans exactly those ASINs (not a normal
        seller storefront check, so it doesn't touch last_asin_snapshot
        or last_checked_at) and links up whatever the pipeline produces
        this time.

        force_rescan=True: these ASINs have no ProductRecord, so
        get_recently_scanned_asins wouldn't skip them anyway UNLESS the
        exact same ASIN happened to get freshly scanned via some other
        campaign since -- force_rescan guarantees this backfill actually
        re-checks every one of them rather than silently no-op'ing on
        whichever few collide with that global cooldown.

        Still leaves sourcing_tag NULL for whatever the pipeline
        genuinely can't produce a ProductRecord for at all (dead
        listing, excluded, no current UK price) -- same "no real data,
        don't guess" rule as run_check.
        """
        db = SessionLocal()

        try:
            unscored = (
                db.query(SellerNewListing)
                .filter(SellerNewListing.product_record_id.is_(None))
                .filter(SellerNewListing.dismissed == False)
                .all()
            )

            if not unscored:
                return {"rescanned": 0, "updated": 0}

            asins_list = sorted({listing.asin for listing in unscored})

            scanner = BrandScanService(usage_category="competitor_watch")
            scan_result = scanner.scan(
                brand="competitor-rescan",
                asins=asins_list,
                limit=len(asins_list),
                include_no_eu_source=True,
                force_rescan=True,
            )

            scored_by_asin = {
                opp["product"]["asin"]: opp
                for opp in scan_result.get("opportunities", [])
            }
            record_ids = ProductRepository.get_latest_record_ids(asins_list)

            updated = 0

            for listing in unscored:
                opp = scored_by_asin.get(listing.asin)
                if not opp:
                    continue

                product = Product(**opp["product"])
                brand_repeat = SellerWatchService._brand_repeat_count(
                    db, listing.tracked_seller_id, product.brand
                )
                classification = SourcingClassifier.classify(
                    product, brand_repeat_count=brand_repeat
                )
                recommendation = (opp.get("report") or {}).get("recommendation")

                listing.product_record_id = record_ids.get(listing.asin)
                SellerWatchService._persist_classification(listing, classification, recommendation)
                updated += 1

            db.commit()

            return {"rescanned": len(asins_list), "updated": updated}
        finally:
            db.close()

    @staticmethod
    def reclassify_all(max_asins: int | None = None) -> dict:
        """
        One-off backfill: re-classifies EVERY non-dismissed detection
        against a FRESH Keepa fetch, regardless of its current
        sourcing_tag -- unlike rescan_unscored() above, which only
        touches detections with no ProductRecord at all. Needed
        whenever SourcingClassifier's rules change (e.g. the
        EU-dip-since-recovered fix): the price-history fields it needs
        (buy_box_min_90d, best_source_cost_min_90d_gbp) aren't
        persisted to ProductRecord, so re-tagging an old detection
        costs real tokens, same as any rescan -- there's no free
        recompute path.

        Also the fix for a real, confirmed bug (2026-09-05, Tamara's
        own report on B09446293Y): a listing classified within days of
        a genuine EU price dip can see "0 priced days" even though the
        dip is real and squarely inside RECENT_WINDOW_DAYS -- Keepa's
        own historical price series for the last few days isn't always
        fully settled yet at classification time. Without a reclassify
        path, that listing stays wrongly tagged "OA / unclear" forever.
        See _weekly_recheck_scheduler in main.py for the daily,
        max_asins-capped call that now catches this automatically
        instead of relying on someone noticing and clicking the manual
        admin button (competitors_reclassify_all) below.

        Processes oldest-reclassified-first (NULL -- never done --
        first), one Keepa scan() call per RECLASSIFY_BATCH_SIZE
        distinct ASINs, committing after each batch. Stops as soon as
        a batch comes back with scan()'s low-token error, OR once
        max_asins distinct ASINs have been processed THIS call
        (checked between batches, never mid-batch) -- whichever comes
        first. max_asins=None (the manual admin route's own default,
        unchanged) means "no cap, run until token-exhausted", exactly
        the existing behaviour; the daily scheduler passes a real cap
        (see RECLASSIFY_DAILY_CAP) so a large backlog clears
        gradually, same "capped, resumable, oldest-first" convention
        as every other daily backstop in this app, rather than one
        job trying to force the whole backlog through in a single tick
        (see the 2026-09-05 bulk-recheck incident this convention
        already exists to avoid). Either way, the caller (or next
        day's scheduler tick) just picks up again from oldest-
        reclassified-first where this call left off.

        Multiple SellerNewListing rows can share the same ASIN
        (different tracked sellers who both listed it) -- each ASIN is
        only fetched from Keepa once per run, but every row for it
        still gets its own brand_repeat_count (seller-specific) and
        classification.
        """
        db = SessionLocal()

        try:
            pending = (
                db.query(SellerNewListing)
                .filter(SellerNewListing.dismissed == False)
                .order_by(SellerNewListing.sourcing_reclassified_at.asc().nullsfirst())
                .all()
            )

            if not pending:
                return {"processed": 0, "updated": 0, "flipped": 0, "remaining": 0, "stopped_early": False}

            rows_by_asin = {}
            for listing in pending:
                rows_by_asin.setdefault(listing.asin, []).append(listing)

            distinct_asins = list(rows_by_asin.keys())

            scanner = BrandScanService(usage_category="competitor_watch")
            processed_asins = 0
            updated = 0
            flipped = 0
            stopped_early = False
            tokens_remaining = None

            for i in range(0, len(distinct_asins), RECLASSIFY_BATCH_SIZE):
                if max_asins is not None and processed_asins >= max_asins:
                    break

                batch_asins = distinct_asins[i:i + RECLASSIFY_BATCH_SIZE]

                scan_result = scanner.scan(
                    brand="competitor-reclassify", asins=batch_asins,
                    limit=len(batch_asins), include_no_eu_source=True,
                    force_rescan=True,
                )
                tokens_remaining = scan_result.get("tokens_remaining", tokens_remaining)

                if scan_result.get("error"):
                    stopped_early = True
                    break

                scored_by_asin = {
                    opp["product"]["asin"]: opp
                    for opp in scan_result.get("opportunities", [])
                }
                record_ids = ProductRepository.get_latest_record_ids(batch_asins)
                now = datetime.now(timezone.utc)

                for asin in batch_asins:
                    opp = scored_by_asin.get(asin)

                    for listing in rows_by_asin[asin]:
                        listing.sourcing_reclassified_at = now

                        if not opp:
                            continue

                        previous_tag = listing.sourcing_tag
                        product = Product(**opp["product"])
                        brand_repeat = SellerWatchService._brand_repeat_count(
                            db, listing.tracked_seller_id, product.brand
                        )
                        classification = SourcingClassifier.classify(
                            product, brand_repeat_count=brand_repeat
                        )
                        recommendation = (opp.get("report") or {}).get("recommendation")

                        listing.product_record_id = record_ids.get(asin) or listing.product_record_id
                        SellerWatchService._persist_classification(listing, classification, recommendation)
                        updated += 1

                        if previous_tag != listing.sourcing_tag:
                            flipped += 1

                    processed_asins += 1

                db.commit()

            # Genuinely-never-reclassified count, queried fresh rather
            # than derived from distinct_asins/processed_asins -- those
            # two only describe THIS call's own slice of `pending`
            # (every non-dismissed row, not just never-done ones), so
            # subtracting them would keep reporting ~the full pool size
            # every call even as real progress is made underneath.
            remaining = (
                db.query(SellerNewListing.asin)
                .filter(SellerNewListing.dismissed == False)
                .filter(SellerNewListing.sourcing_reclassified_at.is_(None))
                .distinct()
                .count()
            )

            return {
                "processed": processed_asins,
                "updated": updated,
                "flipped": flipped,
                "remaining": remaining,
                "stopped_early": stopped_early,
                "tokens_remaining": tokens_remaining,
            }
        finally:
            db.close()

    @staticmethod
    def reclassify_oa_investigate_queue(max_asins: int | None = None, max_age_days: int | None = 14) -> dict:
        """
        Bulk re-run of SourcingClassifier against a fresh Keepa fetch,
        scoped to ONLY the current OA to Investigate population
        (sourcing_tag=="OA / unclear", not dismissed, not already
        manually_classified) -- 2026-09-07, Tamara: "I want everything in
        the OA investigation queues reclassified and details of how many
        were reclassified."

        Deliberately its OWN method rather than reclassify_all(max_asins=...)
        with a bigger cap: reclassify_all() walks the ENTIRE non-dismissed
        backlog oldest-reclassified-first regardless of current tag (it
        exists to catch classifier RULE changes affecting any listing,
        e.g. the EU A2A window widening this method's docstring below
        references) -- that would burn tokens re-checking listings
        already correctly tagged EU A2A/UK A2A/Wholesale, which is not
        what "reclassify the OA queue" asked for. This filters to exactly
        the population feeding that one view.

        max_age_days=14 (Tamara, 2026-09-07: "with our rescan lets keep
        this to recent opportunities... not older than 14 days") -- of
        the real 597-row backlog measured the day this was added, 385
        (nearly two thirds) were already older than 14 days, so this
        matters: without it, most of a real Keepa run would be spent on
        stale detections instead of the recent ones actually worth acting
        on. Filters on detected_at, not sourcing_reclassified_at -- this
        is about how OLD the underlying competitor sighting is, not when
        it was last checked. None means no age limit (the pre-2026-09-07
        behaviour), kept as an explicit opt-out rather than removed.

        Skips manually_classified rows entirely (both from the query and
        from re-persisting) -- a human already corrected those; a bulk
        run has no business overwriting that decision.

        Same batching/token-exhaustion handling as reclassify_all (see
        its own docstring) -- one Keepa scan() call per RECLASSIFY_BATCH_SIZE
        ASINs, stops as soon as a batch reports the low-token error, or
        once max_asins ASINs have been processed. max_asins=None runs
        until the whole (age-filtered) OA queue is done or tokens run out.

        Returns a breakdown by resulting tag, not just a single "flipped"
        count -- exactly what "details of how many were reclassified"
        asked for.
        """
        db = SessionLocal()

        try:
            query = (
                db.query(SellerNewListing)
                .filter(SellerNewListing.sourcing_tag == "OA / unclear")
                .filter(SellerNewListing.dismissed == False)  # noqa: E712
                .filter(SellerNewListing.manually_classified == False)  # noqa: E712
            )
            if max_age_days is not None:
                cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
                query = query.filter(SellerNewListing.detected_at >= cutoff)
            pending = query.all()

            if not pending:
                return {
                    "queue_size": 0, "processed": 0, "flipped": 0,
                    "flipped_to": {}, "stayed_oa": 0, "stopped_early": False,
                    "tokens_remaining": None,
                }

            rows_by_asin = {}
            for listing in pending:
                rows_by_asin.setdefault(listing.asin, []).append(listing)

            distinct_asins = list(rows_by_asin.keys())
            queue_size = len(distinct_asins)

            scanner = BrandScanService(usage_category="competitor_watch")
            processed_asins = 0
            flipped = 0
            flipped_to: dict[str, int] = {}
            stayed_oa = 0
            stopped_early = False
            tokens_remaining = None

            for i in range(0, len(distinct_asins), RECLASSIFY_BATCH_SIZE):
                if max_asins is not None and processed_asins >= max_asins:
                    break

                batch_asins = distinct_asins[i:i + RECLASSIFY_BATCH_SIZE]

                scan_result = scanner.scan(
                    brand="oa-queue-reclassify", asins=batch_asins,
                    limit=len(batch_asins), include_no_eu_source=True,
                    force_rescan=True,
                )
                tokens_remaining = scan_result.get("tokens_remaining", tokens_remaining)

                if scan_result.get("error"):
                    stopped_early = True
                    break

                scored_by_asin = {
                    opp["product"]["asin"]: opp
                    for opp in scan_result.get("opportunities", [])
                }
                record_ids = ProductRepository.get_latest_record_ids(batch_asins)
                now = datetime.now(timezone.utc)

                for asin in batch_asins:
                    opp = scored_by_asin.get(asin)

                    for listing in rows_by_asin[asin]:
                        listing.sourcing_reclassified_at = now

                        if not opp:
                            continue

                        product = Product(**opp["product"])
                        brand_repeat = SellerWatchService._brand_repeat_count(
                            db, listing.tracked_seller_id, product.brand
                        )
                        classification = SourcingClassifier.classify(
                            product, brand_repeat_count=brand_repeat
                        )
                        recommendation = (opp.get("report") or {}).get("recommendation")

                        listing.product_record_id = record_ids.get(asin) or listing.product_record_id
                        SellerWatchService._persist_classification(listing, classification, recommendation)

                        if listing.sourcing_tag != "OA / unclear":
                            flipped += 1
                            flipped_to[listing.sourcing_tag] = flipped_to.get(listing.sourcing_tag, 0) + 1
                        else:
                            stayed_oa += 1

                    processed_asins += 1

                db.commit()

            return {
                "queue_size": queue_size,
                "processed": processed_asins,
                "flipped": flipped,
                "flipped_to": flipped_to,
                "stayed_oa": stayed_oa,
                "stopped_early": stopped_early,
                "tokens_remaining": tokens_remaining,
            }
        finally:
            db.close()

    @staticmethod
    def archive_stale_oa_investigate(max_age_days: int = 14) -> int:
        """
        Daily housekeeping (2026-09-07, Tamara: "maybe we should clear
        out/archive some leads older than say 14 days" -- confirmed
        scope: OA/unclear only, recurring policy): soft-archives
        (dismissed=True, same reversible flag Competitor Watch already
        uses everywhere else -- see SellerNewListing.dismissed's own
        docstring) any OA-to-Investigate listing that's sat untouched
        past max_age_days. Of the real 597-row backlog measured the day
        this was added, 385 were already older than 14 days -- this is
        what stops that number from only ever growing.

        Scoped identically to list_oa_worth_investigating's own
        "outstanding" filter (sourcing_tag=="OA / unclear", dismissed==
        False, review IS NULL) plus manually_classified==False, so this
        can never archive something a human already resolved (review is
        no longer None once Buy/Reject/etc. is applied -- see
        ReviewQueueService.resolve_item) or manually corrected (a human
        choosing to keep something tagged OA/unclear on purpose is not
        the same as it sitting neglected). Pure DB write, zero Keepa
        cost -- safe to run every scheduler tick regardless of token
        budget, same as WatchlistService.prune_stale_auto_adds.

        Returns how many rows were archived.
        """
        db = SessionLocal()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
            stale = (
                db.query(SellerNewListing)
                .filter(SellerNewListing.sourcing_tag == "OA / unclear")
                .filter(SellerNewListing.dismissed == False)  # noqa: E712
                .filter(SellerNewListing.manually_classified == False)  # noqa: E712
                .filter(SellerNewListing.review.is_(None))
                .filter(SellerNewListing.detected_at < cutoff)
                .all()
            )
            for listing in stale:
                listing.dismissed = True
            db.commit()
            return len(stale)
        finally:
            db.close()

    # ---- Tracked sellers CRUD (Competitors page) ----

    @staticmethod
    def list_tracked_sellers():
        db = SessionLocal()

        try:
            return db.query(TrackedSeller).order_by(TrackedSeller.created_at.desc()).all()
        finally:
            db.close()

    @staticmethod
    def add_tracked_seller(seller_id: str, nickname: str = ""):
        db = SessionLocal()

        try:
            seller_id = seller_id.strip()
            nickname = nickname.strip()

            existing = db.query(TrackedSeller).filter(TrackedSeller.seller_id == seller_id).first()

            if existing:
                # Re-adding an existing seller_id reactivates + relabels
                # rather than creating a duplicate row -- e.g. the user
                # paused it earlier and is now turning it back on.
                existing.active = True
                if nickname:
                    existing.nickname = nickname
                db.commit()
                return

            db.add(TrackedSeller(seller_id=seller_id, nickname=nickname))
            db.commit()
        finally:
            db.close()

    @staticmethod
    def set_active(tracked_seller_id: int, active: bool):
        db = SessionLocal()

        try:
            seller = db.get(TrackedSeller, tracked_seller_id)
            if seller:
                seller.active = active
                db.commit()
        finally:
            db.close()

    @staticmethod
    def remove_tracked_seller(tracked_seller_id: int):
        """
        Deletes the tracked seller AND its detection history
        (seller_new_listings) -- use set_active(False) instead if you
        just want to stop checking without losing past detections.
        """
        db = SessionLocal()

        try:
            db.query(SellerNewListing).filter(
                SellerNewListing.tracked_seller_id == tracked_seller_id
            ).delete()

            seller = db.get(TrackedSeller, tracked_seller_id)
            if seller:
                db.delete(seller)

            db.commit()
        finally:
            db.close()

    @staticmethod
    def get_seller_stats() -> dict:
        """
        {tracked_seller_id: {"total": N, "last_24h": N}} -- total new
        listings ever detected, and how many in the last 24h, per
        tracked seller. Powers the tracked-sellers table's summary
        columns without a separate query per row.
        """
        db = SessionLocal()

        try:
            # detected_at comes back from SQLite timezone-NAIVE even
            # though it was written via datetime.now(timezone.utc) --
            # confirmed the same way ProductRepository.list_latest's
            # today_only filter already had to work around this.
            # cutoff must be naive too, or comparing them raises "can't
            # compare offset-naive and offset-aware datetimes".
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).replace(tzinfo=None)
            rows = db.query(
                SellerNewListing.tracked_seller_id, SellerNewListing.detected_at
            ).all()

            stats = {}

            for tracked_seller_id, detected_at in rows:
                entry = stats.setdefault(tracked_seller_id, {"total": 0, "last_24h": 0})
                entry["total"] += 1

                if detected_at and detected_at >= cutoff:
                    entry["last_24h"] += 1

            return stats
        finally:
            db.close()

    @staticmethod
    def _apply_common_filters(query, buyable_only: bool, review_filter: str,
                               category: str, since_days: int, include_dismissed: bool):
        """
        Filters shared by list_detections and get_detection_counts --
        everything EXCEPT sourcing_tag itself, since that's the one
        dimension tabs switch on and counts need to reflect "how many
        would show in each tab under today's other filters", not just
        the currently-active one.
        """
        if not include_dismissed:
            query = query.filter(SellerNewListing.dismissed == False)

        if buyable_only:
            query = query.filter(SellerNewListing.currently_buyable == True)

        if review_filter == "unreviewed":
            query = query.filter(SellerNewListing.review.is_(None))
        elif review_filter == "reviewed":
            query = query.filter(SellerNewListing.review.isnot(None))

        if category:
            query = query.filter(func.lower(ProductRecord.category_name) == category.lower())

        if since_days:
            # detected_at comes back timezone-NAIVE from SQLite even
            # though it was written via datetime.now(timezone.utc) --
            # same workaround as get_seller_stats' cutoff above.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).replace(tzinfo=None)
            query = query.filter(SellerNewListing.detected_at >= cutoff)

        return query

    @staticmethod
    def _hot_counts(db, asins: list) -> dict:
        """
        {asin: distinct_competitor_count} for whichever of the given
        ASINs have 2+ distinct tracked sellers detecting them
        (non-dismissed) within HOT_WINDOW_DAYS -- see that constant's
        docstring. Scoped to `asins` (the current page's rows) rather
        than the whole table, so this stays cheap regardless of how
        much history has piled up.
        """
        if not asins:
            return {}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=HOT_WINDOW_DAYS)).replace(tzinfo=None)

        rows = (
            db.query(SellerNewListing.asin, SellerNewListing.tracked_seller_id)
            .filter(SellerNewListing.asin.in_(asins))
            .filter(SellerNewListing.dismissed == False)
            .filter(SellerNewListing.detected_at >= cutoff)
            .distinct()
            .all()
        )

        counts = {}
        for asin, tracked_seller_id in rows:
            counts[asin] = counts.get(asin, 0) + 1

        return {asin: count for asin, count in counts.items() if count >= MIN_HOT_COMPETITORS}

    @staticmethod
    def distinct_seller_counts(asins: list) -> dict:
        """
        {asin: distinct_competitor_count} -- how many distinct tracked
        sellers have EVER (non-dismissed, no recency window) been
        detected selling this ASIN. Deliberately NOT _hot_counts above
        (windowed to HOT_WINDOW_DAYS and floored at MIN_HOT_COMPETITORS
        for the Competitors page's "hot lead" badge) -- this is a plain
        historical tally, built for OA Source Discovery's results table
        (2026-08-19), where "more than one competitor has this in their
        inventory" is itself the signal worth highlighting regardless
        of when each one was last seen -- a competitor doesn't stop
        having sourced something just because it's been longer than
        HOT_WINDOW_DAYS since detection.

        Opens its own session (unlike _hot_counts, which is always
        called from inside list_detections' already-open session) --
        callers outside this file (e.g. OA Source Discovery's route)
        have no session of their own to pass in.
        """
        if not asins:
            return {}

        db = SessionLocal()

        try:
            rows = (
                db.query(SellerNewListing.asin, func.count(func.distinct(SellerNewListing.tracked_seller_id)))
                .filter(SellerNewListing.asin.in_(asins))
                .filter(SellerNewListing.dismissed == False)
                .group_by(SellerNewListing.asin)
                .all()
            )
            return {asin: count for asin, count in rows}
        finally:
            db.close()

    @staticmethod
    def list_detections(sourcing_tag: str = None, buyable_only: bool = False,
                         include_dismissed: bool = False, review_filter: str = None,
                         category: str = None, since_days: int = None, limit: int = 200):
        """
        Reverse-chronological detections feed for the Competitors page.
        Joins in each detection's tracked seller and (if scored) linked
        ProductRecord up front, batched, rather than one query per row
        on the template side.

        review_filter: None applies no filtering. "unreviewed" keeps
        only detections with no thumbs verdict yet. "reviewed" keeps
        only ones that have one (either up or down).

        category: exact (case-insensitive) match against the linked
        ProductRecord's category_name -- an unscored detection (no
        linked record) never matches a category filter, same as it
        never matches a sourcing_tag filter today.

        since_days: keeps only detections first seen in the last N
        days -- e.g. 3 for "added in the last 3 days", regardless of
        which competitor found them.
        """
        db = SessionLocal()

        try:
            query = (
                db.query(SellerNewListing, TrackedSeller)
                .join(TrackedSeller, SellerNewListing.tracked_seller_id == TrackedSeller.id)
            )

            if category:
                query = query.join(ProductRecord, SellerNewListing.product_record_id == ProductRecord.id)

            query = SellerWatchService._apply_common_filters(
                query, buyable_only, review_filter, category, since_days, include_dismissed
            )

            if sourcing_tag:
                query = query.filter(SellerNewListing.sourcing_tag == sourcing_tag)

            rows = query.order_by(SellerNewListing.detected_at.desc()).limit(limit).all()

            record_ids = [listing.product_record_id for listing, _ in rows if listing.product_record_id]
            records_by_id = {}

            if record_ids:
                records = db.query(ProductRecord).filter(ProductRecord.id.in_(record_ids)).all()
                records_by_id = {r.id: r for r in records}

            hot_counts = SellerWatchService._hot_counts(db, [listing.asin for listing, _ in rows])

            return [
                {
                    "listing": listing,
                    "seller": seller,
                    "record": records_by_id.get(listing.product_record_id),
                    "hot_count": hot_counts.get(listing.asin, 0),
                }
                for listing, seller in rows
            ]
        finally:
            db.close()

    @staticmethod
    def get_detection_counts(buyable_only: bool = False, review_filter: str = None,
                              category: str = None, since_days: int = None) -> dict:
        """
        {sourcing_tag: count} across ALL 4 tags plus "unscored" (NULL),
        under every filter EXCEPT sourcing_tag itself -- powers the
        Competitors page's tab badges, so each tab shows how many rows
        it holds under today's other filters before you click it.
        """
        db = SessionLocal()

        try:
            query = db.query(SellerNewListing.sourcing_tag, func.count(SellerNewListing.id))

            if category:
                query = query.join(ProductRecord, SellerNewListing.product_record_id == ProductRecord.id)

            query = SellerWatchService._apply_common_filters(
                query, buyable_only, review_filter, category, since_days, include_dismissed=False
            )

            rows = query.group_by(SellerNewListing.sourcing_tag).all()

            counts = {tag: 0 for tag in SOURCING_TAG_BY_TAB.values()}
            counts["unscored"] = 0

            for tag, count in rows:
                counts[tag if tag else "unscored"] = count

            return counts
        finally:
            db.close()

    @staticmethod
    def list_detection_categories() -> list:
        """
        Distinct, non-empty category names among scored (non-dismissed)
        detections -- powers the Competitors page's category filter
        dropdown. Deliberately scoped to detections only (not every
        category ProductRecord has ever seen), so the list doesn't fill
        up with categories no competitor has actually surfaced.
        """
        db = SessionLocal()

        try:
            rows = (
                db.query(ProductRecord.category_name)
                .join(SellerNewListing, SellerNewListing.product_record_id == ProductRecord.id)
                .filter(SellerNewListing.dismissed == False)
                .filter(ProductRecord.category_name != "")
                .distinct()
                .all()
            )

            return sorted({r[0] for r in rows})
        finally:
            db.close()

    @staticmethod
    def list_notable_buyable(limit: int = 200):
        """
        Competitor detections that are BOTH live-buyable right now AND
        clear the same "worth a look" bar as a normal scan lead (see
        ProductRepository.is_notable) -- feeds the unified Review Queue
        (see ReviewQueueService), so a good buy found via a competitor
        gets treated exactly like one found via a regular scan.

        Requires a linked, unreviewed ProductRecord (an unscored
        detection has nothing to judge "notable" against, so it can't
        qualify) and an unreviewed SellerNewListing -- reviewing either
        side removes it from here, same as /review/set's dual-update.
        """
        db = SessionLocal()

        try:
            rows = (
                db.query(SellerNewListing, TrackedSeller, ProductRecord)
                .join(TrackedSeller, SellerNewListing.tracked_seller_id == TrackedSeller.id)
                .join(ProductRecord, SellerNewListing.product_record_id == ProductRecord.id)
                .filter(SellerNewListing.currently_buyable == True)
                .filter(SellerNewListing.dismissed == False)
                .filter(SellerNewListing.review.is_(None))
                .filter(ProductRecord.review.is_(None))
                .order_by(SellerNewListing.detected_at.desc())
                .limit(limit)
                .all()
            )

            return [
                {"listing": listing, "seller": seller, "record": record}
                for listing, seller, record in rows
                if ProductRepository.is_notable(
                    record.recommendation, record.monthly_sales, record.roi, record.roi_90d,
                    record.sales_drops_30d,
                )
            ]
        finally:
            db.close()

    @staticmethod
    def list_historical_a2a_not_buyable(limit: int = 200):
        """
        Real EU/UK A2A historical sourcing evidence (see
        SourcingClassifier), but NOT currently buyable -- the exact
        gap the unified Review Queue build's audit found: list_notable_
        buyable above only ever surfaces currently_buyable==True
        listings, so a competitor's genuine historical opportunity
        whose original source has since dried up (or never became
        buyable today under a DIFFERENT marketplace) never reached the
        Review Queue AT ALL, regardless of anything in
        ReviewQueueService's own priority logic downstream.
        atlas-review-queue-backend-v1.md's follow-up build (section 8):
        "an historical A2A classification can remain valid even when
        there is currently no buyable offer... that should become
        NEEDS ATTENTION... rather than incorrectly turning it into OA."

        Deliberately NOT gated on ProductRepository.is_notable (that
        bar is about "worth buying right now" -- ROI/sales evidence at
        TODAY's price -- which doesn't apply to a purely historical
        finding) -- gated on having a real sourcing_tag instead, which
        IS itself the evidence bar (SourcingClassifier only ever sets
        "EU A2A"/"UK A2A" off genuine day-level ROI/dip evidence, see
        its own docstring). No new profitability calculation.
        """
        db = SessionLocal()

        try:
            rows = (
                db.query(SellerNewListing, TrackedSeller, ProductRecord)
                .join(TrackedSeller, SellerNewListing.tracked_seller_id == TrackedSeller.id)
                .join(ProductRecord, SellerNewListing.product_record_id == ProductRecord.id)
                .filter(SellerNewListing.currently_buyable == False)
                .filter(SellerNewListing.sourcing_tag.in_(("EU A2A", "UK A2A")))
                .filter(SellerNewListing.dismissed == False)
                .filter(SellerNewListing.review.is_(None))
                .filter(ProductRecord.review.is_(None))
                # Not gated on is_notable (see docstring above) but a
                # frequently-returned item is excluded here too (Tamara,
                # 2026-09-11) -- the demand-quality risk is timeless, not
                # tied to today's price the way is_notable's ROI bar is.
                .filter(ProductRecord.recommendation != "FREQUENTLY_RETURNED")
                .order_by(SellerNewListing.detected_at.desc())
                .limit(limit)
                .all()
            )

            return [{"listing": listing, "seller": seller, "record": record} for listing, seller, record in rows]
        finally:
            db.close()

    @staticmethod
    def dismiss_detection(listing_id: int):
        db = SessionLocal()

        try:
            listing = db.get(SellerNewListing, listing_id)
            if listing:
                listing.dismissed = True
                db.commit()
        finally:
            db.close()

    @staticmethod
    def set_review(listing_id: int, verdict: str | None, reason: str | None = None, reason_category: str | None = None):
        """
        verdict: "up", "down", or None to clear. Applies to this
        specific detection row (not the underlying product generally --
        see the review column's comment on SellerNewListing).

        reason: optional free-text "why not", only meaningful alongside
        verdict="down" -- see SellerNewListing.review_reason's own
        comment.

        reason_category: optional structured reason (see
        review_queue_service.REVIEW_REASON_CATEGORIES) -- additive
        alongside `reason`, never a replacement for it.
        """
        db = SessionLocal()

        try:
            listing = db.get(SellerNewListing, listing_id)
            if listing:
                listing.review = verdict
                listing.review_reason = reason
                listing.review_reason_category = reason_category
                db.commit()
        finally:
            db.close()

    @staticmethod
    def set_review_bulk(listing_ids: list, verdict: str | None):
        """Bulk form of set_review -- one commit for the whole batch."""
        unique_ids = {i for i in listing_ids if i}

        if not unique_ids:
            return

        db = SessionLocal()

        try:
            for listing_id in unique_ids:
                listing = db.get(SellerNewListing, listing_id)
                if listing:
                    listing.review = verdict

            db.commit()
        finally:
            db.close()

    # ---- Competitor Watch redesign (2026-09-03) -- Opportunities feed,
    # Source Finder and Competitor drill-down additions. Every method
    # below is a pure read/aggregation over existing SellerNewListing/
    # ProductRecord/OaSourceCandidate rows -- no schema change, no new
    # classification logic, no write to any of them. ----

    @staticmethod
    def oa_price_guide_for_record(record) -> dict | None:
        """
        The OA price-guide breakeven/target pair for ONE ProductRecord
        (FeeEngine.max_source_cost against today's Amazon price/fees) --
        extracted from list_oa_worth_investigating below (2026-09-05,
        Review Queue OA view) so a single-ASIN lookup (ReviewQueueService.
        get_queue_item) can reuse the EXACT same computation without
        re-scanning the whole OA/unclear population just to find one row.
        None if there's no breakeven at all (Amazon's own price/fees
        leave no room to source this profitably via OA regardless of
        buy price) -- same "not worth surfacing" bar list_oa_worth_
        investigating already applies.
        """
        breakeven = FeeEngine.max_source_cost(
            record.buy_box_now, record.category_name, record.fba_fee, target_roi_pct=0.0,
        )
        if breakeven <= 0:
            return None
        target = FeeEngine.max_source_cost(
            record.buy_box_now, record.category_name, record.fba_fee,
            target_roi_pct=FeeEngine.OA_TARGET_ROI_PCT,
        )
        return {"target": target, "breakeven": breakeven}

    @staticmethod
    def list_oa_worth_investigating(limit: int = 200):
        """
        OA/unclear detections where the SAME OA price-guide economics
        the old (pre-redesign, now removed) Competitors page already
        computed per-row (FeeEngine.max_source_cost against today's
        Amazon price/fees) show a genuine breakeven: Amazon's own
        price and fees leave ANY room to source
        this profitably via OA at all. This is what "OA -- worth
        investigating" means for the Opportunities feed's summary card
        -- deliberately NOT the raw OA/unclear count (585 detections
        today, most of which Amazon's own fees already rule out before
        a human ever needs to look).

        No new profitability calculation -- FeeEngine.max_source_cost
        is the exact same function/call already trusted elsewhere.
        """
        db = SessionLocal()

        try:
            rows = (
                db.query(SellerNewListing, TrackedSeller, ProductRecord)
                .join(TrackedSeller, SellerNewListing.tracked_seller_id == TrackedSeller.id)
                .join(ProductRecord, SellerNewListing.product_record_id == ProductRecord.id)
                .filter(SellerNewListing.sourcing_tag == "OA / unclear")
                .filter(SellerNewListing.dismissed == False)
                .filter(SellerNewListing.review.is_(None))
                .filter(ProductRecord.review.is_(None))
                .filter(ProductRecord.buy_box_now > 0)
                # A frequently-returned item is excluded here too (Tamara,
                # 2026-09-11) -- see list_historical_a2a_not_buyable's own
                # comment on why this isn't left to is_notable alone.
                .filter(ProductRecord.recommendation != "FREQUENTLY_RETURNED")
                .order_by(SellerNewListing.detected_at.desc())
                .limit(max(limit * 3, limit))  # over-fetch: not every row clears breakeven, trimmed below
                .all()
            )

            result = []
            for listing, seller, record in rows:
                oa_price_guide = SellerWatchService.oa_price_guide_for_record(record)
                if oa_price_guide is None:
                    continue
                result.append({
                    "listing": listing, "seller": seller, "record": record,
                    "oa_price_guide": oa_price_guide,
                })
                if len(result) >= limit:
                    break

            return result
        finally:
            db.close()

    @staticmethod
    def count_recent_detections(days: int = 7) -> int:
        """Non-dismissed detections first seen in the last `days` days, across every sourcing tag."""
        db = SessionLocal()

        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
            return (
                db.query(SellerNewListing)
                .filter(SellerNewListing.dismissed == False)
                .filter(SellerNewListing.detected_at >= cutoff)
                .count()
            )
        finally:
            db.close()

    @staticmethod
    def get_seller_breakdown() -> dict:
        """
        {tracked_seller_id: {total, eu_a2a, uk_a2a, wholesale, oa,
        unscored, last_7d, last_30d, buy_opportunities}} -- richer
        per-seller rollup for the Competitors drill-down, additive
        alongside get_seller_stats (total/last_24h, used elsewhere and
        left completely unchanged). buy_opportunities counts
        currently_buyable rows -- the exact same flag the existing
        per-row "BUY" badge already uses (see _competitors_opportunities.html),
        not a new/stronger bar. One pass over non-dismissed
        SellerNewListing rows, same cost profile as get_seller_stats.
        """
        db = SessionLocal()

        try:
            cutoff7 = (datetime.now(timezone.utc) - timedelta(days=7)).replace(tzinfo=None)
            cutoff30 = (datetime.now(timezone.utc) - timedelta(days=30)).replace(tzinfo=None)

            rows = (
                db.query(
                    SellerNewListing.tracked_seller_id, SellerNewListing.sourcing_tag,
                    SellerNewListing.detected_at, SellerNewListing.currently_buyable,
                )
                .filter(SellerNewListing.dismissed == False)
                .all()
            )

            breakdown = {}
            tag_key = {
                "EU A2A": "eu_a2a", "UK A2A": "uk_a2a",
                "Wholesale (likely)": "wholesale", "OA / unclear": "oa",
            }

            for tracked_seller_id, tag, detected_at, buyable in rows:
                entry = breakdown.setdefault(tracked_seller_id, {
                    "total": 0, "eu_a2a": 0, "uk_a2a": 0, "wholesale": 0, "oa": 0,
                    "unscored": 0, "last_7d": 0, "last_30d": 0, "buy_opportunities": 0,
                })
                entry["total"] += 1
                entry[tag_key.get(tag, "unscored")] += 1

                if detected_at and detected_at >= cutoff7:
                    entry["last_7d"] += 1
                if detected_at and detected_at >= cutoff30:
                    entry["last_30d"] += 1
                if buyable:
                    entry["buy_opportunities"] += 1

            return breakdown
        finally:
            db.close()

    @staticmethod
    def get_seller_marketplace_pattern(tracked_seller_id: int) -> dict | None:
        """
        Most common EU A2A source marketplace among this seller's
        CURRENT EU-A2A-tagged detections (reasoning.marketplace, set by
        SourcingClassifier -- see that module) -- an ATLAS INFERENCE
        from price-history evidence, never a confirmed sourcing
        relationship. None if this seller has no EU A2A detections to
        infer anything from (shown as "not enough evidence" by the
        template, never guessed).
        """
        db = SessionLocal()

        try:
            rows = (
                db.query(SellerNewListing.sourcing_reasoning_json)
                .filter(SellerNewListing.tracked_seller_id == tracked_seller_id)
                .filter(SellerNewListing.sourcing_tag == "EU A2A")
                .filter(SellerNewListing.dismissed == False)
                .all()
            )

            counts = {}
            for (reasoning_json,) in rows:
                if not reasoning_json:
                    continue
                try:
                    reasoning = json.loads(reasoning_json)
                except Exception:
                    continue
                marketplace = reasoning.get("marketplace")
                if marketplace:
                    counts[marketplace] = counts.get(marketplace, 0) + 1

            if not counts:
                return None

            best_marketplace, best_count = max(counts.items(), key=lambda kv: kv[1])
            return {"marketplace": best_marketplace, "count": best_count, "of_total": sum(counts.values())}
        finally:
            db.close()

    @staticmethod
    def get_seller_oa_retailer_patterns(tracked_seller_id: int) -> list:
        """
        Retailer domains OA Source Discovery has ACTUALLY found for
        this seller's OA/unclear detections -- joins on ASIN against
        OaSourceCandidate, which only has rows for ASINs a Source
        Finder run has genuinely searched (see OaSourceDiscoveryService.
        run_batch). Empty list -- never fabricated -- for a seller whose
        OA finds haven't been investigated yet; the template shows "Not
        yet investigated" for that case rather than implying Atlas has
        looked at every OA discovery.
        """
        db = SessionLocal()

        try:
            oa_asins = [
                row[0] for row in
                db.query(SellerNewListing.asin)
                .filter(SellerNewListing.tracked_seller_id == tracked_seller_id)
                .filter(SellerNewListing.sourcing_tag == "OA / unclear")
                .filter(SellerNewListing.dismissed == False)
                .distinct()
                .all()
            ]

            if not oa_asins:
                return []

            rows = (
                db.query(OaSourceCandidate.retailer_domain, func.count(OaSourceCandidate.id))
                .filter(OaSourceCandidate.asin.in_(oa_asins))
                .filter(OaSourceCandidate.retailer_domain != "")
                .group_by(OaSourceCandidate.retailer_domain)
                .order_by(func.count(OaSourceCandidate.id).desc())
                .all()
            )

            return [{"domain": domain, "count": count} for domain, count in rows]
        finally:
            db.close()
