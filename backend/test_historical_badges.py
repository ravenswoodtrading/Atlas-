"""
Regression test for the "ever bought before / ever on VA sheet before"
badges, 2026-09-07 (Tamara: "a badge on the lead if it is an item we
have ever bought before or a lead that has ever appeared on our VA
sheet").

Run with `python test_historical_badges.py` (plain script, no pytest).
Uses dedicated fake ASINs, cleans up in a try/finally, verified against
exact row counts at the end.
"""
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import (
    HistoricalPurchase, SheetLeadSyncState, ProductRecord, Lead,
)
from app.services.review_queue_service import ReviewQueueService

ASIN_BOUGHT = "B0HISTBADGE1"
ASIN_VA_SHEET = "B0HISTBADGE2"
ASIN_BOTH = "B0HISTBADGE3"
ASIN_NEITHER = "B0HISTBADGE4"
ALL_ASINS = [ASIN_BOUGHT, ASIN_VA_SHEET, ASIN_BOTH, ASIN_NEITHER]


def cleanup():
    db = SessionLocal()
    try:
        db.query(HistoricalPurchase).filter(HistoricalPurchase.asin.in_(ALL_ASINS)).delete(synchronize_session=False)
        db.query(SheetLeadSyncState).filter(SheetLeadSyncState.asin.in_(ALL_ASINS)).delete(synchronize_session=False)
        db.query(ProductRecord).filter(ProductRecord.asin.in_(ALL_ASINS)).delete(synchronize_session=False)
        db.query(Lead).filter(Lead.asin.in_(ALL_ASINS)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


cleanup()

try:
    db = SessionLocal()
    try:
        db.add(HistoricalPurchase(asin=ASIN_BOUGHT, product_name="Test bought item"))
        db.add(HistoricalPurchase(asin=ASIN_BOTH, product_name="Test bought+seen item"))
        # date_last_added must be a REAL date, not "" or "New ASIN" --
        # those two both mean "first time seen", per the 2026-09-07 fix
        # below (only a real repeat date counts as "seen before").
        db.add(SheetLeadSyncState(asin=ASIN_VA_SHEET, content_hash="hash1", date_last_added="06 May 26", last_synced_at=datetime.now(timezone.utc)))
        db.add(SheetLeadSyncState(asin=ASIN_BOTH, content_hash="hash2", date_last_added="12 Jun 26", last_synced_at=datetime.now(timezone.utc)))
        db.add(SheetLeadSyncState(asin=ASIN_NEITHER, content_hash="hash3", date_last_added="New ASIN", last_synced_at=datetime.now(timezone.utc)))
        db.commit()
    finally:
        db.close()

    # 1 -- _batch_historical_flags reports the right combination for each ASIN.
    flags = ReviewQueueService._batch_historical_flags(ALL_ASINS)
    assert flags[ASIN_BOUGHT] == {"ever_purchased": True, "ever_on_va_sheet": False}, flags[ASIN_BOUGHT]
    assert flags[ASIN_VA_SHEET] == {"ever_purchased": False, "ever_on_va_sheet": True}, flags[ASIN_VA_SHEET]
    assert flags[ASIN_BOTH] == {"ever_purchased": True, "ever_on_va_sheet": True}, flags[ASIN_BOTH]
    assert flags[ASIN_NEITHER] == {"ever_purchased": False, "ever_on_va_sheet": False}, flags[ASIN_NEITHER]
    print("test 1: _batch_historical_flags reports the correct flag combination per ASIN: ok")

    # 2 -- empty input returns an empty dict, not an error.
    assert ReviewQueueService._batch_historical_flags([]) == {}
    print("test 2: empty ASIN list returns {} rather than raising: ok")

    # 3 -- get_queue_item annotates a real, currently-outstanding item
    # with these same flags (requires a live source -- use a plain
    # scan record so the item is genuinely outstanding).
    db = SessionLocal()
    try:
        db.add(ProductRecord(
            asin=ASIN_BOUGHT, title="Test bought item", brand="TestBrand", category="",
            category_name="", best_source_marketplace="DE", best_source_cost_gbp=10.0,
            buy_box_now=30.0, fba_fee=3.0, profit=5.0, roi=50.0, recommendation="BUY",
            score=80, monthly_sales=10, sales_drops_30d=0,
            scanned_at=datetime.now(timezone.utc).replace(tzinfo=None), review=None,
        ))
        db.commit()
    finally:
        db.close()

    item = ReviewQueueService.get_queue_item(ASIN_BOUGHT)
    assert item is not None, "fixture must be a real outstanding item"
    assert item["ever_purchased"] is True
    assert item["ever_on_va_sheet"] is False
    print("test 3: get_queue_item includes the historical flags on a real item: ok")

    # 4 -- list_queue_items also annotates every item it returns (spot-
    # check our own fixture inside the real, full queue).
    items = ReviewQueueService.list_queue_items()
    match = next((i for i in items if i["asin"] == ASIN_BOUGHT), None)
    assert match is not None, "fixture must appear in the full queue"
    assert match["ever_purchased"] is True
    assert match["ever_on_va_sheet"] is False
    print("test 4: list_queue_items annotates every item, including ours, correctly: ok")

    print("\nALL TESTS PASSED.")
finally:
    cleanup()
    db = SessionLocal()
    try:
        remaining = (
            db.query(HistoricalPurchase).filter(HistoricalPurchase.asin.in_(ALL_ASINS)).count()
            + db.query(SheetLeadSyncState).filter(SheetLeadSyncState.asin.in_(ALL_ASINS)).count()
            + db.query(ProductRecord).filter(ProductRecord.asin.in_(ALL_ASINS)).count()
            + db.query(Lead).filter(Lead.asin.in_(ALL_ASINS)).count()
        )
    finally:
        db.close()
    assert remaining == 0, "cleanup failed -- fake rows still present"
    print("cleanup verified: 0 fake rows remain.")
