"""
Free (no-Keepa-token) rank/price screen for OA candidates -- step 2 of
atlas-oa-scale-up-spec.md's build order. See OaCandidatePool's
docstring (app/database/models.py) for why this only runs a BACKFILL
VALIDATION against ASINs the existing Competitor Watch pipeline has
already scored via Keepa, and is not wired into the live discovery
queue yet -- that's step 4, deliberately later.
"""
from app.database.database import SessionLocal
from app.database.models import OaCandidatePool
from app.services.oa_source_discovery_service import OaSourceDiscoveryService
from app.services.product_repository import ProductRepository
from app.services.fee_engine import FeeEngine
from app.services.token_usage_service import TokenUsageService
from app.config.exclusions import is_excluded, is_gated
from app.sp_api.client import get_sp_api_client

# PLACEHOLDER -- not a real business decision yet. The spec (§11) says
# "start loose, let the first run show the distribution" for exactly
# this number; it has NOT been reviewed against Tamara's real rank
# distribution. Treat the backfill report's rank breakdown as the
# actual input for picking a real threshold -- don't trust this figure
# for a live decision.
MAX_SALES_RANK_UK = 200_000

# Same estimated Keepa-token-equivalent-cost-avoided figure
# brand_scan_service.py's SP-API integration uses for its own
# record_sp_api_saved calls (one EU marketplace lookup skipped = 1
# token), so Token Usage's "SP-API saved" figure stays on one
# consistent estimate across both call sites rather than each guessing
# its own number.
ESTIMATED_TOKENS_PER_SP_API_LOOKUP = 1


def screen_asin(asin: str, record, sp_client, gated_brand_pairs: set, excluded_category_ids: set) -> dict:
    """
    Runs ONE ASIN through the free screen. `record` is an existing
    ProductRecord (see OaSourceDiscoveryService._eligible_candidate_rows,
    already Keepa-scored) -- used as the source of truth for
    brand/category, since SP-API's catalog data doesn't carry Keepa's
    numeric category IDs Atlas's exclusion/gating lists are keyed on
    (unresolved for step 4's brand-new, no-Keepa-data ASINs -- see this
    module's docstring). SP-API's OWN rank/price is used where
    available; record.buy_box_now is only used as the price to test
    against when SP-API had a rank/gating answer but no price of its
    own -- never as a substitute for a fully-unreachable SP-API (see
    "sp_api_unavailable" below), so a backfill row is never silently
    scored using ONLY Keepa data with no SP-API involvement at all.

    Returns a dict shaped for an OaCandidatePool row (not yet
    persisted -- see screen_backfill_candidates for that). Checks run
    cheapest/most-decisive-first: gating and exclusion cost nothing and
    are unambiguous, so they're checked before any SP-API call is made.
    """
    if is_gated(record.brand, record.category, gated_brand_pairs):
        return {
            "status": "screened_out", "screened_out_reason": "gated",
            "amazon_price_gbp": None, "sales_rank": None, "target_price_gbp": 0.0,
        }

    if is_excluded(asin, record.brand, record.category, excluded_category_ids):
        return {
            "status": "screened_out", "screened_out_reason": "excluded",
            "amazon_price_gbp": None, "sales_rank": None, "target_price_gbp": 0.0,
        }

    sp_rank = None
    sp_price = None
    sp_reachable = False

    if sp_client:
        catalog = sp_client.search_catalog_items([asin], marketplace="UK")
        if catalog is not None:
            sp_reachable = True
            TokenUsageService.record_sp_api_saved(
                "oa_candidate_screen", "UK", 1, ESTIMATED_TOKENS_PER_SP_API_LOOKUP,
            )
            entry = catalog.get(asin)
            if entry:
                sp_rank = entry.get("rank")

        offer = sp_client.get_item_offers(asin, "UK")
        if offer is not None:
            sp_reachable = True
            TokenUsageService.record_sp_api_saved(
                "oa_candidate_screen", "UK", 1, ESTIMATED_TOKENS_PER_SP_API_LOOKUP,
            )
            if offer.get("price"):
                sp_price = offer["price"]

    # SP-API not configured, or both calls failed outright -- record
    # this as its own reason rather than quietly falling back to pure
    # Keepa data, so a backfill report can tell "the screen ran and
    # rejected it" apart from "the screen never actually ran".
    if not sp_reachable:
        return {
            "status": "screened_out", "screened_out_reason": "sp_api_unavailable",
            "amazon_price_gbp": None, "sales_rank": None, "target_price_gbp": 0.0,
        }

    if sp_rank is not None and sp_rank > MAX_SALES_RANK_UK:
        return {
            "status": "screened_out", "screened_out_reason": "rank_too_high",
            "amazon_price_gbp": sp_price, "sales_rank": sp_rank, "target_price_gbp": 0.0,
        }

    effective_price = sp_price if sp_price is not None else record.buy_box_now

    target_price = FeeEngine.max_source_cost(
        effective_price, record.category_name, None, FeeEngine.OA_TARGET_ROI_PCT,
    ) if effective_price else 0.0

    if target_price <= 0:
        return {
            "status": "screened_out", "screened_out_reason": "no_headroom",
            "amazon_price_gbp": sp_price, "sales_rank": sp_rank, "target_price_gbp": target_price,
        }

    return {
        "status": "screened_in", "screened_out_reason": "",
        "amazon_price_gbp": sp_price, "sales_rank": sp_rank, "target_price_gbp": target_price,
    }


