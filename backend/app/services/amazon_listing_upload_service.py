"""
Automatic Amazon listing upload (2026-09-12) -- replaces the manual step
Tamara's team did for every Buy Sheet row marked "Listing Uploader (Y)":
download the Listing Uploader/Amazon uploader tab as a file, upload it to
Seller Central, then by hand clear the Y flag and mark "In Amazon
Inventory". Every field that flows into that upload already has a
formula-verified source (traced live, 2026-09-12): SKU/ASIN/price come
from Buy Sheet columns 'SKU'/'ASIN'/'Sale Price' (price x1.2, matching the
sheet's own FILTER formula), condition/fulfillment-channel/tax-code/
batteries are fixed constants the sheet already hardcodes, and Country of
Origin defaults to Germany (Tamara, 2026-09-12: "just put germany for the
time being and we can change in the Amazon inventory at a later date" --
the sheet itself has no formula source for this field either). The one
piece of information genuinely missing from the sheet is Amazon's own
productType classification, fetched per-ASIN via the Catalog Items API
(SPAPIClient.get_product_types) since putListingsItem requires it and it
isn't derivable from anything on the sheet.

This module owns the sheet reading, attribute construction and Buy Sheet
write-back; SPAPIClient.put_listing_item owns only the HTTP call.
"""
import time

from app.database.database import SessionLocal
from app.database.models import AmazonListingUpload
from app.services.google_sheets_client import open_sheet
from app.services.va_performance_service import PURCHASING_SHEET_URL, _optional_number
from app.sp_api.client import MARKETPLACE_IDS, SPAPIClient, get_sp_api_client

BUY_SHEET_TAB = "Buy Sheet"
TRIGGER_COLUMN = "Listing Uploader (Y)"
DONE_COLUMN = "In Amazon Inventory"
SKU_COLUMN = "SKU"
ASIN_COLUMN = "ASIN"
PRICE_COLUMN = "Sale Price"

# Matches 'Listing Uploader' tab's own FILTER formula: price submitted to
# Amazon is Buy Sheet's Sale Price x1.2, never the bare Sale Price.
PRICE_MARKUP = 1.2

# Country of Origin has no Buy Sheet source at all (typed directly into
# Listing Uploader today) -- Tamara's explicit standing default ("just put
# germany for the time being"), correctable per-listing in Seller Central
# afterwards. Must be an ISO 3166-1 alpha-2 code, NOT the full country
# name -- confirmed live (2026-09-12) via a real submission: Amazon
# rejected "Germany" outright ("We can't accept the Germany you entered
# for Country of Origin... select an approved value from the list"), then
# confirmed against BREAST_PUMP's own live product-type schema (Product
# Type Definitions API) that the enum is alpha-2 codes ("DE" present).
# This is a shared Amazon attribute definition reused across virtually
# every product type schema, not something specific to BREAST_PUMP, but
# not independently verified against a second product type either.
DEFAULT_COUNTRY_OF_ORIGIN = "DE"

MARKETPLACE = "UK"


def _find_column(header, name):
    for i, h in enumerate(header):
        if h.strip() == name:
            return i
    raise ValueError(f"Buy Sheet column not found: {name!r}")


