"""
Tests for the "unified Review Queue / one decision per ASIN" build,
atlas-review-queue-backend-v1.md's follow-up task, 2026-09-03.

Run with `python test_unified_review_queue.py` (same plain-script style
as the other test_*.py files here, no pytest).

Covers section 19's scenario list: unified identity, BUY-appears-
everywhere, VA-appears-in-two-views-resolved-by-one-decision, the
Competitor Watch historical-vs-current distinction, and all four
review outcomes actually resolving the item. resolve_item genuinely
writes to the real database (that's the whole point of testing it) --
every test here uses a dedicated, obviously-fake ASIN, and cleans up
every row it creates in a try/finally, verified against exact row
counts at the end so nothing is left behind in a real Atlas database.

No Keepa calls anywhere in this file.
"""
from datetime import datetime, timedelta, timezone

from app.database.database import SessionLocal
from app.database.models import Lead, ProductRecord, SellerNewListing, TrackedSeller
from app.services.review_queue_service import (
    ReviewQueueService,
    QUEUE_PRIORITY_BUY_NOW, QUEUE_PRIORITY_VA_TO_REVIEW,
    QUEUE_PRIORITY_BORDERLINE, QUEUE_PRIORITY_NEEDS_ATTENTION,
)

TEST_ASIN_PREFIX = "B0UQTEST"


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


TEST_SELLER_IDS = ["TESTSELLER1", "TESTSELLER2", "TESTSELLER3", "TESTSELLER4"]


