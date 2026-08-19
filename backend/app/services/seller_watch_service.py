import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.database.database import SessionLocal
from app.database.models import TrackedSeller, SellerNewListing, ProductRecord
from app.keepa.client import get_keepa_client
from app.models.product import Product
from app.services.brand_scan_service import BrandScanService
from app.services.product_repository import ProductRepository
from app.services.sourcing_classifier import SourcingClassifier
from app.services.activity_log import ActivityLog

# How many DISTINCT ASINs to send to BrandScanService.scan() per Keepa
# call within one reclassify_all() run -- same chunking spirit as
# ReplenService.CHECK_BATCH_SIZE, sized so a single low-token stop
# doesn't waste a huge partially-fetched batch.
RECLASSIFY_BATCH_SIZE = 15

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
        """
        api = get_keepa_client()
        results = {}

        for seller_id in seller_ids:
            try:
                try:
                    response = api.seller_query(seller_id, domain="GB", storefront=True, wait=False)
                except TypeError:
                    # Installed keepa version doesn't accept wait= here --
                    # same fallback ProductFinder.find_brand already uses
                    # for product_finder().
                    response = api.seller_query(seller_id, domain="GB", storefront=True)
            except Exception as exc:
                print(f"Seller storefront lookup failed for {seller_id}: {exc}")
                continue

            results.update(response or {})

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
    def run_check() -> dict:
        """
        One full pass over every active tracked seller. Returns a
        small summary dict for logging/status, same convention as
        ScanQueueService.run_next_tick.
        """
        db = SessionLocal()

        try:
            tracked = db.query(TrackedSeller).filter(TrackedSeller.active == True).all()

            if not tracked:
                return {"checked": 0, "new_listings": 0}

            storefronts = SellerWatchService._fetch_storefronts(
                [t.seller_id for t in tracked]
            )

            scanner = BrandScanService()
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
                    # still won't have a product dict to classify
                    # against (missing EU source alone no longer drops
                    # it, see include_no_eu_source above).
                    scored_by_asin = {
                        opp["product"]["asin"]: opp["product"]
                        for opp in scan_result.get("opportunities", [])
                    }
                    record_ids = ProductRepository.get_latest_record_ids(new_asins_list)

                    for asin in new_asins_list:
                        product_dict = scored_by_asin.get(asin)
                        sourcing_tag = None
                        currently_buyable = False
                        reasoning_json = None

                        if product_dict:
                            product = Product(**product_dict)
                            brand_repeat = SellerWatchService._brand_repeat_count(
                                db, seller.id, product.brand
                            )
                            classification = SourcingClassifier.classify(
                                product, brand_repeat_count=brand_repeat
                            )
                            sourcing_tag = classification.sourcing_tag
                            currently_buyable = classification.currently_buyable
                            reasoning_json = json.dumps(classification.reasoning)

                        db.add(SellerNewListing(
                            tracked_seller_id=seller.id,
                            asin=asin,
                            product_record_id=record_ids.get(asin),
                            sourcing_tag=sourcing_tag,
                            currently_buyable=currently_buyable,
                            sourcing_reasoning_json=reasoning_json,
                        ))

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

            scanner = BrandScanService()
            scan_result = scanner.scan(
                brand="competitor-rescan",
                asins=asins_list,
                limit=len(asins_list),
                include_no_eu_source=True,
                force_rescan=True,
            )

            scored_by_asin = {
                opp["product"]["asin"]: opp["product"]
                for opp in scan_result.get("opportunities", [])
            }
            record_ids = ProductRepository.get_latest_record_ids(asins_list)

            updated = 0

            for listing in unscored:
                product_dict = scored_by_asin.get(listing.asin)
                if not product_dict:
                    continue

                product = Product(**product_dict)
                brand_repeat = SellerWatchService._brand_repeat_count(
                    db, listing.tracked_seller_id, product.brand
                )
                classification = SourcingClassifier.classify(
                    product, brand_repeat_count=brand_repeat
                )

                listing.product_record_id = record_ids.get(listing.asin)
                listing.sourcing_tag = classification.sourcing_tag
                listing.currently_buyable = classification.currently_buyable
                listing.sourcing_reasoning_json = json.dumps(classification.reasoning)
                updated += 1

            db.commit()

            return {"rescanned": len(asins_list), "updated": updated}
        finally:
            db.close()

    @staticmethod
    def reclassify_all() -> dict:
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

        Processes oldest-reclassified-first (NULL -- never done --
        first), one Keepa scan() call per RECLASSIFY_BATCH_SIZE
        distinct ASINs, committing after each batch. Stops as soon as
        a batch comes back with scan()'s low-token error rather than
        waiting/blocking, so a single call stays bounded (safe to run
        from an HTTP request) -- the caller just calls this again
        (e.g. clicking the button again once tokens refill) to
        continue from where it left off, same resume pattern as
        ReplenService._run_check.

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

            scanner = BrandScanService()
            processed_asins = 0
            updated = 0
            flipped = 0
            stopped_early = False
            tokens_remaining = None

            for i in range(0, len(distinct_asins), RECLASSIFY_BATCH_SIZE):
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
                    opp["product"]["asin"]: opp["product"]
                    for opp in scan_result.get("opportunities", [])
                }
                record_ids = ProductRepository.get_latest_record_ids(batch_asins)
                now = datetime.now(timezone.utc)

                for asin in batch_asins:
                    product_dict = scored_by_asin.get(asin)

                    for listing in rows_by_asin[asin]:
                        listing.sourcing_reclassified_at = now

                        if not product_dict:
                            continue

                        previous_tag = listing.sourcing_tag
                        product = Product(**product_dict)
                        brand_repeat = SellerWatchService._brand_repeat_count(
                            db, listing.tracked_seller_id, product.brand
                        )
                        classification = SourcingClassifier.classify(
                            product, brand_repeat_count=brand_repeat
                        )

                        listing.product_record_id = record_ids.get(asin) or listing.product_record_id
                        listing.sourcing_tag = classification.sourcing_tag
                        listing.currently_buyable = classification.currently_buyable
                        listing.sourcing_reasoning_json = json.dumps(classification.reasoning)
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
    def set_review(listing_id: int, verdict: str | None):
        """
        verdict: "up", "down", or None to clear. Applies to this
        specific detection row (not the underlying product generally --
        see the review column's comment on SellerNewListing).
        """
        db = SessionLocal()

        try:
            listing = db.get(SellerNewListing, listing_id)
            if listing:
                listing.review = verdict
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
