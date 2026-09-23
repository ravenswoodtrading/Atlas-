"""
Opportunity Engine 2.0, Phase 3A -- Targeted Revisit Pool.

Fixes the "dead end" problem the Phase 1/2 audits found: an ASIN that
falls out of the Review Queue (IGNORE'd, or simply not touched again by
any brand search/competitor check for 30+ days) had NO way back in
except by chance -- the ASIN Re-Entry Audit found 75.4% of ever-IGNORE
ASINs are never rescanned by anything at all, even though rescanned
ones recovered to a real opportunity 29.5% of the time.

Deliberately NOT a new scanning subsystem (Phase 3A point 4/5): every
actual rescan below goes through the EXACT SAME BrandScanService.scan(
asins=..., force_rescan=True) call ReviewQueueService.recheck_stale_
items already uses. The only new code is candidate SELECTION (which
ASINs, in what order, how many) and OUTCOME LOGGING (RevisitLog) --
the feedback data needed to eventually improve that selection
empirically instead of guessing.

Ranking is profit-primary, not a blended score (Phase 3A point 6/12):
real data showed ROI-primary ranking pulls implausible outliers to the
top (e.g. a near-zero EU cost against a normal UK price -- the same
"phantom cost" signature KeepaParser's own docstring documents fixing
elsewhere) ahead of genuinely large, trustworthy opportunities.

Scope boundaries, deliberate (Phase 3A points 9-11):
  - No persistent rejection memory -- a revisited ASIN gets scored
    exactly like any other rescan (a brand new ProductRecord with
    review=None), same as every existing rescan path already behaves.
    See RevisitLog for what a LATER change would need.
  - No include_offers (no bulk Amazon Buy-Box-share data) -- this
    reuses the ordinary full=True scan path, same token profile as
    every other existing bulk rescan.
  - No Discovery Intelligence integration.
"""
from collections import defaultdict
from datetime import datetime, timezone

from app.database.database import SessionLocal, engine
from app.database.base import Base
from app.database.models import ProductRecord, RevisitLog
from app.services.activity_log import ActivityLog
from app.services.brand_scan_service import BrandScanService
from app.services.product_repository import ProductRepository
from app.services.review_queue_service import QUEUE_PRIORITY_BUY_NOW, QUEUE_PRIORITY_BORDERLINE, BORDERLINE_RECOMMENDATIONS


