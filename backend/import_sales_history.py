"""
One-off, human-triggered import of a SellerToolKit "Sales Summary - By
ASIN" export into SalesHistorySnapshot (2026-09-07) -- see
app/services/sales_history_service.py's own module docstring for the
full design rationale and real file-format quirks.

Never called by a scheduler or any route -- run this by hand whenever
there's a fresh export to bring in. Safe to re-run: each run REPLACES
the entire sales_history_snapshots table with a fresh extract of
whatever file you point it at (see import_seller_toolkit_export's own
docstring for why "replace", not "merge").

Usage:
    python import_sales_history.py "C:\\path\\to\\SellerToolKit export.txt"
"""
import sys

from app.database.base import Base
from app.database.database import engine
from app.services.sales_history_service import import_seller_toolkit_export

if __name__ == "__main__":
    # Same reasoning as import_buy_sheet.py's own comment: this script
    # deliberately imports only the service it needs, not the whole
    # app, so it has to create any brand-new table itself. Safe to call
    # every run -- a no-op for tables that already exist.
    Base.metadata.create_all(bind=engine)

    if len(sys.argv) != 2:
        print("Usage: python import_sales_history.py \"<path to SellerToolKit export>.txt\"")
        sys.exit(1)

    path = sys.argv[1]
    print(f"Importing SellerToolKit sales history from: {path}")
    summary = import_seller_toolkit_export(path)

    if "error" in summary:
        print(f"IMPORT FAILED: {summary['error']}")
        sys.exit(1)

    print()
    print(f"Rows in file:              {summary['total_rows_in_file']}")
    print(f"Rows imported:             {summary['rows_imported']}")
    print(f"Rows missing ASIN:         {summary['rows_missing_asin']}")
    print(f"Distinct ASINs:            {summary['distinct_asins']}")
    print(f"Total units sold:          {summary['total_units_sold']}")
    print(f"Total profit:              {summary['total_profit']}")
    print(f"Period:                    {summary['period_start']} to {summary['period_end']}")
    print()
    print(f"Previous rows replaced:    {summary['previous_rows_replaced']}")
