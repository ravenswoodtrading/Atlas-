"""
Regression tests for folding OA Source Discovery's "worth investigating"
bucket into Review Queue as a fifth view (OA_INVESTIGATE), 2026-09-05.

Run with `python test_oa_review_queue.py` (same plain-script style as
the other test_*.py files here, no pytest).

Every DB-writing test here uses a dedicated, obviously-fake ASIN/seller
ID and cleans up in a try/finally, verified against exact row counts at
the end so nothing is left behind in a real Atlas database.

Safety, per the scoped brief this implements:
- NO live Keepa/SP-API calls anywhere in this file.
- NO scheduler/scan execution -- every fixture is a plain DB row, never
  a real BrandScanService.scan()/ScanQueueService tick.
- NO production Buy/Reject/Resolve actions -- resolve_item is never
  called against these fixtures; the six checks below are read-only
  assertions on list_queue_items()/queue_priority_summary()/the
  TestClient's GET responses.
- NO schema changes -- reuses the existing ProductRecord/SellerNewListing/
  TrackedSeller tables exactly as test_competitor_watch_redesign.py's
  own OA fixture (test 3) already does.
"""
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import ProductRecord, SellerNewListing, TrackedSeller
from app.services.review_queue_service import (
    ReviewQueueService, QUEUE_PRIORITY_BUY_NOW, QUEUE_PRIORITY_OA_INVESTIGATE,
)
from fastapi.testclient import TestClient
from app.main import app

TEST_ASIN_PREFIX = "B0OATEST"
TEST_SELLER_ID = "OATESTSELLER1"


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cleanup(asins):
    db = SessionLocal()
    try:
        db.query(ProductRecord).filter(ProductRecord.asin.in_(asins)).delete(synchronize_session=False)
        db.query(SellerNewListing).filter(SellerNewListing.asin.in_(asins)).delete(synchronize_session=False)
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


before_counts = snapshot_counts()
created_asins = set()


def make_seller():
    db = SessionLocal()
    try:
        seller = TrackedSeller(seller_id=TEST_SELLER_ID, nickname="OA Test Seller", active=True)
        db.add(seller)
        db.commit()
        db.refresh(seller)
        return seller.id
    finally:
        db.close()