def cleanup(asins):
    db = SessionLocal()
    try:
        db.query(ProductRecord).filter(ProductRecord.asin.in_(asins)).delete(synchronize_session=False)
        db.query(SellerNewListing).filter(SellerNewListing.asin.in_(asins)).delete(synchronize_session=False)
        db.query(Lead).filter(Lead.asin.in_(asins)).delete(synchronize_session=False)
        db.query(TrackedSeller).filter(TrackedSeller.seller_id.in_(TEST_SELLER_IDS)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def snapshot_counts():
    db = SessionLocal()
    try:
        return (
            db.query(ProductRecord).count(),
            db.query(SellerNewListing).count(),
            db.query(Lead).count(),
            db.query(TrackedSeller).count(),
        )
    finally:
        db.close()


before_counts = snapshot_counts()
created_asins = set()


def make_product_record(asin, recommendation="BUY", profit=10.0, roi=50.0, review=None, monthly_sales=10):
    db = SessionLocal()
    try:
        record = ProductRecord(
            asin=asin, title=f"Test product {asin}", brand="TestBrand", category="",
            best_source_marketplace="DE", best_source_cost_gbp=10.0,
            profit=profit, roi=roi, recommendation=recommendation, score=80,
            monthly_sales=monthly_sales,
            scanned_at=utcnow(), review=review,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        created_asins.add(asin)
        return record.id
    finally:
        db.close()


def make_lead(asin, verdict="BUY", status="analyzed", decision=None):
    db = SessionLocal()
    try:
        lead = Lead(
            asin=asin, source="sheet", status=status, verdict=verdict,
            rationale="Test rationale", keepa_metrics='{"title": "Test", "brand": "TestBrand"}',
            added_at=utcnow(), analyzed_at=utcnow(), decision=decision,
        )
        db.add(lead)
        db.commit()
        db.refresh(lead)
        created_asins.add(asin)
        return lead.id
    finally:
        db.close()


try:
    # =====================================================================
    # TEST 5/12 -- VA BUY + BUY NOW -> ONE underlying item, both views
    # =====================================================================
    asin = f"{TEST_ASIN_PREFIX}A"
    make_product_record(asin, recommendation="BUY", review=None)
    lead_id = make_lead(asin, verdict="BUY")

    items = [it for it in ReviewQueueService.list_queue_items() if it["asin"] == asin]
    assert len(items) == 1, f"expected ONE unified item, got {len(items)}"
    item = items[0]
    assert set(item["sources"]) == {"scan", "lead"}
    assert QUEUE_PRIORITY_BUY_NOW in item["views"], item["views"]
    assert QUEUE_PRIORITY_VA_TO_REVIEW in item["views"], item["views"]
    print("test 5: VA BUY + scan BUY, same ASIN -> ONE item, appears in BOTH BUY_NOW and VA_TO_REVIEW: ok")

    # --- TEST 6/15 -- reviewing it ONCE resolves BOTH outstanding views ---
    resolved = ReviewQueueService.resolve_item(asin, "approved", reason="Looks great", reason_category=None)
    assert resolved["scan"] is True
    assert lead_id in resolved["lead"]

    db = SessionLocal()
    try:
        record = db.query(ProductRecord).filter(ProductRecord.asin == asin).order_by(ProductRecord.scanned_at.desc()).first()
        lead = db.get(Lead, lead_id)
        assert record.review == "up", record.review
        assert record.recommendation == "BUY", "original recommendation must survive review, never overwritten"
        assert lead.decision == "approved", lead.decision
        assert lead.status == "reviewed"
    finally:
        db.close()

    items_after = [it for it in ReviewQueueService.list_queue_items() if it["asin"] == asin]
    assert len(items_after) == 0, "resolved item must no longer appear as outstanding in ANY view"
    print("test 6: ONE decision resolves both the BUY_NOW and VA_TO_REVIEW views -- item no longer outstanding: ok")

    # =====================================================================
    # SECTION 12 -- duplicate handling, all source combinations
    # =====================================================================

    # --- Scan + Competitor -> one item --------------------------------------
    asin = f"{TEST_ASIN_PREFIX}B"
    record_id = make_product_record(asin, recommendation="BUY", review=None)
    db = SessionLocal()
    try:
        seller = TrackedSeller(seller_id="TESTSELLER1", nickname="Test Seller", active=True)
        db.add(seller)
        db.commit()
        db.refresh(seller)
        listing = SellerNewListing(
            tracked_seller_id=seller.id, asin=asin, product_record_id=record_id,
            sourcing_tag="EU A2A", currently_buyable=True, detected_at=utcnow(),
        )
        db.add(listing)
        db.commit()
    finally:
        db.close()
    created_asins.add(asin)

    items = [it for it in ReviewQueueService.list_queue_items() if it["asin"] == asin]
    assert len(items) == 1
    assert set(items[0]["sources"]) == {"scan", "competitor"}
    print("test 1 (section 12): Scan + Competitor -> one Review Queue item: ok")

    # --- Scan + VA -> one item -----------------------------------------------
    asin = f"{TEST_ASIN_PREFIX}C"
    make_product_record(asin, recommendation="CONSIDER", review=None)
    make_lead(asin, verdict="WATCH")
    items = [it for it in ReviewQueueService.list_queue_items() if it["asin"] == asin]
    assert len(items) == 1
    assert set(items[0]["sources"]) == {"scan", "lead"}
    print("test 2 (section 12): Scan + VA -> one Review Queue item: ok")

    # --- Competitor + VA -> one item ------------------------------------------
    # currently_buyable=False (routed through list_historical_a2a_not_
    # buyable, which has NO is_notable requirement) + a non-notable
    # recommendation/no sales evidence on the linked ProductRecord, so
    # the record does NOT also independently qualify as its own "scan"
    # lead. (list_notable_buyable's currently_buyable=True path ALWAYS
    # requires is_notable() to pass, and passing is_notable() would
    # itself make the record surface as "scan" too -- so isolating a
    # true 2-source Competitor+VA-only merge specifically exercises the
    # new historical-evidence-not-buyable competitor pathway.)
    asin = f"{TEST_ASIN_PREFIX}D"
    record_id = make_product_record(asin, recommendation="CONSIDER", profit=1.0, roi=10.0, review=None, monthly_sales=0)
    db = SessionLocal()
    try:
        seller = TrackedSeller(seller_id="TESTSELLER2", nickname="Test Seller 2", active=True)
        db.add(seller)
        db.commit()
        db.refresh(seller)
        listing = SellerNewListing(
            tracked_seller_id=seller.id, asin=asin, product_record_id=record_id,
            sourcing_tag="UK A2A", currently_buyable=False, detected_at=utcnow(),
        )
        db.add(listing)
        db.commit()
    finally:
        db.close()
    make_lead(asin, verdict="WATCH")
    items = [it for it in ReviewQueueService.list_queue_items() if it["asin"] == asin]
    assert len(items) == 1
    assert set(items[0]["sources"]) == {"competitor", "lead"}, items[0]["sources"]
    print("test 3 (section 12): Competitor + VA -> one Review Queue item: ok")

    # --- Scan + Competitor + VA -> one item, all three retained --------------
    asin = f"{TEST_ASIN_PREFIX}E"
    record_id = make_product_record(asin, recommendation="BUY", review=None)
    db = SessionLocal()
    try:
        seller = TrackedSeller(seller_id="TESTSELLER3", nickname="Test Seller 3", active=True)
        db.add(seller)
        db.commit()
        db.refresh(seller)
        listing = SellerNewListing(
            tracked_seller_id=seller.id, asin=asin, product_record_id=record_id,
            sourcing_tag="EU A2A", currently_buyable=True, detected_at=utcnow(),
        )
        db.add(listing)
        db.commit()
    finally:
        db.close()
    make_lead(asin, verdict="BUY")
    items = [it for it in ReviewQueueService.list_queue_items() if it["asin"] == asin]
    assert len(items) == 1
    assert set(items[0]["sources"]) == {"scan", "competitor", "lead"}
    assert len(items[0]["source_items"]) == 3, "all three original per-source dicts must be preserved"
    print("test 4 (section 12): Scan + Competitor + VA -> one item, all three sources retained: ok")

    # =====================================================================
    # BUY appears in BUY NOW regardless of source
    # =====================================================================
    for label, setup in [
        ("scan", lambda a: make_product_record(a, recommendation="BUY", review=None)),
        ("VA", lambda a: make_lead(a, verdict="BUY")),
    ]:
        asin = f"{TEST_ASIN_PREFIX}BN{label[:2].upper()}"
        setup(asin)
        items = [it for it in ReviewQueueService.list_queue_items() if it["asin"] == asin]
        assert len(items) == 1
        assert QUEUE_PRIORITY_BUY_NOW in items[0]["views"], (label, items[0]["views"])
        print(f"BUY: {label} BUY -> appears in BUY_NOW: ok")

    # Competitor BUY
    asin = f"{TEST_ASIN_PREFIX}BNCO"
    record_id = make_product_record(asin, recommendation="BUY", review=None)
    db = SessionLocal()
    try:
        seller = TrackedSeller(seller_id="TESTSELLER4", nickname="Test Seller 4", active=True)
        db.add(seller)
        db.commit()
        db.refresh(seller)
        listing = SellerNewListing(
            tracked_seller_id=seller.id, asin=asin, product_record_id=record_id,
            sourcing_tag="EU A2A", currently_buyable=True, detected_at=utcnow(),
        )
        db.add(listing)
        db.commit()
    finally:
        db.close()
    items = [it for it in ReviewQueueService.list_queue_items() if it["asin"] == asin]
    assert QUEUE_PRIORITY_BUY_NOW in items[0]["views"]
    print("BUY: competitor BUY -> appears in BUY_NOW: ok")

    # =====================================================================
    # Review outcomes: BUY / WATCH / AVOID / NEED_MORE_INFO each resolve
    # =====================================================================
    for decision, expected_raw in [("approved", "up"), ("watch", "watch"), ("rejected", "down"), ("need_more_info", "need_more_info")]:
        asin = f"{TEST_ASIN_PREFIX}R{decision[:3].upper()}"
        make_product_record(asin, recommendation="BUY", review=None)

        resolved = ReviewQueueService.resolve_item(asin, decision)
        assert resolved["scan"] is True

        db = SessionLocal()
        try:
            record = db.query(ProductRecord).filter(ProductRecord.asin == asin).first()
            assert record.review == expected_raw, (decision, record.review)
        finally:
            db.close()

        items_after = [it for it in ReviewQueueService.list_queue_items() if it["asin"] == asin]
        assert len(items_after) == 0, f"{decision} must resolve the item (no longer outstanding)"
        print(f"review: decision={decision!r} resolves the item (stored as {expected_raw!r}): ok")

    # =====================================================================
    # Competitor Watch: historical DE + current ES BUY -> EU A2A + BUY NOW
    # (pure dict-level, mirrors the real merge shape, no DB needed for
    # the classification math itself -- SourcingClassifier's own tests
    # already cover the day-by-day evidence computation exhaustively)
    # =====================================================================
    competitor_item = {
        "source": "competitor", "asin": "B0DUMMY", "recommendation": "BUY", "freshness": "FRESH",
        "sourcing_tag": "EU A2A", "best_source_marketplace": "ES",
        "reasoning": {"marketplace": "DE", "historical_a2a_evidence": {"eu_a2a": {"DE": {"viable_days": 3}}}},
    }
    views = ReviewQueueService._item_views(competitor_item)
    assert views == {QUEUE_PRIORITY_BUY_NOW}, views
    merged = ReviewQueueService.merge_by_asin([competitor_item])[0]
    assert merged["sourcing_tag"] == "EU A2A", "historical classification must survive alongside a current BUY"
    assert merged["best_source_marketplace"] == "ES", "current source can differ from the historical one"
    print("competitor: historical DE + current ES BUY -> EU A2A classification retained, BUY_NOW: ok")

    # --- historical A2A evidence, no current BUY -> NEEDS_ATTENTION, NOT OA -
    competitor_item_stale = {
        "source": "competitor", "asin": "B0DUMMY2", "recommendation": "IGNORE",
        "sourcing_tag": "EU A2A",  # SourcingClassifier already keeps this correctly, per its own tests
        "reasoning": {"historical_a2a_evidence": {"eu_a2a": {"DE": {"viable_days": 3}}}},
    }
    views = ReviewQueueService._item_views(competitor_item_stale)
    assert QUEUE_PRIORITY_NEEDS_ATTENTION in views, views
    merged_stale = ReviewQueueService.merge_by_asin([competitor_item_stale])[0]
    assert merged_stale["sourcing_tag"] == "EU A2A", (
        "must NOT become OA just because there's no current BUY -- got", merged_stale["sourcing_tag"]
    )
    print("competitor: historical A2A evidence + no current BUY -> NEEDS_ATTENTION, sourcing_tag stays EU A2A (not OA): ok")

finally:
    cleanup(created_asins)

after_counts = snapshot_counts()
assert after_counts == before_counts, (
    f"row counts must be identical after cleanup -- before={before_counts}, after={after_counts}"
)
print(f"\ncleanup verified -- {len(created_asins)} test ASINs created and fully removed, row counts unchanged")

print("\nALL PASS")
