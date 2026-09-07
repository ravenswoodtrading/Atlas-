"""
Scan Economics (Phase 5A, 2026-09-04) -- a read-only MEASUREMENT layer,
deliberately separate from Discovery priority scoring. See the Phase 5
Attention Engine design report for the finding this exists to surface:
Discovery tier does not reliably predict scan yield -- Grohe/Makita/
Brother (all MEDIUM) produced 2-7x more useful opportunities per token
than several HIGH-tier brands, and Philips (HIGH) showed the worst
yield of any brand with a confirmed BUY at all. This module makes that
comparison a first-class, always-visible metric instead of something
that needs a one-off analysis script to see.

NOTHING here changes what any brand's tier IS, changes Scan Queue
selection, or spends a single Keepa token -- it only reads ProductRecord
history that scanning has ALREADY produced, through the same
OpportunityLens every other Atlas screen uses, and reads real average
token costs from TokenUsageEvent. Purely additive, purely display.

Two different "how much have we scanned this brand" numbers appear
together here, deliberately, not merged into one:
- `total_records`: real, saved ProductRecord rows for this brand's EU
  A2A history -- what the useful/confirmed Action counts are actually
  computed FROM, so it's directly interpretable next to them.
- The TOKEN ESTIMATE instead uses ScanQueueItem.scanned_count/next_page
  -- Keepa is charged for every ASIN LOOKED UP, not just the ones that
  turned out to have a real UK price + EU source and got saved as a
  ProductRecord (see that model's own docstring) -- so scanned_count,
  a lifetime raw-lookup counter, is the closer proxy for real spend,
  even though it's usually larger than total_records.

Token cost-per-call is read LIVE from TokenUsageEvent each time the
cache refreshes (never hardcoded) -- the Phase 5 report already found
these ratios move over time, so a frozen constant would silently drift
from reality.
"""
from collections import defaultdict
from datetime import datetime, timezone
import time as _time

from app.database.database import SessionLocal
from app.database.models import ProductRecord, TokenUsageEvent
from app.services.scan_queue_service import ScanQueueService
from app.services.discovery_intelligence_service import EU_MARKETPLACES
from app.services.revisit_pool_service import RevisitPoolService
from app.services.review_queue_service import ReviewQueueService
from app.services import opportunity_lens_service as lens

# "Useful opportunity" per the approved Phase 5 brief -- everything
# short of an outright confirmed BUY that still represents real,
# actionable signal. Deliberately excludes WATCH/HISTORICAL_RECURRING/
# BLOCKED (weak or inactive signal only).
USEFUL_ACTIONS = frozenset({
    lens.ACTION_BUY_NOW, lens.ACTION_PRICE_DROP_BUY_NOW, lens.ACTION_BUY_WITH_CAUTION,
    lens.ACTION_HIGH_VALUE_LOW_CONFIDENCE, lens.ACTION_INVESTIGATE,
})
CONFIRMED_BUY_ACTIONS = frozenset({lens.ACTION_BUY_NOW, lens.ACTION_PRICE_DROP_BUY_NOW})

# Sample-size floor (Phase 5 report finding, not invented here) -- below
# this many real scan records, a useful/10k-tokens ratio is noise (a
# single lucky HVC hit on a 1-2 record brand showed 350+/10k in the
# report, next to nothing statistically). Brands below this floor still
# show their raw counts, never a ratio.
MIN_SCANS_FOR_EFFICIENCY = 10

# "QUIET" threshold -- reuses RevisitPoolService's own existing 30-day
# staleness bar rather than inventing a second, different number for a
# very similar concept ("how long since this ASIN/brand last showed
# real signal"). Explicitly informational only (see STATUS_QUIET below)
# -- never affects Scan Queue selection.
QUIET_AFTER_DAYS = RevisitPoolService.DEFAULT_STALE_DAYS

STATUS_ACTIVE = "ACTIVE"
STATUS_QUIET = "QUIET"
# A brand that HAS scan records but has never once produced a useful
# Action -- distinct from "quiet" (which implies it used to, then
# stopped). Still informational only.
STATUS_NEVER_USEFUL = "NEVER_USEFUL"

# Performance cache -- the full computation (fetch + run every EU-scoped
# ProductRecord for the queued brands through the live OpportunityLens)
# measured ~3s directly against the real database; Discovery evidence/
# scan history moves at the pace of scans, not seconds, so recomputing
# on literally every page view would be pure overhead. Same pattern as
# ScanQueueService._brand_tier_map's own cache (Phase 4A).
_CACHE_TTL_SECONDS = 300
_cache: dict = {}
_cache_built_at: float = 0.0


