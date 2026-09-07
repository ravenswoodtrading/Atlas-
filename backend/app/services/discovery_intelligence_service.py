"""
EU A2A Discovery Intelligence (approved 2026-09-04) -- Phase 1 (read-
only aggregation) and Phase 2 (explainable scoring).

Pure read/aggregation over EXISTING tables (ProductRecord,
SellerNewListing, TrackedSeller) -- NO schema changes, NO writes, NO
Keepa/SP-API/SerpApi/Brave calls anywhere in this file. Reuses the
exact same evidence SourcingClassifier/Competitor Watch already trust
(SellerNewListing.sourcing_tag, sourcing_reasoning_json) and the same
scan-history data Products/Scan Queue already show -- no new
competitor pipeline, no new scan pipeline, per the approved brief's
explicit instruction not to duplicate either.

IMPORTANT correctness note, caught while building this: brand-level
aggregation is keyed on ProductRecord.brand (Keepa's own reported
brand, normalized strip+lower), NOT ProductRecord.brand_query.
brand_query is "what search produced this row" -- for a genuine
Discovery/Scan Queue brand search it IS the brand, but for a
Competitor-Watch-triggered scan (see SellerWatchService.run_check's
own scanner.scan(brand=f"competitor:{seller.nickname}", ...) call) it
reads "competitor:<seller nickname>" instead. Grouping by brand_query
would have silently UNDERCOUNTED our own real scan history for any
brand with meaningful competitor-detection volume (exactly the brands
this feature cares about most). ProductRecord.brand is populated by
Keepa on every scored row regardless of what triggered the scan, so
it's the correct, consistent key for both competitor evidence and our
own scan performance.
"""
import time as _time
from datetime import datetime, timedelta, timezone

from app.config.exclusions import is_gated_by_name
from app.database.database import SessionLocal
from app.database.models import ProductRecord, SellerNewListing, TrackedSeller
from app.services.product_repository import ProductRepository
from app.services.review_queue_service import ReviewQueueService
from app.services import opportunity_lens_service as lens_service

# Every possible OpportunityLensService Action, in a fixed display
# order -- used to seed each brand's actions_all_time/actions_recent
# dict with 0 for every key (not just the ones that happened to occur),
# so a template/report can iterate a stable key set without a KeyError.
ALL_ACTIONS = (
    lens_service.ACTION_BUY_NOW, lens_service.ACTION_PRICE_DROP_BUY_NOW,
    lens_service.ACTION_BUY_WITH_CAUTION, lens_service.ACTION_HIGH_VALUE_LOW_CONFIDENCE,
    lens_service.ACTION_INVESTIGATE, lens_service.ACTION_WATCH,
    lens_service.ACTION_HISTORICAL_RECURRING, lens_service.ACTION_BLOCKED,
)

# Action states that count as a genuinely confirmed, currently-fresh
# BUY-tier opportunity (Discovery Intelligence Phase 2, 2026-09-04) --
# the SAME two states OpportunityLensService itself treats as "act on
# this now" (see that module's own compute() docstring). Deliberately
# excludes BUY_WITH_CAUTION -- that's still "notable" but paired with
# an active risk flag, one step down from a clean confirmed BUY.
CONFIRMED_BUY_ACTIONS = (lens_service.ACTION_BUY_NOW, lens_service.ACTION_PRICE_DROP_BUY_NOW)

# "Give it the small additional evidence signal" bar (Discovery
# Intelligence Phase 1 design, approved 2026-09-04) -- same style as
# distinct_competitors' own >=3 threshold below, not a new kind of
# number: a real, inspectable count, not a percentage.
HIGH_VALUE_LOW_CONFIDENCE_MEANINGFUL_COUNT = 3

EU_MARKETPLACES = ("DE", "FR", "ES", "IT")

# Same recent-evidence window SourcingClassifier already uses
# (RECENT_WINDOW_DAYS) -- reused here rather than inventing a second
# "recent" definition that could disagree with Competitor Watch's own.
RECENT_WINDOW_DAYS = 30

# How many of a brand's most recent scan EVENTS (not ASINs -- see
# _own_scan_events below) to look at for the "recent failures" signal.
RECENT_SCAN_EVENTS = 6

# A "top" category, for the category-context scoring bonus -- top N by
# competitor EU/UK A2A evidence count.
TOP_CATEGORY_COUNT = 3

# How many of our own scanned ASINs count as "meaningfully scanned" for
# the conflict cap (approved 2026-09-04): a brand with >= this many
# scans and ZERO confirmed BUYs cannot reach HIGH on competitor/
# category evidence alone. Deliberately reuses the SAME bar the
# existing "3 straight failures" rule already used (recent_events
# length), rather than introducing a second, different threshold --
# checked against real data first (2/3/5/10 all inspected; 3 affects
# 151 of the ~650 brands with any scan history, a real but not
# over-aggressive cut) before being fixed here.
MEANINGFUL_SCAN_THRESHOLD = 3

# EU A2A plug-risk categories (approved 2026-09-04) -- a laptop or
# mains-powered electrical appliance sourced from an EU marketplace
# may physically ship with an EU (two-pin) plug rather than a UK
# (three-pin) one. Real, previously-encountered problem: 3 past manual
# rejections on record (a Brother printer, two Philips products)
# specifically citing "EU plug" as the reason, before Atlas even had a
# formal category for it (see REVIEW_REASON_CATEGORIES' EU_PLUG, still
# 0 rows ever tagged with it).
#
# Deliberately a CAUTION, not a reject or score penalty -- Atlas isn't
# confident enough to say "don't buy this", only "check the plug
# before treating this one as EU A2A". And deliberately category-
# scoped, not brand-scoped: the risk is about the PRODUCT TYPE (does
# it have a mains plug at all), not the brand -- e.g. Canon sells both
# cameras (plugged) and photo paper (not), so a brand-level flag would
# either miss the cameras or over-warn on the paper. A brand-level
# gate would also incorrectly follow the brand into UK A2A/OA
# evidence, which never carries this risk at all -- those source
# domestically, the plug is never in question -- so this only ever
# fires against EU A2A evidence specifically (see score_target's own
# eu_a2a_count check), matching Discovery's own EU-A2A-only scope.
#
# User-maintained, same static-set pattern as GATED_BRAND_CATEGORIES
# (app/config/exclusions.py) -- add a category_name here (as it
# appears on ProductRecord.category_name) when you know or suspect it
# regularly contains mains-powered electrical items.
EU_A2A_PLUG_RISK_CATEGORIES = {
    "Computers & Accessories",
    "Electronics & Photo",
    "PC & Video Games",
    "Home & Garden",
    "Beauty",
}