def pending_rows():
    """
    [{row: 1-indexed sheet row, asin, sku, price}] for every Buy Sheet
    row currently marked TRIGGER_COLUMN="Y" -- no de-duplication by SKU:
    the Buy Sheet is already known to sometimes carry an identical
    duplicated row for one order (va_performance_service.purchase_rows'
    own docstring), and each physical row needs its own Y flag cleared
    and Done column set regardless of whether another row shares its
    SKU -- Amazon's PUT is idempotent, so resubmitting the same SKU
    costs nothing beyond one extra API call.
    """
    ws = open_sheet(PURCHASING_SHEET_URL).worksheet(BUY_SHEET_TAB)
    values = ws.get_all_values()
    if not values:
        return []
    header = values[0]
    trigger_idx = _find_column(header, TRIGGER_COLUMN)
    sku_idx = _find_column(header, SKU_COLUMN)
    asin_idx = _find_column(header, ASIN_COLUMN)
    price_idx = _find_column(header, PRICE_COLUMN)

    rows = []
    for offset, row in enumerate(values[1:]):
        if len(row) <= trigger_idx or row[trigger_idx].strip().upper() != "Y":
            continue
        sku = row[sku_idx].strip() if len(row) > sku_idx else ""
        asin = row[asin_idx].strip().upper() if len(row) > asin_idx else ""
        price = _optional_number(row[price_idx]) if len(row) > price_idx else None
        if not sku or not asin or not price:
            continue
        rows.append(dict(row=offset + 2, asin=asin, sku=sku, price=round(price * PRICE_MARKUP, 2)))
    return rows


def build_attributes(asin, price, marketplace_id):
    """The fixed attribute set every Buy-Sheet-driven listing sends --
    matches exactly what Listing Uploader/Amazon uploader already
    populate today (traced live, 2026-09-12): merchant-suggested ASIN,
    New condition, FBA fulfilment, offer price, and Country of Origin."""
    return {
        "merchant_suggested_asin": [{"value": asin, "marketplace_id": marketplace_id}],
        "condition_type": [{"value": "new_new", "marketplace_id": marketplace_id}],
        "fulfillment_availability": [{"fulfillment_channel_code": "AMAZON_EU", "marketplace_id": marketplace_id}],
        "purchasable_offer": [{
            "marketplace_id": marketplace_id, "currency": "GBP",
            "our_price": [{"schedule": [{"value_with_tax": price}]}],
        }],
        "country_of_origin": [{"value": DEFAULT_COUNTRY_OF_ORIGIN, "marketplace_id": marketplace_id}],
    }


def _mark_done(ws, row_number, header):
    trigger_idx = _find_column(header, TRIGGER_COLUMN)
    done_idx = _find_column(header, DONE_COLUMN)
    ws.update_cell(row_number, trigger_idx + 1, "")
    ws.update_cell(row_number, done_idx + 1, "Y")


PRODUCT_TYPE_LOOKUP_RETRIES = 3
PRODUCT_TYPE_LOOKUP_RETRY_DELAY_SECONDS = 3.0


def _get_product_types_chunked(client, asins):
    """
    get_product_types caps at SPAPIClient.CATALOG_ITEMS_BATCH_SIZE (20)
    per call and silently truncates anything beyond that -- chunk here,
    same division of labour its own docstring already assigns callers.

    Real incident, 2026-09-12: the live first batch run treated 20 rows
    (6 distinct ASINs) as permanently unresolvable because one lookup
    call came back without them -- retesting those exact ASINs
    immediately after found every single one resolved cleanly. That
    means a first-attempt gap here is a transient hiccup (likely catalog
    cache propagation, possibly a rate-limit edge get_product_types'
    own MAX_RETRIES didn't fully absorb), NOT a genuine "Amazon hasn't
    classified this ASIN" case -- treating it as permanent would wrongly
    strand a meaningful fraction of every batch in manual-only limbo
    ("If we can't get all products to load automatically this whole
    thing doesn't really help us", Tamara). So: retry only the ASINs
    still missing after each pass, not the whole batch, up to
    PRODUCT_TYPE_LOOKUP_RETRIES times, with a real delay between passes
    to let any transient cause actually clear.
    """
    results = {}
    size = SPAPIClient.CATALOG_ITEMS_BATCH_SIZE
    remaining = list(asins)

    for attempt in range(PRODUCT_TYPE_LOOKUP_RETRIES):
        if not remaining:
            break
        if attempt:
            time.sleep(PRODUCT_TYPE_LOOKUP_RETRY_DELAY_SECONDS)
        still_missing = []
        for i in range(0, len(remaining), size):
            chunk_asins = remaining[i:i + size]
            chunk = client.get_product_types(chunk_asins, MARKETPLACE)
            chunk = chunk or {}
            results.update(chunk)
            still_missing.extend(a for a in chunk_asins if a not in chunk)
        remaining = still_missing

    return results


