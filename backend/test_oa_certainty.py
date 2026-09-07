"""
Regression test for the OA lead certainty rating, 2026-09-07 (Tamara's
own worked examples: plug-risk categories can't be EU A2A, a laptop
with no UK price drops is "very very likely OA", WORX/Makita often
being genuine EU A2A vs Philips being ambiguous, and a live UK price
dip must never affect trust in a PAST EU A2A finding).

SourcingClassifier.assess_certainty is a pure function (no DB, no
Keepa) -- tested directly with hand-built reasoning dicts, exactly the
shapes classify()'s own branches persist. brand_sourcing_pattern is
tested against a dedicated, obviously-fake brand name only (never a
real one), so this can never touch real production data the way an
earlier test in this session accidentally did.

Run with `python test_oa_certainty.py` (plain script, no pytest).
"""
from app.database.database import SessionLocal
from app.database.models import ProductRecord, SellerNewListing, TrackedSeller
from app.services.sourcing_classifier import SourcingClassifier, requires_uk_plug

FAKE_BRAND = "ZZZCertaintyTestBrand"
TEST_SELLER_ID = "OACERTAINTYTESTSELLER"


def cleanup():
    db = SessionLocal()
    try:
        db.query(ProductRecord).filter(ProductRecord.brand == FAKE_BRAND).delete(synchronize_session=False)
        db.query(SellerNewListing).filter(SellerNewListing.tracked_seller_id.in_(
            db.query(TrackedSeller.id).filter(TrackedSeller.seller_id == TEST_SELLER_ID)
        )).delete(synchronize_session=False)
        db.query(TrackedSeller).filter(TrackedSeller.seller_id == TEST_SELLER_ID).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


cleanup()

