"""
Regression test for the SellerToolKit sales-history import, 2026-09-07.

Builds a small synthetic export file (same real quirks as the actual
upload: tab-delimited, HYPERLINK-wrapped ASIN, comma-thousands numeric
fields, "n/a" Date) rather than depending on the user's real uploaded
file, so this test is stable and portable.

Run with `python test_sales_history_import.py` (plain script, no pytest).
"""
import os
import tempfile

from app.database.database import SessionLocal
from app.database.models import SalesHistorySnapshot
from app.services.sales_history_service import import_seller_toolkit_export, _parse_asin, _parse_float

FAKE_HEADER = (
    "Date\tASIN\tTitle\tBrand\tCategory\tMarketplace\tOrders\tUnits\tProfit_Loss\tSales\t"
    "Shipping_Charge\tShipping_Cost\tCoG\tFees\tPromotion\tVAT\tRoI\tMargin\t"
    "Current_Stock_QTY\tSupplier\tSupplier_Link\t\n"
)
FAKE_ROW_1 = (
    'n/a\t=HYPERLINK("http://Amazon.co.uk/dp/B0TESTSALE1","B0TESTSALE1")\tTest Product One\tTestBrand\tToys & Games\tAll\t'
    '106\t116\t222.30\t2,205.69\t0.00\t0.00\t1,088.08\t708.61\t0.00\t186.70\t24.74\t10.08\t5\t\t=HYPERLINK("","")\t\n'
)
FAKE_ROW_2 = (
    'n/a\t=HYPERLINK("http://Amazon.co.uk/dp/B0TESTSALE2","B0TESTSALE2")\tTest Product Two\tTestBrand\tBeauty\tAll\t'
    '3\t3\t5.30\t38.67\t0.00\t0.00\t19.44\t13.67\t0.00\t0.26\t32.72\t13.71\t0\t\t=HYPERLINK("","")\t\n'
)
FAKE_ROW_NO_ASIN = (
    'n/a\t\tTest Product No ASIN\tTestBrand\tBeauty\tAll\t'
    '1\t1\t1.00\t10.00\t0.00\t0.00\t5.00\t3.00\t0.00\t1.00\t20.00\t10.00\t0\t\t=HYPERLINK("","")\t\n'
)


def cleanup():
    db = SessionLocal()
    try:
        db.query(SalesHistorySnapshot).delete()
        db.commit()
    finally:
        db.close()


def snapshot_count():
    db = SessionLocal()
    try:
        return db.query(SalesHistorySnapshot).count()
    finally:
        db.close()


# 1 -- unit-level parsing helpers.
assert _parse_asin('=HYPERLINK("http://Amazon.co.uk/dp/B0D7PVW562","B0D7PVW562")') == "B0D7PVW562"
assert _parse_float("2,205.69") == 2205.69
assert _parse_float("n/a") == 0.0
assert _parse_float("") == 0.0
print("test 1: ASIN/float parsing helpers handle real export quirks correctly: ok")

# 2 -- full import, using a fake filename with a real date-range shape.
before = snapshot_count()

fd, path = tempfile.mkstemp(suffix="_2026-05-01_To_2026-09-07.txt")
try:
    with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
        f.write(FAKE_HEADER)
        f.write(FAKE_ROW_1)
        f.write(FAKE_ROW_2)
        f.write(FAKE_ROW_NO_ASIN)

    result = import_seller_toolkit_export(path)
    assert "error" not in result, result
    assert result["total_rows_in_file"] == 3, result
    assert result["rows_imported"] == 2, result
    assert result["rows_missing_asin"] == 1, result
    assert result["distinct_asins"] == 2, result
    assert result["period_start"] == "2026-05-01", result
    assert result["period_end"] == "2026-09-07", result
    print("test 2: import parses the file correctly, skips the ASIN-less row, extracts the filename date range: ok")

    db = SessionLocal()
    try:
        row = db.query(SalesHistorySnapshot).filter(SalesHistorySnapshot.asin == "B0TESTSALE1").first()
        assert row is not None
        assert row.sales == 2205.69, row.sales
        assert row.cog == 1088.08, row.cog
        assert row.units == 116
        assert row.title == "Test Product One"
    finally:
        db.close()
    print("test 3: comma-thousands numeric fields parsed correctly on the actual stored row: ok")

    # 4 -- re-importing REPLACES rather than duplicates.
    result2 = import_seller_toolkit_export(path)
    assert result2["previous_rows_replaced"] == 2, result2
    assert snapshot_count() == 2, "re-import must replace, not duplicate"
    print("test 4: re-running the import replaces the previous snapshot, not duplicates it: ok")

    print("\nALL TESTS PASSED.")
finally:
    os.remove(path)
    cleanup()
    after = snapshot_count()
    assert after == 0, f"cleanup failed -- {after} rows remain"
    print("cleanup verified: 0 rows remain.")
