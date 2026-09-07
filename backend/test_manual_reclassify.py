"""
Regression test for manual sourcing-tag correction, 2026-09-07 (Tamara:
"I want an option to be able to classify this if I find the source ...
in which case Atlas was wrong with the classifying").

Run with `python test_manual_reclassify.py` (same plain-script style as
the other test_*.py files here, no pytest).

Uses a dedicated fake ASIN/seller, cleans up in a try/finally, verified
against exact row counts at the end.
"""
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import ProductRecord, SellerNewListing, TrackedSeller
from app.services.review_queue_service import ReviewQueueService, QUEUE_PRIORITY_OA_INVESTIGATE
from fastapi.testclient import TestClient
from app.main import app

TEST_ASIN = "B0RECLASS1"
TEST_SELLER_ID = "RECLASSIFYTESTSELLER"


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cleanup():
    db = SessionLocal()
    try:
        db.query(ProductRecord).filter(ProductRecord.asin == TEST_ASIN).delete(synchronize_session=False)
        db.query(SellerNewListing).filter(SellerNewListing.asin == TEST_ASIN).delete(synchronize_session=False)
        db.query(TrackedSeller).filter(TrackedSeller.seller_id == TEST_SELLER_ID).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def snapshot_counts():
    db = SessionLocal()
    try:
        return (
            db.query(ProductRecord).count(),
            db.query(SellerNewListing).count(),
            db.query(TrackedSeller).count(),
        )
    finally:
        db.close()


client = TestClient(app)

cleanup()
before_counts = snapshot_counts()

try:
    db = SessionLocal()
    try:
        seller = TrackedSeller(seller_id=TEST_SELLER_ID, nickname="Reclassify Test Seller", active=True)
        db.add(seller)
        db.commit()
        db.refresh(seller)
        seller_id = seller.id

        record = ProductRecord(
            asin=TEST_ASIN, title="Test Reclassify Product", brand="TestBrand", category="",
            category_name="", best_source_marketplace="", best_source_cost_gbp=0.0,
            buy_box_now=150.0, fba_fee=6.0, profit=0.0, roi=0.0, recommendation="IGNORE",
            score=80, monthly_sales=0, sales_drops_30d=0, scanned_at=utcnow(), review=None,
        )
        db.add(record)
        db.commit()
        db.refresh(record)

        listing = SellerNewListing(
            tracked_seller_id=seller_id, asin=TEST_ASIN, product_record_id=record.id,
            sourcing_tag="OA / unclear", currently_buyable=False, detected_at=utcnow(),
        )
        db.add(listing)
        db.commit()
        db.refresh(listing)
        listing_id = listing.id
    finally:
        db.close()

    # 1 -- starts in the OA_INVESTIGATE view, not manually classified.
    items = ReviewQueueService.list_queue_items()
    item = next((i for i in items if i["asin"] == TEST_ASIN), None)
    assert item is not None, "fixture must appear in list_queue_items()"
    assert QUEUE_PRIORITY_OA_INVESTIGATE in item["views"]
    assert item["manually_classified"] is False
    print("test 1: fixture starts in OA_INVESTIGATE, not manually classified: ok")

    # 2 -- the detail panel HTML renders the classification buttons and
    # the Amazon link while it's still a genuine OA item (checked BEFORE
    # reclassifying below -- once corrected to EU A2A it correctly stops
    # being an OA-investigate item at all, so the workbench block
    # legitimately disappears; that's tested separately as test 5/6).
    resp = client.get(f"/review-queue/item/{TEST_ASIN}")
    assert resp.status_code == 200
    html = resp.text
    assert "qi-reclassify-btn" in html
    assert f"https://www.amazon.co.uk/dp/{TEST_ASIN}" in html
    assert "Manually set" not in html
    print("test 2: detail panel renders reclassify buttons + Amazon link before any correction: ok")

    # 3 -- an unknown tag is rejected.
    resp = client.post("/review-queue/reclassify", data={"asin": TEST_ASIN, "sourcing_tag": "Nonsense"})
    assert resp.status_code == 400, resp.text
    print("test 3: an invalid sourcing_tag value is rejected: ok")

    # 4 -- reclassifying to EU A2A updates the listing and marks it
    # manually_classified.
    resp = client.post("/review-queue/reclassify", data={"asin": TEST_ASIN, "sourcing_tag": "EU A2A"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["updated"] == 1
    print("test 4: reclassify route updates exactly one listing: ok")

    db = SessionLocal()
    try:
        refreshed = db.get(SellerNewListing, listing_id)
        assert refreshed.sourcing_tag == "EU A2A"
        assert refreshed.manually_classified is True
    finally:
        db.close()
    print("test 5: listing.sourcing_tag == 'EU A2A' and manually_classified == True: ok")

    # 6 -- it has dropped OUT of the OA_INVESTIGATE view on the next load
    # (the query is scoped to sourcing_tag == "OA / unclear").
    items_after = ReviewQueueService.list_queue_items()
    item_after = next((i for i in items_after if i["asin"] == TEST_ASIN), None)
    assert item_after is None or QUEUE_PRIORITY_OA_INVESTIGATE not in item_after["views"], (
        "a listing reclassified to EU A2A must no longer show as OA_INVESTIGATE"
    )
    print("test 6: reclassified item no longer appears as OA_INVESTIGATE: ok")

    # 7 -- a subsequent automated _persist_classification pass must NOT
    # silently overwrite the manual correction back to Atlas's own guess.
    from app.services.seller_watch_service import SellerWatchService
    from app.services.sourcing_classifier import SourcingClassification

    db = SessionLocal()
    try:
        refreshed = db.get(SellerNewListing, listing_id)
        fake_classification = SourcingClassification(sourcing_tag="OA / unclear", reasoning={})
        SellerWatchService._persist_classification(refreshed, fake_classification, recommendation="IGNORE")
        db.commit()
        still_manual = db.get(SellerNewListing, listing_id)
        assert still_manual.sourcing_tag == "EU A2A", (
            f"manually_classified must block an automated overwrite, got {still_manual.sourcing_tag}"
        )
    finally:
        db.close()
    print("test 7: a manually-classified listing survives an automated _persist_classification pass: ok")

    print("\nALL TESTS PASSED.")
finally:
    cleanup()
    after_counts = snapshot_counts()
    assert after_counts == before_counts, (
        f"row counts must be identical after cleanup -- before={before_counts}, after={after_counts}"
    )
    print(f"\ncleanup verified -- row counts unchanged ({after_counts})")
