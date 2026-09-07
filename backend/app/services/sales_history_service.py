"""
SellerToolKit "Sales Summary - By ASIN" import, 2026-09-07 -- Tamara's
own response to "shall I upload some data on everything we have ever
purchased to date?": she uploaded a real Amazon SALES export instead of
the Buy Sheet purchasing workbook HistoricalBuyingService already
covers. Kept as its own module/table (SalesHistorySnapshot) rather than
forced into HistoricalPurchase's schema -- see that model's own
docstring for exactly why the two are genuinely different data shapes.

Real export quirks confirmed against the actual file (446 rows,
2026-05-01 to 2026-09-07):
- Tab-delimited, NOT comma-delimited (commas appear INSIDE numeric
  fields as thousands separators, e.g. "2,205.69" -- stripped before
  float() below).
- "ASIN" is a literal Excel formula string, not a bare ASIN:
  =HYPERLINK("http://Amazon.co.uk/dp/B0D7PVW562","B0D7PVW562") --
  parsed out with a regex rather than trusting either quoted argument
  blindly (the display-text argument is the more reliable one to
  extract; the URL argument is occasionally blank in real rows with
  "Supplier_Link").
- "Date" is always literally the string "n/a" at this per-ASIN
  aggregation level -- there is no real per-row date to parse, hence
  period_start/period_end (parsed from the filename instead) rather
  than a per-row date field.
- Never called by a scheduler or any route -- run by hand via
  import_sales_history.py whenever there's a fresh export.
"""
import csv
import re
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import SalesHistorySnapshot

ASIN_FORMULA_PATTERN = re.compile(r'HYPERLINK\("[^"]*","([A-Z0-9]{6,})"\)', re.IGNORECASE)

# Matches the date range embedded in SellerToolKit's own export filename,
# e.g. "..._All_2026-05-01_To_2026-09-07.txt". Best-effort only -- a
# filename that doesn't match this shape just leaves period_start/end
# as None rather than blocking the import.
FILENAME_DATE_RANGE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})_To_(\d{4}-\d{2}-\d{2})")

REQUIRED_COLUMNS = ("ASIN", "Title", "Brand", "Category", "Orders", "Units", "Profit_Loss", "Sales")


def _parse_asin(raw: str) -> str:
    match = ASIN_FORMULA_PATTERN.search(raw or "")
    return match.group(1).strip().upper() if match else (raw or "").strip().upper()


def _parse_float(raw: str) -> float:
    if not raw or raw.strip().lower() == "n/a":
        return 0.0
    try:
        return float(raw.replace(",", "").strip())
    except ValueError:
        return 0.0


def _parse_int(raw: str) -> int:
    return int(_parse_float(raw))


def _parse_period_from_filename(path: str) -> tuple[datetime | None, datetime | None]:
    match = FILENAME_DATE_RANGE_PATTERN.search(path)
    if not match:
        return None, None
    try:
        start = datetime.strptime(match.group(1), "%Y-%m-%d")
        end = datetime.strptime(match.group(2), "%Y-%m-%d")
        return start, end
    except ValueError:
        return None, None


def import_seller_toolkit_export(path: str) -> dict:
    """
    Manual, human-triggered one-off import (see import_sales_history.py)
    -- NEVER called by a scheduler or any live request handler. REPLACES
    the entire sales_history_snapshots table with this export's rows
    (not an incremental merge) -- this is meant to be a fresh full
    snapshot from the latest export each time it's run, same convention
    HistoricalBuyingService.import_workbook already uses for the same
    reason (a stale prior export must never linger and be double-
    counted alongside a fresh one).

    Returns a summary dict with row counts and any data-quality issues
    found (missing/unparseable ASIN) so a human can judge the import
    before trusting it -- never silently drops a row without accounting
    for it somewhere in this summary.
    """
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter="\t")
        raw_rows = list(reader)

    missing_cols = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing_cols:
        return {
            "error": f"File is missing expected column(s): {missing_cols}. "
                     f"Import aborted -- no existing data was touched.",
        }

    period_start, period_end = _parse_period_from_filename(path)

    total_rows_in_file = len(raw_rows)
    rows = []
    missing_asin = 0

    for r in raw_rows:
        asin = _parse_asin(r.get("ASIN", ""))
        if not asin:
            missing_asin += 1
            continue

        rows.append(SalesHistorySnapshot(
            asin=asin,
            title=(r.get("Title") or "").strip(),
            brand=(r.get("Brand") or "").strip(),
            category=(r.get("Category") or "").strip(),
            orders=_parse_int(r.get("Orders", "")),
            units=_parse_int(r.get("Units", "")),
            profit_loss=_parse_float(r.get("Profit_Loss", "")),
            sales=_parse_float(r.get("Sales", "")),
            cog=_parse_float(r.get("CoG", "")),
            fees=_parse_float(r.get("Fees", "")),
            roi_pct=_parse_float(r.get("RoI", "")),
            margin_pct=_parse_float(r.get("Margin", "")),
            current_stock_qty=_parse_int(r.get("Current_Stock_QTY", "")),
            period_start=period_start,
            period_end=period_end,
        ))

    distinct_asins = {row.asin for row in rows}
    total_units = sum(row.units for row in rows)
    total_profit = round(sum(row.profit_loss for row in rows), 2)

    db = SessionLocal()
    try:
        previous_count = db.query(SalesHistorySnapshot).count()
        db.query(SalesHistorySnapshot).delete()
        db.add_all(rows)
        db.commit()
    finally:
        db.close()

    return {
        "total_rows_in_file": total_rows_in_file,
        "rows_imported": len(rows),
        "rows_missing_asin": missing_asin,
        "distinct_asins": len(distinct_asins),
        "total_units_sold": total_units,
        "total_profit": total_profit,
        "period_start": period_start.date().isoformat() if period_start else None,
        "period_end": period_end.date().isoformat() if period_end else None,
        "previous_rows_replaced": previous_count,
    }