def make_record(asin, recommendation="IGNORE", profit=0.0, roi=0.0, buy_box_now=150.0,
                 best_source_marketplace="", best_source_cost_gbp=0.0, monthly_sales=0,
                 sales_drops_30d=0, category_name="", fba_fee=6.0):
    db = SessionLocal()
    try:
        record = ProductRecord(
            asin=asin, title=f"Test OA Product {asin}", brand="TestBrand", category="",
            category_name=category_name, best_source_marketplace=best_source_marketplace,
            best_source_cost_gbp=best_source_cost_gbp, buy_box_now=buy_box_now, fba_fee=fba_fee,
            profit=profit, roi=roi, recommendation=recommendation, score=80,
            monthly_sales=monthly_sales, sales_drops_30d=sales_drops_30d,
            scanned_at=utcnow(), review=None,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        created_asins.add(asin)
        return record.id
    finally:
        db.close()


def make_listing(seller_id, asin, record_id, sourcing_tag, currently_buyable=False):
    db = SessionLocal()
    try:
        listing = SellerNewListing(
            tracked_seller_id=seller_id, asin=asin, product_record_id=record_id,
            sourcing_tag=sourcing_tag, currently_buyable=currently_buyable,
            detected_at=utcnow(),
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
    # 1 & 2 -- an OA-worth-investigating item appears in the new view and
    # is counted correctly
    # =====================================================================
    summary_before = ReviewQueueService.queue_priority_summary()

    asin_oa = f"{TEST_ASIN_PREFIX}A"
    record_oa = make_record(asin_oa, buy_box_now=150.0, fba_fee=6.0)  # clears breakeven, same fixture shape as test_competitor_watch_redesign.py's own OA test
    make_listing(seller, asin_oa, record_oa, "OA / unclear")

    items = ReviewQueueService.list_queue_items()
    item_oa = next((i for i in items if i["asin"] == asin_oa), None)

    assert item_oa is not None, "OA-worth-investigating item must appear in list_queue_items()"
    assert QUEUE_PRIORITY_OA_INVESTIGATE in item_oa["views"], "item must carry the OA_INVESTIGATE view"
    assert item_oa["oa_price_guide"] and item_oa["oa_price_guide"]["breakeven"] > 0
    assert "oa_investigate" in item_oa["sources"]
    print("test 1: OA-worth-investigating item appears in Review Queue with the OA_INVESTIGATE view: ok")

    # NOTE: NOT asserted as a before/after delta on the live global count
    # -- confirmed live that SellerWatchService.list_oa_worth_investigating's
    # own MAX_LEADS=500 cap is already saturated by real production data
    # (500 raw listings / 490 unique ASINs right now), so one more
    # recently-detected fixture can push out an older real entry with no
    # visible change to the exposed total. That's a real, pre-existing
    # volume cap on the OA population specifically (worth flagging
    # separately -- MAX_LEADS was sized for a "realistic notable
    # backlog", not the much larger real OA/unclear one), not something
    # this test should depend on. Verified instead as a structural
    # invariant: the summary's oa_investigate figure must always equal a
    # fresh independent count of items actually carrying the view, and
    # this specific item is one of them.
    summary_after = ReviewQueueService.queue_priority_summary()
    independent_oa_count = sum(1 for i in ReviewQueueService.list_queue_items() if QUEUE_PRIORITY_OA_INVESTIGATE in i["views"])
    assert summary_after["oa_investigate"] == independent_oa_count, (
        f"summary's oa_investigate count ({summary_after['oa_investigate']}) must match an independent "
        f"tally of items actually carrying the view ({independent_oa_count})"
    )
    assert item_oa["asin"] in {
        i["asin"] for i in ReviewQueueService.list_queue_items() if QUEUE_PRIORITY_OA_INVESTIGATE in i["views"]
    }
    print("test 2: oa_investigate count in queue_priority_summary() matches an independent tally, and includes this item: ok")

    # =====================================================================
    # 3 -- OA membership does not push an item out of another applicable
    # view: an ASIN that's BOTH a real scan-sourced BUY and separately
    # flagged OA/unclear by a competitor listing must show BOTH views.
    # =====================================================================
    asin_dual = f"{TEST_ASIN_PREFIX}B"
    record_dual = make_record(
        asin_dual, recommendation="BUY", profit=20.0, roi=60.0, monthly_sales=15, sales_drops_30d=8,
        best_source_marketplace="DE", best_source_cost_gbp=40.0, buy_box_now=150.0, fba_fee=6.0,
    )
    make_listing(seller, asin_dual, record_dual, "OA / unclear")

    items = ReviewQueueService.list_queue_items()
    item_dual = next((i for i in items if i["asin"] == asin_dual), None)
    assert item_dual is not None
    assert QUEUE_PRIORITY_BUY_NOW in item_dual["views"], "the real scan-sourced BUY view must survive"
    assert QUEUE_PRIORITY_OA_INVESTIGATE in item_dual["views"], "OA membership must be ADDITIVE, not exclusive"
    print("test 3: an item keeps its BUY_NOW view alongside OA_INVESTIGATE -- multi-view membership preserved: ok")

    # =====================================================================
    # 4 -- existing Scan/VA/Competitor Review Queue behaviour unchanged:
    # a plain scan-sourced BUY item with NO OA listing at all must NOT
    # pick up OA_INVESTIGATE, and a plain competitor A2A find must not
    # be relabelled "oa_investigate".
    # =====================================================================
    asin_plain_scan = f"{TEST_ASIN_PREFIX}C"
    make_record(
        asin_plain_scan, recommendation="BUY", profit=20.0, roi=60.0, monthly_sales=15, sales_drops_30d=8,
        best_source_marketplace="DE", best_source_cost_gbp=40.0, buy_box_now=150.0, fba_fee=6.0,
    )
    items = ReviewQueueService.list_queue_items()
    item_plain = next((i for i in items if i["asin"] == asin_plain_scan), None)
    assert item_plain is not None
    assert QUEUE_PRIORITY_BUY_NOW in item_plain["views"]
    assert QUEUE_PRIORITY_OA_INVESTIGATE not in item_plain["views"], "a plain scan BUY with no OA listing must never get OA_INVESTIGATE"
    assert item_plain["oa_price_guide"] is None
    assert item_plain["sources"] == ["scan"]
    print("test 4: existing scan-only Review Queue behaviour is unchanged (no spurious OA_INVESTIGATE): ok")

    # =====================================================================
    # 5 -- existing OA Source Discovery/Google/Source Finder pathways
    # remain available from the new Review Queue view.
    # =====================================================================
    r_detail = client.get(f"/review-queue/item/{asin_oa}")
    assert r_detail.status_code == 200
    assert "OA" in r_detail.text and "Worth Investigating" in r_detail.text
    assert "Google" in r_detail.text
    assert f"/competitors?tab=source_finder&amp;asin={asin_oa}" in r_detail.text  # HTML-escaped & from Jinja autoescape
    print("test 5: Review Queue's OA detail view still surfaces Google search + the existing Source Finder link: ok")

    # The Opportunities page's own OA pathway (unchanged, still there).
    r_opportunities = client.get("/competitors?tab=opportunities")
    assert r_opportunities.status_code == 200
    r_source_finder = client.get(f"/competitors?tab=source_finder&asin={asin_oa}")
    assert r_source_finder.status_code == 200
    print("test 5b: Opportunities/Source Finder pages themselves are untouched and still render: ok")

    # =====================================================================
    # 6 -- no duplicate item is created merely because it is now visible
    # in Review Queue: one ASIN, one merged item, even when it has BOTH
    # a scan-sourced record AND an OA-tagged competitor listing.
    # =====================================================================
    matches = [i for i in ReviewQueueService.list_queue_items() if i["asin"] == asin_dual]
    assert len(matches) == 1, f"expected exactly one merged item for {asin_dual}, got {len(matches)}"
    assert sorted(matches[0]["sources"]) == ["oa_investigate", "scan"]
    print("test 6: dual-sourced ASIN merges into exactly ONE Review Queue item, not a duplicate: ok")

    # =====================================================================
    # 7 -- the live page itself renders cleanly with the new view/filter.
    # =====================================================================
    for url in ("/review-queue", "/review-queue?view=oa_investigate", "/review-queue?source=oa_investigate", "/"):
        r = client.get(url)
        assert r.status_code == 200, f"{url} -> {r.status_code}"
    r_oa_tab = client.get("/review-queue?view=oa_investigate")
    assert asin_oa in r_oa_tab.text
    assert asin_plain_scan not in r_oa_tab.text, "the OA-only filtered view must not show a plain scan BUY with no OA membership"
    print("test 7: Review Queue page (including the new OA tab/filter) and Command Centre render cleanly: ok")

finally:
    cleanup(created_asins)

after_counts = snapshot_counts()
assert after_counts == before_counts, (
    f"row counts must be identical after cleanup -- before={before_counts}, after={after_counts}"
)
print(f"\ncleanup verified -- {len(created_asins)} test ASINs created and fully removed, row counts unchanged")
print("\nALL PASS")
