"""
Fill in missing Unit Cost on a Seller Toolkit (STK) Cost-of-Goods export.

STK only creates a Cost-of-Goods row for a SKU once a shipment for it has
actually gone to Amazon (Tamara, 2026-09-12) -- so this is always a
human-triggered, ad hoc process: she downloads STK's "Update List" export
whenever she wants to top up costs, uploads it here, and re-uploads the
result to STK. There is no STK write API to automate the two upload/
download steps away (confirmed: STK's own docs describe a bulk TSV
upload as the only ingestion path; the one push-integration we found,
Seller Amp -> STK, is a private partnership, not a public API).

Two-tier matching, in order (Tamara, 2026-09-12: "just use the SKU data
this will be accurate" / "get it from the purchasing sheet"):

1. The SKU itself. Every SKU this team creates already encodes its own
   cost -- e.g. "CUR_128.00_239.00_1_16FEB" is Store-prefix_CostPrice_
   SalePrice_Quantity_DateOrdered, assigned at Buy Sheet order time (see
   the real Buy Sheet 'SKU' column, same convention verbatim). A loose,
   unanchored search tolerates prefixes/suffixes some SKUs pick up later
   (e.g. "amzn.gr.AMA_10.38_19.80_3_25JU-qc2R45-VG") -- confirmed against
   this system's live export: 45/45 previously-missing rows resolved this
   way, and the extracted number matched STK's own already-filled
   UNIT_COST_VAT_INCLUSIVE exactly on every one of 139 sampled rows
   already carrying a cost (0 matched UNIT_COST_NET_OF_VAT instead).
2. The Purchasing sheet's own "Buy Sheet" tab, for the rare SKU with no
   parseable cost at all: first an exact SKU match (same 'SKU' column
   that originated the convention above), else the same ASIN's closest
   Date Ordered on or before this row's own PURCHASE_DATE.

Anything neither tier resolves is left blank and reported, never guessed.
"""
import json
import re

from app.database.database import SessionLocal
from app.database.models import StkCogsRun
from app.services.actual_performance_service import _purchase_date

STATUS_COL, TITLE_COL, SKU_COL, ASIN_COL = 0, 1, 2, 3
UNIT_COST_VAT_INC_COL = 5
PURCHASE_DATE_COL = 19

MISSING_STATUS = "CoG Missing"

# Loose on purpose: some SKUs carry an extra prefix ("amzn.gr.") or a
# trailing hash/suffix picked up by a later process. We only need the
# Store_Cost_Price_Qty_Date core to still be findable inside the string.
SKU_COST_PATTERN = re.compile(r'[A-Za-z0-9]+_(\d+\.\d+)_(\d+\.\d+)_(\d+)_')


def _strip_currency(value):
    try:
        return float(str(value).replace('£', '').replace('$', '').replace(',', '').strip())
    except (ValueError, TypeError):
        return None


def _load_buy_sheet_index():
    """asin/sku -> list of (ordered_on, cost_price), for the Buy Sheet fallback."""
    from app.services.google_sheets_client import open_sheet
    from app.services.va_performance_service import PURCHASING_SHEET_URL, _value

    values = open_sheet(PURCHASING_SHEET_URL).worksheet("Buy Sheet").get_all_values()
    if not values:
        return {}, {}
    headers = {}
    for index, header in enumerate(values[0]):
        headers.setdefault(' '.join(header.lower().split()), index)
    rows = [{header: row[index] if index < len(row) else '' for header, index in headers.items()}
            for row in values[1:]]

    by_sku = {}
    by_asin = {}
    for row in rows:
        cost = _strip_currency(_value(row, 'cost price'))
        if cost is None:
            continue
        sku = str(_value(row, 'sku')).strip()
        asin = str(_value(row, 'asin')).strip().upper()
        ordered_on = _purchase_date(str(_value(row, 'date ordered')))
        if sku:
            by_sku.setdefault(sku, cost)
        if asin:
            by_asin.setdefault(asin, []).append((ordered_on, cost))
    for asin, entries in by_asin.items():
        entries.sort(key=lambda e: (e[0] is None, e[0]))
    return by_sku, by_asin


