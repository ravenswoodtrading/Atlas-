from app.database.database import SessionLocal
from app.database.models import ProductRecord, SellerNewListing

# The existing deterministic bar (OpportunityEngine) already excludes
# IGNORE and GATED -- a shortlist candidate must already clear that,
# same "gated brands excluded from anything actionable" rule as
# everywhere else in Atlas. Ordered worst-to-best for _tier_rank below.
GOOD_RECOMMENDATIONS = ("PEAK_WINDOW", "CONSIDER", "BUY")

# Per the user's explicit choice 2026-08-24 -- Keepa's ~22 tokens/min
# refill is the real constraint on how big a single sweep can be
# without starving other scans/schedulers of token budget.
DEFAULT_SHORTLIST_SIZE = 20


class ShortlistService:
    """
    Sourcing agent brief step 9 territory, but scoped by explicit user
    request 2026-08-24 rather than the brief's own OA v2/SAS items --
    "go through all the leads Atlas has already found, deep-dive the
    strongest, give me a short buy/no-buy list" instead of one more
    place to manually paste ASINs into.

    Deliberately a thin READ-ONLY layer over Discovery (ProductRecord)
    and Competitor Watch (SellerNewListing) -- an audit before building
    this (2026-08-24) confirmed both pipelines already share the same
    OpportunityEngine/FeeEngine scoring core, so this never re-scores
    anything itself, only reads what's already been computed to decide
    which stale candidates deserve a fresh, live re-check. Signals and
    OA Source Discovery were explicitly excluded from this first
    version by the user (see the same conversation) -- low volume
    today, revisit later if that changes.
    """

    @staticmethod
    def get_candidate_pool() -> list[dict]:
        """
        Every unreviewed ASIN that clears the existing CONSIDER/
        PEAK_WINDOW/BUY bar, from either pipeline, deduped by ASIN
        (same ASIN can accumulate multiple ProductRecord rows across
        repeat scans -- confirmed live 2026-08-24, ~5% of the pool).
        Keeps the freshest scanned_at per ASIN. Discovery's own
        ProductRecord.review and Competitor Watch's own
        SellerNewListing.review are separate action queues over the
        same underlying scored row -- a record already dismissed via
        one is still included here if the other pipeline's queue
        hasn't actioned it yet.
        """
        db = SessionLocal()
        try:
            discovery_rows = (
                db.query(ProductRecord)
                .filter(
                    ProductRecord.review.is_(None),
                    ProductRecord.recommendation.in_(GOOD_RECOMMENDATIONS),
                )
                .all()
            )

            competitor_watch_rows = (
                db.query(SellerNewListing, ProductRecord)
                .join(ProductRecord, ProductRecord.id == SellerNewListing.product_record_id)
                .filter(
                    SellerNewListing.review.is_(None),
                    ProductRecord.recommendation.in_(GOOD_RECOMMENDATIONS),
                )
                .all()
            )

            best_by_asin: dict[str, ProductRecord] = {}
            seen_via_competitor_watch: set[str] = set()

            for rec in discovery_rows:
                existing = best_by_asin.get(rec.asin)
                if existing is None or rec.scanned_at > existing.scanned_at:
                    best_by_asin[rec.asin] = rec

            for _snl, rec in competitor_watch_rows:
                seen_via_competitor_watch.add(rec.asin)
                existing = best_by_asin.get(rec.asin)
                if existing is None or rec.scanned_at > existing.scanned_at:
                    best_by_asin[rec.asin] = rec

            pool = []
            for asin, rec in best_by_asin.items():
                pool.append({
                    "asin": asin,
                    "record_id": rec.id,
                    "title": rec.title,
                    "brand": rec.brand,
                    "recommendation": rec.recommendation,
                    "score": rec.score,
                    "profit": rec.profit,
                    "roi": rec.roi,
                    # Already-resolved buyable source (see product_mapper.py
                    # for A2A candidates) -- fed straight into the deep-dive
                    # check below with no manual cost/source entry needed.
                    "best_source_marketplace": rec.best_source_marketplace,
                    "best_source_cost_gbp": rec.best_source_cost_gbp,
                    "scanned_at": rec.scanned_at.isoformat(),
                    "seen_via_competitor_watch": asin in seen_via_competitor_watch,
                })

            return pool
        finally:
            db.close()

    @staticmethod
    def rank_candidates(pool: list[dict]) -> list[dict]:
        """
        Cheap ranking ONLY -- decides which stale candidates get the
        live deep-dive below, never treated as the final word (the
        stored profit/roi can be up to several weeks old). BUY first,
        then CONSIDER, then PEAK_WINDOW (PEAK's own stricter bar is
        already baked into OpportunityEngine before a row even reaches
        that tier), each ordered by stored profit within its tier.
        """
        tier_rank = {"BUY": 0, "CONSIDER": 1, "PEAK_WINDOW": 2}
        return sorted(
            pool,
            key=lambda c: (tier_rank.get(c["recommendation"], 3), -(c["profit"] or 0.0)),
        )

    @staticmethod
    def get_shortlist_targets(limit: int = DEFAULT_SHORTLIST_SIZE) -> list[dict]:
        """Convenience wrapper: the top `limit` candidates, ready for the deep-dive pass."""
        pool = ShortlistService.get_candidate_pool()
        return ShortlistService.rank_candidates(pool)[:limit]
