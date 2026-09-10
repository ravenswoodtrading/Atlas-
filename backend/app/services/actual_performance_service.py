"""Seller Toolkit SKU imports and cautious 30-day performance assessment."""
import csv
import io
import re
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from app.database.database import SessionLocal
from app.database.models import (SkuDailyPerformance, SkuPerformanceSnapshot, VaSalesLine,
    VaSalesImport, ReportUpload, AmazonInventoryLedgerLine)
from app.services.google_sheets_client import open_sheet

ASIN_PATTERN = re.compile(r'"([A-Z0-9]{10})"', re.I)
REQUIRED = ("SKU", "ASIN", "Units", "Profit_Loss", "Sales")
PURCHASING_SHEET_URL = "https://docs.google.com/spreadsheets/d/1WcO8SQ6cQEmoVG-GUmAJwBde1AIgp4aBg9pUBJ62UA0/edit"
_purchase_dates_cache = {"at": None, "value": {}}


def _asin(value):
    found = ASIN_PATTERN.search(value or "")
    return (found.group(1) if found else (value or "")).strip().upper()


def _number(value):
    try:
        return float(str(value or "").replace(",", "").replace("£", "").strip())
    except ValueError:
        return 0.0


def _date(value):
    for pattern in ("%d %B %Y", "%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d %b %y"):
        try:
            return datetime.strptime((value or "").strip(), pattern)
        except ValueError:
            pass
    return None


def _purchase_date(value):
    for pattern in ("%d %b %y", "%Y-%m-%d", "%d %b %Y", "%d/%m/%Y", "%d %B %Y"):
        try:
            return datetime.strptime((value or "").strip(), pattern).date()
        except ValueError:
            pass
    return None


def _purchase_dates():
    now = datetime.now(timezone.utc)
    if _purchase_dates_cache["at"] and now - _purchase_dates_cache["at"] < timedelta(hours=1):
        return _purchase_dates_cache["value"]
    dates = {}
    try:
        values = open_sheet(PURCHASING_SHEET_URL).worksheet("Buy Sheet").get_all_values()
        headers = {}
        for index, value in enumerate(values[0]):
            headers.setdefault(value.strip().lower(), index)
        for row in values[1:]:
            sku_index, date_index = headers.get("sku"), headers.get("date ordered")
            sku = row[sku_index].strip() if sku_index is not None and len(row) > sku_index else ""
            ordered = _purchase_date(row[date_index]) if date_index is not None and len(row) > date_index else None
            if sku and ordered:
                dates.setdefault(sku, ordered)
    except Exception:
        pass
    _purchase_dates_cache.update({"at": now, "value": dates})
    return dates


def _rows(text):
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise ValueError("The export is empty.")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t" if "\t" in text.splitlines()[0] else ",")
    missing = [item for item in REQUIRED if item not in (reader.fieldnames or [])]
    if missing:
        raise ValueError("Missing expected column(s): " + ", ".join(missing))
    return list(reader)


def _parse_summary(text):
    rows = _rows(text)
    missing = [key for key in ('CoG', 'RoI', 'Margin', 'Current_Stock_QTY') if not rows or key not in rows[0]]
    if missing:
        raise ValueError('Summary export is missing expected column(s): ' + ', '.join(missing))
    imported = []
    for row in rows:
        sku = (row.get("SKU") or "").strip().strip('"')
        if not sku:
            continue
        imported.append(SkuPerformanceSnapshot(
            sku=sku, asin=_asin(row.get("ASIN", "")), title=(row.get("Title") or "").strip(),
            units=int(_number(row.get("Units"))), profit_loss=_number(row.get("Profit_Loss")),
            sales=_number(row.get("Sales")), cog=_number(row.get("CoG")),
            roi_pct=_number(row.get("RoI")), margin_pct=_number(row.get("Margin")),
            current_stock_qty=int(_number(row.get("Current_Stock_QTY"))),
        ))
    if not imported:
        raise ValueError("Summary export contains no SKU rows.")
    return imported