def _avg_tokens_per_call_type() -> dict:
    """
    {call_type: avg_tokens_per_call}, read live from TokenUsageEvent's
    own scan_queue rows -- e.g. "keepa_query" (per-ASIN detail lookup)
    and "keepa_product_finder" (page search). Never hardcoded: the
    Phase 5 report already found these move over time (68.7%/28.2% now
    vs 73%/27% a few days earlier), so freezing them as constants would
    silently go stale. Returns {} (not a crash) if there's no
    TokenUsageEvent data yet -- callers fall back to a 0 estimate rather
    than a fabricated one.
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(TokenUsageEvent.call_type, TokenUsageEvent.tokens)
            .filter(TokenUsageEvent.category == "scan_queue")
            .all()
        )
    finally:
        db.close()

    totals: dict = defaultdict(lambda: [0.0, 0])
    for call_type, tokens in rows:
        totals[call_type][0] += tokens or 0.0
        totals[call_type][1] += 1

    return {ct: total / count for ct, (total, count) in totals.items() if count}


def _status(days_since_last_useful: int | None) -> str:
    if days_since_last_useful is None:
        return STATUS_NEVER_USEFUL
    return STATUS_QUIET if days_since_last_useful >= QUIET_AFTER_DAYS else STATUS_ACTIVE


def get_brand_economics(brands: list | None = None, use_cache: bool = True) -> dict:
    """
    Read-only. {normalized_brand: {
        "total_records": int -- real saved EU A2A ProductRecord rows,
        "useful_count": int, "confirmed_buy_count": int,
        "est_tokens": float -- see this module's own docstring for the
            scanned_count/next_page-based estimate,
        "useful_per_10k": float | None -- None below MIN_SCANS_FOR_EFFICIENCY,
        "confirmed_per_10k": float | None -- same floor,
        "tokens_per_confirmed_buy": float | None -- None if zero confirmed BUYs,
        "days_since_last_useful": int | None -- None if never,
        "status": STATUS_ACTIVE / STATUS_QUIET / STATUS_NEVER_USEFUL,
        "meets_sample_floor": bool,
    }}

    brands=None (the default) computes this for every brand CURRENTLY in
    the Scan Queue -- the population this was built to make visible on
    Scan Intelligence. Pass an explicit list to compute it for others
    (e.g. a specific unqueued brand a human wants to check), bypassing
    the queue-based cache.
    """
    global _cache, _cache_built_at

    explicit_brands = brands is not None

    if not explicit_brands and use_cache:
        now = _time.monotonic()
        if _cache and (now - _cache_built_at) < _CACHE_TTL_SECONDS:
            return _cache

    if explicit_brands:
        target_brands = {b.strip().lower() for b in brands}
        queue_totals = defaultdict(lambda: {"scanned_count": 0, "next_page": 0})
        for item in ScanQueueService.list_items():
            if item.brand in target_brands:
                queue_totals[item.brand]["scanned_count"] += item.scanned_count
                queue_totals[item.brand]["next_page"] += item.next_page
    else:
        queue_totals = defaultdict(lambda: {"scanned_count": 0, "next_page": 0})
        for item in ScanQueueService.list_items():
            queue_totals[item.brand]["scanned_count"] += item.scanned_count
            queue_totals[item.brand]["next_page"] += item.next_page
        target_brands = set(queue_totals.keys())

    avg_tokens = _avg_tokens_per_call_type()
    avg_query = avg_tokens.get("keepa_query", 0.0)
    avg_finder = avg_tokens.get("keepa_product_finder", 0.0)

    db = SessionLocal()
    try:
        all_records = db.query(ProductRecord).all()
    finally:
        db.close()

    by_brand_records = defaultdict(list)
    for r in all_records:
        if not r.brand:
            continue
        brand = r.brand.strip().lower()
        if brand in target_brands and r.best_source_marketplace in EU_MARKETPLACES:
            by_brand_records[brand].append(r)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = {}

    for brand in target_brands:
        recs = by_brand_records.get(brand, [])
        q = queue_totals.get(brand, {"scanned_count": 0, "next_page": 0})
        est_tokens = q["scanned_count"] * avg_query + q["next_page"] * avg_finder

        useful_count = 0
        confirmed_buy_count = 0
        last_useful_at = None

        for r in recs:
            lead = ReviewQueueService._scan_lead_dict(r)
            action = lens.compute(lead)["action"]
            if action in USEFUL_ACTIONS:
                useful_count += 1
                if last_useful_at is None or (r.scanned_at and r.scanned_at > last_useful_at):
                    last_useful_at = r.scanned_at
            if action in CONFIRMED_BUY_ACTIONS:
                confirmed_buy_count += 1

        days_since_last_useful = (now - last_useful_at).days if last_useful_at else None
        meets_floor = len(recs) >= MIN_SCANS_FOR_EFFICIENCY

        result[brand] = {
            "total_records": len(recs),
            "useful_count": useful_count,
            "confirmed_buy_count": confirmed_buy_count,
            "est_tokens": round(est_tokens, 0),
            "useful_per_10k": round(useful_count / est_tokens * 10000, 2) if (meets_floor and est_tokens) else None,
            "confirmed_per_10k": round(confirmed_buy_count / est_tokens * 10000, 3) if (meets_floor and est_tokens) else None,
            "tokens_per_confirmed_buy": round(est_tokens / confirmed_buy_count, 0) if confirmed_buy_count else None,
            "days_since_last_useful": days_since_last_useful,
            "status": _status(days_since_last_useful),
            "meets_sample_floor": meets_floor,
        }

    if not explicit_brands and use_cache:
        _cache = result
        _cache_built_at = _time.monotonic()

    return result