def _record(sku, asin, price, row, status, error):
    with SessionLocal() as db:
        db.add(AmazonListingUpload(sku=sku, asin=asin, price=price, buy_sheet_row=row, status=status, error=error))
        db.commit()


def run_pending_uploads(preview=False):
    """
    Submits every pending row to Amazon and updates the Buy Sheet on
    success. preview=True builds and returns what WOULD be submitted for
    every pending row without calling Amazon or touching the sheet --
    used for the first live-format verification pass before trusting the
    automatic scheduler with a real backlog.

    Returns dict(succeeded, failed, results: [{row, sku, asin, status,
    error, attributes (preview only)}]).
    """
    rows = pending_rows()
    if not rows:
        return dict(succeeded=0, failed=0, results=[])

    marketplace_id = MARKETPLACE_IDS[MARKETPLACE]
    results = []

    if preview:
        client = get_sp_api_client()
        asins = list({r["asin"] for r in rows})
        product_types = _get_product_types_chunked(client, asins) if client else {}
        for r in rows:
            results.append(dict(
                row=r["row"], sku=r["sku"], asin=r["asin"], price=r["price"],
                product_type=product_types.get(r["asin"]),
                attributes=build_attributes(r["asin"], r["price"], marketplace_id),
            ))
        return dict(succeeded=0, failed=0, results=results, preview=True)

    client = get_sp_api_client()
    if client is None:
        return dict(succeeded=0, failed=len(rows), results=[
            dict(row=r["row"], sku=r["sku"], asin=r["asin"], status="failed",
                 error="SP-API not configured") for r in rows
        ])

    asins = list({r["asin"] for r in rows})
    product_types = _get_product_types_chunked(client, asins)

    ws = open_sheet(PURCHASING_SHEET_URL).worksheet(BUY_SHEET_TAB)
    header = ws.row_values(1)

    succeeded = failed = 0
    for r in rows:
        product_type = product_types.get(r["asin"])
        if not product_type:
            error = f"No Amazon product type found for ASIN {r['asin']}"
            _record(r["sku"], r["asin"], r["price"], r["row"], "failed", error)
            results.append(dict(row=r["row"], sku=r["sku"], asin=r["asin"], status="failed", error=error))
            failed += 1
            continue

        attributes = build_attributes(r["asin"], r["price"], marketplace_id)
        outcome = client.put_listing_item(r["sku"], product_type, attributes, MARKETPLACE)

        if outcome["success"]:
            _mark_done(ws, r["row"], header)
            _record(r["sku"], r["asin"], r["price"], r["row"], "succeeded", "")
            results.append(dict(row=r["row"], sku=r["sku"], asin=r["asin"], status="succeeded", error=""))
            succeeded += 1
        else:
            _record(r["sku"], r["asin"], r["price"], r["row"], "failed", outcome["error"])
            results.append(dict(row=r["row"], sku=r["sku"], asin=r["asin"], status="failed", error=outcome["error"]))
            failed += 1

    return dict(succeeded=succeeded, failed=failed, results=results)


def recent_failures(limit=20):
    with SessionLocal() as db:
        rows = (db.query(AmazonListingUpload)
                .filter(AmazonListingUpload.status == "failed")
                .order_by(AmazonListingUpload.submitted_at.desc())
                .limit(limit).all())
        # A later SUCCEEDED attempt for the same SKU supersedes an
        # earlier failure -- only surface a SKU still unresolved.
        succeeded_skus = {r[0] for r in db.query(AmazonListingUpload.sku)
                           .filter(AmazonListingUpload.status == "succeeded").all()}
        return [r for r in rows if r.sku not in succeeded_skus]
