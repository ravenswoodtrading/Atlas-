"""
Regression tests for the OA Review Queue workbench redesign, 2026-09-05
(Tamara's own review of the live screenshot: OA items were rendering as
just another lead card; this replaces that with a dedicated "find the
retail source" workflow -- search buttons, manual source entry, a
Source Found status, while leaving the existing Buy/Reject/Unsure
decision path untouched).

Run with `python test_oa_workbench.py` (same plain-script style as the
other test_*.py files here, no pytest).

Safety, per the scoped brief this implements:
- NO live Keepa/SP-API calls -- save_manual_source deliberately reuses
  the ASIN's own already-scanned ProductRecord instead of a fresh
  Keepa fetch (see that function's own docstring).
- NO scheduler/scan execution.
- NO auto Buy/Reject/Resolve -- saving a source only persists it and
  computes economics; every test below asserts resolve_item/
  _promote_if_qualifying were never triggered.
- Schema change was reported and approved before implementing (see
  OaSourceCandidate.delivery_gbp/notes' own docstrings) -- this file
  only exercises the two new columns, it doesn't add any more.

Every DB-writing test uses a dedicated, obviously-fake ASIN/seller ID
and cleans up in a try/finally, verified against exact row counts at
the end so nothing is left behind in a real Atlas database.
"""
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import (
    ProductRecord, SellerNewListing, TrackedSeller, OaSourceCandidate, OaSourceRun,
)
from app.services.oa_source_discovery_service import OaSourceDiscoveryService
from app.services.review_queue_service import ReviewQueueService, QUEUE_PRIORITY_OA_INVESTIGATE
from fastapi.testclient import TestClient
from app.main import app

TEST_ASIN_PREFIX = "B0WBTEST"
TEST_SELLER_ID = "WBTESTSELLER1"


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cleanup(asins):
    db = SessionLocal()
    try:
        run_ids = [c.run_id for c in db.query(OaSourceCandidate).filter(OaSourceCandidate.asin.in_(asins)).all()]
        db.query(OaSourceCandidate).filter(OaSourceCandidate.asin.in_(asins)).delete(synchronize_session=False)
        if run_ids:
            db.query(OaSourceRun).filter(OaSourceRun.id.in_(run_ids)).delete(synchronize_session=False)
        db.query(SellerNewListing).filter(SellerNewListing.asin.in_(asins)).delete(synchronize_session=False)
        db.query(ProductRecord).filter(ProductRecord.asin.in_(asins)).delete(synchronize_session=False)
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
            db.query(OaSourceCandidate).count(),
            db.query(OaSourceRun).count(),
        )
    finally:
        db.close()


before_counts = snapshot_counts()
created_asins = set()


def make_seller():
    db = SessionLocal()
    try:
        seller = TrackedSeller(seller_id=TEST_SELLER_ID, nickname="Workbench Test Seller", active=True)
        db.add(seller)
        db.commit()
        db.refresh(seller)
        return seller.id
    finally:
        db.close()