def screen_backfill_candidates(limit: int = 200) -> dict:
    """
    Step 2's actual deliverable: run the free screen against ASINs the
    EXISTING Competitor Watch pipeline already scored via Keepa (see
    OaSourceDiscoveryService._eligible_candidate_rows), persist each
    outcome as an OaCandidatePool row (source="competitor_watch_backfill"),
    and return a summary so a human can judge whether the free screen
    rejects sensibly before step 4 lets it gate brand-new ASINs with no
    Keepa fallback at all.

    Deliberately makes NO Keepa calls itself -- SP-API only, plus data
    already sitting on the ProductRecord from a PRIOR Keepa scan -- so
    KeepaPriority/ScanCoordinator's low-priority-yield machinery (spec
    §6) doesn't apply here; that machinery matters once step 4 adds a
    live, Product-Finder-driven intake that DOES spend fresh Keepa
    tokens on candidates nothing has scored yet.
    """
    sp_client = get_sp_api_client()
    if sp_client is None:
        print(
            "SP-API not configured (SP_API_CLIENT_ID/SECRET/REFRESH_TOKEN missing "
            "from .env) -- every ASIN below will screen out as sp_api_unavailable."
        )

    gated_brand_pairs = ProductRepository.get_gated_brand_pairs()
    excluded_category_ids = ProductRepository.get_excluded_category_ids()

    print("Fetching eligible Competitor Watch ASINs (this alone can take a moment)...")
    rows = OaSourceDiscoveryService._eligible_candidate_rows()[:limit]
    print(f"Found {len(rows)} eligible ASINs -- screening now (roughly 1-2s per ASIN, so "
          f"this can take a few minutes; progress prints every ASIN, and each row is "
          f"committed as it's screened, so killing this partway keeps what's done so far).")

    db = SessionLocal()
    summary = {"screened_in": 0, "screened_out": 0, "reasons": {}}

    try:
        for i, row in enumerate(rows, start=1):
            asin = row["listing"].asin
            record = row["record"]

            result = screen_asin(asin, record, sp_client, gated_brand_pairs, excluded_category_ids)

            db.add(OaCandidatePool(
                asin=asin,
                source="competitor_watch_backfill",
                status=result["status"],
                screened_out_reason=result["screened_out_reason"],
                amazon_price_gbp=result["amazon_price_gbp"],
                sales_rank=result["sales_rank"],
                target_price_gbp=result["target_price_gbp"],
                notes=f"known buy_box_now={record.buy_box_now}, category={record.category_name}",
            ))
            # Committed per-ASIN (not batched at the end) so a killed/crashed
            # run keeps whatever it already screened, and the table can be
            # inspected mid-run rather than only after a full pass completes.
            db.commit()

            if result["status"] == "screened_in":
                summary["screened_in"] += 1
            else:
                summary["screened_out"] += 1
                reason = result["screened_out_reason"]
                summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1

            print(f"  [{i}/{len(rows)}] {asin}: {result['status']}"
                  + (f" ({result['screened_out_reason']})" if result["screened_out_reason"] else ""))
    finally:
        db.close()

    summary["total"] = len(rows)
    return summary
