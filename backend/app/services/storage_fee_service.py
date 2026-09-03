"""
Combines SP-API's monthly storage-fee report, long-term/aged-inventory
surcharge report, current FBA stock, and recent sell-through into one
ranked view -- Storage Fee Watch (2026-09-02, Tamara: "identify which
items in my inventory are contributing lots to my storage fees" AND
"identify products that are getting close to long term storage or that
we should really try and shift").

Unlike InventoryCleanupService this is read-only reporting -- no
review/approve/delete workflow (see StorageFeeWatch's own docstring in
app/database/models.py), just a ranked table a human reads and acts on
manually in Seller Central. refresh() is the nightly sweep (see
app/main.py's _storage_fee_scheduler) and is also safe to trigger
manually from the page's "Refresh now" button -- same convention as
InventoryCleanupService.detect_out_of_stock_candidates.
"""
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import StorageFeeWatch
from app.services.activity_log import ActivityLog
from app.sp_api.client import get_sp_api_client

# How far back get_fba_fulfilled_shipments_units looks for "has this SKU
# sold RECENTLY". Originally 90 -- but a live test (2026-09-02, see that
# method's own docstring) confirmed the report's real request window
# tops out around 30 days; 90 always ends the report FATAL, so this had
# never actually worked. 30 is the confirmed-working ceiling, not a
# deliberate design choice -- if Amazon's real limit turns out to be
# looser than today's test showed, this can safely go back up.
SELL_THROUGH_LOOKBACK_DAYS = 30

# A SKU's current-month storage fee counts as "high relative to recent
# sales" once it's at least this many times its recent units shipped (in
# the account's own currency, never converted). A SKU with ANY storage
# fee at all and zero recent sales always qualifies regardless of this
# ratio (see _should_consider_shifting) -- the ratio only matters for
# slow-but-nonzero sellers. Configurable here rather than buried inline,
# same convention as MIN_DAYS_OUT_OF_STOCK in inventory_cleanup_service.py.
HIGH_FEE_PER_UNIT_SOLD_THRESHOLD = 2.0

DEFAULT_MARKETPLACE = "UK"


def _should_consider_shifting(storage_fee_amount: float, recent_units_shipped: int) -> bool:
    if storage_fee_amount <= 0:
        return False
    if recent_units_shipped <= 0:
        return True
    return (storage_fee_amount / recent_units_shipped) >= HIGH_FEE_PER_UNIT_SOLD_THRESHOLD