# Performance cache for list_discovery_targets (2026-09-04, Phase 5A
# follow-up) -- measured directly at ~7s per uncapped call against the
# live database (the DISCOVERY_TARGETS_LIMIT bump in Phase 4D means
# every caller now effectively computes the FULL ranked list regardless
# of the `limit` they pass, since scoring happens before the final
# slice). Discovery evidence moves at the pace of scans, not seconds,
# so recomputing this on every single page view is pure overhead --
# same pattern already used for ScanQueueService._brand_tier_map
# (Phase 4A) and scan_economics_service (Phase 5A). Caches the FULL
# scored+sorted list (unsliced); list_discovery_targets just slices it,
# so every caller's own `limit` still behaves identically.
_TARGETS_CACHE_TTL_SECONDS = 300
_targets_cache: list = []
_targets_cache_built_at: float = 0.0


def _normalize(value: str) -> str:
    return (value or "").strip().lower()


class DiscoveryIntelligenceService:
    """
    Everything here is read-only. Nothing in this class writes to the
    database, calls Keepa/SP-API/SerpApi/Brave, or triggers a scan --
    it only reads ProductRecord/SellerNewListing/TrackedSeller rows
    that scans already produced, and reasons about them.
    """

    # ================================================================
    # PHASE 1 -- read-only aggregation
    # ================================================================

    @staticmethod
    def get_competitor_brand_evidence() -> dict:
        """
        Per-brand competitor EU/UK A2A evidence -- "are competitors
        finding EU A2A here?" ONLY. Says nothing about whether it's
        worked for US (see get_own_scan_performance for that, kept
        deliberately separate per the approved brief's section 9: "do
        not average the two into false confidence").

        Reuses the exact fields Competitor Watch's own drawer already
        reads (SellerNewListing.sourcing_tag, detected_at) -- no new
        classification, no new evidence source.

        Returns {normalized_brand: {
            "category_name": str (most common non-empty value seen),
            "eu_a2a_count": int, "uk_a2a_count": int,
            "distinct_competitors": int,
            "marketplaces_recent": set of "DE"/"FR"/"ES"/"IT" hit within
                RECENT_WINDOW_DAYS,
            "most_recent_detected_at": datetime | None,
            "recent": bool (any evidence within RECENT_WINDOW_DAYS),
        }}
        """
        db = SessionLocal()

        try:
            rows = (
                db.query(SellerNewListing, ProductRecord, TrackedSeller.id)
                .join(ProductRecord, SellerNewListing.product_record_id == ProductRecord.id)
                .join(TrackedSeller, SellerNewListing.tracked_seller_id == TrackedSeller.id)
                .filter(SellerNewListing.dismissed == False)
                .filter(SellerNewListing.sourcing_tag.in_(("EU A2A", "UK A2A")))
                .all()
            )
        finally:
            db.close()

        cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_WINDOW_DAYS)).replace(tzinfo=None)

        by_brand = {}
        competitors_by_brand = {}
        category_votes = {}

        for listing, record, tracked_seller_id in rows:
            brand = _normalize(record.brand)
            if not brand:
                continue

            entry = by_brand.setdefault(brand, {
                "eu_a2a_count": 0, "uk_a2a_count": 0,
                "marketplaces_recent": set(), "most_recent_detected_at": None,
            })

            if listing.sourcing_tag == "EU A2A":
                entry["eu_a2a_count"] += 1
            else:
                entry["uk_a2a_count"] += 1

            competitors_by_brand.setdefault(brand, set()).add(tracked_seller_id)

            if record.category_name:
                category_votes.setdefault(brand, {}).setdefault(record.category_name, 0)
                category_votes[brand][record.category_name] += 1

            detected_at = listing.detected_at
            if detected_at and (entry["most_recent_detected_at"] is None or detected_at > entry["most_recent_detected_at"]):
                entry["most_recent_detected_at"] = detected_at

            if detected_at and detected_at >= cutoff:
                marketplace = None
                if listing.sourcing_tag == "UK A2A":
                    marketplace = "UK"
                elif record.best_source_marketplace in EU_MARKETPLACES:
                    marketplace = record.best_source_marketplace
                if marketplace:
                    entry["marketplaces_recent"].add(marketplace)

        for brand, entry in by_brand.items():
            entry["distinct_competitors"] = len(competitors_by_brand.get(brand, set()))
            votes = category_votes.get(brand, {})
            entry["category_name"] = max(votes, key=votes.get) if votes else ""
            entry["recent"] = bool(entry["marketplaces_recent"])

        return by_brand

    @staticmethod
    def _own_scan_events(records: list) -> list:
        """
        Groups a brand's ProductRecord rows into discrete "scan events"
        -- there's no explicit scan-run id anywhere in Atlas, so this
        infers one event per distinct scanned_at MINUTE (BrandScanService
        persists every ASIN from one scan() call within the same
        second-or-two, confirmed by inspecting save_opportunity's own
        call sites -- one straight loop, no delay between rows). This
        is an inference, not a stored fact -- documented here rather
        than presented as more precise than it is.
        """
        by_minute = {}
        for r in records:
            if not r.scanned_at:
                continue
            key = r.scanned_at.replace(second=0, microsecond=0)
            by_minute.setdefault(key, []).append(r)

        events = []
        for when, batch in sorted(by_minute.items(), reverse=True):
            events.append({
                "when": when,
                "products": len(batch),
                "eu_a2a": sum(1 for r in batch if r.best_source_marketplace in EU_MARKETPLACES),
                "buy": sum(1 for r in batch if r.recommendation == "BUY"),
                "consider": sum(1 for r in batch if r.recommendation == "CONSIDER"),
                "avg_profit": (
                    round(sum(r.profit for r in batch if r.profit > 0) / max(1, sum(1 for r in batch if r.profit > 0)), 2)
                    if any(r.profit > 0 for r in batch) else 0.0
                ),
            })
        return events

    @staticmethod
    def get_own_scan_performance() -> dict:
        """
        Per-brand OUR OWN scan track record -- "when we've scanned this
        brand ourselves, did it produce real results?" Grouped by
        ProductRecord.brand (see this module's own top-of-file note for
        why, not brand_query), latest-record-per-ASIN for the headline
        counts (same convention ProductRepository.get_brand_performance
        already uses), plus real scan-event history (see
        _own_scan_events) for recency/trend.

        Returns {normalized_brand: {
            "category_name": str,
            "scanned": int, "eu_a2a_count": int, "buy_count": int,
            "consider_count": int, "hit_rate": float | None,
            "buy_rate": float | None -- buy_count / scanned only, NEVER
                blended with CONSIDER (2026-09-04 scoring fix -- see
                score_target's own note on why CONSIDER must not stand
                in for a confirmed BUY). This is the number the UI
                shows as "our BUY rate", separate from hit_rate above
                (which stays a broader "did we find ANYTHING worth a
                look" figure, still useful as display context, just no
                longer used to justify a profitability score bonus),
            "avg_profit": float (over profit>0 BUY/CONSIDER-tier rows --
                kept exactly as before, still real, still shown as
                context, just no longer alone enough to earn a score
                point -- see avg_buy_profit below for that),
            "avg_buy_profit": float (over profit>0 rows with a
                CONFIRMED BUY recommendation ONLY, 0.0 if buy_count==0)
                -- this is what score_target's profitability bonus
                actually reads now,
            "last_buy_at": datetime | None,
            "recent_events": list (see _own_scan_events, newest first,
                capped at RECENT_SCAN_EVENTS),
            "recent_hit_rate": float | None (over recent_events only),

            ---- ALL FIELDS ABOVE ARE MARKETPLACE-AGNOSTIC (kept for
            display context only as of 2026-09-04) -- EVERY ONE OF
            THEM COUNTS "BUY" REGARDLESS OF best_source_marketplace,
            including "UK-OA" (UK Online Arbitrage, not EU sourcing at
            all). Real bug found this way: Ninja and Shark's ONLY
            confirmed BUYs were both UK-OA -- every genuine EU-sourced
            (DE/FR) scan for both was IGNORE and unprofitable -- yet
            the score_target reason literally claimed "genuine EU A2A
            BUY". Affected 6 of 18 HIGH-tier brands when checked
            against real data (ninja, shark, amd, ubiquiti, cardo,
            blackmagic design -- ALL their BUY evidence was 100%
            non-EU). Fixed below with EU-scoped fields (same latest-
            per-ASIN dedup convention, additionally filtered to
            best_source_marketplace in EU_MARKETPLACES) -- these, NOT
            the fields above, are what score_target's OUR_SCANS
            bonuses now read. The fields above stay for transparency
            (the UI can still show "we've scanned this brand N times
            total, M of which were EU-sourced"), never as EU A2A
            evidence: ----

            "eu_buy_count": int, "eu_consider_count": int,
            "eu_buy_rate": float | None -- eu_buy_count / eu_a2a_count,
            "eu_avg_buy_profit": float,
            "eu_last_buy_at": datetime | None,
            "eu_recent_events": list (see _own_scan_events, restricted
                to EU-marketplace records BEFORE grouping, newest
                first, capped at RECENT_SCAN_EVENTS),
            "eu_recent_hit_rate": float | None (over eu_recent_events),

            ---- Phase 2 (2026-09-04) -- Action-aware evidence via
            OpportunityLensService.compute(), the SAME lens the Review
            Queue itself uses. This is what score_target's strongest
            own-scan signal and conflict cap read now -- see that
            method's own docstring for why literal recommendation==
            "BUY" (eu_buy_count above, kept unchanged for display) is no
            longer trusted as the primary signal: a confirmed BUY that's
            gone stale reads as HISTORICAL_RECURRING under the lens, not
            a live BUY_NOW, and a brand can carry real economic signal
            (HIGH_VALUE_LOW_CONFIDENCE) with zero literal BUY at all: ----

            "actions_all_time": {action: count} for all 8
                OpportunityLensService actions (ALL_ACTIONS), computed
                over eu_latest (already latest-per-ASIN, EU-scoped --
                cannot double-count a brand via superseded historical
                rows for the same ASIN). Never discarded.
            "actions_recent": same shape, restricted to eu_latest rows
                scanned within RECENT_WINDOW_DAYS -- kept VISIBLY
                SEPARATE from actions_all_time, never merged into one
                number (Phase 1's own recency requirement).
            "confirmed_buy_count_all_time" / "_recent": BUY_NOW +
                PRICE_DROP_BUY_NOW counts (CONFIRMED_BUY_ACTIONS) --
                deliberately excludes BUY_WITH_CAUTION (notable, but
                paired with an active risk flag).
            "high_value_low_confidence_count": actions_all_time's
                HIGH_VALUE_LOW_CONFIDENCE count -- real economics, still
                thin evidence; score_target treats this as meaningfully
                weaker than a confirmed BUY, never equivalent to it.
        }}
        """
        db = SessionLocal()

        try:
            rows = db.query(ProductRecord).order_by(ProductRecord.scanned_at.desc()).all()
        finally:
            db.close()

        by_brand_all = {}
        for r in rows:
            brand = _normalize(r.brand)
            if not brand:
                continue
            by_brand_all.setdefault(brand, []).append(r)

        result = {}
        for brand, records in by_brand_all.items():
            latest_by_asin = {}
            for r in records:  # already newest-first
                if r.asin not in latest_by_asin:
                    latest_by_asin[r.asin] = r
            latest = list(latest_by_asin.values())

            scanned = len(latest)
            eu_a2a_count = sum(1 for r in latest if r.best_source_marketplace in EU_MARKETPLACES)
            buy_count = sum(1 for r in latest if r.recommendation == "BUY")
            consider_count = sum(1 for r in latest if r.recommendation == "CONSIDER")
            buy_tier = [r for r in latest if r.recommendation in ("BUY", "CONSIDER") and r.profit > 0]
            avg_profit = round(sum(r.profit for r in buy_tier) / len(buy_tier), 2) if buy_tier else 0.0
            confirmed_buys = [r for r in latest if r.recommendation == "BUY" and r.profit > 0]
            avg_buy_profit = round(sum(r.profit for r in confirmed_buys) / len(confirmed_buys), 2) if confirmed_buys else 0.0
            buy_records = [r for r in latest if r.recommendation == "BUY" and r.scanned_at]
            last_buy_at = max((r.scanned_at for r in buy_records), default=None)

            category_votes = {}
            for r in latest:
                if r.category_name:
                    category_votes[r.category_name] = category_votes.get(r.category_name, 0) + 1

            events = DiscoveryIntelligenceService._own_scan_events(records)[:RECENT_SCAN_EVENTS]
            recent_buy = sum(e["buy"] for e in events)
            recent_products = sum(e["products"] for e in events)

            # EU-A2A-SCOPED fields (2026-09-04 fix) -- same latest-per-
            # ASIN dedup as above, additionally filtered to a real EU
            # marketplace. See this method's own docstring for the
            # Ninja/Shark bug this closes.
            eu_latest = [r for r in latest if r.best_source_marketplace in EU_MARKETPLACES]
            eu_buy_count = sum(1 for r in eu_latest if r.recommendation == "BUY")
            eu_consider_count = sum(1 for r in eu_latest if r.recommendation == "CONSIDER")
            eu_confirmed_buys = [r for r in eu_latest if r.recommendation == "BUY" and r.profit > 0]
            eu_avg_buy_profit = round(sum(r.profit for r in eu_confirmed_buys) / len(eu_confirmed_buys), 2) if eu_confirmed_buys else 0.0
            eu_buy_records = [r for r in eu_latest if r.recommendation == "BUY" and r.scanned_at]
            eu_last_buy_at = max((r.scanned_at for r in eu_buy_records), default=None)

            eu_records = [r for r in records if r.best_source_marketplace in EU_MARKETPLACES]
            eu_events = DiscoveryIntelligenceService._own_scan_events(eu_records)[:RECENT_SCAN_EVENTS]
            eu_recent_buy = sum(e["buy"] for e in eu_events)
            eu_recent_products = sum(e["products"] for e in eu_events)

            # ================================================================
            # Discovery Intelligence Phase 2 (2026-09-04) -- Action-aware
            # evidence via OpportunityLensService, the SAME lens the Review
            # Queue itself uses. Runs on eu_latest -- already the latest-
            # per-ASIN, EU-marketplace-scoped list computed above, so this
            # cannot double-count a brand's history via old, superseded
            # ProductRecord rows for the same ASIN. NOT recommendation==
            # "BUY" -- see this method's own docstring update below for why.
            actions_all_time = {a: 0 for a in ALL_ACTIONS}
            actions_recent = {a: 0 for a in ALL_ACTIONS}
            recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_WINDOW_DAYS)).replace(tzinfo=None)

            for r in eu_latest:
                lead = ReviewQueueService._scan_lead_dict(r)
                action = lens_service.compute(lead)["action"]
                actions_all_time[action] += 1
                if r.scanned_at and r.scanned_at >= recent_cutoff:
                    actions_recent[action] += 1

            confirmed_buy_count_all_time = sum(actions_all_time[a] for a in CONFIRMED_BUY_ACTIONS)
            confirmed_buy_count_recent = sum(actions_recent[a] for a in CONFIRMED_BUY_ACTIONS)
            high_value_low_confidence_count = actions_all_time[lens_service.ACTION_HIGH_VALUE_LOW_CONFIDENCE]

            result[brand] = {
                "category_name": max(category_votes, key=category_votes.get) if category_votes else "",
                "scanned": scanned,
                "eu_a2a_count": eu_a2a_count,
                "buy_count": buy_count,
                "consider_count": consider_count,
                "hit_rate": round((buy_count + consider_count) / scanned * 100, 1) if scanned else None,
                "buy_rate": round(buy_count / scanned * 100, 1) if scanned else None,
                "avg_profit": avg_profit,
                "avg_buy_profit": avg_buy_profit,
                "last_buy_at": last_buy_at,
                "recent_events": events,
                "recent_hit_rate": round(recent_buy / recent_products * 100, 1) if recent_products else None,
                "eu_buy_count": eu_buy_count,
                "eu_consider_count": eu_consider_count,
                "eu_buy_rate": round(eu_buy_count / eu_a2a_count * 100, 1) if eu_a2a_count else None,
                "eu_avg_buy_profit": eu_avg_buy_profit,
                "eu_last_buy_at": eu_last_buy_at,
                "eu_recent_events": eu_events,
                "eu_recent_hit_rate": round(eu_recent_buy / eu_recent_products * 100, 1) if eu_recent_products else None,

                # ---- Phase 2 Action-aware evidence (2026-09-04) ----
                # actions_all_time/actions_recent kept VISIBLY SEPARATE
                # (never merged into one count) -- Discovery Intelligence
                # Phase 1's own recency requirement: older evidence is
                # never discarded, just distinguished from current
                # evidence. Both are EU-marketplace-scoped, latest-per-
                # ASIN (same eu_latest list eu_buy_count etc above use).
                "actions_all_time": actions_all_time,
                "actions_recent": actions_recent,
                # BUY_NOW + PRICE_DROP_BUY_NOW -- what score_target's
                # strongest own-scan signal and conflict cap now read,
                # replacing the old literal recommendation=="BUY" count
                # (still available above as eu_buy_count, unchanged, for
                # any caller still relying on that literal count).
                "confirmed_buy_count_all_time": confirmed_buy_count_all_time,
                "confirmed_buy_count_recent": confirmed_buy_count_recent,
                "high_value_low_confidence_count": high_value_low_confidence_count,
            }

        return result

    @staticmethod
    def get_category_performance() -> dict:
        """
        Category-level rollup (root category only -- see this module's
        own note on why: Atlas has never stored Keepa's deeper category
        tree). Combines competitor evidence and our own performance,
        grouped by category_name, kept as two separate sub-dicts (never
        blended into one number) for the same reason brand-level
        evidence stays separated -- see get_competitor_brand_evidence/
        get_own_scan_performance's own docstrings.
        """
        competitor = DiscoveryIntelligenceService.get_competitor_brand_evidence()
        own = DiscoveryIntelligenceService.get_own_scan_performance()

        by_category = {}

        def _bucket(category_name):
            if not category_name:
                return None
            return by_category.setdefault(category_name, {
                "brands": set(),
                "competitor_eu_a2a": 0, "competitor_uk_a2a": 0,
                "own_buy_count": 0, "own_scanned": 0,
            })

        for brand, evidence in competitor.items():
            bucket = _bucket(evidence.get("category_name"))
            if bucket is None:
                continue
            bucket["brands"].add(brand)
            bucket["competitor_eu_a2a"] += evidence["eu_a2a_count"]
            bucket["competitor_uk_a2a"] += evidence["uk_a2a_count"]

        for brand, perf in own.items():
            bucket = _bucket(perf.get("category_name"))
            if bucket is None:
                continue
            bucket["brands"].add(brand)
            bucket["own_buy_count"] += perf["buy_count"]
            bucket["own_scanned"] += perf["scanned"]

        for cat in by_category.values():
            cat["brand_count"] = len(cat["brands"])
            del cat["brands"]

        return by_category

    # ================================================================
    # PHASE 2 -- explainable scoring (pure function, no I/O)
    # ================================================================

    @staticmethod
    def score_target(brand: str, competitor: dict | None, own: dict | None,
                      top_categories: set, extra_gated_pairs_by_name: set = frozenset()) -> dict:
        """
        The approved additive scoring model (design brief section C),
        corrected 2026-09-04 after real-data validation surfaced one
        genuine flaw (the "Western Digital" case: 5 competitors + top
        category + a single profitable CONSIDER-tier row, but ZERO
        confirmed BUY across 5 of our own scans, reached HIGH). Two
        changes, both authorised, neither a redesign:

        1. The profitability bonus now requires a CONFIRMED BUY
           (buy_count > 0) and reads avg_buy_profit (BUY-tier only),
           not avg_profit (which blends in CONSIDER). CONSIDER-tier
           evidence is still real and still shown (see the `evidence`
           dict below) -- it just can no longer stand in for a
           confirmed BUY when deciding profitability.

        2. CONFLICT CAP: if a brand has been "meaningfully scanned"
           (>= MEANINGFUL_SCAN_THRESHOLD of our own ASINs -- reusing
           the SAME bar the existing "3 straight failures" rule
           already used, rather than inventing a second threshold) and
           produced ZERO confirmed BUYs, competitor/category evidence
           alone can no longer push it past MEDIUM. The raw score and
           every reason are still shown UNCAPPED -- the cap is a
           visible, explained ceiling on the final tier, not a hidden
           point deduction. A brand with real evidence toward
           MEANINGFUL_SCAN_THRESHOLD isn't affected (see section 3 of
           the approving instruction -- "must NOT be interpreted as
           0 BUY = bad" for brands we simply haven't tested yet).

        3. GATED OVERRIDE (2026-09-04, second real bug caught the same
           way as #1 above -- this time by direct user review of the
           Top 10 rather than the sensitivity script): Canon reached
           HIGH despite being in the gated_brands table with no
           category restriction (Atlas cannot currently get Amazon's
           approval to sell it AT ALL). This scoring model had zero
           awareness of gating before this fix -- wired in the exact
           same gated_brands lookup BrandScanService already uses to
           block brand-search scanning of a gated brand, not a new
           gating concept. Unconditional and takes priority over every
           other tier outcome (including a real, evidence-backed HIGH):
           a brand we cannot legally sell isn't a "scan more of this"
           recommendation no matter how good the competitor/own-scan
           evidence looks.

        4. PLUG-RISK CAUTION (2026-09-04, user-raised concern, not a
           confirmed bug -- unlike #1-3, no bad ranking was found; this
           is a gap Atlas has no data to close automatically). A
           laptop or mains-powered appliance sourced via EU A2A may
           physically arrive with an EU plug. Explicitly NOT a score
           penalty or gate ("I don't want to reject the whole brand")
           -- see EU_A2A_PLUG_RISK_CATEGORIES' own comment for the full
           reasoning, including why this is category-scoped rather
           than brand-scoped, and why it only fires against real EU
           A2A evidence (never UK A2A/OA, which source domestically).

        5. NON-EU "OUR SCANS" EVIDENCE FIX (2026-09-04, third real bug,
           caught the same way as #1/#3 -- user noticed Ninja/Shark
           ranking HIGH despite rarely seeing EU A2A leads from them in
           practice). "Our own scan performance" previously counted a
           BUY as evidence regardless of best_source_marketplace --
           including UK-OA (UK Online Arbitrage, not EU sourcing at
           all). Confirmed against real data: Ninja and Shark's ONLY
           confirmed BUYs were both UK-OA, while every genuine EU-
           sourced (DE/FR) scan for both was IGNORE and unprofitable --
           yet the score claimed "genuine EU A2A BUY". Affected 6 of
           18 HIGH-tier brands (ninja, shark, amd, ubiquiti, cardo,
           blackmagic design -- every one had 100% non-EU BUY
           evidence). Fixed by reading get_own_scan_performance's new
           eu_-prefixed fields (own EU-marketplace-only, same latest-
           per-ASIN dedup) instead of the marketplace-agnostic ones,
           which remain for display context only -- see that method's
           own docstring.

        competitor/own: the per-brand dicts get_competitor_brand_evidence/
        get_own_scan_performance return (or None if this brand has no
        evidence of that kind at all -- a brand can legitimately have
        one without the other).

        extra_gated_pairs_by_name: DB-backed GatedBrand rows as
        (brand_lower, category_name_lower or "") pairs -- see
        ProductRepository.get_gated_brand_pairs_by_name(). Fetched ONCE
        by list_discovery_targets() for the whole ranked list, not
        per-brand -- same pattern BrandScanService's own gating check
        already uses.

        Returns {score, tier, reasons: [{text, points, kind}],
        source, category_name, capped: bool, gated: bool,
        plug_risk: bool, evidence: {competitor, own, category} -- the
        three kept SEPARATE (never blended into one number) for the
        UI's three-box display, each with the raw numbers a human
        needs to judge Atlas's own recommendation against}. `kind` is
        "pos"/"neg"/"weak" for the UI's checklist styling.
        """
        score = 0
        reasons = []
        sources = set()

        competitor = competitor or {}
        own = own or {}
        category_name = competitor.get("category_name") or own.get("category_name") or ""

        distinct_competitors = competitor.get("distinct_competitors", 0)
        if distinct_competitors >= 3:
            score += 3
            reasons.append({"text": f"{distinct_competitors} distinct competitors found EU/UK A2A here (last {RECENT_WINDOW_DAYS}d)", "points": 3, "kind": "pos"})
            sources.add("COMPETITOR")
        elif distinct_competitors == 2:
            score += 2
            reasons.append({"text": "2 distinct competitors found EU/UK A2A here", "points": 2, "kind": "pos"})
            sources.add("COMPETITOR")
        elif distinct_competitors == 1 and competitor.get("recent"):
            score += 1
            reasons.append({"text": "1 competitor, recent evidence", "points": 1, "kind": "pos"})
            sources.add("COMPETITOR")

        if category_name and category_name in top_categories:
            score += 1
            reasons.append({"text": f'"{category_name}" is currently a top-{TOP_CATEGORY_COUNT} EU A2A category', "points": 1, "kind": "pos"})
            sources.add("CATEGORY")

        # scanned/buy_count are EU-MARKETPLACE-SCOPED as of 2026-09-04
        # (own["eu_a2a_count"]/own["eu_buy_count"], not the marketplace-
        # agnostic own["scanned"]/own["buy_count"]) -- see this method's
        # own docstring point 5 for the real bug this closes (Ninja/
        # Shark's only BUYs were UK-OA, not EU A2A at all). total_scanned
        # is kept separately, display-only, so the UI can still show
        # "we've scanned this brand N times total" for context.
        scanned = own.get("eu_a2a_count", 0)
        # Discovery Intelligence Phase 2 (2026-09-04) -- buy_count now
        # means Action-CONFIRMED evidence (BUY_NOW + PRICE_DROP_BUY_NOW,
        # via OpportunityLensService -- see get_own_scan_performance's
        # own docstring), not literal recommendation=="BUY". A stale
        # confirmed BUY no longer counts as fresh evidence here; it's
        # handled by the separate "historical" branch just below instead.
        buy_count = own.get("confirmed_buy_count_all_time", 0)
        buy_count_recent = own.get("confirmed_buy_count_recent", 0)
        high_value_low_confidence_count = own.get("high_value_low_confidence_count", 0)
        total_scanned = own.get("scanned", 0)

        if scanned:
            if buy_count_recent > 0:
                score += 3
                plural = "" if buy_count_recent == 1 else "s"
                reasons.append({
                    "text": f"Our recent EU scans produced {buy_count_recent} confirmed BUY opportunit{'y' if buy_count_recent == 1 else 'ies'}",
                    "points": 3, "kind": "pos",
                })
                sources.add("OUR_SCANS")
            elif buy_count > 0:
                # Historical confirmed BUY evidence (Discovery
                # Intelligence Phase 1 design) -- real, but no longer
                # recent (see actions_recent vs actions_all_time's own
                # docstring: the SAME confirmed-BUY-tier ASIN is now
                # reading as HISTORICAL_RECURRING under the lens, e.g.
                # because it hasn't been rechecked recently). Weaker
                # signal, existing +1 magnitude (not the +3 a fresh
                # confirmed BUY earns) -- never invented, reused from
                # the profitability bonus just below.
                score += 1
                reasons.append({
                    "text": "Previous confirmed BUY evidence exists, but it is no longer recent",
                    "points": 1, "kind": "pos",
                })
                sources.add("OUR_SCANS")

            if high_value_low_confidence_count >= HIGH_VALUE_LOW_CONFIDENCE_MEANINGFUL_COUNT:
                # Real economics, still-thin evidence (Discovery
                # Intelligence Phase 1 design) -- deliberately the SAME
                # +1 magnitude as the historical-BUY branch above, and
                # additive with it (a brand can genuinely have both) --
                # explicitly NEVER the +3 a confirmed BUY earns, per the
                # brief's own instruction not to treat these as
                # equivalent.
                score += 1
                reasons.append({
                    "text": f"{high_value_low_confidence_count} HIGH VALUE — LOW CONFIDENCE opportunities across {scanned} EU scans",
                    "points": 1, "kind": "pos",
                })
                sources.add("OUR_SCANS")

            # Plain factual context (Discovery Intelligence Phase 2) --
            # shown whenever it's genuinely true, independent of whether
            # the conflict cap below actually changes the tier, so the
            # "0 confirmed BUY despite real scanning" fact is always
            # visible, not only on the (rarer) occasions it's strong
            # enough to cap a HIGH down to MEDIUM. kind="weak": a
            # neutral fact, not itself a score penalty (see interpretation
            # rule: 0 BUY must never be read as "bad brand" on its own).
            if scanned >= MEANINGFUL_SCAN_THRESHOLD and buy_count == 0:
                reasons.append({
                    "text": f"We have scanned this brand {scanned} times with no confirmed BUY",
                    "points": 0, "kind": "weak",
                })

            recent_hit_rate = own.get("eu_recent_hit_rate")
            if recent_hit_rate is not None and recent_hit_rate >= 20:
                score += 2
                reasons.append({"text": f"Our EU A2A hit rate is {recent_hit_rate:.0f}% over recent EU-sourced scans", "points": 2, "kind": "pos"})
                sources.add("OUR_SCANS")

            # FIXED 2026-09-04: was `own.get("avg_profit", 0) > 5` --
            # avg_profit blends in CONSIDER-tier rows, which let a
            # brand with ZERO confirmed BUYs still earn a
            # "profitability" point (the exact Western Digital bug).
            # Now gated on buy_count > 0 and reads eu_avg_buy_profit
            # (EU-sourced BUY-tier only).
            if buy_count > 0 and own.get("eu_avg_buy_profit", 0) > 5:
                score += 1
                reasons.append({"text": f"Average profit £{own['eu_avg_buy_profit']:.2f} on our CONFIRMED EU A2A BUY finds", "points": 1, "kind": "pos"})
                sources.add("OUR_SCANS")

            recent_events = own.get("eu_recent_events") or []
            if len(recent_events) >= 3:
                last_3 = recent_events[:3]
                if all(e["buy"] == 0 and e["consider"] == 0 for e in last_3):
                    score -= 2
                    reasons.append({"text": "Our last 3 EU-sourced scans found 0 BUY and 0 CONSIDER", "points": -2, "kind": "neg"})

            if not competitor and not (own.get("eu_last_buy_at")):
                score -= 1
                reasons.append({"text": f"No evidence (ours or competitors') in {RECENT_WINDOW_DAYS}+ days", "points": -1, "kind": "neg"})
        elif total_scanned:
            # We HAVE scanned this brand, just never via a real EU
            # marketplace (e.g. Ninja/Shark before this fix -- all
            # their history was UK-OA) -- genuinely different from
            # never having looked at the brand at all, so said plainly
            # rather than reusing the "haven't scanned yet" wording.
            reasons.append({
                "text": f"We've scanned this brand {total_scanned} time(s), but never via a real EU marketplace (DE/FR/ES/IT) -- no EU A2A evidence of our own yet",
                "points": 0, "kind": "weak",
            })
        else:
            reasons.append({"text": "We haven't scanned this brand ourselves yet", "points": 0, "kind": "weak"})

        # Display labels for each internal source token -- kept separate
        # from the token itself (rather than just underscore->space)
        # so the two can drift in wording without a fragile string
        # transform, and so the join order below is always predictable.
        source_labels = {"COMPETITOR": "COMPETITOR", "OUR_SCANS": "OUR SCANS", "CATEGORY": "CATEGORY"}
        source_order = ["COMPETITOR", "OUR_SCANS", "CATEGORY"]

        if not competitor and not scanned:
            source = "MANUAL"
        elif sources:
            source = " + ".join(source_labels[s] for s in source_order if s in sources)
        else:
            source = "MIXED EVIDENCE" if (competitor or scanned) else "MANUAL"

        raw_tier = "HIGH" if score >= 5 else ("MEDIUM" if score >= 2 else "LOW")

        # CONFLICT CAP (approved 2026-09-04; buy_count recalculated to
        # Action-confirmed evidence in Phase 2 -- see this method's own
        # docstring point 6) -- meaningfully scanned, zero CONFIRMED
        # (BUY_NOW/PRICE_DROP_BUY_NOW, recent or historical) EU A2A
        # evidence: competitor/category evidence alone cannot reach
        # HIGH. A brand we haven't meaningfully tested yet (scanned <
        # MEANINGFUL_SCAN_THRESHOLD) is explicitly NOT affected -- see
        # PROMISING_INSUFFICIENT_DATA below, which is the correct label
        # for THAT case, distinct from a conflict. A brand with real
        # HIGH_VALUE_LOW_CONFIDENCE volume is still capped here too --
        # that's the brief's own explicit instruction (never equivalent
        # to a confirmed BUY), the +1 it already earned above stays on
        # the score, it just isn't enough alone to clear the cap.
        capped = False
        if scanned >= MEANINGFUL_SCAN_THRESHOLD and buy_count == 0 and raw_tier == "HIGH":
            tier = "MEDIUM"
            capped = True
            reasons.append({
                "text": f"Capped at MEDIUM — {scanned} of our own EU-sourced scans, 0 confirmed EU A2A BUY, despite strong competitor/category evidence",
                "points": 0, "kind": "neg",
            })
        else:
            tier = raw_tier

        # GATED override (2026-09-04, real bug: Canon reached HIGH despite
        # being in gated_brands with no category restriction -- i.e.
        # Atlas cannot currently get Amazon's approval to sell it at
        # all). Checked against the SAME gated_brands table BrandScanService
        # itself already blocks brand-search scanning against (see
        # exclusions.is_gated_by_name/ProductRepository.get_gated_brand_
        # pairs_by_name) -- not a new gating concept, just wiring an
        # existing one into this scoring, which previously had zero
        # awareness of it. Unconditional: unlike the conflict cap above,
        # a real evidence-backed HIGH score doesn't matter if we can't
        # sell the brand at all -- takes priority over any other tier
        # this brand would otherwise have earned.
        gated = is_gated_by_name(brand, category_name or "", extra_gated_pairs_by_name)
        if gated:
            tier = "LOW"
            reasons.append({
                "text": "GATED on this account -- Atlas cannot currently get Amazon approval to sell this brand, so scanning more of it isn't actionable",
                "points": 0, "kind": "neg",
            })

        # EU A2A PLUG-RISK CAUTION (approved 2026-09-04) -- see
        # EU_A2A_PLUG_RISK_CATEGORIES' own comment for the full
        # reasoning. Deliberately does NOT touch score/tier (explicit
        # instruction: "I don't want to reject the whole brand") --
        # purely an informational flag for a human to check the
        # listing's plug type before buying. Only fires when there is
        # REAL EU A2A evidence behind this recommendation (competitor
        # eu_a2a_count or our own eu_a2a_count > 0) -- UK A2A and OA
        # source domestically, so the plug is never in question there,
        # and this must not follow the brand into those.
        eu_a2a_evidence = competitor.get("eu_a2a_count", 0) > 0 or own.get("eu_a2a_count", 0) > 0
        plug_risk = bool(category_name in EU_A2A_PLUG_RISK_CATEGORIES and eu_a2a_evidence)
        if plug_risk:
            reasons.append({
                "text": f'"{category_name}" often includes mains-powered items -- if sourcing this EU A2A, confirm the plug type before buying',
                "points": 0, "kind": "weak",
            })

        # Explicit, never-blended evidence for the UI's three-box
        # display (approved brief section 6) -- competitor, own, and
        # category always shown separately, buy_rate always visible
        # rather than hidden behind the score.
        evidence = {
            "competitor": {
                "distinct_competitors": distinct_competitors,
                "eu_a2a_count": competitor.get("eu_a2a_count", 0),
                "uk_a2a_count": competitor.get("uk_a2a_count", 0),
                "recent": competitor.get("recent", False),
                "marketplaces_recent": sorted(competitor.get("marketplaces_recent", set())),
            } if competitor else None,
            "own": {
                # EU-marketplace-sourced evidence -- what score_target's
                # OUR_SCANS bonuses actually read (2026-09-04 fix).
                "eu_scanned": scanned,
                # Phase 2 (2026-09-04) -- Action-CONFIRMED evidence
                # (BUY_NOW + PRICE_DROP_BUY_NOW via OpportunityLensService),
                # kept recent/all-time VISIBLY SEPARATE, plus the full
                # Action breakdown for a caller/report that wants the
                # whole picture, not just the confirmed-BUY count.
                "eu_confirmed_buy_count_all_time": buy_count,
                "eu_confirmed_buy_count_recent": buy_count_recent,
                "eu_high_value_low_confidence_count": high_value_low_confidence_count,
                "eu_actions_all_time": own.get("actions_all_time"),
                "eu_actions_recent": own.get("actions_recent"),
                # Literal recommendation=="BUY" count -- kept for
                # comparison/transparency only, NOT what score_target
                # reads any more (see this method's own docstring).
                "eu_literal_buy_count": own.get("eu_buy_count", 0),
                "eu_buy_rate": own.get("eu_buy_rate"),
                "eu_avg_buy_profit": own.get("eu_avg_buy_profit", 0.0),
                "eu_consider_count": own.get("eu_consider_count", 0),
                # Total scan activity across EVERY channel (including
                # UK-OA/UK marketplace) -- kept visible for context,
                # NEVER counted as EU A2A evidence. This block used to
                # only appear when `scanned` (now EU-only) was truthy,
                # which hid a brand's real, if non-EU, scan history
                # entirely -- fixed so it shows whenever we've looked
                # at the brand via ANY channel.
                "total_scanned": total_scanned,
                "total_buy_count": own.get("buy_count", 0),
                "non_eu_buy_count": own.get("buy_count", 0) - own.get("eu_buy_count", 0),
            } if total_scanned else None,
            "category": {"name": category_name, "is_top": category_name in top_categories} if category_name else None,
        }

        return {
            "brand": brand, "category_name": category_name, "score": score,
            "raw_tier": raw_tier, "tier": tier, "capped": capped, "gated": gated,
            "plug_risk": plug_risk, "reasons": reasons, "source": source, "evidence": evidence,
        }

    @staticmethod
    def list_discovery_targets(limit: int = 200, use_cache: bool = True) -> list:
        """
        The full ranked list Phase 4's UI will show -- every brand with
        EITHER competitor evidence OR our own scan history, scored and
        sorted HIGH-to-LOW. Manually-added brands with NO history at
        all can't appear here yet (there's nothing to rank them
        against) -- that's Phase 3's job (DiscoveryTarget persistence),
        not this read-only phase.

        Cached for _TARGETS_CACHE_TTL_SECONDS (see that constant's own
        comment) -- `limit` only slices the cached, already-scored list,
        so passing a smaller limit never returns fewer BRANDS considered,
        only fewer shown. Pass use_cache=False to force a fresh
        recomputation (e.g. right after a scan you know changed the
        evidence and want to see reflected immediately).
        """
        global _targets_cache, _targets_cache_built_at

        now = _time.monotonic()
        if use_cache and _targets_cache and (now - _targets_cache_built_at) < _TARGETS_CACHE_TTL_SECONDS:
            return _targets_cache[:limit]

        competitor = DiscoveryIntelligenceService.get_competitor_brand_evidence()
        own = DiscoveryIntelligenceService.get_own_scan_performance()
        categories = DiscoveryIntelligenceService.get_category_performance()

        # Fetched once for the whole ranked list, same pattern
        # BrandScanService's own gating check uses (one query per scan/
        # list-build, not per brand) -- see score_target's own note on
        # the 2026-09-04 Canon fix.
        gated_pairs = ProductRepository.get_gated_brand_pairs_by_name()

        top_categories = set(
            sorted(
                categories.keys(),
                key=lambda c: categories[c]["competitor_eu_a2a"] + categories[c]["competitor_uk_a2a"],
                reverse=True,
            )[:TOP_CATEGORY_COUNT]
        )

        all_brands = set(competitor.keys()) | set(own.keys())

        results = []
        for brand in all_brands:
            scored = DiscoveryIntelligenceService.score_target(
                brand, competitor.get(brand), own.get(brand), top_categories, gated_pairs,
            )
            scored["competitor_evidence"] = competitor.get(brand)
            scored["own_performance"] = own.get(brand)
            results.append(scored)

        results.sort(key=lambda r: r["score"], reverse=True)

        if use_cache:
            _targets_cache = results
            _targets_cache_built_at = now

        return results[:limit]
