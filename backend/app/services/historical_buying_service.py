"""
Historical Buying Intelligence (Phase 4C analysis / Phase 4D build,
2026-09-04) -- ground-truth evidence of what the team has ACTUALLY
bought and sold before, extracted from the team's own purchasing
workbook (the "Buy Sheet"), as a NEW evidence layer alongside our own
Keepa-derived scan history and competitor intelligence -- never a
replacement for either.

Why this exists: the Phase 4C read-only analysis of a real purchasing
workbook (1,153 dated rows, Feb-Sep 2026) found that 89 of 102 real EU
A2A buy-sheet brands were already visible to Discovery Intelligence in
some form, but 80 of those 89 (90%) showed ZERO confirmed BUY under the
current Opportunity Lens despite documented real recorded profit --
i.e. real buying history was completely outside Atlas's intelligence
loop. This module is the read-only integration layer that brief asked
for (Phase 4D Stage 1): make that history queryable per brand/ASIN,
alongside the existing evidence layers, WITHOUT touching scoring,
Scan Queue selection, or adding anything to the queue automatically.

Data quality note carried through every function here: the workbook's
own ROI column is literally named "Potential ROI", not a confirmed/
realized outcome -- every function below keeps that name rather than
calling it "roi", so nothing downstream mistakes a purchase-time
estimate for a verified result.

EU A2A classification is ALWAYS derived from an explicit Store match
(Amazon.de/.fr/.es/.it) -- see HistoricalPurchase.is_eu_a2a and
import_workbook's own docstring. Sourcing Method is preserved
separately and is NEVER used to infer A2A -- a brand sourced via
"Replen" or "Arbisource" from Amazon.de is still EU A2A; a brand
sourced "Manual" from Boots is not, regardless of method.

NOTHING in this file calls Keepa/SP-API/SerpApi/Brave, writes to the
Scan Queue, or changes scoring. The only write path is
import_workbook(), a manual/human-triggered one-off (see
import_buy_sheet.py at the repo root) -- never called from a
scheduler.
"""
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, median
from uuid import uuid4

import pandas as pd

from app.database.database import SessionLocal
from app.database.models import HistoricalPurchase

# Explicit Store values that count as EU A2A -- deliberately a closed,
# exact-match set (not a substring/fuzzy match) so a store like
# "Amazon.de Marketplace" or a typo doesn't silently slip in or out.
# Extend this set by hand if a genuinely new EU A2A store value shows
# up in a future workbook -- never inferred.
EU_A2A_STORES = {"amazon.de", "amazon.fr", "amazon.es", "amazon.it"}

# Buy Sheet's own column names, exact (including the workbook's own
# trailing space on "Sourcing Method " -- not a typo here, matches the
# source file so a copy/paste of a real column name from the workbook
# still works).
REQUIRED_COLUMNS = (
    "Date Ordered", "ASIN", "Product Name", "Cost Price", "Sale Price",
    "Potential ROI", "Profit (Per Unit)", "Quantity", "Profit (Total)",
    "Category", "Brand", "Store", "Sourcing Method ",
)


def _normalize_brand(value) -> str:
    return str(value).strip().lower() if value is not None and str(value).strip().lower() != "nan" else ""


