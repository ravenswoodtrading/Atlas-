"""Read VA expectations and allocate uploaded sales without changing either sheet."""
from datetime import date
import math
import time
from app.database.database import SessionLocal
from app.database.models import VaSalesLine, VaSalesImport, ReplenItem, AmazonInventoryLedgerLine, ReportUpload
from collections import defaultdict
from app.services.actual_performance_service import _purchase_date
from app.services.va_sales_allocation import allocate_sales
from app.services.va_actuals_insights import date_selection, report_view

VA_START = date(2026, 4, 1)
PURCHASING_SHEET_URL = 'https://docs.google.com/spreadsheets/d/1WcO8SQ6cQEmoVG-GUmAJwBde1AIgp4aBg9pUBJ62UA0/edit'

# These inputs change occasionally, not on every page view. Short-lived
# process caches keep household use responsive while still picking up new
# sheet edits and ledger uploads without a restart.
_SHEET_CACHE = {}
_SHEET_CACHE_TTL = 300
_LEDGER_CACHE_STAMP = None
_LEDGER_CACHE = {}
_PERFORMANCE_CACHE = {}
_PERFORMANCE_CACHE_TTL = 15


def _value(row, *names):
    fields = {' '.join(k.lower().split()): v for k, v in row.items()}
    return next((fields[n] for n in names if str(fields.get(n, '')).strip()), '')


def _optional_number(value):
    try:
        number = float(str(value).replace('£', '').replace(',', '').strip())
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def purchase_rows(lead_rows, buy_rows):
    result = []
    for index, row in enumerate(lead_rows, 2):
        quantity = _optional_number(_value(row, 'purchased qty', 'purchased quantity'))
        if not quantity or quantity <= 0:
            continue
        submitted = _purchase_date(str(_value(row, 'date', 'date submitted')))
        if submitted and submitted < VA_START:
            continue
        asin = str(_value(row, 'asin', 'amazon asin')).strip().upper()
        purchased = _purchase_date(str(_value(row, 'date ordered', 'purchase date')))
        # Both audit-only columns (va_submission_sync.AUDIT_COLUMNS) -- backfilled
        # after the fact, not at submission time, so absent on older rows and on
        # anything not yet audited. price_drop is normalised to a real Yes/No/None
        # rather than passed through raw: the sheet's own "Price drop (>5%)"
        # formula depends on a now-dead webhook tunnel, so some cells literally
        # contain that request's DNS error string instead of an answer.
        price_drop_raw = str(_value(row, 'price drop (>5%)') or '').strip().lower()
        price_drop = True if price_drop_raw == 'yes' else False if price_drop_raw == 'no' else None
        result.append(dict(id=index, asin=asin, submitted=submitted, purchased_on=purchased,
            quantity=quantity, title=_value(row, 'title', 'product name', 'product'),
            expected_price=_optional_number(_value(row, 'sale price', 'expected sale price')),
            expected_profit=_optional_number(_value(row, 'expected profit')),
            cost=_optional_number(_value(row, 'actual cog', 'cog (unit)')),
            rating=_value(row, 'client rating'), comments=_value(row, 'client notes'),
            va_notes=_value(row, 'va notes'), method=_value(row, 'sourcing method used', 'sourcing method'),
            price_drop=price_drop,
            buy_box_on_lead_date=_optional_number(_value(row, 'buy box on lead date')),
            issue='Invalid purchased quantity' if not quantity.is_integer() else 'Missing ASIN' if not asin else 'Submission date unavailable' if not submitted else ''))
    # Resolve missing purchase dates by treating each ASIN's repeat VA leads
    # and Buy Sheet orders as one FIFO queue (Tamara, 2026-09-10): "If the
    # ASIN appears more than once just attribute to the item that sells
    # first." We don't need to prove which specific write-up caused which
    # specific order -- we need to know what happened to the stock, and
    # va_sales_allocation.allocate_sales already sells FIFO against
    # whatever purchased_on each batch ends up with. So the oldest
    # unresolved submission for an ASIN gets the oldest eligible order,
    # the next-oldest submission gets the next order, and so on.
    unresolved_by_asin = defaultdict(list)
    for batch in result:
        if not batch['purchased_on'] and batch['submitted']:
            unresolved_by_asin[batch['asin']].append(batch)
    for asin, batches in unresolved_by_asin.items():
        # Tie-broken by row id (sheet order) -- submissions on the exact
        # same day have no other signal to order them by, and FIFO still
        # needs *a* stable order to hand out queue positions.
        batches.sort(key=lambda b: (b['submitted'], b['id']))
        candidates = [r for r in buy_rows if str(_value(r, 'asin')).strip().upper() == asin
            and _purchase_date(str(_value(r, 'date ordered')))]
        # The Buy Sheet can contain an identical duplicated row for one order
        # (same date, quantity and SKU). Collapse those export duplicates
        # before queuing them.
        orders = []
        seen_candidates = set()
        for candidate in candidates:
            ordered_on = _purchase_date(str(_value(candidate, 'date ordered')))
            key = (ordered_on, _optional_number(_value(candidate, 'quantity')), str(_value(candidate, 'sku')).strip())
            if key not in seen_candidates:
                seen_candidates.add(key)
                orders.append(ordered_on)
        orders.sort()
        order_idx = 0
        for batch in batches:
            # A submission can never be matched to an order dated before its
            # own submission date (Tamara's caveat: a VA can write up an
            # ASIN that was already purchased earlier, e.g. a replenishment
            # of existing stock) -- otherwise an already-settled older
            # purchase could get silently attributed to a brand-new lead.
            # Submissions are walked oldest-first, so any order skipped here
            # predates every remaining submission too and is never revisited.
            while order_idx < len(orders) and orders[order_idx] < batch['submitted']:
                order_idx += 1
            if order_idx < len(orders):
                batch['purchased_on'] = orders[order_idx]
                order_idx += 1
            else:
                batch['issue'] = batch['issue'] or 'No Buy Sheet purchase order available on or after the submission date'
    return result


