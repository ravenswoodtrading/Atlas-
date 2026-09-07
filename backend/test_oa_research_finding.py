"""
Regression tests for OaResearchFinding (OA Source Intelligence shadow
worker), 2026-09-05.

Run with `python test_oa_research_finding.py` (same plain-script style
as the other test_*.py files here, no pytest).

NO AI/network calls anywhere in this file -- these test the DB write
path (save_finding/record_human_verdict) and the pure economics/status
logic (_classify_current_status/_recommend_outcome) directly, never the
Claude/Brave-calling pipeline functions. Every fixture uses a dedicated,
obviously-fake ASIN and cleans up in a try/finally, verified against
exact row counts at the end so nothing is left behind in a real Atlas
database.
"""
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import OaResearchFinding, ProductRecord
from app.services.oa_research_worker_service import (
    save_finding, record_human_verdict, _classify_current_status, _recommend_outcome,
    _extract_verified_evidence,
)

FAKE_ASIN = "B0TESTFINDING1"


def cleanup():
    db = SessionLocal()
    try:
        db.query(OaResearchFinding).filter(OaResearchFinding.asin == FAKE_ASIN).delete()
        db.commit()
    finally:
        db.close()


def test_save_and_read_finding():
    finding_id = save_finding({
        "seller_new_listing_id": None,
        "asin": FAKE_ASIN,
        "result_state": "VERIFIED_HISTORICAL",
        "current_retailer": "", "current_url": "", "current_price_gbp": None,
        "current_stock_text": "", "current_checked_at": None,
        "historical_retailer": "Currys", "historical_price_gbp": 59.99,
        "historical_observed_note": "sale ended ~5 days ago", "historical_checked_at": datetime.now(timezone.utc),
        "current_source_status": "UNKNOWN", "historical_source_status": "FOUND", "sourcing_classification": "OA",
        "source_confidence": "HIGH", "atlas_inference": "Likely historical source: Currys.",
        "recommended_outcome": "SOURCE_INTELLIGENCE", "estimated_profit_gbp": None, "estimated_roi_pct": None,
        "existing_pipeline_outcome": "no_prior_oa_source_discovery_run",
        "queries_used_json": "[]", "reasoning_json": "{}", "cost_usd": 0.1234,
    })
    assert finding_id > 0

    db = SessionLocal()
    try:
        row = db.query(OaResearchFinding).filter(OaResearchFinding.id == finding_id).first()
        assert row is not None
        assert row.historical_retailer == "Currys"
        assert row.historical_price_gbp == 59.99
        assert row.recommended_outcome == "SOURCE_INTELLIGENCE"
        assert row.human_verdict is None  # not yet reviewed
    finally:
        db.close()
    print("PASS: save_finding writes a row with all fields correctly.")
    return finding_id


def test_human_verdict_updates_without_touching_evidence(finding_id):
    record_human_verdict(finding_id, "GENUINE_OPPORTUNITY", notes="Confirmed by hand, real Currys promo.")

    db = SessionLocal()
    try:
        row = db.query(OaResearchFinding).filter(OaResearchFinding.id == finding_id).first()
        assert row.human_verdict == "GENUINE_OPPORTUNITY"
        assert row.human_notes == "Confirmed by hand, real Currys promo."
        assert row.reviewed_at is not None
        # Evidence fields must be untouched by a verdict update.
        assert row.historical_retailer == "Currys"
        assert row.historical_price_gbp == 59.99
    finally:
        db.close()
    print("PASS: record_human_verdict updates only verdict fields.")


def test_historical_evidence_never_clobbered_by_later_current_check():
    """
    Simulates the real scenario from the brief: an earlier run found
    Currys @ £59.99 historically; today's run finds a current price of
    £119.99 at the SAME retailer. Since each research run writes a NEW
    row rather than updating in place, the original historical evidence
    must still be readable afterwards, untouched.
    """
    old_id = save_finding({
        "seller_new_listing_id": None, "asin": FAKE_ASIN, "result_state": "VERIFIED_HISTORICAL",
        "current_retailer": "", "current_url": "", "current_price_gbp": None, "current_stock_text": "",
        "current_checked_at": None, "historical_retailer": "Currys", "historical_price_gbp": 59.99,
        "historical_observed_note": "28 Aug", "historical_checked_at": datetime.now(timezone.utc),
        "current_source_status": "UNKNOWN", "historical_source_status": "FOUND", "sourcing_classification": "OA",
        "source_confidence": "HIGH", "atlas_inference": "", "recommended_outcome": "SOURCE_INTELLIGENCE",
        "estimated_profit_gbp": None, "estimated_roi_pct": None, "existing_pipeline_outcome": "no_prior_oa_source_discovery_run",
        "queries_used_json": "[]", "reasoning_json": "{}", "cost_usd": 0.1,
    })
    new_id = save_finding({
        "seller_new_listing_id": None, "asin": FAKE_ASIN, "result_state": "VERIFIED_CURRENT",
        "current_retailer": "Currys", "current_url": "https://example.test", "current_price_gbp": 119.99,
        "current_stock_text": "in_stock", "current_checked_at": datetime.now(timezone.utc),
        "historical_retailer": "", "historical_price_gbp": None, "historical_observed_note": "",
        "historical_checked_at": None, "current_source_status": "ABOVE_AMAZON", "historical_source_status": "NOT_FOUND",
        "sourcing_classification": "OA", "source_confidence": "HIGH", "atlas_inference": "",
        "recommended_outcome": "SOURCE_INTELLIGENCE", "estimated_profit_gbp": None, "estimated_roi_pct": None,
        "existing_pipeline_outcome": "no_prior_oa_source_discovery_run", "queries_used_json": "[]",
        "reasoning_json": "{}", "cost_usd": 0.1,
    })

    db = SessionLocal()
    try:
        old_row = db.query(OaResearchFinding).filter(OaResearchFinding.id == old_id).first()
        assert old_row.historical_retailer == "Currys"
        assert old_row.historical_price_gbp == 59.99
        new_row = db.query(OaResearchFinding).filter(OaResearchFinding.id == new_id).first()
        assert new_row.current_price_gbp == 119.99
        assert new_row.historical_price_gbp is None  # this run found no historical evidence itself
        count = db.query(OaResearchFinding).filter(OaResearchFinding.asin == FAKE_ASIN).count()
        assert count == 3  # the two from this test + one from test_save_and_read_finding
    finally:
        db.close()
    print("PASS: historical evidence from an earlier run is never overwritten by a later current-price check.")


