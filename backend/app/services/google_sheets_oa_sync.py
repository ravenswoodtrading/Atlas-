"""
Google Sheet <-> OaResearchFinding sync for the manual VA OA-sourcing
workflow, 2026-09-05.

Tamara paused the automated AI research worker to instead gather
ground-truth data by hand: she (and her VA) manually Google a daily
list of real "OA / unclear" competitor candidates, fill in what they
found, and this gets read back into the SAME OaResearchFinding table
the shadow-mode AI worker already writes to -- same schema, same hard
economics rule (FeeEngine decides buyability, never a free-text guess),
just a different (human, not AI) source of the evidence.

Writes to a DEDICATED new tab ("Atlas OA Leads") on Tamara's real,
actively-used team sheet -- never touches any of her existing tabs
(Lead Sheet, Team Logger, etc). Column names ("Store", "Sourcing
Method", "VA Notes") deliberately match her existing Lead Sheet's own
vocabulary (confirmed via a read-only check of its real Sourcing Method
values: "A2A", "EUA2A", "Online Arbitrage", "Storefront") so the VA
isn't learning a new format.

THIS MODULE MAKES NO ATLAS PRODUCTION WRITES beyond OaResearchFinding
rows -- no SellerNewListing mutation (sourcing_tag correction stays a
manual follow-up decision, not automatic), no queue/opportunity writes.
"""
from datetime import datetime, timezone
from urllib.parse import quote_plus

from app.database.database import SessionLocal
from app.database.models import ProductRecord, SellerNewListing
from app.services.google_sheets_client import open_sheet
from app.services.fee_engine import FeeEngine
from app.services.oa_research_worker_service import (
    get_shadow_batch, save_finding, _latest_candidate_row, _existing_pipeline_outcome,
    _classify_current_status, _recommend_outcome,
)

TAB_NAME = "Atlas OA Leads"

HEADERS = [
    "Date Added", "ASIN", "Brand", "Product Name", "Amazon Price",
    "Target Source Price", "Google Search",
    "Store", "Source URL", "Source Price", "Sourcing Method", "VA Notes",
    "Status", "Imported",
]


def _google_search_formula(record) -> str:
    # Same plain-web-search, EAN+title query pattern as
    # competitors.py::_google_search_url_variants -- reused here so the
    # VA gets an identical search to what Atlas's own "Find Source"
    # button would build.
    query = f"{record.title or ''} {record.ean or ''}".strip()
    url = f"https://www.google.com/search?q={quote_plus(query)}"
    return f'=HYPERLINK("{url}", "Search")'

SOURCING_METHOD_TO_CLASSIFICATION = {
    "online arbitrage": "OA",
    "a2a": "A2A",
    "eua2a": "A2A",
    "storefront": "A2A",
}


def _get_or_create_tab(sh):
    for ws in sh.worksheets():
        if ws.title == TAB_NAME:
            return ws
    ws = sh.add_worksheet(title=TAB_NAME, rows=200, cols=len(HEADERS))
    ws.append_row(HEADERS)
    return ws


def write_daily_leads(sheet_url: str, limit: int = 10) -> dict:
    """
    Appends up to `limit` new, real "OA / unclear" competitor candidates
    (same ranking-by-opportunity-value, recency-windowed selection the
    shadow AI worker used -- see get_shadow_batch) that aren't already
    on the sheet. Never removes or edits existing rows.
    """
    sh = open_sheet(sheet_url)
    ws = _get_or_create_tab(sh)

    existing_rows = ws.get_all_values()
    header = existing_rows[0] if existing_rows else HEADERS
    asin_col = header.index("ASIN") if "ASIN" in header else 1
    existing_asins = {row[asin_col] for row in existing_rows[1:] if len(row) > asin_col}

    candidates = get_shadow_batch(limit=limit * 3)  # over-fetch, then filter out dupes
    new_rows = []
    for row in candidates:
        asin = row["listing"].asin
        if asin in existing_asins:
            continue
        record = row["record"]
        target_price = FeeEngine.max_source_cost(record.buy_box_now, record.category_name, record.fba_fee, FeeEngine.OA_TARGET_ROI_PCT)
        new_rows.append([
            datetime.now(timezone.utc).date().isoformat(),
            asin, record.brand, record.title, record.buy_box_now,
            round(target_price, 2), _google_search_formula(record),
            "", "", "", "", "", "", "",
        ])
        if len(new_rows) >= limit:
            break

    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")

    return {"tab": TAB_NAME, "rows_added": len(new_rows), "asins_added": [r[1] for r in new_rows]}


