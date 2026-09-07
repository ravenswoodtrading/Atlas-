"""
One-off, human-triggered import of the team's purchasing workbook into
Historical Buying Intelligence (Phase 4D, 2026-09-04) -- see
app/services/historical_buying_service.py's own module docstring for
the full design rationale.

Never called by a scheduler or any route -- run this by hand whenever
there's a new export of the Buy Sheet to bring in. Safe to re-run: each
run REPLACES the entire historical_purchases table with a fresh
extract of whatever workbook you point it at (see import_workbook's
own docstring for why "replace", not "merge").

Usage:
    python import_buy_sheet.py "C:\\path\\to\\Purchasing Sheet.xlsx"
"""
import sys

from app.database.base import Base
from app.database.database import engine
from app.services.historical_buying_service import import_workbook

if __name__ == "__main__":
    # Base.metadata.create_all() normally runs as a side effect of
    # importing app.main -- this script deliberately imports only the
    # service it needs, not the whole app (avoids pulling in the
    # scheduler/route wiring for a one-off CLI import), so it has to
    # create any brand-new table (historical_purchases) itself. Safe
    # to call every run -- a no-op for tables that already exist.
    Base.metadata.create_all(bind=engine)

    if len(sys.argv) != 2:
        print("Usage: python import_buy_sheet.py \"<path to workbook>.xlsx\"")
        sys.exit(1)

    path = sys.argv[1]
    print(f"Importing Buy Sheet from: {path}")
    summary = import_workbook(path)

    if "error" in summary:
        print(f"IMPORT FAILED: {summary['error']}")
        sys.exit(1)

    print()
    print(f"Batch id:                 {summary['batch_id']}")
    print(f"Rows in sheet:             {summary['total_rows_in_sheet']}")
    print(f"Rows imported:             {summary['rows_imported']}")
    print(f"Rows dropped (no date):    {summary['rows_dropped_no_date']}")
    print(f"Rows missing ASIN:         {summary['rows_missing_asin']}")
    print(f"Rows missing brand:        {summary['rows_missing_brand']}")
    print()
    print(f"EU A2A rows:               {summary['eu_a2a_rows']}")
    print(f"EU A2A distinct brands:    {summary['eu_a2a_distinct_brands']}")
    print(f"EU A2A distinct ASINs:     {summary['eu_a2a_distinct_asins']}")
    print(f"EU A2A total profit:       {summary['eu_a2a_total_profit']}")
    print()
    print(f"Previous rows replaced:    {summary['previous_rows_replaced']}")