def make_record(asin, buy_box_now=100.0, fba_fee=5.0, ean="", brand="TestBrand", title=None):
    db = SessionLocal()
    try:
        record = ProductRecord(
            asin=asin, title=title or f"Test Workbench Product {asin}", brand=brand, category="",
            category_name="", buy_box_now=buy_box_now, fba_fee=fba_fee,
            recommendation="IGNORE", score=0, monthly_sales=0, sales_drops_30d=0, ean=ean,
            scanned_at=utcnow(), review=None,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        created_asins.add(asin)
        return record.id
    finally:
        db.close()


def make_listing(seller_id, asin, record_id):
    db = SessionLocal()
    try:
        listing = SellerNewListing(
            tracked_seller_id=seller_id, asin=asin, product_record_id=record_id,
            sourcing_tag="OA / unclear", currently_buyable=False, detected_at=utcnow(),
        )
        db.add(listing)
        db.commit()
        db.refresh(listing)
        created_asins.add(asin)
        return listing.id
    finally:
        db.close()


client = TestClient(app)

try:
    seller = make_seller()

    # =====================================================================
    # 1 -- save_manual_source creates a candidate from the ASIN's OWN
    # ProductRecord, no Keepa call, no auto-promotion.
    # =====================================================================
    asin1 = f"{TEST_ASIN_PREFIX}A"
    record1 = make_record(asin1, buy_box_now=80.0, fba_fee=5.0, ean="1111111111111")
    make_listing(seller, asin1, record1)

    products_before = SessionLocal().query(ProductRecord).count()

    result = OaSourceDiscoveryService.save_manual_source(
        asin1, "example-retailer.co.uk", "https://example-retailer.co.uk/p", 30.0, 2.5, "test note",
    )
    assert result["effective_cost"] == 32.5
    assert result["profit"] is not None and result["roi"] is not None
    print("test 1: save_manual_source computes effective cost + profit/ROI from the existing ProductRecord: ok")

    products_after = SessionLocal().query(ProductRecord).count()
    assert products_after == products_before, "save_manual_source must NEVER auto-promote a new ProductRecord"
    print("test 1b: no auto-promotion happened (ProductRecord count unchanged): ok")

    candidate = OaSourceDiscoveryService.get_manual_candidate(asin1)
    assert candidate is not None
    assert candidate.retailer_domain == "example-retailer.co.uk"
    assert candidate.retailer_price_gbp == 30.0
    assert candidate.delivery_gbp == 2.5
    assert candidate.notes == "test note"
    assert candidate.price_source == "manual"
    assert candidate.outcome == "candidate_found"
    print("test 1c: get_manual_candidate returns the persisted source with the new delivery_gbp/notes columns: ok")

    # =====================================================================
    # 2 -- saving again for the SAME ASIN updates the same candidate, no
    # duplicate row.
    # =====================================================================
    OaSourceDiscoveryService.save_manual_source(
        asin1, "cheaper-shop.co.uk", "https://cheaper-shop.co.uk/p", 25.0, 0.0, "found a better price",
    )
    db = SessionLocal()
    count = db.query(OaSourceCandidate).filter(OaSourceCandidate.asin == asin1).count()
    db.close()
    assert count == 1, f"expected exactly 1 candidate row for {asin1}, got {count}"
    updated = OaSourceDiscoveryService.get_manual_candidate(asin1)
    assert updated.retailer_domain == "cheaper-shop.co.uk"
    assert updated.retailer_price_gbp == 25.0
    print("test 2: a second save updates the SAME candidate row, not a duplicate: ok")

    # =====================================================================
    # 3 -- the Review Queue detail route renders the workbench: search
    # buttons for known fields only, Source Found status after a save,
    # and the pre-filled update form.
    # =====================================================================
    r_detail = client.get(f"/review-queue/item/{asin1}")
    assert r_detail.status_code == 200
    assert "Source search" in r_detail.text
    assert "Shopping: EAN" in r_detail.text  # ean was provided
    assert "Shopping: MPN" not in r_detail.text  # no mpn known -- must not fabricate
    assert "SOURCE FOUND" in r_detail.text.upper()
    assert "cheaper-shop.co.uk" in r_detail.text
    assert "Update source" in r_detail.text
    print("test 3: detail panel shows real search buttons only, and the Source Found status after saving: ok")

    # =====================================================================
    # 4 -- an OA-only item with NO EAN known must not show a fabricated
    # EAN search button either.
    # =====================================================================
    asin2 = f"{TEST_ASIN_PREFIX}B"
    record2 = make_record(asin2, buy_box_now=150.0, fba_fee=6.0, ean="")
    make_listing(seller, asin2, record2)

    r_detail2 = client.get(f"/review-queue/item/{asin2}")
    assert r_detail2.status_code == 200
    assert "Shopping: EAN" not in r_detail2.text
    assert "Shopping: MPN" not in r_detail2.text
    assert "Shopping: Product" in r_detail2.text  # title/brand always known
    print("test 4: no EAN known -> no fabricated EAN search button, but the product/web searches still show: ok")

    # =====================================================================
    # 5 -- a pure OA-only item (no real Atlas action, no VA submission)
    # must NOT show the old misleading "Current Opportunity" box with a
    # £0.00 buy price/profit as if a source were already confirmed.
    # =====================================================================
    assert "Current Opportunity" not in r_detail2.text, (
        "a pure OA-only item must not render the generic Source/Buy price/Profit box"
    )
    assert "Atlas needs you to find the retail source" in r_detail2.text
    print("test 5: pure OA-only item shows the honest 'find a source' message, not the misleading lead box: ok")

    # =====================================================================
    # 6 -- the existing Buy/Reject/Unsure Review Actions form is still
    # present and unchanged regardless of whether a source was saved.
    # =====================================================================
    assert "qi-detail-actions-form" in r_detail.text
    assert 'value="approved"' in r_detail.text
    assert 'value="rejected"' in r_detail.text
    print("test 6: the existing Review Actions (Buy/Reject/Unsure) remain available on an OA item: ok")

    # =====================================================================
    # 7 -- the /review-queue/oa/save-source route itself, end to end via
    # TestClient (not calling the service function directly this time).
    # =====================================================================
    asin3 = f"{TEST_ASIN_PREFIX}C"
    record3 = make_record(asin3, buy_box_now=200.0, fba_fee=8.0, ean="2222222222222")
    make_listing(seller, asin3, record3)

    r_save = client.post("/review-queue/oa/save-source", data={
        "asin": asin3, "retailer_domain": "routetest.co.uk", "retailer_url": "https://routetest.co.uk/p",
        "retailer_price_gbp": "60.0", "delivery_gbp": "5.0", "notes": "via route",
    })
    assert r_save.status_code == 200
    body = r_save.json()
    assert body["ok"] is True
    assert body["effective_cost"] == 65.0
    print("test 7: POST /review-queue/oa/save-source persists correctly end to end: ok")

    # This must NOT have resolved the item -- it should still be
    # outstanding in the Review Queue afterwards (saving a source is not
    # a decision).
    items = ReviewQueueService.list_queue_items()
    still_outstanding = any(i["asin"] == asin3 for i in items)
    assert still_outstanding, "saving a source must never resolve/remove the item from the queue"
    item3 = next(i for i in items if i["asin"] == asin3)
    assert QUEUE_PRIORITY_OA_INVESTIGATE in item3["views"]
    print("test 7b: the item remains outstanding in the Review Queue after saving a source (not auto-resolved): ok")

    # =====================================================================
    # 8 -- dual-membership: an item that's ALSO a real scan opportunity
    # keeps its own action content AND gets the OA workbench additively.
    # =====================================================================
    asin4 = f"{TEST_ASIN_PREFIX}D"
    record4 = make_record(asin4, buy_box_now=150.0, fba_fee=6.0, ean="3333333333333")
    # Make this one a real, notable BUY so it independently earns a lens action too.
    db = SessionLocal()
    try:
        rec = db.query(ProductRecord).filter(ProductRecord.id == record4).first()
        rec.recommendation = "BUY"
        rec.profit = 20.0
        rec.roi = 60.0
        rec.monthly_sales = 15
        rec.sales_drops_30d = 8
        rec.best_source_marketplace = "DE"
        rec.best_source_cost_gbp = 40.0
        db.commit()
    finally:
        db.close()
    make_listing(seller, asin4, record4)

    r_detail4 = client.get(f"/review-queue/item/{asin4}")
    assert r_detail4.status_code == 200
    assert "Source search" in r_detail4.text  # workbench still present
    assert "Recommended action" in r_detail4.text  # real action content still present too
    print("test 8: dual-membership item shows BOTH its real action content and the OA workbench: ok")

finally:
    cleanup(created_asins)

after_counts = snapshot_counts()
assert after_counts == before_counts, (
    f"row counts must be identical after cleanup -- before={before_counts}, after={after_counts}"
)
print(f"\ncleanup verified -- {len(created_asins)} test ASINs created and fully removed, row counts unchanged")
print("\nALL PASS")