def _parse_daily(text):
    grouped = defaultdict(lambda: {"asin": "", "first": None, "last": None, "days": 0, "units": 0})
    for row in _rows(text):
        sku = (row.get("SKU") or "").strip().strip('"')
        day = _date(row.get("Date"))
        if not sku:
            continue
        if not day:
            raise ValueError(f"Invalid or missing daily sales date for SKU {sku}.")
        bucket = grouped[sku]
        bucket["asin"] = _asin(row.get("ASIN", ""))
        bucket["first"] = min(filter(None, (bucket["first"], day)), default=day)
        bucket["last"] = max(filter(None, (bucket["last"], day)), default=day)
        bucket["days"] += 1
        bucket["units"] += int(_number(row.get("Units")))
    if not grouped:
        raise ValueError("Daily export contains no SKU histories.")
    return [SkuDailyPerformance(sku=sku, asin=data["asin"], first_sale_at=data["first"], last_sale_at=data["last"], sale_days=data["days"], units_sold=data["units"]) for sku, data in grouped.items()]


def _sales_lines(text):
    lines = []
    for index, row in enumerate(_rows(text), 2):
        if not (row.get("SKU") or "").strip():
            continue
        numbers = {}
        for field in ("Units", "Sales", "Profit_Loss", "CoG"):
            try:
                number = float(str(row.get(field) or "").replace(",", "").replace("£", "").strip())
                if not math.isfinite(number):
                    raise ValueError()
                numbers[field] = number
            except ValueError:
                raise ValueError(f"Invalid {field} on daily export row {index}.") from None
        if not numbers["Units"].is_integer():
            raise ValueError(f"Non-whole unit quantity on daily export row {index}.")
        asin, day = _asin(row.get("ASIN")), _date(row.get("Date"))
        if not re.fullmatch(r"[A-Z0-9]{10}", asin) or not day:
            raise ValueError(f"Missing or invalid ASIN/date on daily export row {index}.")
        lines.append(VaSalesLine(source_row=index, sku=row["SKU"].strip().strip('"'), asin=asin,
                                 sold_at=day, units=int(numbers["Units"]), sales=numbers["Sales"], profit=numbers["Profit_Loss"], cog=numbers["CoG"]))
    return lines


def import_reports(summary_text, daily_text, period_start=None, period_end=None, summary_filename='', daily_filename=''):
    """Validate both exports and replace them in one transaction."""
    summaries = _parse_summary(summary_text)
    histories = _parse_daily(daily_text)
    lines = _sales_lines(daily_text)
    start, end = _date(period_start), _date(period_end)
    if not start or not end or start > end:
        raise ValueError("Enter the daily export's full reporting start and end dates.")
    if any(not start <= line.sold_at <= end for line in lines):
        raise ValueError("Daily sales rows fall outside the supplied reporting period.")
    db = SessionLocal()
    try:
        db.query(SkuPerformanceSnapshot).delete()
        db.query(SkuDailyPerformance).delete()
        db.query(VaSalesLine).delete()
        db.query(VaSalesImport).delete()
        db.add_all(summaries)
        db.add_all(histories)
        db.add_all(lines)
        db.add(VaSalesImport(period_start=start, period_end=end))
        for key, filename, count in [('seller_toolkit_summary', summary_filename, len(summaries)),
                                      ('seller_toolkit_daily', daily_filename, len(lines))]:
            # Keep only the display name; upload filenames never become paths.
            filename = filename.replace('\\', '/').rsplit('/', 1)[-1][:255]
            db.merge(ReportUpload(report_key=key, filename=filename, row_count=count,
                period_start=start, period_end=end, uploaded_at=datetime.now(timezone.utc)))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return len(summaries), len(histories)


def report_rows():
    db = SessionLocal()
    try:
        daily = {row.sku: row for row in db.query(SkuDailyPerformance).all()}
        purchase_dates = _purchase_dates()
        result = []
        for row in db.query(SkuPerformanceSnapshot).all():
            timing = daily.get(row.sku)
            # Until a received/listed date is available, first-sale timing is useful
            # but the report does not pretend it proves the full 30-day stock target.
            financial_pass = row.roi_pct >= 25 or row.margin_pct >= 14
            ordered_on = purchase_dates.get(row.sku)
            age_days = (date.today() - ordered_on).days if ordered_on else None
            first_sale_days = (timing.first_sale_at.date() - ordered_on).days if timing and timing.first_sale_at and ordered_on else None
            if age_days is None:
                outcome = "Timing unavailable"
            elif age_days < 30:
                outcome = "Early performance"
            elif row.current_stock_qty == 0 and financial_pass:
                outcome = "Passed"
            elif row.current_stock_qty == 0:
                outcome = "Sold, return below target"
            elif financial_pass:
                outcome = "Return met, stock held"
            else:
                outcome = "Stock held"
            result.append({
                "sku": row.sku, "asin": row.asin, "title": row.title, "units": row.units,
                "profit": row.profit_loss, "roi": row.roi_pct, "margin": row.margin_pct,
                "stock": row.current_stock_qty, "outcome": outcome, "age_days": age_days,
                "first_sale_days": first_sale_days,
                "first_sale": timing.first_sale_at.date() if timing and timing.first_sale_at else None,
                "last_sale": timing.last_sale_at.date() if timing and timing.last_sale_at else None,
                "sale_days": timing.sale_days if timing else 0,
                "units_sold": timing.units_sold if timing else row.units,
            })
        return sorted(result, key=lambda item: (item["outcome"] != "Stock held", -item["stock"], -item["profit"]))
    finally:
        db.close()