def _fake_record(buy_box_now, category_name="DIY & Tools", fba_fee=3.0):
    return type("FakeRecord", (), {
        "asin": FAKE_ASIN, "title": "Fake Product", "brand": "FakeBrand", "ean": "",
        "buy_box_now": buy_box_now, "category_name": category_name, "fba_fee": fba_fee,
    })()


def test_classify_and_recommend_hard_rule():
    record = _fake_record(buy_box_now=100.0)

    # No price at all -> UNKNOWN status, never a buying outcome.
    assert _classify_current_status(record, None, None) == "UNKNOWN"
    outcome, profit, roi = _recommend_outcome(record, "UNKNOWN", None)
    assert outcome == "SOURCE_INTELLIGENCE" and profit is None and roi is None

    # Priced above Amazon -> ABOVE_AMAZON, never a buying outcome even
    # though a "current source" was verified (the DEWALT DS150 case).
    assert _classify_current_status(record, 105.0, "in_stock") == "ABOVE_AMAZON"
    outcome, profit, roi = _recommend_outcome(record, "ABOVE_AMAZON", 105.0)
    assert outcome == "SOURCE_INTELLIGENCE"

    # Out of stock -> OUT_OF_STOCK, never a buying outcome (the Milwaukee
    # M18B4 case) even though the price itself would have been viable.
    assert _classify_current_status(record, 40.0, "out_of_stock") == "OUT_OF_STOCK"
    outcome, profit, roi = _recommend_outcome(record, "OUT_OF_STOCK", 40.0)
    assert outcome == "SOURCE_INTELLIGENCE"

    # A genuinely cheap, viable, in-stock price -> a real buying outcome.
    assert _classify_current_status(record, 30.0, "in_stock") == "VIABLE"
    outcome, profit, roi = _recommend_outcome(record, "VIABLE", 30.0)
    assert outcome in ("BUY_NOW", "BORDERLINE")
    assert profit is not None and profit > 0
    print("PASS: hard rule holds -- only a VIABLE current price ever produces BUY_NOW/BORDERLINE.")


def test_unconfirmed_evidence_never_trusted_when_stage_says_fetch_failed():
    """
    Regression test for a real bug found in the first live shadow run
    (Stihl BGA 45, 2026-09-05, id=4 in that batch): the model reported
    stage_classification="FETCH_FAILED" but still included a plausible
    verified_current_source object built from search snippets, not a
    confirmed page fetch. That field must be discarded whenever the
    stage's own classification isn't actually VERIFIED_CURRENT/
    VERIFIED_HISTORICAL -- otherwise unconfirmed evidence could reach
    the hard BUY_NOW/BORDERLINE rule.
    """
    parsed_with_smuggled_evidence = {
        "stage_classification": "FETCH_FAILED",
        "verified_current_source": {"retailer": "PTE", "price": 99.17},
        "verified_historical_source": {"retailer": "Old Shop", "price": 40.0},
    }
    current, historical = _extract_verified_evidence("FETCH_FAILED", parsed_with_smuggled_evidence)
    assert current is None
    assert historical is None

    # Sanity check the positive case still works when the stage genuinely says VERIFIED_CURRENT.
    parsed_genuinely_verified = {
        "stage_classification": "VERIFIED_CURRENT",
        "verified_current_source": {"retailer": "Argos", "price": 34.99},
    }
    current, historical = _extract_verified_evidence("VERIFIED_CURRENT", parsed_genuinely_verified)
    assert current == {"retailer": "Argos", "price": 34.99}
    print("PASS: unconfirmed evidence is discarded unless the stage's own classification actually verified it.")


if __name__ == "__main__":
    cleanup()
    try:
        fid = test_save_and_read_finding()
        test_human_verdict_updates_without_touching_evidence(fid)
        test_historical_evidence_never_clobbered_by_later_current_check()
        test_classify_and_recommend_hard_rule()
        test_unconfirmed_evidence_never_trusted_when_stage_says_fetch_failed()
        print("\nALL TESTS PASSED.")
    finally:
        cleanup()
        db = SessionLocal()
        remaining = db.query(OaResearchFinding).filter(OaResearchFinding.asin == FAKE_ASIN).count()
        db.close()
        assert remaining == 0, "Cleanup failed -- fake rows still present."
        print("Cleanup verified: 0 fake rows remain.")