def import_workbook(path: str, sheet_name: str = "Buy Sheet") -> dict:
    """
    Manual, human-triggered one-off import (see import_buy_sheet.py) --
    NEVER called by a scheduler or any live request handler. Reads the
    given workbook's Buy Sheet, classifies EU A2A rows by Store only,
    and REPLACES the entire historical_purchases table with this
    import's rows (not an incremental merge) -- this is meant to be a
    fresh full extract from the latest workbook each time it's run, so
    stale rows from a superseded export never linger. Existing
    Atlas data (ProductRecord, ScanQueueItem, etc.) is never touched.

    Returns a summary dict with row counts and any data-quality issues
    found (missing ASIN/brand/date, unparseable numeric fields) so a
    human can judge the import before trusting it -- never silently
    drops a row without accounting for it somewhere in this summary.
    """
    df = pd.read_excel(path, sheet_name=sheet_name, header=0)

    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        return {
            "error": f"Workbook is missing expected column(s): {missing_cols}. "
                     f"Import aborted -- no existing data was touched.",
        }

    total_rows_in_sheet = len(df)

    df["Date Ordered"] = pd.to_datetime(df["Date Ordered"], errors="coerce")
    dateless = int(df["Date Ordered"].isna().sum())
    df = df[df["Date Ordered"].notna()].copy()

    for col in ("Cost Price", "Sale Price", "Potential ROI", "Profit (Per Unit)", "Quantity", "Profit (Total)"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["store_norm"] = df["Store"].astype(str).str.strip().str.lower()
    df["is_eu_a2a"] = df["store_norm"].isin(EU_A2A_STORES)

    missing_asin = int(df["ASIN"].isna().sum())
    missing_brand = int(df["Brand"].isna().sum() + (df["Brand"].astype(str).str.strip() == "").sum())

    batch_id = f"import_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:8]}"

    rows = []
    for _, r in df.iterrows():
        brand_raw = "" if pd.isna(r["Brand"]) else str(r["Brand"]).strip()
        asin = "" if pd.isna(r["ASIN"]) else str(r["ASIN"]).strip()
        rows.append(HistoricalPurchase(
            date_ordered=r["Date Ordered"].to_pydatetime() if pd.notna(r["Date Ordered"]) else None,
            asin=asin,
            product_name="" if pd.isna(r["Product Name"]) else str(r["Product Name"]).strip(),
            brand_raw=brand_raw,
            brand=_normalize_brand(brand_raw),
            category="" if pd.isna(r["Category"]) else str(r["Category"]).strip(),
            store="" if pd.isna(r["Store"]) else str(r["Store"]).strip(),
            is_eu_a2a=bool(r["is_eu_a2a"]),
            sourcing_method="" if pd.isna(r["Sourcing Method "]) else str(r["Sourcing Method "]).strip(),
            cost_price=float(r["Cost Price"]) if pd.notna(r["Cost Price"]) else 0.0,
            sale_price=float(r["Sale Price"]) if pd.notna(r["Sale Price"]) else 0.0,
            potential_roi=float(r["Potential ROI"]) if pd.notna(r["Potential ROI"]) else 0.0,
            profit_per_unit=float(r["Profit (Per Unit)"]) if pd.notna(r["Profit (Per Unit)"]) else 0.0,
            quantity=float(r["Quantity"]) if pd.notna(r["Quantity"]) else 0.0,
            profit_total=float(r["Profit (Total)"]) if pd.notna(r["Profit (Total)"]) else 0.0,
            import_batch_id=batch_id,
        ))

    # Computed from the plain Python objects BEFORE they're handed to
    # the session -- reading these same attributes AFTER commit() would
    # raise DetachedInstanceError once the session expires them and
    # closes (SQLAlchemy's normal expire-on-commit behaviour), and none
    # of this needs a DB round-trip anyway since we already know it.
    eu_rows = [r for r in rows if r.is_eu_a2a]
    eu_brands = {r.brand for r in eu_rows if r.brand}
    eu_asins = {r.asin for r in eu_rows if r.asin}
    eu_total_profit = round(sum(r.profit_total for r in eu_rows), 2)

    db = SessionLocal()
    try:
        previous_count = db.query(HistoricalPurchase).count()
        db.query(HistoricalPurchase).delete()
        db.add_all(rows)
        db.commit()
    finally:
        db.close()

    return {
        "batch_id": batch_id,
        "total_rows_in_sheet": total_rows_in_sheet,
        "rows_imported": len(rows),
        "rows_dropped_no_date": dateless,
        "rows_missing_asin": missing_asin,
        "rows_missing_brand": missing_brand,
        "eu_a2a_rows": len(eu_rows),
        "eu_a2a_distinct_brands": len(eu_brands),
        "eu_a2a_distinct_asins": len(eu_asins),
        "eu_a2a_total_profit": eu_total_profit,
        "previous_rows_replaced": previous_count,
    }


def get_brand_history(eu_only: bool = True) -> dict:
    """
    Read-only. {normalized_brand: {
        "purchase_count", "distinct_asins", "units", "total_profit",
        "avg_potential_roi", "median_potential_roi", "eu_markets"
        (sorted list of Store values actually used, only meaningful
        when eu_only=True), "most_recent" (datetime), "earliest",
        "sourcing_methods" ({method: count}), "category" (most common
        non-empty value seen), "asins" (sorted list),
    }}. eu_only=True (the default) restricts to is_eu_a2a rows -- the
    population Phase 4C's analysis and Scan Intelligence both care
    about; pass False to see a brand's FULL purchasing history
    (including UK OA/replen/etc) for context.
    """
    db = SessionLocal()
    try:
        query = db.query(HistoricalPurchase)
        if eu_only:
            query = query.filter(HistoricalPurchase.is_eu_a2a == True)
        rows = query.all()
    finally:
        db.close()

    by_brand = defaultdict(list)
    for r in rows:
        if r.brand:
            by_brand[r.brand].append(r)

    result = {}
    for brand, recs in by_brand.items():
        rois = [r.potential_roi for r in recs if r.potential_roi is not None]
        category_votes = defaultdict(int)
        for r in recs:
            if r.category:
                category_votes[r.category] += 1
        sourcing_methods = defaultdict(int)
        for r in recs:
            method = r.sourcing_method or "(unspecified)"
            sourcing_methods[method] += 1

        result[brand] = {
            "purchase_count": len(recs),
            "distinct_asins": len({r.asin for r in recs if r.asin}),
            "units": sum(r.quantity or 0 for r in recs),
            "total_profit": round(sum(r.profit_total or 0 for r in recs), 2),
            "avg_potential_roi_pct": round(mean(rois) * 100, 1) if rois else None,
            "median_potential_roi_pct": round(median(rois) * 100, 1) if rois else None,
            "eu_markets": sorted({r.store.strip().lower() for r in recs if r.store}),
            "most_recent": max((r.date_ordered for r in recs if r.date_ordered), default=None),
            "earliest": min((r.date_ordered for r in recs if r.date_ordered), default=None),
            "category": max(category_votes, key=category_votes.get) if category_votes else "",
            "sourcing_methods": dict(sourcing_methods),
            "asins": sorted({r.asin for r in recs if r.asin}),
        }

    return result


def get_asin_history(asin: str) -> list:
    """
    Read-only. Every HistoricalPurchase row for one ASIN (any store,
    not EU-only -- an ASIN-level lookup is meant to answer "have we
    ever bought this exact product before, from anywhere", which is a
    different question from the brand-level EU A2A rollup above),
    newest first.
    """
    db = SessionLocal()
    try:
        return (
            db.query(HistoricalPurchase)
            .filter(HistoricalPurchase.asin == asin)
            .order_by(HistoricalPurchase.date_ordered.desc())
            .all()
        )
    finally:
        db.close()


# Discovery-state classification (Phase 4D) -- the three buckets the
# approved brief asked for. Pure function, no I/O: takes one brand's
# buying-history dict (from get_brand_history, or None if the brand
# has no EU A2A purchase history at all) plus that SAME brand's
# existing DiscoveryIntelligenceService evidence dicts (own/competitor,
# from score_target's own `evidence` dict, or None), and returns which
# bucket it belongs in. Mirrors exactly the classification the Phase 4C
# analysis used by hand, so the numbers in that report and this
# function agree.
STATE_PROVEN_ACTIVE = "proven_active"
STATE_PROVEN_QUIET = "proven_quiet"
STATE_PROVEN_INVISIBLE = "proven_invisible"
STATE_NO_HISTORY = "no_history"


def classify_discovery_state(buying_history: dict | None, own_evidence: dict | None,
                              competitor_evidence: dict | None) -> str:
    """
    - STATE_NO_HISTORY: no EU A2A buying history for this brand at all
      -- Historical Buying Intelligence has nothing to say about it.
    - STATE_PROVEN_INVISIBLE: real buying history exists, but Atlas has
      NEITHER own-scan evidence NOR competitor evidence for this brand
      -- completely outside the current intelligence loop (the 13-brand
      bucket from the Phase 4C report).
    - STATE_PROVEN_ACTIVE: real buying history exists AND current
      evidence agrees it's live right now (a confirmed BUY, recent or
      historical, OR recent competitor activity).
    - STATE_PROVEN_QUIET: real buying history exists, Atlas has SOME
      evidence (own scans and/or competitor sightings), but none of it
      currently clears the "active" bar above -- proven before, quiet
      right now (the 80-brand bucket from the Phase 4C report -- by far
      the largest).
    """
    if not buying_history:
        return STATE_NO_HISTORY

    if not own_evidence and not competitor_evidence:
        return STATE_PROVEN_INVISIBLE

    has_confirmed_buy = bool(own_evidence) and (
        own_evidence.get("eu_confirmed_buy_count_recent", 0) > 0
        or own_evidence.get("eu_confirmed_buy_count_all_time", 0) > 0
    )
    has_recent_competitor = bool(competitor_evidence) and competitor_evidence.get("recent", False)

    if has_confirmed_buy or has_recent_competitor:
        return STATE_PROVEN_ACTIVE

    return STATE_PROVEN_QUIET
