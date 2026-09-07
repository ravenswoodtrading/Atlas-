from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import func

from app.database.database import SessionLocal
from app.database.models import (
    ProductRecord, KnownProduct, WatchedProduct, ExcludedProduct, ExcludedCategory,
    GatedBrand, ExcludedBrand, SignalQuery, SignalMatch, CeilingRejected,
)



class ProductRepository:
    """
    Handles saving opportunity scan results to the database and
    reading them back. Manages its own DB session per call, since
    this is used from services (BrandScanService) rather than routes
    that have FastAPI's Depends(get_db) available.
    """

    # sales_drops_30d (Keepa's salesRankDrops30) at or above this
    # counts as evidence of sales when monthly_sales has no confirmed
    # figure -- high enough to filter out a single stray rank
    # fluctuation, low enough to still catch real-but-thin velocity.
    SALES_DROPS_NOTABLE_THRESHOLD = 3

    @staticmethod
    def is_notable(recommendation: str, monthly_sales: int, roi: float, roi_90d: float,
                    sales_drops_30d: int = 0) -> bool:
        """
        "Worth a look" bar shared by list_latest's "notable" filter,
        the Dashboard's star-buy/BUY counters, and
        SellerWatchService.list_notable_buyable (for competitor
        detections) -- extracted so all three stay provably in sync
        rather than each keeping their own copy of the same rule.
        Confirmed sales (or, absent that, 3+ rank drops in 30d as a
        proxy) + 25%+ ROI, OR a BUY recommendation outright. Raised
        from 18% to 25% -- 18% wasn't a high enough bar for what
        counts as a genuinely good lead.

        The ROI-only branch used to fire regardless of
        OpportunityEngine's own recommendation, so a product it had
        already tagged IGNORE (score < 65 or confidence < 60 --
        see OpportunityEngine.analyse) could still leak into the
        Review Queue just because ROI on one of today's/90d crossed
        25%. Requiring recommendation != "IGNORE" here keeps the
        queue limited to what OpportunityEngine itself considers at
        least CONSIDER-worthy, which is what the Review Queue's own
        "star buy or BUY recommendation" description promises.

        GATED is excluded the same way -- a gated-brand product can
        score brilliantly (see OpportunityEngine's product.gated
        override) but isn't actionable right now, so it must never
        count toward is_notable/the Review Queue/Discord/the Dashboard
        BUY-CONSIDER counters. It has its own dedicated view instead
        (see ProductRepository.list_gated_opportunities /
        the Gated Brand Opportunities page).

        LOW_CONFIDENCE (2026-09-03) is excluded explicitly for the same
        reason, and deliberately NOT left to chance the way PEAK_WINDOW
        originally was -- PEAK_WINDOW isn't listed here either, but was
        never SUPPOSED to satisfy is_notable (see its own "never affects
        is_notable/Discord's own trust bar" comment in
        ReviewQueueService.list_leads), and a real bug confirmed a
        PEAK_WINDOW record with strong regular ROI could slip through
        the ROI-only branch below anyway and get double-counted. A
        LOW_CONFIDENCE record often has real ROI (that's exactly why
        it's visible at all instead of IGNORE), so without this explicit
        exclusion it would hit the exact same leak -- pinging Discord
        and counting as a full-trust star buy despite the whole point of
        the tier being "this needs a human's judgment on the
        confidence penalty first".

        LOW_SCORE (2026-09-03) is excluded for the identical reason --
        it also often carries real ROI (that's exactly why it's visible
        at all instead of IGNORE), and the whole point of the tier is
        "the composite score is weak, look at the actual score factors
        before trusting it", not full-trust-star-buy treatment.
        """
        if recommendation in ("IGNORE", "GATED", "LOW_CONFIDENCE", "LOW_SCORE"):
            return False

        has_sales_evidence = monthly_sales > 0 or sales_drops_30d >= ProductRepository.SALES_DROPS_NOTABLE_THRESHOLD
        return recommendation == "BUY" or (has_sales_evidence and (roi > 25 or roi_90d > 25))

    @staticmethod
    def save_opportunity(product_dict: dict, report_dict: dict, brand_query: str):
        db = SessionLocal()

        try:
            record = ProductRecord(
                asin=product_dict.get("asin") or "",
                title=product_dict.get("title") or "",
                brand=product_dict.get("brand") or "",
                category=product_dict.get("category") or "",
                ean=product_dict.get("ean") or "",
                image=product_dict.get("image") or "",
                brand_query=brand_query,
                buy_box_now=product_dict.get("buy_box_now") or 0.0,
                buy_box_90d=product_dict.get("buy_box_90d") or 0.0,
                best_source_marketplace=product_dict.get("best_source_marketplace") or "",
                best_source_cost_gbp=product_dict.get("best_source_cost_gbp") or 0.0,
                fba_fee=product_dict.get("fba_fee") or 0.0,
                referral_fee=product_dict.get("referral_fee") or 0.0,
                profit=product_dict.get("profit") or 0.0,
                roi=product_dict.get("roi") or 0.0,
                profit_90d=product_dict.get("profit_90d") or 0.0,
                roi_90d=product_dict.get("roi_90d") or 0.0,
                category_name=product_dict.get("category_name") or "",
                referral_rate_used=product_dict.get("referral_rate_used") or 0.0,
                uk_vat_rate_used=product_dict.get("uk_vat_rate_used") or 0.0,
                eu_vat_rate_used=product_dict.get("eu_vat_rate_used") or 0.0,
                score=report_dict.get("score") or 0,
                confidence=report_dict.get("confidence") or 0,
                recommendation=report_dict.get("recommendation") or "",
                monthly_sales=product_dict.get("monthly_sales") or 0,
                monthly_sales_as_of=product_dict.get("monthly_sales_as_of"),
                sales_drops_30d=product_dict.get("sales_drops_30d") or 0,
                report_json=json.dumps(report_dict),
            )
            db.add(record)
            db.commit()

        except Exception as exc:
            db.rollback()
            # A failed save shouldn't break the scan itself -- just log it.
            print(f"Failed to save product record for {product_dict.get('asin')}: {exc}")

        finally:
            db.close()

    # Each maps a `sort` query value to (key function, reverse) -- reverse=True
    # means highest-first, which is what every option except cost wants.
    SORT_OPTIONS = {
        "scanned_desc": (lambda r: r.scanned_at, True),
        "score_desc": (lambda r: r.score, True),
        "profit_desc": (lambda r: max(r.profit, r.profit_90d), True),
        "roi_desc": (lambda r: max(r.roi, r.roi_90d), True),
        "monthly_sales_desc": (lambda r: r.monthly_sales, True),
        "cost_asc": (lambda r: r.best_source_cost_gbp if r.best_source_cost_gbp > 0 else float("inf"), False),
    }

    @staticmethod
    def get_latest_per_asin() -> list:
        """
        The most recent ProductRecord per ASIN, newest-first -- the
        same "load everything, dedupe in Python" work list_latest()
        below does internally, pulled out so a caller that needs to
        run several DIFFERENT review_filter passes in a row (see
        ReviewQueueService.list_leads/list_consider_leads) can compute
        it ONCE and pass the result into list_latest()'s own
        `latest_records` parameter, instead of each pass independently
        re-querying and re-deduping the whole table (2026-09-04 perf
        fix -- profiled at 5-6 full-table reloads, ~90,000 ORM row
        hydrations, 9+ seconds for one Review Queue page load before
        this).

        Deliberately NOT cached/memoized here -- an earlier version of
        this fix used a short-TTL cross-request cache, but that broke
        "resolve an item, then immediately re-check" (a real test
        failure: test_unified_review_queue.py) because ProductRecord.
        review gets mutated from more than one place (ProductRepository.
        set_review/set_review_bulk, but ALSO eu_a2a_freshness_service.py
        marking a record "stale_auto" directly) -- chasing every write
        site to invalidate a global cache is fragile. Scoping reuse to
        a single caller's own call stack (an explicit parameter, not
        global state) gets the same win with no staleness risk at all.
        """
        db = SessionLocal()
        try:
            all_records = (
                db.query(ProductRecord)
                .order_by(ProductRecord.scanned_at.desc())
                .all()
            )

            seen = set()
            latest = []

            for record in all_records:
                if record.asin in seen:
                    continue

                seen.add(record.asin)
                latest.append(record)

            return latest
        finally:
            db.close()

    @staticmethod
    def list_latest(page: int = 1, page_size: int = 25, profitable_only: bool = None,
                     brand: str = None, sort: str = "scanned_desc", today_only: bool = None,
                     review_filter: str = None, latest_records: list = None):
        """
        Returns (records_for_this_page, total_count) using the most
        recent scan record per ASIN (not every historical row).

        profitable_only: None shows everything, True shows only
        products profitable today or at their 90-day typical price,
        False shows only the ones that aren't either way.

        brand: exact (case-insensitive) match against the product's
        real Keepa brand -- not brand_query, which is just the label
        of whatever scan/upload found it (e.g. an uploaded list name).

        sort: one of ProductRepository.SORT_OPTIONS. Falls back to
        scanned_desc (the previous, only, behaviour) if unrecognised.

        today_only: None applies no date filtering. True keeps only
        ASINs whose LATEST scan happened today (UTC calendar day, same
        as scanned_at's own timezone) -- powers the "Today's Scans"
        page. False keeps everything else -- powers "Historical
        Scans". An ASIN rescanned today only ever shows up in "today",
        never both, since this already only looks at the latest record
        per ASIN.

        review_filter: None applies no filtering. "notable" keeps only
        unreviewed star buys (confirmed sales + 25%+ ROI) OR unreviewed
        BUY recommendations, merged into one filter -- same two
        criteria the Dashboard alert counts. "consider" keeps only
        unreviewed CONSIDER-recommended items. "any" keeps every
        unreviewed item regardless of star/BUY/CONSIDER status, for
        finding anything you haven't looked at yet. "consider_worthwhile"
        is the Review Queue's "Consider" tab (2026-08-19, see
        ReviewQueueService.list_consider_leads) -- unreviewed CONSIDER
        leads that are profitable (today OR the 90-day typical price)
        with some sign of real sales, excluding anything is_notable()
        already claims (already shown on the main tab). Reviewing a
        Consider lead removes it from this list the same way as every
        other filter here -- the tab is just not EXPECTED to be
        cleared to zero regularly the way the main tab is; new
        profitable-but-not-star-tier leads keep landing on top as
        older ones get reviewed off. "peak" keeps unreviewed
        PEAK_WINDOW-recommended items (see OpportunityEngine.
        PEAK_WINDOW). "low_confidence" keeps unreviewed LOW_CONFIDENCE-
        recommended items (2026-09-03, see OpportunityEngine.analyse's
        LOW_CONFIDENCE branch) -- genuinely viable leads a confidence
        penalty alone knocked out of CONSIDER/BUY. "low_score" keeps
        unreviewed LOW_SCORE-recommended items (2026-09-03, see
        OpportunityEngine.analyse's LOW_SCORE branch) -- genuinely
        viable leads with real sales evidence that a weak composite
        score alone knocked out of CONSIDER/BUY.

        latest_records: optional pre-computed result of
        get_latest_per_asin() -- pass this when calling list_latest()
        several times in a row with different review_filter values
        (see that method's own docstring) to avoid re-querying/re-
        deduping the whole table on every call. None (the default)
        queries fresh, exactly as before -- every existing caller is
        unaffected.
        """
        if latest_records is not None:
            latest = list(latest_records)
        else:
            latest = ProductRepository.get_latest_per_asin()

        if profitable_only is True:
            latest = [r for r in latest if r.profit > 0 or r.profit_90d > 0]
        elif profitable_only is False:
            latest = [r for r in latest if r.profit <= 0 and r.profit_90d <= 0]

        if review_filter == "notable":
            latest = [
                r for r in latest if not r.review
                and ProductRepository.is_notable(
                    r.recommendation, r.monthly_sales, r.roi, r.roi_90d, r.sales_drops_30d
                )
            ]
        elif review_filter == "consider":
            latest = [r for r in latest if not r.review and r.recommendation == "CONSIDER"]
        elif review_filter == "peak":
            # Riskier "peak price window" leads -- see
            # OpportunityEngine.PEAK_WINDOW. Deliberately its own
            # filter, not folded into "notable" or "consider",
            # since is_notable's roi/roi_90d checks never see these
            # (they only clear the bar at the 90-day PEAK price).
            latest = [r for r in latest if not r.review and r.recommendation == "PEAK_WINDOW"]
        elif review_filter == "low_confidence":
            # Genuinely viable, CONSIDER-tier-scoring leads a
            # confidence penalty knocked out of CONSIDER/BUY -- see
            # OpportunityEngine.analyse's LOW_CONFIDENCE branch
            # (2026-09-03). Its own filter for the same reason
            # "peak" is: is_notable() explicitly excludes it (see
            # that method's own comment), so it would never surface
            # via "notable" no matter how good its ROI looks.
            latest = [r for r in latest if not r.review and r.recommendation == "LOW_CONFIDENCE"]
        elif review_filter == "low_score":
            # Genuinely viable leads with real sales evidence a weak
            # composite score knocked out of CONSIDER/BUY -- see
            # OpportunityEngine.analyse's LOW_SCORE branch
            # (2026-09-03). Same reasoning as "low_confidence" above.
            latest = [r for r in latest if not r.review and r.recommendation == "LOW_SCORE"]
        elif review_filter == "any":
            latest = [r for r in latest if not r.review]
        elif review_filter == "consider_worthwhile":
            latest = [
                r for r in latest
                if not r.review
                and r.recommendation == "CONSIDER"
                and (r.profit > 0 or r.profit_90d > 0)
                and (
                    r.monthly_sales > 0
                    or r.sales_drops_30d >= ProductRepository.SALES_DROPS_NOTABLE_THRESHOLD
                )
                and not ProductRepository.is_notable(
                    r.recommendation, r.monthly_sales, r.roi, r.roi_90d, r.sales_drops_30d
                )
            ]

        if brand:
            latest = [r for r in latest if r.brand.lower() == brand.lower()]

        if today_only is not None:
            # scanned_at is stored as UTC but comes back timezone-NAIVE
            # from SQLite (confirmed: tzinfo is None even though it was
            # written via datetime.now(timezone.utc)) -- this cutoff
            # must be naive too, or the comparison below raises
            # "can't compare offset-naive and offset-aware datetimes".
            start_of_today = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=None
            )

            if today_only:
                latest = [r for r in latest if r.scanned_at and r.scanned_at >= start_of_today]
            else:
                latest = [r for r in latest if not r.scanned_at or r.scanned_at < start_of_today]

        key, reverse = ProductRepository.SORT_OPTIONS.get(
            sort, ProductRepository.SORT_OPTIONS["scanned_desc"]
        )
        latest.sort(key=key, reverse=reverse)

        total_count = len(latest)

        start = max(page - 1, 0) * page_size
        end = start + page_size

        return latest[start:end], total_count

    @staticmethod
    def get_distinct_brands() -> list:
        """
        Distinct, non-empty real Keepa brands seen across every scan
        record (not just the latest per ASIN) -- populates the
        Products page brand filter dropdown.

        Deduped case-insensitively for display (Keepa/Amazon listings
        occasionally report the same real brand with different casing
        across products, e.g. "PHILIPS" vs "Philips") -- doesn't touch
        the underlying `brand` column, which stays exactly as Keepa
        reported it per record. The brand filter itself already
        compares case-insensitively, so whichever casing shows up here
        matches every variant regardless.
        """
        db = SessionLocal()

        try:
            rows = (
                db.query(ProductRecord.brand)
                .filter(ProductRecord.brand != "")
                .distinct()
                .order_by(ProductRecord.brand)
                .all()
            )

            seen_lower = set()
            deduped = []

            for row in rows:
                key = row[0].strip().lower()

                if key in seen_lower:
                    continue

                seen_lower.add(key)
                deduped.append(row[0])

            return sorted(deduped, key=str.lower)

        finally:
            db.close()

    @staticmethod
    def get_brand_performance(brand_queries: list) -> dict:
        """
        Per-brand success snapshot for the Scan Queue page -- lets you
        judge whether a brand is actually worth continuing to scan,
        not just how many pages it's walked. Keyed by the same
        normalized (stripped + lowercased) string ScanQueueItem.brand
        and ProductRecord.brand_query both already use, so it lines up
        directly with each queue row regardless of the real Keepa
        `brand` field's casing/variants.

        Counts are over the LATEST record per ASIN (not raw row
        count), same convention as get_summary_stats -- the queue now
        cycles continuously and rescans the same ASINs constantly, so
        raw row counts would keep inflating even when nothing new was
        actually found.
        """
        db = SessionLocal()

        try:
            normalized = [b.strip().lower() for b in brand_queries]

            if not normalized:
                return {}

            rows = (
                db.query(ProductRecord)
                .filter(ProductRecord.brand_query.in_(normalized))
                .order_by(ProductRecord.scanned_at.desc())
                .all()
            )

            latest_by_brand = {}  # {brand_query: {asin: latest record}}

            for record in rows:
                bucket = latest_by_brand.setdefault(record.brand_query, {})
                if record.asin not in bucket:
                    bucket[record.asin] = record

            result = {}

            for brand in normalized:
                records = list(latest_by_brand.get(brand, {}).values())
                scanned = len(records)
                buy = sum(1 for r in records if r.recommendation == "BUY")
                consider = sum(1 for r in records if r.recommendation == "CONSIDER")

                result[brand] = {
                    "scanned": scanned,
                    "buy": buy,
                    "consider": consider,
                    "hit_rate": round((buy + consider) / scanned * 100, 1) if scanned else None,
                }

            return result

        finally:
            db.close()

    @staticmethod
    def get_recently_scanned_asins(since_hours: int) -> set:
        """
        Returns the set of ASINs scanned by ANY campaign (a brand
        search, an uploaded list, Watchlist, Replen, a Scan Queue item
        -- anything with a different brand_query label) within the
        last `since_hours` hours -- used to skip re-spending tokens on
        products we already have fresh data for.

        Deliberately GLOBAL, not scoped to one brand_query label. It
        used to be scoped that way, but once Keepa data for an ASIN
        has been freshly fetched, it doesn't matter which campaign
        fetched it -- confirmed via direct inspection of real scan
        history that the same ASIN routinely gets found by several
        different campaigns (e.g. a brand search AND Replen AND the
        Scan Queue), and the old per-label scoping meant each one paid
        for its own "first" fetch of that ASIN within the same day
        regardless of how many times it had already been fetched
        moments earlier by a different campaign. Pass force_rescan=True
        on any individual scan to bypass this entirely when you
        genuinely want fresh data regardless of what's been checked
        recently.
        """
        db = SessionLocal()

        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

            rows = (
                db.query(ProductRecord.asin)
                .filter(ProductRecord.scanned_at >= cutoff)
                .all()
            )

            return {row[0] for row in rows}

        finally:
            db.close()

    @staticmethod
    def get_summary_stats():
        """
        Counts across the latest scan record per ASIN (not raw row
        count) -- so re-scanning the same ASIN over time doesn't
        inflate the numbers.
        """
        db = SessionLocal()

        try:
            recent = (
                db.query(ProductRecord)
                .order_by(ProductRecord.scanned_at.desc())
                .all()
            )

            seen = set()
            latest = []

            for record in recent:
                if record.asin in seen:
                    continue

                seen.add(record.asin)
                latest.append(record)

            # "This week" = the product's MOST RECENT scan (still
            # per-ASIN deduped via `latest` above) landed within the
            # last 7 days -- added 2026-08-19 for the Dashboard
            # redesign, replacing the old all-time total_buy/
            # total_consider tiles. An all-time count only ever grows
            # and stops being informative day to day (and blends
            # already-reviewed items in with new ones, duplicating
            # what the unreviewed-star-buys alert already answers
            # better); "found this week" is a genuine, moving signal.
            # total_buy/total_consider (all-time) are kept below too,
            # in case anything else ever wants the full-catalog figure
            # -- only the Dashboard's own display changed, not what's
            # computed here.
            week_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).replace(tzinfo=None)

            return {
                "total_scanned": len(latest),
                "total_profitable": sum(1 for r in latest if r.profit > 0),
                "total_buy": sum(1 for r in latest if r.recommendation == "BUY"),
                "total_consider": sum(1 for r in latest if r.recommendation == "CONSIDER"),
                "buy_this_week": sum(
                    1 for r in latest
                    if r.recommendation == "BUY" and r.scanned_at and r.scanned_at >= week_cutoff
                ),
                "consider_this_week": sum(
                    1 for r in latest
                    if r.recommendation == "CONSIDER" and r.scanned_at and r.scanned_at >= week_cutoff
                ),
            }

        finally:
            db.close()

    @staticmethod
    def get_known_products(asins: list) -> dict:
        """
        Batch lookup against known_products (imported from a CSV
        export) -- returns {asin: KnownProduct} for whichever of the
        given ASINs have been imported. Used to check category/brand
        exclusions BEFORE spending any Keepa tokens at all.
        """
        if not asins:
            return {}

        db = SessionLocal()

        try:
            rows = (
                db.query(KnownProduct)
                .filter(KnownProduct.asin.in_(asins))
                .all()
            )

            return {row.asin: row for row in rows}

        finally:
            db.close()

    # ---- Watchlist ----

    @staticmethod
    def add_watch(asin: str, title: str = "", brand: str = "", note: str = "", viable_days_90d: int = 0):
        db = SessionLocal()

        try:
            existing = db.get(WatchedProduct, asin)

            if existing:
                if title:
                    existing.title = title
                if brand:
                    existing.brand = brand
                if note:
                    existing.note = note
                if viable_days_90d:
                    existing.viable_days_90d = viable_days_90d
            else:
                db.add(WatchedProduct(
                    asin=asin, title=title, brand=brand, note=note, viable_days_90d=viable_days_90d,
                ))

            db.commit()

        finally:
            db.close()

    @staticmethod
    def remove_watch(asin: str):
        db = SessionLocal()

        try:
            existing = db.get(WatchedProduct, asin)

            if existing:
                db.delete(existing)
                db.commit()

        finally:
            db.close()

    @staticmethod
    def list_watched():
        db = SessionLocal()

        try:
            return (
                db.query(WatchedProduct)
                .order_by(WatchedProduct.watched_at.desc())
                .all()
            )

        finally:
            db.close()

    @staticmethod
    def get_watched_asins() -> set:
        db = SessionLocal()

        try:
            rows = db.query(WatchedProduct.asin).all()
            return {row[0] for row in rows}

        finally:
            db.close()

    # ---- User exclusions (separate from the static exclusions.py file --
    # this is DB-backed so it can be controlled from the page itself) ----

    @staticmethod
    def add_exclusion(asin: str, title: str = "", reason: str = ""):
        db = SessionLocal()

        try:
            existing = db.get(ExcludedProduct, asin)

            if existing:
                if title:
                    existing.title = title
                if reason:
                    existing.reason = reason
            else:
                db.add(ExcludedProduct(asin=asin, title=title, reason=reason))

            db.commit()

        finally:
            db.close()

    @staticmethod
    def add_exclusion_bulk(asins: list, reason: str = ""):
        """
        Bulk form of add_exclusion -- same upsert-by-asin behaviour,
        one commit for the whole batch. The Review Queue's bulk
        checkboxes only carry the ASIN (not a title), so the title is
        looked up here from each ASIN's most recent scan record --
        same source add_exclusion's callers normally pass in
        explicitly from page context.
        """
        unique_asins = {a.strip().upper() for a in asins if a}

        if not unique_asins:
            return

        db = SessionLocal()

        try:
            for asin in unique_asins:
                existing = db.get(ExcludedProduct, asin)

                if existing:
                    if reason:
                        existing.reason = reason
                    continue

                latest_record = (
                    db.query(ProductRecord)
                    .filter(ProductRecord.asin == asin)
                    .order_by(ProductRecord.scanned_at.desc())
                    .first()
                )
                title = latest_record.title if latest_record else ""

                db.add(ExcludedProduct(asin=asin, title=title, reason=reason))

            db.commit()

        finally:
            db.close()

    @staticmethod
    def remove_exclusion(asin: str):
        db = SessionLocal()

        try:
            existing = db.get(ExcludedProduct, asin)

            if existing:
                db.delete(existing)
                db.commit()

        finally:
            db.close()

    @staticmethod
    def list_exclusions():
        db = SessionLocal()

        try:
            return (
                db.query(ExcludedProduct)
                .order_by(ExcludedProduct.excluded_at.desc())
                .all()
            )

        finally:
            db.close()

    @staticmethod
    def get_known_products_catalog_stats() -> dict:
        """
        Summary of the imported known_products catalog (see
        KnownProduct's docstring) for the Exclusions page -- this data
        silently pre-filters ASINs before any Keepa token is spent
        (see is_excluded_by_name / BrandScanService.scan Step 1b), but
        KnownProduct.imported_at was previously captured on import and
        never shown anywhere, so a stale import (e.g. months-old brand
        catalog data, or a category that's since been re-organized on
        Amazon) could be silently pre-excluding real opportunities
        with no way to notice. oldest_imported_at is the more useful
        of the two for that purpose -- a single freshest import gives
        false reassurance if most of the catalog is actually much
        older.
        """
        db = SessionLocal()

        try:
            count = db.query(func.count(KnownProduct.asin)).scalar() or 0
            oldest = db.query(func.min(KnownProduct.imported_at)).scalar()
            newest = db.query(func.max(KnownProduct.imported_at)).scalar()

            return {
                "count": count,
                "oldest_imported_at": oldest,
                "newest_imported_at": newest,
            }

        finally:
            db.close()

    @staticmethod
    def get_excluded_asins() -> set:
        """
        Used as a zero-token pre-check before spending any tokens on a
        scan, same idea as the known_products category check.
        """
        db = SessionLocal()

        try:
            rows = db.query(ExcludedProduct.asin).all()
            return {row[0] for row in rows}

        finally:
            db.close()

    # ---- User category exclusions (DB-backed companion to the static
    # EXCLUDED_CATEGORIES/EXCLUDED_CATEGORY_NAMES sets in
    # app/config/exclusions.py -- see ExcludedCategory for why a row
    # can carry an ID, a name, or both) ----

    @staticmethod
    def add_category_exclusion(category_id: str = "", category_name: str = "", reason: str = ""):
        category_id = category_id.strip()
        category_name = category_name.strip()

        if not category_id and not category_name:
            return

        db = SessionLocal()

        try:
            db.add(ExcludedCategory(
                category_id=category_id or None,
                category_name=category_name or None,
                reason=reason,
            ))
            db.commit()

        finally:
            db.close()

    @staticmethod
    def remove_category_exclusion(exclusion_id: int):
        db = SessionLocal()

        try:
            existing = db.get(ExcludedCategory, exclusion_id)

            if existing:
                db.delete(existing)
                db.commit()

        finally:
            db.close()

    @staticmethod
    def list_category_exclusions():
        db = SessionLocal()

        try:
            return (
                db.query(ExcludedCategory)
                .order_by(ExcludedCategory.excluded_at.desc())
                .all()
            )

        finally:
            db.close()

    @staticmethod
    def get_excluded_category_ids() -> set:
        """
        Fetched ONCE per scan (see BrandScanService.scan Step 3) and
        passed into is_excluded(), same "one query, not one per ASIN"
        convention as get_excluded_asins().
        """
        db = SessionLocal()

        try:
            rows = db.query(ExcludedCategory.category_id).filter(
                ExcludedCategory.category_id.isnot(None)
            ).all()
            return {row[0] for row in rows}

        finally:
            db.close()

    @staticmethod
    def get_excluded_category_names() -> set:
        """
        Lower-cased, matching is_excluded_by_name()'s case-insensitive
        comparison against known_products' imported CSV category text.
        """
        db = SessionLocal()

        try:
            rows = db.query(ExcludedCategory.category_name).filter(
                ExcludedCategory.category_name.isnot(None)
            ).all()
            return {row[0].lower() for row in rows}

        finally:
            db.close()

    # ---- Gated brands (DB-backed companion to the static
    # GATED_BRAND_CATEGORIES set in app/config/exclusions.py -- see
    # GatedBrand's docstring for why this is a separate table/behaviour
    # from ExcludedCategory rather than reusing it) ----

    @staticmethod
    def add_gated_brand(brand: str = "", category_id: str = "", category_name: str = "", reason: str = ""):
        brand = brand.strip().lower()
        category_id = category_id.strip()
        category_name = category_name.strip()

        if not brand:
            return

        db = SessionLocal()

        try:
            db.add(GatedBrand(
                brand=brand,
                category_id=category_id or None,
                category_name=category_name or None,
                reason=reason,
            ))
            db.commit()

        finally:
            db.close()

    @staticmethod
    def remove_gated_brand(gated_id: int):
        db = SessionLocal()

        try:
            existing = db.get(GatedBrand, gated_id)

            if existing:
                db.delete(existing)
                db.commit()

        finally:
            db.close()

    @staticmethod
    def list_gated_brands():
        db = SessionLocal()

        try:
            return (
                db.query(GatedBrand)
                .order_by(GatedBrand.gated_at.desc())
                .all()
            )

        finally:
            db.close()

    @staticmethod
    def get_whole_gated_brand_names() -> set:
        """
        Brand names gated with NO category scoping (category_id AND
        category_name both blank) -- i.e. gated across the board.
        Used as the pre-token check in BrandScanService.scan Step 1
        that blocks brand-search-DRIVEN scanning entirely: no point
        spending a single Keepa token hunting for MORE of a brand you
        can't sell at all. A category-scoped gate can't be checked
        this early (no category is known until after the UK lookup),
        so those only ever apply at Step 3 via is_gated().
        """
        db = SessionLocal()

        try:
            rows = db.query(GatedBrand.brand).filter(
                GatedBrand.category_id.is_(None), GatedBrand.category_name.is_(None)
            ).all()
            return {row[0] for row in rows}  # already stored lower-cased

        finally:
            db.close()

    @staticmethod
    def get_gated_brand_pairs() -> set:
        """
        Fetched ONCE per scan (see BrandScanService.scan Step 3) and
        passed into is_gated(), same "one query, not one per ASIN"
        convention as get_excluded_category_ids(). Returns (brand,
        category_id or "") pairs -- "" meaning "whole brand, any
        category", same convention is_gated() expects.
        """
        db = SessionLocal()

        try:
            rows = db.query(GatedBrand.brand, GatedBrand.category_id).all()
            return {(brand, category_id or "") for brand, category_id in rows}

        finally:
            db.close()

    @staticmethod
    def get_gated_brand_pairs_by_name() -> set:
        """
        Same idea as get_gated_brand_pairs(), for is_gated_by_name()'s
        category-NAME matching (known_products' imported CSV text).
        """
        db = SessionLocal()

        try:
            rows = db.query(GatedBrand.brand, GatedBrand.category_name).all()
            return {(brand, (category_name or "").lower()) for brand, category_name in rows}

        finally:
            db.close()

    # ---- Excluded brands (2026-09-05) -- see ExcludedBrand's own
    # docstring for how this differs from gating: unconditional,
    # brand-wide, and removes any existing Scan Queue rows immediately
    # rather than leaving them to go stale in place. ----

    @staticmethod
    def add_brand_exclusion(brand: str, reason: str = ""):
        """
        Excludes the brand AND immediately removes any ScanQueueItem
        rows for it (deliberately different from gating's "leave it
        stuck in the queue for a human to notice" behaviour -- see
        ExcludedBrand's own docstring for why an explicit exclusion
        means "get it out now"). Import here, not at module level, to
        avoid a circular import (ScanQueueService already imports
        DiscoveryIntelligenceService lazily for the same reason).
        """
        from app.database.models import ScanQueueItem

        brand = brand.strip().lower()
        if not brand:
            return

        db = SessionLocal()
        try:
            if not db.query(ExcludedBrand).filter(ExcludedBrand.brand == brand).first():
                db.add(ExcludedBrand(brand=brand, reason=reason))
            db.query(ScanQueueItem).filter(ScanQueueItem.brand == brand).delete()
            db.commit()
        finally:
            db.close()

    @staticmethod
    def remove_brand_exclusion(exclusion_id: int):
        db = SessionLocal()
        try:
            existing = db.get(ExcludedBrand, exclusion_id)
            if existing:
                db.delete(existing)
                db.commit()
        finally:
            db.close()

    @staticmethod
    def list_excluded_brands() -> list:
        db = SessionLocal()
        try:
            return db.query(ExcludedBrand).order_by(ExcludedBrand.excluded_at.desc()).all()
        finally:
            db.close()

    @staticmethod
    def get_excluded_brand_names() -> set:
        """
        Used as the SAME kind of pre-token check in BrandScanService.scan
        Step 1 that get_whole_gated_brand_names already provides for
        gating -- no Keepa token spent hunting for more of an excluded
        brand.
        """
        db = SessionLocal()
        try:
            rows = db.query(ExcludedBrand.brand).all()
            return {r[0] for r in rows}
        finally:
            db.close()

    @staticmethod
    def list_gated_opportunities() -> list:
        """
        Latest scan record per ASIN where OpportunityEngine tagged the
        product "GATED" (see Product.gated / OpportunityEngine.analyse)
        -- i.e. real, scored A2A opportunities Atlas currently can't
        act on because the brand is gated. This is the data behind the
        Gated Brand Opportunities page: the case for pursuing ungating
        on a specific brand is exactly "look how many/how strong these
        would be if we could sell them".
        """
        db = SessionLocal()

        try:
            recent = (
                db.query(ProductRecord)
                .filter(ProductRecord.recommendation == "GATED")
                .order_by(ProductRecord.scanned_at.desc())
                .all()
            )

            seen = set()
            latest = []

            for record in recent:
                if record.asin in seen:
                    continue

                seen.add(record.asin)
                latest.append(record)

            latest.sort(key=lambda r: max(r.roi, r.roi_90d), reverse=True)

            return latest

        finally:
            db.close()

    @staticmethod
    def get_gated_brand_summary() -> list:
        """
        Per-brand rollup of list_gated_opportunities(), for the
        top-of-page summary on the Gated Brand Opportunities view --
        lets you see at a glance which gated brands have the strongest
        case for pursuing ungating (most opportunities, best ROI)
        without reading every row.
        """
        records = ProductRepository.list_gated_opportunities()
        by_brand = {}

        for record in records:
            brand = record.brand or "(unknown)"
            bucket = by_brand.setdefault(brand, {"brand": brand, "count": 0, "best_roi": 0.0, "total_roi": 0.0})
            best_roi_this = max(record.roi, record.roi_90d)

            bucket["count"] += 1
            bucket["best_roi"] = max(bucket["best_roi"], best_roi_this)
            bucket["total_roi"] += best_roi_this

        summary = list(by_brand.values())

        for bucket in summary:
            bucket["avg_roi"] = round(bucket["total_roi"] / bucket["count"], 1) if bucket["count"] else 0.0

        summary.sort(key=lambda b: (b["count"], b["best_roi"]), reverse=True)

        return summary

    # ---- Review (thumbs up/down) ----

    @staticmethod
    def set_review(asin: str, verdict: str | None, reason: str | None = None, reason_category: str | None = None):
        """
        verdict: "up", "down", or None to clear. Applies to the MOST
        RECENT scan record for this ASIN -- the one currently being
        shown on whichever page the thumbs button was clicked from.

        reason: optional free-text "why not", only meaningful alongside
        verdict="down" -- the reject-flow field added 2026-08-23 (see
        ProductRecord.review_reason's own comment). Passed through as
        given; callers decide whether it's worth prompting for.

        reason_category: optional structured reason (atlas-review-
        queue-backend-v1.md section 5, see review_queue_service.
        REVIEW_REASON_CATEGORIES) -- purely additive alongside `reason`,
        never a replacement for it.
        """
        db = SessionLocal()

        try:
            record = (
                db.query(ProductRecord)
                .filter(ProductRecord.asin == asin)
                .order_by(ProductRecord.scanned_at.desc())
                .first()
            )

            if record:
                record.review = verdict
                record.review_reason = reason
                record.review_reason_category = reason_category
                db.commit()

        finally:
            db.close()

    @staticmethod
    def set_review_bulk(asins: list, verdict: str | None):
        """
        Bulk form of set_review -- same "most recent scan record per
        ASIN" semantics, one query per unique ASIN (dedup'd first
        since the same ASIN could in principle appear twice in a
        selection) then a single commit for the whole batch.
        """
        unique_asins = {a for a in asins if a}

        if not unique_asins:
            return

        db = SessionLocal()

        try:
            for asin in unique_asins:
                record = (
                    db.query(ProductRecord)
                    .filter(ProductRecord.asin == asin)
                    .order_by(ProductRecord.scanned_at.desc())
                    .first()
                )

                if record:
                    record.review = verdict

            db.commit()

        finally:
            db.close()

    @staticmethod
    def get_latest_record_ids(asins) -> dict:
        """
        Returns {asin: latest ProductRecord.id} for the given ASINs --
        used to link a fresh detection (e.g. SellerWatchService's
        seller_new_listings rows) to whatever ProductRecord the scan
        pipeline just created (or already had, if it was recently
        scanned via another campaign) for it, without a separate query
        per ASIN. Same "first hit per asin, newest first" shape as
        get_reviews. An ASIN with no record at all (e.g. it got
        excluded/filtered before ever producing one) is simply absent
        from the returned dict.
        """
        if not asins:
            return {}

        db = SessionLocal()

        try:
            rows = (
                db.query(ProductRecord)
                .filter(ProductRecord.asin.in_(asins))
                .order_by(ProductRecord.scanned_at.desc())
                .all()
            )

            ids = {}

            for row in rows:
                if row.asin not in ids:
                    ids[row.asin] = row.id

            return ids

        finally:
            db.close()

    @staticmethod
    def get_reviews(asins) -> dict:
        """
        Returns {asin: "up"|"down"} for the latest scan record of each
        given ASIN -- lets a results page show which thumb (if any) is
        currently active without a separate query per row.
        """
        if not asins:
            return {}

        db = SessionLocal()

        try:
            rows = (
                db.query(ProductRecord)
                .filter(ProductRecord.asin.in_(asins))
                .order_by(ProductRecord.scanned_at.desc())
                .all()
            )

            reviews = {}

            for row in rows:
                if row.asin not in reviews and row.review:
                    reviews[row.asin] = row.review

            return reviews

        finally:
            db.close()

    # ---- Ceiling-rejected pool (feeds the "ceiling_recheck" Signal --
    # see BrandScanService Step 3b and SignalService) ----

    @staticmethod
    def upsert_ceiling_rejected(asin: str, title: str = "", brand: str = "",
                                 category: str = "", category_name: str = "",
                                 fba_fee: float = 0.0, buy_box_at_reject: float = 0.0):
        """
        Records/refreshes one ASIN that just failed BrandScanService's
        Step 3b ceiling check (today's-or-90d UK price wasn't even
        enough to clear Amazon's own fees, before any EU cost was
        looked up). One row per ASIN -- re-rejecting an ASIN that's
        already here just refreshes its price/fee snapshot and
        last_seen_at, it doesn't duplicate the row. Costs nothing
        extra: this is purely persisting data BrandScanService's scan
        already paid Keepa tokens for.
        """
        db = SessionLocal()

        try:
            existing = db.get(CeilingRejected, asin)

            if existing:
                if title:
                    existing.title = title
                if brand:
                    existing.brand = brand
                if category:
                    existing.category = category
                if category_name:
                    existing.category_name = category_name
                existing.fba_fee = fba_fee
                existing.buy_box_at_reject = buy_box_at_reject
                existing.last_seen_at = datetime.now(timezone.utc)
            else:
                db.add(CeilingRejected(
                    asin=asin, title=title, brand=brand, category=category,
                    category_name=category_name, fba_fee=fba_fee,
                    buy_box_at_reject=buy_box_at_reject,
                ))

            db.commit()

        finally:
            db.close()

    @staticmethod
    def remove_ceiling_rejected(asin: str):
        """
        Drops an ASIN from the ceiling-rejected pool -- called either
        when a normal scan finds it DOES clear the ceiling now (see
        BrandScanService Step 3b), or when SignalService's
        ceiling_recheck signal surfaces it as a SignalMatch (no point
        re-checking something already surfaced).
        """
        db = SessionLocal()

        try:
            existing = db.get(CeilingRejected, asin)

            if existing:
                db.delete(existing)
                db.commit()

        finally:
            db.close()

    @staticmethod
    def list_ceiling_rejected(category_ids: list = None, limit: int = 500) -> list:
        """
        Returns CeilingRejected rows, optionally narrowed to a set of
        Keepa category IDs (matched against the `category` column --
        the rootCategory ID recorded at reject time). Used by
        SignalService's ceiling_recheck to pick which previously-
        rejected ASINs to re-price this run, oldest-checked first so
        the whole pool eventually cycles through rather than the same
        few ASINs getting re-checked forever.
        """
        db = SessionLocal()

        try:
            query = db.query(CeilingRejected)

            if category_ids:
                query = query.filter(CeilingRejected.category.in_([str(c) for c in category_ids]))

            return (
                query.order_by(CeilingRejected.last_seen_at.asc())
                .limit(limit)
                .all()
            )

        finally:
            db.close()

    @staticmethod
    def get_last_eu_check(asin: str):
        """
        Returns Atlas's own most recent ProductRecord for this ASIN
        (if any), or None -- a free enrichment for a SignalMatch using
        data Atlas already has, NOT a fresh EU lookup. Lets the
        Signals page show "we last checked this and found a source in
        DE at £X, Y% ROI" without spending a single extra Keepa token.
        """
        db = SessionLocal()

        try:
            return (
                db.query(ProductRecord)
                .filter(ProductRecord.asin == asin)
                .order_by(ProductRecord.scanned_at.desc())
                .first()
            )

        finally:
            db.close()

    @staticmethod
    def get_recheck_summary_since(asin: str, since) -> dict:
        """
        {"recheck_count": int, "ever_profitable": bool} across every
        ProductRecord for this ASIN scanned at/after `since` (a naive
        UTC datetime, matching how scanned_at is stored everywhere
        else in this codebase). Built for WatchlistService.
        prune_stale_auto_adds -- it needs to judge, for an auto-added
        watch, whether it's had a fair number of REAL rechecks since
        being added and whether any one of them ever found it
        profitable, without pulling full ProductRecord rows into
        memory just to check two booleans-worth of information.

        product_records is append-only (a new row per scan, not an
        upsert -- confirmed via direct inspection: some ASINs already
        have a dozen-plus rows), so counting rows in a date range is a
        genuine count of real, separate Keepa rechecks, not an
        artifact of one row being updated repeatedly.
        """
        db = SessionLocal()

        try:
            rows = (
                db.query(ProductRecord.profit, ProductRecord.profit_90d)
                .filter(ProductRecord.asin == asin, ProductRecord.scanned_at >= since)
                .all()
            )

            return {
                "recheck_count": len(rows),
                "ever_profitable": any(
                    (profit or 0) > 0 or (profit_90d or 0) > 0
                    for profit, profit_90d in rows
                ),
            }

        finally:
            db.close()

    # ---- Signal queries / matches (Signals opportunity-discovery page --
    # see SignalService) ----

    @staticmethod
    def list_signal_queries(enabled_only: bool = False) -> list:
        db = SessionLocal()

        try:
            query = db.query(SignalQuery)

            if enabled_only:
                query = query.filter(SignalQuery.enabled == True)  # noqa: E712

            return query.order_by(SignalQuery.created_at.asc()).all()

        finally:
            db.close()

    @staticmethod
    def get_signal_query(query_id: int):
        db = SessionLocal()

        try:
            return db.get(SignalQuery, query_id)

        finally:
            db.close()

    @staticmethod
    def create_signal_query(name: str, signal_type: str, category_ids: str = "") -> int:
        db = SessionLocal()

        try:
            row = SignalQuery(name=name, signal_type=signal_type, category_ids=category_ids)
            db.add(row)
            db.commit()
            db.refresh(row)
            return row.id

        finally:
            db.close()

    @staticmethod
    def delete_signal_query(query_id: int):
        """
        Also clears any SignalMatch rows tied to this query -- a
        SignalMatch has no meaning once its parent query is gone, and
        leaving them behind would let a stale query_id linger in the
        matches table forever.
        """
        db = SessionLocal()

        try:
            db.query(SignalMatch).filter(SignalMatch.signal_query_id == query_id).delete()

            existing = db.get(SignalQuery, query_id)
            if existing:
                db.delete(existing)

            db.commit()

        finally:
            db.close()

    @staticmethod
    def set_signal_query_enabled(query_id: int, enabled: bool):
        db = SessionLocal()

        try:
            existing = db.get(SignalQuery, query_id)
            if existing:
                existing.enabled = enabled
                db.commit()

        finally:
            db.close()

    @staticmethod
    def update_signal_query_snapshot(query_id: int, snapshot_json: str = None):
        """
        Stamps last_checked_at, and -- if snapshot_json is given --
        records the ASIN list a Product-Finder-based run just saw
        (JSON-encoded), so the NEXT run diffs against it to find only
        newly-appearing ASINs (same idea as TrackedSeller.
        last_asin_snapshot for Competitor Watch).

        snapshot_json=None (the ceiling_recheck case, which has no
        Product Finder call/diff concept at all -- see SignalQuery's
        docstring) just refreshes last_checked_at for the "last run
        X ago" display, leaving last_match_snapshot untouched.
        """
        db = SessionLocal()

        try:
            existing = db.get(SignalQuery, query_id)
            if existing:
                if snapshot_json is not None:
                    existing.last_match_snapshot = snapshot_json
                existing.last_checked_at = datetime.now(timezone.utc)
                db.commit()

        finally:
            db.close()

    @staticmethod
    def save_signal_match(signal_query_id: int, signal_type: str, asin: str, title: str = "",
                           brand: str = "", category_name: str = "", buy_box_now: float = 0.0,
                           monthly_sales: int = 0, sales_drops_30d: int = 0,
                           target_buy_price_gbp: float = 0.0, signal_reasoning_json: str = "",
                           eu_history_json: str = ""):
        db = SessionLocal()

        try:
            db.add(SignalMatch(
                signal_query_id=signal_query_id, signal_type=signal_type, asin=asin,
                title=title, brand=brand, category_name=category_name,
                buy_box_now=buy_box_now, monthly_sales=monthly_sales,
                sales_drops_30d=sales_drops_30d, target_buy_price_gbp=target_buy_price_gbp,
                signal_reasoning_json=signal_reasoning_json, eu_history_json=eu_history_json,
            ))
            db.commit()

        finally:
            db.close()

    @staticmethod
    def list_signal_matches(signal_query_id: int = None, include_dismissed: bool = False,
                             limit: int = 200) -> list:
        db = SessionLocal()

        try:
            query = db.query(SignalMatch)

            if signal_query_id is not None:
                query = query.filter(SignalMatch.signal_query_id == signal_query_id)

            if not include_dismissed:
                query = query.filter(SignalMatch.dismissed == False)  # noqa: E712

            return (
                query.order_by(SignalMatch.detected_at.desc())
                .limit(limit)
                .all()
            )

        finally:
            db.close()

    @staticmethod
    def dismiss_signal_match(match_id: int):
        db = SessionLocal()

        try:
            existing = db.get(SignalMatch, match_id)
            if existing:
                existing.dismissed = True
                db.commit()

        finally:
            db.close()

    @staticmethod
    def count_new_signal_matches() -> int:
        """
        Cheap COUNT-only query (never fetches the actual rows) for the
        "N new" sidebar badge next to the Signals link -- see
        base.html and main.py's inject_sidebar_badges middleware,
        which calls this on every single page load. Deliberately kept
        separate from list_signal_matches (which fetches full rows
        and is only called by the /signals page itself) so the badge
        stays cheap regardless of how many matches have piled up.
        """
        db = SessionLocal()

        try:
            return (
                db.query(func.count(SignalMatch.id))
                .filter(SignalMatch.dismissed == False)  # noqa: E712
                .scalar()
            ) or 0

        finally:
            db.close()