try:
    # 1 -- plug-risk category detection.
    assert requires_uk_plug("Computers & Accessories > Laptops") is True
    assert requires_uk_plug("Electronics > Headphones") is True
    assert requires_uk_plug("Toys & Games > Dolls") is False
    assert requires_uk_plug(None) is False
    print("test 1: requires_uk_plug correctly flags plug-risk vs plug-free categories: ok")

    # 2 -- Tamara's laptop example: plug-risk category, EU A2A tag would
    # be structurally very unlikely -> LOW certainty regardless of any
    # evidence numbers.
    result = SourcingClassifier.assess_certainty(
        "Computers & Accessories > Laptops", "EU A2A",
        {"viable_days_recent": 10, "priced_days_recent": 20},
    )
    assert result["level"] == "LOW", result
    assert any("plug" in f.lower() for f in result["facts"])
    print("test 2: plug-risk category tagged EU A2A -> LOW certainty with a plug-risk fact: ok")

    # 3 -- Tamara's laptop example, the OA side: plug-risk category (EU
    # ruled out structurally) AND no UK price drops in 30 days -> HIGH
    # certainty this is genuinely OA, by elimination.
    result = SourcingClassifier.assess_certainty(
        "Computers & Accessories > Laptops", "OA / unclear",
        {"eu_a2a_priced_days_recent": 0, "eu_a2a_viable_days_recent": 0, "uk_a2a_dip_days_recent": 0},
    )
    assert result["level"] == "HIGH", result
    print("test 3: plug-risk laptop, zero UK dip days, tagged OA -> HIGH certainty (by elimination): ok")

    # 4 -- no EU offers at all (non-plug category) also rules out EU A2A
    # -> HIGH certainty OA when UK dip evidence is also absent.
    result = SourcingClassifier.assess_certainty(
        "Toys & Games", "OA / unclear",
        {"eu_a2a_priced_days_recent": 0, "eu_a2a_viable_days_recent": 0, "uk_a2a_dip_days_recent": 0},
    )
    assert result["level"] == "HIGH", result
    print("test 4: zero EU offers at all (non-plug item) + zero UK dip -> HIGH certainty OA: ok")

    # 5 -- EU offers exist but never cleared the viable bar, AND there
    # IS UK dip evidence -> genuinely ambiguous, LOW certainty in the OA
    # tag (could plausibly be UK A2A instead).
    result = SourcingClassifier.assess_certainty(
        "Toys & Games", "OA / unclear",
        {"eu_a2a_priced_days_recent": 15, "eu_a2a_viable_days_recent": 0, "uk_a2a_dip_days_recent": 4},
    )
    assert result["level"] == "LOW", result
    print("test 5: EU priced but never viable + real UK dip evidence -> LOW certainty in the OA tag: ok")

    # 6 -- strong EU A2A evidence, no plug risk -> HIGH certainty.
    result = SourcingClassifier.assess_certainty(
        "Sports & Outdoors", "EU A2A",
        {"viable_days_recent": 8, "priced_days_recent": 25},
    )
    assert result["level"] == "HIGH", result
    print("test 6: strong recent EU viable-day count, no plug risk -> HIGH certainty: ok")

    # 7 -- UK price context must NEVER leak into this function: passing
    # no buy_box_now/buy_box_90d at all (the function doesn't even
    # accept them) and getting the same HIGH result proves today's live
    # price can't move this -- Tamara's "seller may be expecting the
    # price to go back up" point.
    import inspect
    sig = inspect.signature(SourcingClassifier.assess_certainty)
    assert "buy_box_now" not in sig.parameters and "buy_box_90d" not in sig.parameters, (
        "assess_certainty must never take today's live price as an input"
    )
    print("test 7: assess_certainty's signature has no live-price parameter at all: ok")

    # 8 -- brand pattern: too small a sample is ignored entirely.
    db = SessionLocal()
    try:
        seller = TrackedSeller(seller_id=TEST_SELLER_ID, nickname="OA Certainty Test Seller", active=True)
        db.add(seller)
        db.commit()
        db.refresh(seller)
        seller_id = seller.id

        # 2 detections only -- below BRAND_PATTERN_MIN_SAMPLE (3).
        for i in range(2):
            record = ProductRecord(
                asin=f"B0CERT{i}TEST", title="t", brand=FAKE_BRAND, category="",
                category_name="", best_source_marketplace="", best_source_cost_gbp=0.0,
                buy_box_now=10.0, fba_fee=1.0, profit=0.0, roi=0.0, recommendation="IGNORE", score=0,
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            db.add(SellerNewListing(
                tracked_seller_id=seller_id, asin=f"B0CERT{i}TEST", product_record_id=record.id,
                sourcing_tag="EU A2A", currently_buyable=False,
            ))
        db.commit()
    finally:
        db.close()

    from app.services.seller_watch_service import SellerWatchService
    pattern = SellerWatchService.brand_sourcing_pattern(FAKE_BRAND)
    assert pattern is None, f"a 2-detection sample must be ignored (below MIN_SAMPLE), got {pattern}"
    print("test 8: brand_sourcing_pattern returns None below the minimum sample size: ok")

    # 9 -- a third detection (now EU A2A, 3/3 = 100%) crosses the sample
    # floor and reports a real pattern.
    db = SessionLocal()
    try:
        record = ProductRecord(
            asin="B0CERT2TEST", title="t", brand=FAKE_BRAND, category="",
            category_name="", best_source_marketplace="", best_source_cost_gbp=0.0,
            buy_box_now=10.0, fba_fee=1.0, profit=0.0, roi=0.0, recommendation="IGNORE", score=0,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        db.add(SellerNewListing(
            tracked_seller_id=seller_id, asin="B0CERT2TEST", product_record_id=record.id,
            sourcing_tag="EU A2A", currently_buyable=False,
        ))
        db.commit()
    finally:
        db.close()

    pattern = SellerWatchService.brand_sourcing_pattern(FAKE_BRAND)
    assert pattern is not None
    assert pattern["sample_size"] == 3
    assert pattern["eu_a2a_pct"] == 100.0
    print("test 9: brand_sourcing_pattern reports 100% EU A2A once the sample floor is met: ok")

    print("\nALL TESTS PASSED.")
finally:
    cleanup()
    db = SessionLocal()
    try:
        remaining = db.query(ProductRecord).filter(ProductRecord.brand == FAKE_BRAND).count()
    finally:
        db.close()
    assert remaining == 0, "cleanup failed -- fake rows still present"
    print("cleanup verified: 0 fake rows remain.")