def performance_report(period='all', month='', quarter='', start_date='', end_date='', target_days=30):
    cache_key = (period, month, quarter, start_date, end_date, target_days)
    cached_report = _PERFORMANCE_CACHE.get(cache_key)
    if cached_report and time.monotonic() - cached_report[0] < _PERFORMANCE_CACHE_TTL:
        return cached_report[1]
    selection = date_selection(period, month, quarter, start_date, end_date)
    from app.services.google_sheets_client import open_sheet
    from app.services.google_sheets_lead_sync import LEAD_SHEET_URL, LEAD_SHEET_TAB
    def read_rows(url, tab):
        cache_key = (url, tab)
        cached = _SHEET_CACHE.get(cache_key)
        if cached and time.monotonic() - cached[0] < _SHEET_CACHE_TTL:
            return cached[1]
        values = open_sheet(url).worksheet(tab).get_all_values()
        if not values:
            return []
        # Buy Sheet repeats headers in a second export block; use the first block.
        headers = {}
        for index, header in enumerate(values[0]):
            headers.setdefault(' '.join(header.lower().split()), index)
        rows = [{header: row[index] if index < len(row) else '' for header, index in headers.items()}
                for row in values[1:]]
        _SHEET_CACHE[cache_key] = (time.monotonic(), rows)
        return rows
    errors = []
    try:
        leads = read_rows(LEAD_SHEET_URL, LEAD_SHEET_TAB)
    except Exception:
        leads = []
        errors.append('VA sheet unavailable. Purchase totals cannot be calculated; refresh when the connection is available.')
    try:
        buys = read_rows(PURCHASING_SHEET_URL, 'Buy Sheet')
    except Exception:
        buys = []
        errors.append('Buy Sheet unavailable. Purchases without a date on the VA sheet need review.')
    with SessionLocal() as db:
        imported = db.query(VaSalesImport).first()
        start = imported.period_start.date() if imported and imported.period_start else None
        end = imported.period_end.date() if imported and imported.period_end else None
        uploaded = imported.imported_at if imported else None
        sales = [dict(source_row=r.source_row, sku=r.sku, asin=r.asin, date=r.sold_at.date(),
            units=r.units, sales=r.sales, profit=r.profit, cog=r.cog) for r in db.query(VaSalesLine).all()]
        global _LEDGER_CACHE_STAMP, _LEDGER_CACHE
        ledger_upload = db.query(ReportUpload).filter(ReportUpload.report_key == 'amazon_inventory_ledger').first()
        ledger_stamp = ledger_upload.uploaded_at if ledger_upload else None
        if ledger_stamp == _LEDGER_CACHE_STAMP:
            ledger = _LEDGER_CACHE
        else:
            ledger = defaultdict(lambda: dict(returns=0, shipments=0, receipts=0, net_change=0,
                latest_date=None, ending_balance=None))
            for row in db.query(AmazonInventoryLedgerLine).all():
                key = str(row.asin or '').strip().upper()
                if not key:
                    continue
                item = ledger[key]
                item['returns'] += row.customer_returns or 0
                item['shipments'] += row.customer_shipments or 0
                item['receipts'] += row.receipts or 0
                item['net_change'] += sum((row.receipts or 0, row.customer_shipments or 0,
                    row.customer_returns or 0, row.transfers or 0, row.found or 0,
                    row.lost or 0, row.damaged or 0, row.disposed or 0,
                    row.other_events or 0, row.unknown_events or 0))
                if item['latest_date'] is None or row.event_date > item['latest_date']:
                    item['latest_date'] = row.event_date
                    item['ending_balance'] = row.ending_balance
            _LEDGER_CACHE_STAMP, _LEDGER_CACHE = ledger_stamp, ledger
        # Replen imports are user-entered/uploaded, so normalise the ASIN in
        # the same way as the VA and Buy Sheet parsers before matching.
        replen = {str(r.asin or '').strip().upper(): r.id for r in db.query(ReplenItem).all()
            if str(r.asin or '').strip()}
    batches = allocate_sales(purchase_rows(leads, buys), sales, start, end)
    for batch in batches:
        batch_asin = str(batch.get('asin') or '').strip().upper()
        batch['on_replen_checker'] = batch_asin in replen
        batch['replen_url'] = f"/replen#replen-{replen[batch_asin]}" if batch['on_replen_checker'] else '/replen'
        batch['ledger'] = ledger.get(batch_asin)
    buy_evidence = []
    for index, row in enumerate(buys, 2):
        ordered = _purchase_date(str(_value(row, 'date ordered')))
        quantity = _optional_number(_value(row, 'quantity'))
        if ordered and quantity and quantity > 0 and quantity.is_integer():
            buy_evidence.append(dict(row=index, asin=str(_value(row, 'asin')).strip().upper(),
                date=ordered, quantity=quantity, sku=_value(row, 'sku')))
    view = report_view(batches, buy_evidence, selection, end, target_days)
    result = dict(**view, errors=errors, period_start=start, period_end=end, uploaded=uploaded)
    _PERFORMANCE_CACHE[cache_key] = (time.monotonic(), result)
    # Keep the process-local cache bounded if many custom date ranges are used.
    if len(_PERFORMANCE_CACHE) > 32:
        oldest = min(_PERFORMANCE_CACHE, key=lambda key: _PERFORMANCE_CACHE[key][0])
        _PERFORMANCE_CACHE.pop(oldest, None)
    return result
