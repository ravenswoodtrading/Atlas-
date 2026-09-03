"""
Step 3 of atlas-oa-scale-up-spec.md: runs the SAME fixed batch of
ASINs through both search providers (SerpApi and Serper) via
OaSourceDiscoveryService.run_batch(test_mode=True, ...), then prints a
side-by-side comparison so you can pick a provider ON EVIDENCE (per
app/services/shopping_search.py's own docstring) rather than on cost
alone -- the spec is explicit that Serper's much lower price isn't by
itself a reason to switch if SerpApi finds materially more/better
matches.

Both passes are test_mode=True: nothing here touches your real Review
Queue, Dashboard counters, or Discord -- see OaCandidatePool/OaSourceRun.is_test
in app/database/models.py for the full isolation. Safe to re-run.
Fetches Keepa data ONCE and shares it between both provider passes
(the two runs only differ in the search step), so this doesn't spend
Keepa tokens twice for a difference that isn't there.

Run with `python test_harness_search_providers.py [N]` -- N optional,
defaults to 25 ASINs. Needs whichever of SERPAPI_API_KEY / SERPER_API_KEY
(see .env) you want compared; a provider with no key configured will
legitimately show 0 candidates for its pass -- a real, if boring,
result, not an error.
"""
import os
import sys
import time

from app.database.database import SessionLocal
from app.database.models import OaSourceCandidate
from app.services.oa_source_discovery_service import OaSourceDiscoveryService
from app.services.product_service import ProductService, KeepaTokensExhaustedError
from app.services import shopping_search

PROVIDERS = ["serpapi", "serper"]


def match_tier_breakdown(run_id: int) -> dict:
    db = SessionLocal()
    try:
        rows = db.query(OaSourceCandidate.match_tier).filter(OaSourceCandidate.run_id == run_id).all()
        breakdown = {}
        for (tier,) in rows:
            key = tier or "no_match"
            breakdown[key] = breakdown.get(key, 0) + 1
        return breakdown
    finally:
        db.close()


def run_one_provider(provider: str, asins: list, raw_products: list) -> dict:
    os.environ["OA_SHOPPING_PROVIDER"] = provider
    active = shopping_search.active_provider_name()
    if active != provider:
        print(f"  WARNING: asked for {provider!r} but shopping_search reports {active!r} is active "
              f"(likely an unrecognised value falling back to the default -- see active_provider_name's "
              f"own docstring). Results below are actually for {active!r}.")

    cost_per_1000 = shopping_search.cost_per_1000_usd()
    print(f"\n--- Running test batch through {active} ({len(asins)} ASINs, "
          f"${cost_per_1000}/1000 searches) ---")

    started = time.monotonic()
    result = OaSourceDiscoveryService.run_batch(
        limit=len(asins), test_mode=True, test_asins=asins, raw_products=raw_products,
    )
    result["elapsed_seconds"] = round(time.monotonic() - started, 1)
    result["provider"] = active
    result["shopping_cost_usd_est"] = round(
        result.get("serpapi_search_count", 0) / 1000 * cost_per_1000, 4
    )
    result["match_tiers"] = match_tier_breakdown(result["run_id"])
    return result


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25

    print(f"Selecting {n} ASINs (one fixed list, reused for both providers)...")
    asins = OaSourceDiscoveryService.get_candidate_asins(n)
    if not asins:
        print("No eligible ASINs available right now -- nothing to test.")
        raise SystemExit(0)
    print(f"Got {len(asins)} ASINs.")

    print("Fetching Keepa data ONCE (shared by both provider passes, so the comparison doesn't "
          "double your real Keepa spend for a difference that's only in the search step)...")
    try:
        raw_products = ProductService().get_products(
            asins, "UK", full=True, usage_category="oa_discovery_test_harness"
        )
    except KeepaTokensExhaustedError as exc:
        # get_products RAISES on token exhaustion rather than returning
        # falsy (see product_service.py) -- caught separately from the
        # "returned nothing" case below so you get the real reason
        # (with its own refill-time estimate) instead of a traceback.
        print(str(exc))
        print("Try a smaller batch (e.g. `python test_harness_search_providers.py 10`) or wait "
              "for tokens to refill, then re-run.")
        raise SystemExit(1)
    if not raw_products:
        print("Keepa lookup failed or returned nothing -- can't run the comparison. Try again shortly.")
        raise SystemExit(1)

    results = {provider: run_one_provider(provider, asins, raw_products) for provider in PROVIDERS}

    print("\n" + "=" * 66)
    print(f"COMPARISON -- {len(asins)} ASINs, both is_test=1 (nothing promoted for real)")
    print("=" * 66)

    rows = [
        ("Candidates found", "asins_with_retailer"),
        ("Exact match (EAN/MPN/brand_mpn)", "asins_with_exact_match"),
        ("Auto-priced by shopping search", "asins_auto_priced"),
        ("Would promote (dry_run)", "asins_profitable"),
        ("Searches spent", "serpapi_search_count"),
        ("Est. shopping-search cost ($)", "shopping_cost_usd_est"),
        ("Brave fallback searches", "search_count"),
        ("Run time (s)", "elapsed_seconds"),
    ]
    print(f"{'':32}{'serpapi':>16}{'serper':>16}")
    for label, key in rows:
        print(f"{label:32}{str(results['serpapi'].get(key, '-')):>16}{str(results['serper'].get(key, '-')):>16}")

    print("\nMatch-tier breakdown:")
    for provider in PROVIDERS:
        print(f"  {provider}: {results[provider]['match_tiers']}")

    print("\nRun IDs (oa_source_runs / oa_source_candidates, both is_test=1) for closer inspection:")
    for provider in PROVIDERS:
        print(f"  {provider}: run_id={results[provider]['run_id']}")

    print("\nWhat this CAN'T tell you: real price/stock accuracy against the live retailer page --")
    print("that needs step 5 (browser verification), not built yet. Judge this run on candidates")
    print("found, match-tier strength, and cost per §10 step 3's own checkpoint -- 'pick a search")
    print("provider on evidence' -- then set OA_SHOPPING_PROVIDER in .env to the winner and delete")
    print("the loser's client, per shopping_search.py's own instructions.")