def import_status():
    db = SessionLocal()
    try:
        summary = db.query(SkuPerformanceSnapshot).count()
        daily = db.query(SkuDailyPerformance).count()
        # Older local databases may predate the ledger table; treat that as
        # an empty upload until the additive schema migration runs.
        try:
            ledger_rows = db.query(AmazonInventoryLedgerLine).count()
        except Exception as exc:
            if 'no such table' not in str(exc).lower():
                raise
            ledger_rows = 0
        uploads = {r.report_key: dict(filename=r.filename, rows=r.row_count, start=r.period_start.date(),
            end=r.period_end.date(), uploaded_at=r.uploaded_at) for r in db.query(ReportUpload).all()}
        return {"summary_rows": summary, "daily_rows": daily, "ledger_rows": ledger_rows,
            "ready": bool(summary and daily), "uploads": uploads}
    finally:
        db.close()


def import_inventory_ledger(text, filename=''):
    """Validate and replace Amazon FBA Inventory Ledger Detailed View."""
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')), delimiter='\t')
    if not reader.fieldnames:
        raise ValueError('The Amazon ledger file is empty.')
    required = {'Date', 'FNSKU', 'ASIN', 'MSKU', 'Disposition', 'Starting Warehouse Balance',
                'Receipts', 'Customer Shipments', 'Customer Returns', 'Ending Warehouse Balance'}
    missing = sorted(required - set(reader.fieldnames))
    if missing:
        raise ValueError('Amazon ledger is missing expected column(s): ' + ', '.join(missing))
    lines = []
    for row_number, row in enumerate(reader, 2):
        asin = (row.get('ASIN') or '').strip().upper()
        if not asin:
            continue
        event = None
        # Amazon's ledger specifically uses MM/DD/YYYY. Keep this local so
        # existing UK-style VA and sales uploads retain their date semantics.
        for pattern in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y", "%d %B %Y", "%d %b %Y"):
            try:
                event = datetime.strptime((row.get('Date') or '').strip(), pattern)
                break
            except ValueError:
                pass
        if not event:
            raise ValueError(f'Invalid Date on Amazon ledger row {row_number}.')
        def integer(field):
            try:
                return int(float(str(row.get(field) or '0').replace(',', '').strip()))
            except ValueError:
                raise ValueError(f'Invalid {field} on Amazon ledger row {row_number}.') from None
        lines.append(AmazonInventoryLedgerLine(source_row=row_number, event_date=event,
            fnsku=(row.get('FNSKU') or '').strip(), asin=asin, msku=(row.get('MSKU') or '').strip(),
            title=(row.get('Title') or '').strip(), disposition=(row.get('Disposition') or '').strip(),
            starting_balance=integer('Starting Warehouse Balance'), receipts=integer('Receipts'),
            customer_shipments=integer('Customer Shipments'), customer_returns=integer('Customer Returns'),
            transfers=integer('Warehouse Transfer In/Out'), found=integer('Found'), lost=integer('Lost'),
            damaged=integer('Damaged'), disposed=integer('Disposed'), other_events=integer('Other Events'),
            ending_balance=integer('Ending Warehouse Balance'), unknown_events=integer('Unknown Events'),
            location=(row.get('Location') or '').strip()))
    if not lines:
        raise ValueError('Amazon ledger contains no ASIN rows.')
    start, end = min(line.event_date for line in lines), max(line.event_date for line in lines)
    db = SessionLocal()
    try:
        db.query(AmazonInventoryLedgerLine).delete()
        db.merge(ReportUpload(report_key='amazon_inventory_ledger', filename=(filename or 'Amazon Inventory Ledger').replace('\\', '/').rsplit('/', 1)[-1][:255], row_count=len(lines), period_start=start, period_end=end, uploaded_at=datetime.now(timezone.utc)))
        db.add_all(lines)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return len(lines), start.date(), end.date()
