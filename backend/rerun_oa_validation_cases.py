"""
Post-hardening acceptance rerun -- 2026-09-05. Re-runs the 5 real ASINs
from the first shadow batch that were manually scored, to check whether
the A2 fetch/retry hardening (oa_research_worker_service.py) fixes the
Stihl BGA 45 miss without breaking any of the other 4 acceptance
targets. Writes NEW OaResearchFinding rows (does not overwrite the
originals) -- same "no live queue/opportunity writes" guarantee as the
main shadow batch.

Target acceptance results:
  B072NBFYMK (Stihl)  -> UK Planet Tools ~£73.99, SKU 4513 011 5905, VERIFIED_CURRENT, BUY_NOW
  B0F995J8FR (DJI)    -> same price as Amazon (£182) -> NOT a buy
  B06XJ3MFMN (Krups)  -> EU-plug variant -> should be rejected/flagged, not a genuine buy
  B07LB5Z9Y4 (Fenty)  -> only a buy if the exact "Fussy" shade + stock verified
  B088HWR46P (Makita) -> only a buy if the source can actually be verified
"""
from app.database.database import SessionLocal
from app.database.models import ProductRecord, SellerNewListing
from app.services.oa_research_worker_service import (
    run_research_for_candidate, save_finding, _latest_candidate_row, _existing_pipeline_outcome,
)
import anthropic
import os

TARGET_ASINS = ["B072NBFYMK", "B0F995J8FR", "B06XJ3MFMN", "B07LB5Z9Y4", "B088HWR46P"]

if __name__ == "__main__":
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set.")
    client = anthropic.Anthropic(api_key=api_key)

    db = SessionLocal()
    try:
        rows = []
        for asin in TARGET_ASINS:
            listing = (
                db.query(SellerNewListing)
                .filter(SellerNewListing.asin == asin)
                .order_by(SellerNewListing.detected_at.desc())
                .first()
            )
            record = (
                db.query(ProductRecord)
                .filter(ProductRecord.asin == asin)
                .order_by(ProductRecord.scanned_at.desc())
                .first()
            )
            rows.append({"listing": listing, "record": record})
    finally:
        db.close()

    total_cost = 0.0
    for row in rows:
        asin = row["listing"].asin
        finding = run_research_for_candidate(client, row)
        finding_id = save_finding(finding)
        total_cost += finding["cost_usd"]

        print("=" * 100)
        print(f"[id={finding_id}] {asin} -- {row['record'].title[:70]}")
        print(f"  result_state: {finding['result_state']} | recommended: {finding['recommended_outcome']}")
        print(f"  current: {finding['current_retailer'] or '(none)'} £{finding['current_price_gbp']} [{finding['current_source_status']}]")
        print(f"  historical: {finding['historical_retailer'] or '(none)'} £{finding['historical_price_gbp']} ({finding['historical_observed_note']})")
        print(f"  confidence: {finding['source_confidence']} | inference: {finding['atlas_inference'][:200]}")
        if finding["estimated_profit_gbp"] is not None:
            print(f"  economics: profit £{finding['estimated_profit_gbp']} / ROI {finding['estimated_roi_pct']}%")
        print(f"  cost: ${finding['cost_usd']}")

    print("\n" + "=" * 100)
    print(f"TOTAL COST: ${round(total_cost, 4)} for {len(rows)} ASINs (avg ${round(total_cost/len(rows), 4)}/ASIN)")
    print("=" * 100)