def import_completed_leads(sheet_url: str) -> dict:
    """
    Reads every row marked Status="Done" (case-insensitive) that hasn't
    already been imported (Imported column), writes ONE OaResearchFinding
    row per it, then marks Imported="Yes" on the sheet so a re-run never
    double-imports. A row with a Source Price gets run through the SAME
    FeeEngine-based economics/hard-rule logic the AI worker uses --
    manual evidence is never exempt from that rule.
    """
    sh = open_sheet(sheet_url)
    ws = _get_or_create_tab(sh)
    rows = ws.get_all_values()
    if not rows:
        return {"imported": 0, "asins": []}

    header = rows[0]
    idx = {name: header.index(name) for name in HEADERS if name in header}

    db = SessionLocal()
    try:
        imported = []
        for i, row in enumerate(rows[1:], start=2):  # sheet rows are 1-indexed, header is row 1
            def cell(name):
                col = idx.get(name)
                return row[col].strip() if col is not None and col < len(row) else ""

            status = cell("Status").lower()
            already_imported = cell("Imported").lower() == "yes"
            if status != "done" or already_imported:
                continue

            asin = cell("ASIN")
            record = db.query(ProductRecord).filter(ProductRecord.asin == asin).order_by(ProductRecord.scanned_at.desc()).first()
            listing = db.query(SellerNewListing).filter(SellerNewListing.asin == asin).order_by(SellerNewListing.detected_at.desc()).first()
            if record is None:
                continue

            store = cell("Store")
            source_url = cell("Source URL")
            source_price_raw = cell("Source Price").replace("£", "").replace(",", "").strip()
            source_price = float(source_price_raw) if source_price_raw else None
            sourcing_method = cell("Sourcing Method")
            notes = cell("VA Notes")

            classification = SOURCING_METHOD_TO_CLASSIFICATION.get(sourcing_method.lower(), "UNKNOWN")
            current_status = _classify_current_status(record, source_price, "in_stock" if source_price else None)
            recommended_outcome, profit, roi_pct = _recommend_outcome(record, current_status, source_price)
            if source_price is None:
                recommended_outcome = "NO_USEFUL_SOURCE"

            candidate_row = _latest_candidate_row(db, asin)
            finding_id = save_finding({
                "seller_new_listing_id": listing.id if listing else None,
                "asin": asin,
                "result_state": "VERIFIED_CURRENT" if source_price else "NO_MATCH",
                "current_retailer": store, "current_url": source_url,
                "current_price_gbp": source_price, "current_stock_text": "",
                "current_checked_at": datetime.now(timezone.utc) if source_price else None,
                "historical_retailer": "", "historical_price_gbp": None,
                "historical_observed_note": "", "historical_checked_at": None,
                "current_source_status": current_status,
                "historical_source_status": "NOT_FOUND",
                "sourcing_classification": classification,
                "source_confidence": "HIGH",  # human-verified, per Tamara's own manual check
                "atlas_inference": f"Manually sourced by VA/Tamara via Google Sheet -- {notes}" if notes else "Manually sourced by VA/Tamara via Google Sheet.",
                "recommended_outcome": recommended_outcome,
                "estimated_profit_gbp": profit, "estimated_roi_pct": roi_pct,
                "existing_pipeline_outcome": _existing_pipeline_outcome(candidate_row),
                "queries_used_json": "[]",
                "reasoning_json": "{}",
                "cost_usd": 0.0,
                "human_verdict": "GENUINE_OPPORTUNITY" if recommended_outcome in ("BUY_NOW", "BORDERLINE") else None,
                "human_notes": notes, "reviewed_at": datetime.now(timezone.utc),
            })
            imported.append({"asin": asin, "finding_id": finding_id, "row": i})

        if imported:
            imported_col = idx.get("Imported")
            if imported_col is not None:
                for item in imported:
                    ws.update_cell(item["row"], imported_col + 1, "Yes")
    finally:
        db.close()

    return {"imported": len(imported), "asins": [x["asin"] for x in imported]}