class StorageFeeService:

    @staticmethod
    def refresh(marketplace: str = DEFAULT_MARKETPLACE) -> dict:
        """
        One full sweep: pulls the current-month storage-fee report, the
        long-term/aged-inventory surcharge report, current FBA stock
        (for sku/asin/title -- see get_storage_fee_charges' own
        docstring for why that report alone can't provide SKU), and
        recent sell-through, joins them all by SKU, then replaces
        storage_fee_watch's contents wholesale. Returns a summary dict
        for ActivityLog, same convention as every other Atlas sweep.

        Safe to call with no SP-API configured -- get_sp_api_client()
        returns None (never raises), same convention as
        InventoryCleanupService.detect_out_of_stock_candidates.
        """
        sp_client = get_sp_api_client()
        if not sp_client:
            return {
                "checked": 0, "flagged_surcharge": 0, "flagged_shift": 0,
                "message": "SP-API not configured -- nothing checked. Set SP_API_CLIENT_ID/"
                           "SECRET/REFRESH_TOKEN in .env.",
            }

        inventory = sp_client.get_inventory_summaries(marketplace)
        if inventory is None:
            return {
                "checked": 0, "flagged_surcharge": 0, "flagged_shift": 0,
                "message": "getInventorySummaries call failed -- nothing checked this sweep "
                           "(check server logs for the real HTTP error).",
            }

        # Both storage-fee reports are treated as independently optional
        # (degrade to {} rather than bail the whole sweep) -- unlike Out
        # of Stock Cleanup's two checks, which are both hard-required
        # because a delete decision needs BOTH to be safe, this is
        # read-only reporting with no delete action (see StorageFeeWatch's
        # own docstring), so partial data is still useful, not unsafe.
        # This matters in practice: live testing (2026-09-02, see
        # get_storage_fee_charges' own docstring) found the two reports
        # can fail completely independently of each other -- one hit
        # Amazon's once-per-day-per-seller quota while the other
        # succeeded cleanly on the same call.
        storage_fees = sp_client.get_storage_fee_charges(marketplace)
        if storage_fees is None:
            print(
                "Storage fee report failed this sweep -- proceeding without current-month fee "
                "data (check server logs above for the real HTTP error, or see "
                "/debug/sp-api/storage-fee-check)."
            )
            storage_fees = {}

        ltsf = sp_client.get_longterm_storage_fee_charges(marketplace)
        if ltsf is None:
            print(
                "Long-term storage fee report failed this sweep -- proceeding without "
                "aged-inventory surcharge data (check server logs above for the real HTTP "
                "error, or see /debug/sp-api/storage-fee-check)."
            )
            ltsf = {}

        if not storage_fees and not ltsf:
            return {
                "checked": 0, "flagged_surcharge": 0, "flagged_shift": 0,
                "message": "Both storage fee reports failed -- nothing checked this sweep "
                           "(check server logs for the real HTTP error -- may be a missing "
                           "SP-API role or a same-day quota hit, see "
                           "/debug/sp-api/storage-fee-check).",
            }

        # Sell-through is a nice-to-have ranking signal here, not a hard
        # requirement the way it is for Out of Stock Cleanup's delete
        # safety net -- a failed report degrades the should-shift flag
        # to "unknown recent sales" rather than blocking the whole
        # sweep, since the storage fee numbers are still real and worth
        # showing even without it.
        sales = sp_client.get_fba_fulfilled_shipments_units(marketplace, SELL_THROUGH_LOOKBACK_DAYS)
        if sales is None:
            sales = {}

        # get_storage_fee_charges is keyed by ASIN (that report has no
        # seller-SKU column) -- join it back to SKU via inventory's own
        # asin field before combining everything below.
        asin_to_sku = {inv["asin"]: sku for sku, inv in inventory.items() if inv.get("asin")}

        combined: dict[str, dict] = {}
        for sku, inv in inventory.items():
            combined.setdefault(sku, {})["inv"] = inv
        for asin, fee in storage_fees.items():
            sku = asin_to_sku.get(asin)
            if sku:
                combined.setdefault(sku, {})["fee"] = fee
        for sku, lt in ltsf.items():
            combined.setdefault(sku, {})["lt"] = lt
        for sku, sale in sales.items():
            if sku in combined:
                combined[sku]["sale"] = sale

        db = SessionLocal()
        now = datetime.now(timezone.utc)
        flagged_surcharge = 0
        flagged_shift = 0
        rows_written = 0

        try:
            db.query(StorageFeeWatch).delete()

            for sku, parts in combined.items():
                inv = parts.get("inv", {})
                fee = parts.get("fee", {})
                lt = parts.get("lt", {})
                sale = parts.get("sale", {})

                # A SKU that only showed up via inventory (in stock, no
                # storage fee at all, never surcharged) has nothing
                # worth ranking here -- Storage Fee Watch is about fees,
                # not a general inventory list (that's already Replen/
                # Out of Stock Cleanup's job).
                if not fee and not lt:
                    continue

                recent_units = sale.get("units_shipped", 0)
                storage_fee_amount = fee.get("storage_fee_amount", 0.0)
                has_surcharge = bool(lt.get("amount_charged"))
                should_shift = _should_consider_shifting(storage_fee_amount, recent_units)

                row = StorageFeeWatch(
                    sku=sku,
                    asin=inv.get("asin") or lt.get("asin", ""),
                    title=inv.get("title") or fee.get("title") or lt.get("title", ""),
                    marketplace=marketplace,
                    average_quantity_on_hand=fee.get("average_quantity_on_hand", 0),
                    storage_fee_amount=storage_fee_amount,
                    month_of_charge=fee.get("month_of_charge", ""),
                    currency=fee.get("currency") or lt.get("currency", ""),
                    ltsf_quantity_charged=lt.get("quantity_charged", 0),
                    ltsf_amount_charged=lt.get("amount_charged", 0.0),
                    ltsf_surcharge_age_tier=lt.get("surcharge_age_tier", ""),
                    recent_units_shipped=recent_units,
                    last_shipment_date=sale.get("last_shipment_date", ""),
                    has_ltsf_surcharge=has_surcharge,
                    should_consider_shifting=should_shift,
                    last_refreshed_at=now,
                )
                db.add(row)
                rows_written += 1
                if has_surcharge:
                    flagged_surcharge += 1
                if should_shift:
                    flagged_shift += 1

            db.commit()
        finally:
            db.close()

        summary = {"checked": rows_written, "flagged_surcharge": flagged_surcharge, "flagged_shift": flagged_shift}
        ActivityLog.record(
            "storage_fee_watch",
            f"{rows_written} SKUs with a storage fee or surcharge, {flagged_surcharge} carrying an "
            f"aged-inventory surcharge, {flagged_shift} flagged to consider shifting",
        )
        return summary

    @staticmethod
    def list_ranked():
        db = SessionLocal()
        try:
            return (
                db.query(StorageFeeWatch)
                .order_by(StorageFeeWatch.storage_fee_amount.desc())
                .all()
            )
        finally:
            db.close()
