"""
CLI entry point for a real OA Source Intelligence shadow batch,
2026-09-05. See oa_research_worker_service.run_shadow_batch's own
docstring for the full safety guarantee: writes OaResearchFinding rows
only, no other write anywhere in Atlas.

Run with:
    ANTHROPIC_API_KEY=sk-ant-... python run_oa_shadow_batch.py [limit]
"""
import sys

from app.services.oa_research_worker_service import run_shadow_batch

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    print(f"Running OA Source Intelligence shadow batch against the top {limit} real competitor candidates...\n")

    outcome = run_shadow_batch(limit=limit)
    results = outcome["results"]
    summary = outcome["summary"]

    for r in results:
        print("=" * 100)
        print(f"[id={r['id']}] {r['asin']} -- {r['title'][:70]}")
        print(f"  result_state: {r['result_state']} | recommended: {r['recommended_outcome']}")
        print(f"  current: {r['current_retailer'] or '(none)'} £{r['current_price_gbp']} [{r['current_source_status']}]")
        print(f"  historical: {r['historical_retailer'] or '(none)'} £{r['historical_price_gbp']} ({r['historical_observed_note']})")
        print(f"  confidence: {r['source_confidence']} | inference: {r['atlas_inference'][:150]}")
        if r["estimated_profit_gbp"] is not None:
            print(f"  economics: profit £{r['estimated_profit_gbp']} / ROI {r['estimated_roi_pct']}%")
        print(f"  existing pipeline outcome: {r['existing_pipeline_outcome']}")
        print(f"  cost: ${r['cost_usd']}")

    print("\n" + "=" * 100)
    print("SUMMARY")
    print(f"  candidates: {summary['n_candidates']}")
    print(f"  total cost: ${summary['total_cost_usd']} (avg ${summary['avg_cost_per_asin_usd']}/ASIN)")
    print(f"  by recommended outcome: {summary['by_outcome']}")
    print(f"  by result state: {summary['by_result_state']}")
    print("=" * 100)