def _buy_sheet_lookup(by_sku, by_asin, sku, asin, purchase_date_str):
    if sku in by_sku:
        return by_sku[sku]
    entries = by_asin.get(asin)
    if not entries:
        return None
    target = _purchase_date(purchase_date_str) if purchase_date_str else None
    on_or_before = [e for e in entries if e[0] and target and e[0] <= target]
    if on_or_before:
        return on_or_before[-1][1]
    # No usable date to compare against -- a single Buy Sheet order for this
    # ASIN is still a reasonable fallback; more than one is genuinely ambiguous.
    if len(entries) == 1:
        return entries[0][1]
    return None


def fill_missing_cogs(raw_text: str) -> dict:
    """
    raw_text: the STK Cost-of-Goods export file's exact content (CRLF, tab
    delimited, STK's own quoting). Returns dict(filled_text, filled_from_sku,
    filled_from_buy_sheet, unresolved: [(asin, sku, title)]).
    """
    lines = raw_text.split("\r\n")
    trailing_blank = bool(lines) and lines[-1] == ""
    if trailing_blank:
        lines = lines[:-1]

    header = lines[0]
    out_lines = [header]

    by_sku = by_asin = None  # lazy: only hit the sheet if something actually needs it

    filled_from_sku = 0
    filled_from_buy_sheet = 0
    unresolved = []

    for line in lines[1:]:
        fields = line.split("\t")
        needs_fill = (len(fields) > UNIT_COST_VAT_INC_COL
                      and fields[STATUS_COL] == MISSING_STATUS
                      and not fields[UNIT_COST_VAT_INC_COL].strip())
        if needs_fill:
            sku_raw = fields[SKU_COL].strip('"')
            m = SKU_COST_PATTERN.search(sku_raw)
            if m:
                fields[UNIT_COST_VAT_INC_COL] = m.group(1)
                filled_from_sku += 1
            else:
                if by_sku is None:
                    by_sku, by_asin = _load_buy_sheet_index()
                asin = fields[ASIN_COL].strip('"').upper()
                purchase_date = fields[PURCHASE_DATE_COL].strip('"') if len(fields) > PURCHASE_DATE_COL else ''
                cost = _buy_sheet_lookup(by_sku, by_asin, sku_raw, asin, purchase_date)
                if cost is not None:
                    fields[UNIT_COST_VAT_INC_COL] = f"{cost:.2f}"
                    filled_from_buy_sheet += 1
                else:
                    unresolved.append((asin, sku_raw, fields[TITLE_COL].strip('"')))
        out_lines.append("\t".join(fields))

    filled_text = "\r\n".join(out_lines) + ("\r\n" if trailing_blank else "")
    return dict(filled_text=filled_text, filled_from_sku=filled_from_sku,
                filled_from_buy_sheet=filled_from_buy_sheet, unresolved=unresolved)


def record_run(filename, filled_from_sku, filled_from_buy_sheet, unresolved):
    with SessionLocal() as db:
        db.add(StkCogsRun(
            filename=filename, filled_from_sku=filled_from_sku,
            filled_from_buy_sheet=filled_from_buy_sheet,
            unresolved_json=json.dumps(unresolved),
        ))
        db.commit()


def latest_run():
    with SessionLocal() as db:
        run = db.query(StkCogsRun).order_by(StkCogsRun.run_at.desc()).first()
        if not run:
            return None
        return dict(
            run_at=run.run_at, filename=run.filename,
            filled_from_sku=run.filled_from_sku, filled_from_buy_sheet=run.filled_from_buy_sheet,
            unresolved=json.loads(run.unresolved_json or "[]"),
        )