class RevisitPoolService:

    # Phase 3A point 4: start conservative, not a full sweep of the
    # whole pool at once -- same "clears gradually, oldest/highest-value
    # first" convention MAX_STALE_RECHECK_PER_RUN already established
    # for review_queue_recheck.
    #
    # 25 -> 5 (2026-09-20, Tamara approved after a token review): 600 revisits had produced 0
    # "recovered" and 8 BUY/CONSIDER (~1.3%). Kept rather than removed -- it still finds the odd
    # one -- but at a fifth of the cost.
    DEFAULT_DAILY_LIMIT = 5

    # Matches the ASIN Re-Entry Audit's own 30-day cutoff -- the pool
    # this phase targets IS that audit's 736-ASIN population, not a
    # newly invented window.
    DEFAULT_STALE_DAYS = 30

    # Empirical cutoff from the Opportunity Engine 2.0 simulation
    # (Phase 3A point 7) -- NOT a scoring weight. A display/trust flag
    # only: real data showed source-match/price-parsing errors
    # concentrate almost exclusively above this ROI band.
    VERIFY_SOURCE_MATCH_ROI = 500

    # ---- pure logic (no DB access) -- fixture-testable in isolation ----

    @staticmethod
    def select_and_rank(raw_candidates: list, limit: int, stale_days: int, now: datetime) -> list:
        """
        raw_candidates: [{"asin", "title", "previous_record",
        "best_ever_profit", "last_scanned_at"}, ...] -- plain dicts, no
        DB/ORM dependency, so this can be exercised directly with
        synthetic fixtures (see test_revisit_pool.py).

        Filters to "ever showed real profit, AND its most recent scan
        is stale_days+ old" (the ASIN Re-Entry Audit's own population),
        then ranks profit-primary descending with days-stale as the
        tie-break (oldest-neglected wins a tie) -- see this module's
        own docstring for why profit, not ROI. Returns at most `limit`.
        """
        eligible = []

        for c in raw_candidates:
            if c["best_ever_profit"] <= 0:
                continue
            if c["last_scanned_at"] is None:
                continue

            days = (now - c["last_scanned_at"]).days

            if days < stale_days:
                continue

            eligible.append({**c, "days_since_last_scan": days})

        eligible.sort(key=lambda c: (-c["best_ever_profit"], -c["days_since_last_scan"]))

        return eligible[:limit]

    @staticmethod
    def build_log_entry(candidate: dict, fresh_record, rank: int) -> RevisitLog:
        """
        Pure comparison logic (no DB write, no Keepa call) -- takes the
        candidate's `previous_record` and the FRESH ProductRecord (or
        None if the rescan produced nothing new for this ASIN) and
        returns an unsaved RevisitLog row. Both records may be plain,
        never-committed ORM instances (or None) -- this only reads
        their attributes, so it's directly fixture-testable with
        synthetic ProductRecord(...) objects.
        """
        prev = candidate["previous_record"]

        base_kwargs = dict(
            asin=candidate["asin"], title=candidate["title"], selection_rank=rank,
            days_since_last_scan=candidate["days_since_last_scan"],
            previous_scanned_at=prev.scanned_at, previous_profit=prev.profit or 0.0,
            previous_profit_90d=prev.profit_90d or 0.0, previous_roi=prev.roi or 0.0,
            previous_roi_90d=prev.roi_90d or 0.0, previous_recommendation=prev.recommendation or "",
        )

        if fresh_record is None or fresh_record.id == prev.id:
            # The rescan produced no NEW record for this ASIN -- it no
            # longer resolves, got excluded/gated since it was first
            # found, or the batch ran out of tokens partway through.
            # Logged honestly as a real outcome, not silently dropped.
            #
            # recovered/verify_source_match set explicitly (not left to
            # the column's `default=False`) -- that default only takes
            # effect once SQLAlchemy actually flushes/inserts the row;
            # a caller reading .recovered on this object BEFORE commit
            # (as run_batch's own accounting loop does) would otherwise
            # see None, not False.
            return RevisitLog(**base_kwargs, status="no_fresh_record", recovered=False, verify_source_match=False)

        was_notable = ProductRepository.is_notable(
            prev.recommendation, prev.monthly_sales or 0, prev.roi or 0, prev.roi_90d or 0,
            prev.sales_drops_30d or 0, prev.buy_box_now or 0,
        )
        is_notable_fresh = ProductRepository.is_notable(
            fresh_record.recommendation, fresh_record.monthly_sales or 0, fresh_record.roi or 0,
            fresh_record.roi_90d or 0, fresh_record.sales_drops_30d or 0,
            fresh_record.buy_box_now or 0,
        )
        recovered = is_notable_fresh and not was_notable

        fresh_best_roi = max(fresh_record.roi or 0, fresh_record.roi_90d or 0)
        verify_flag = fresh_best_roi > RevisitPoolService.VERIFY_SOURCE_MATCH_ROI

        if is_notable_fresh:
            resulting_action = QUEUE_PRIORITY_BUY_NOW
        elif fresh_record.recommendation in BORDERLINE_RECOMMENDATIONS:
            resulting_action = QUEUE_PRIORITY_BORDERLINE
        else:
            resulting_action = fresh_record.recommendation or "UNKNOWN"

        return RevisitLog(
            **base_kwargs,
            fresh_scanned_at=fresh_record.scanned_at, fresh_profit=fresh_record.profit or 0.0,
            fresh_profit_90d=fresh_record.profit_90d or 0.0, fresh_roi=fresh_record.roi or 0.0,
            fresh_roi_90d=fresh_record.roi_90d or 0.0, fresh_recommendation=fresh_record.recommendation or "",
            resulting_action=resulting_action, recovered=recovered,
            verify_source_match=verify_flag, status="scanned",
        )

    # ---- DB-touching methods ----

    @staticmethod
    def _latest_and_best_per_asin() -> dict:
        """
        {asin: {"latest": ProductRecord, "best_ever_profit": float}} --
        one pass over the whole table, same "load everything, reduce in
        Python" convention ProductRepository.get_latest_per_asin
        already uses. best_ever_profit is the highest profit (today OR
        90d) ever recorded for that ASIN across its ENTIRE scan
        history, not just its latest record -- this is what
        "historically attractive" means here (see the ASIN Re-Entry
        Audit).
        """
        db = SessionLocal()
        try:
            rows = db.query(ProductRecord).order_by(ProductRecord.scanned_at.asc()).all()
        finally:
            db.close()

        by_asin = defaultdict(list)
        for r in rows:
            by_asin[r.asin].append(r)

        result = {}
        for asin, recs in by_asin.items():
            latest = recs[-1]  # scanned_at ascending -> last element is newest
            best_ever_profit = max(max(r.profit or 0, r.profit_90d or 0) for r in recs)
            result[asin] = {"latest": latest, "best_ever_profit": best_ever_profit}

        return result

    @staticmethod
    def get_candidates(limit: int = DEFAULT_DAILY_LIMIT, stale_days: int = DEFAULT_STALE_DAYS) -> list:
        """
        Read-only. Returns up to `limit` candidates: {asin, title,
        previous_record, best_ever_profit, days_since_last_scan} --
        DB read (impure) feeding straight into select_and_rank (pure).
        Safe to call as often as needed for inspection; makes no Keepa
        call and writes nothing.
        """
        info = RevisitPoolService._latest_and_best_per_asin()
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        raw = [
            {
                "asin": asin, "title": data["latest"].title,
                "previous_record": data["latest"], "best_ever_profit": data["best_ever_profit"],
                "last_scanned_at": data["latest"].scanned_at,
            }
            for asin, data in info.items()
        ]

        return RevisitPoolService.select_and_rank(raw, limit, stale_days, now)

    @staticmethod
    def run_batch(limit: int = DEFAULT_DAILY_LIMIT, stale_days: int = DEFAULT_STALE_DAYS,
                  dry_run: bool = False) -> dict:
        """
        Selects candidates, and (unless dry_run) rescans them via the
        SAME force_rescan=True BrandScanService path recheck_stale_
        items already uses -- Phase 3A point 4/5, no new scanning
        mechanism. dry_run=True (the "controlled live-data selection
        test", Phase 3A point 14) only computes and returns the
        candidate list -- no Keepa call, no DB write.

        Caller is responsible for ScanCoordinator, same convention as
        every other automated-tick entry point (see main.py).
        """
        candidates = RevisitPoolService.get_candidates(limit=limit, stale_days=stale_days)

        if dry_run or not candidates:
            return {
                "candidates": candidates, "scanned": 0, "recovered": 0,
                "verify_source_match": 0, "no_fresh_record": 0, "dry_run": dry_run,
            }

        asins = [c["asin"] for c in candidates]
        scanner = BrandScanService(usage_category="revisit_pool")
        scanner.scan("revisit-pool", asins=asins, limit=len(asins), force_rescan=True)

        db = SessionLocal()
        try:
            fresh_rows = (
                db.query(ProductRecord)
                .filter(ProductRecord.asin.in_(asins))
                .order_by(ProductRecord.scanned_at.desc())
                .all()
            )
        finally:
            db.close()

        fresh_by_asin = {}
        for r in fresh_rows:
            if r.asin not in fresh_by_asin:
                fresh_by_asin[r.asin] = r

        recovered_count = 0
        verify_count = 0
        no_fresh_count = 0

        db = SessionLocal()
        try:
            for rank, c in enumerate(candidates, start=1):
                fresh = fresh_by_asin.get(c["asin"])
                log = RevisitPoolService.build_log_entry(c, fresh, rank)

                if log.status == "no_fresh_record":
                    no_fresh_count += 1
                else:
                    if log.recovered:
                        recovered_count += 1
                    if log.verify_source_match:
                        verify_count += 1

                db.add(log)

            db.commit()
        finally:
            db.close()

        summary = {
            "candidates": candidates, "scanned": len(candidates), "recovered": recovered_count,
            "verify_source_match": verify_count, "no_fresh_record": no_fresh_count, "dry_run": False,
        }

        ActivityLog.record(
            "revisit_pool",
            f"{len(candidates)} revisited, {recovered_count} recovered, "
            f"{verify_count} flagged for source-match verification, {no_fresh_count} produced no fresh record",
        )

        return summary


def ensure_table_exists():
    """
    Base.metadata.create_all() only creates MISSING tables -- safe and
    idempotent to call again here for a test/script context that runs
    outside the normal app startup path (see main.py's own call to the
    same thing), rather than assuming a prior server start already did
    it.
    """
    Base.metadata.create_all(bind=engine)
