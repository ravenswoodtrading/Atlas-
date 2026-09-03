"""
Detects Amazon FBA SKUs that are genuinely sold out -- zero current stock
(fulfillable + everything inbound + reserved, all zero) AND confirmed real
sales history -- as opposed to a SKU that simply hasn't been stocked yet,
which this deliberately never flags (2026-09-01, Tamara: "identify which
products in my inventory are out of stock, as in have been sold and then
all stock sold, rather than not yet reached inventory").

"Confirmed sold" evidence is TWO signals, not one (see
_is_confirmed_sold): primarily Tamara's own SKU-batch naming convention,
which embeds a listing date she reads herself to judge this exact
question (2026-09-02: "If I see the SKU name I should know if an item has
never been stocked or has sold through. The SKU name includes a date
field so anything recent has likely never been stocked but anything old
will have sold through") -- and only for SKUs that don't carry that date
pattern at all, a fallback to the FBA Fulfilled Shipments report, whose
real request window turned out (2026-09-02, live-tested) to cover only
about the last 30 days, not the ~2-year "ever sold" window the original
2026-09-01 design assumed. See SPAPIClient.get_fba_fulfilled_shipments_
units's own docstring for that finding.

Two-stage safety net before anything is ever deleted from Amazon:

1. A SKU first found at zero stock + confirmed sales goes to "monitoring"
   -- visible on the page but not yet actionable. It only graduates to
   "pending_review" once it's stayed at zero stock for MIN_DAYS_OUT_OF_
   STOCK days straight, so a listing that's mid-restock (stock in transit
   that just hasn't landed yet) doesn't get flagged the moment it happens
   to hit zero.
2. Even a "pending_review" row is NEVER auto-deleted. Deletion only
   happens when a human selects it on /inventory-cleanup and clicks
   Delete (see approve_and_delete) -- which re-checks live stock ONE more
   time immediately before calling the real Listings Items API, in case
   something changed between the last sweep and the click.

detect_out_of_stock_candidates() is the nightly sweep (see app/main.py's
_inventory_cleanup_scheduler) and is also safe to trigger manually from
the page's "Check stock now" button -- same method either way, same
convention as every other Atlas sweep (ReplenService.check_now/
check_stale, recheck_pending_eu_a2a).
"""
import re
from datetime import datetime, timedelta, timezone

from app.database.database import SessionLocal
from app.database.models import OutOfStockListing
from app.services.activity_log import ActivityLog
from app.sp_api.client import get_sp_api_client

# How many consecutive days a SKU must show zero stock before it's treated
# as actionable rather than just "worth watching" -- long enough that an
# inbound shipment simply in transit (not yet reflected as inbound_
# working/shipped/receiving, or a brief FC processing gap) has time to
# resolve itself before a human is asked to consider deleting the listing
# over it. Configurable here rather than hard-coded inline, same
# convention as the OA_MAX_* settings in atlas-oa-scale-up-spec.md.
#
# Reused (2026-09-02, Tamara's own choice) as the SKU-listing-date age
# threshold too -- see _is_confirmed_sold below -- rather than introducing
# a second, separately-tuned number for a conceptually different question
# ("has this listing existed long enough to trust its age as sold-through
# evidence" vs "has this stockout lasted long enough to act on").
MIN_DAYS_OUT_OF_STOCK = 21

# How far back the FBA Fulfilled Shipments report looks for "has this SKU
# sold RECENTLY" -- the FALLBACK sold-evidence signal, only used for SKUs
# _parse_sku_listing_date can't read a date out of (see _is_confirmed_sold).
# Originally 730 (~2 years), on the assumption a SKU absent from that wide
# a window had essentially certainly never sold -- but a live test
# (2026-09-02, see SPAPIClient.get_fba_fulfilled_shipments_units's own
# docstring) found the report's real request window tops out around 30
# days; 730 (and even 365) always ended the report FATAL, so that
# assumption never actually held in practice. 30 is the confirmed-working
# ceiling for a single call, not a deliberate design choice.
SALES_LOOKBACK_DAYS = 30

# How long Dismiss suppresses a SKU before a later sweep re-evaluates it
# from scratch, same as a brand new candidate (2026-09-02, Tamara: "the
# use case for dismiss is that I saw... an item that is quite old but
# hasn't yet been delivered so I don't want to delete it but may want
# Atlas to flag it again... maybe dismiss stops flagging it for a couple
# of weeks?") -- e.g. an inbound shipment Amazon's Inventory API hasn't
# reflected yet, so it looks like a genuine stockout even though it
# isn't really one. Dismiss is intentionally NEVER permanent in this
# domain -- see InventoryCleanupService.dismiss's own docstring for why.
DISMISS_SNOOZE_DAYS = 14

# Tamara's own SKU-batch naming convention embeds a day+3-letter-month
# listing-date suffix with NO year (e.g. "ARG_5.00_15.00_6_25FEB",
# "AMA_9.69_19.70_6_06AUG") -- see _parse_sku_listing_date. Not every SKU
# in the account uses this (manually-named/branded-resale listings like
# "Schwarzkopf" or "&HONEYOIL3.0100ML" don't), which is exactly why
# _is_confirmed_sold treats a non-match as "fall back to the sales
# report" rather than "not sold".
_SKU_DATE_SUFFIX = re.compile(r"_(\d{2})([A-Za-z]{3})$")
_MONTH_ABBREVIATIONS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

DEFAULT_MARKETPLACE = "UK"


def _is_zero_stock(inv: dict) -> bool:
    return (
        inv["fulfillable"] + inv["inbound_working"] + inv["inbound_shipped"]
        + inv["inbound_receiving"] + inv["reserved"]
    ) <= 0


def _parse_sku_listing_date(sku: str, now: datetime) -> datetime | None:
    """
    Extracts the DDMON listing-date suffix from Tamara's own SKU-batch
    naming convention (2026-09-02: "If I see the SKU name I should know
    if an item has never been stocked or has sold through. The SKU name
    includes a date field so anything recent has likely never been
    stocked but anything old will have sold through"). Returns None for
    any SKU that doesn't match at all -- _is_confirmed_sold uses that as
    the signal to fall back to the fulfilled-shipments report instead,
    NOT as "never sold".

    No year in the suffix (Tamara's own convention, not Atlas's choice),
    so this resolves to the most recent PAST occurrence of that day/month
    relative to `now` (Tamara's own choice, 2026-09-02) -- e.g. on
    2026-09-02, "06AUG" resolves to 2026-08-06 and "25FEB" resolves to
    2026-02-25, never into the future. Breaks down for a SKU genuinely
    older than ~12 months (would resolve to a date within the last year
    rather than further back) -- accepted as fine, since a SKU that old
    is already far past MIN_DAYS_OUT_OF_STOCK regardless of which exact
    year it actually landed in.
    """
    match = _SKU_DATE_SUFFIX.search(sku)
    if not match:
        return None

    day_str, month_str = match.groups()
    month = _MONTH_ABBREVIATIONS.get(month_str.upper())
    if not month:
        return None

    try:
        day = int(day_str)
        candidate = now.replace(month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
    except ValueError:
        # Not a real day/month combination (e.g. day=31 for a 30-day
        # month) -- not actually a listing-date suffix, just something
        # that happened to match the pattern.
        return None

    if candidate > now:
        try:
            candidate = candidate.replace(year=candidate.year - 1)
        except ValueError:
            return None

    return candidate


def _is_confirmed_sold(sku: str, sale: dict | None, now: datetime) -> tuple[bool, str, str]:
    """
    Returns (confirmed, evidence, listing_date_str) -- whether there's
    enough evidence this SKU has ever actually sold to be a safe deletion
    candidate once at zero stock, which of the two signals decided that
    (for display on /inventory-cleanup, so "Ready to review" never shows
    "Units sold: 0" with no explanation), and the parsed listing date (or
    "" if none).

    Primary signal: the SKU's own embedded listing date (see
    _parse_sku_listing_date) -- old enough (>= MIN_DAYS_OUT_OF_STOCK) that
    a genuinely-never-stocked listing wouldn't be old yet either.
    Fallback, ONLY for SKUs that don't carry that date pattern at all: the
    30-day fulfilled-shipments report showing a confirmed unit shipped
    (see SALES_LOOKBACK_DAYS above for why this can no longer be the
    primary signal the way the original design assumed).
    """
    listing_date = _parse_sku_listing_date(sku, now)
    if listing_date is not None:
        confirmed = (now - listing_date).days >= MIN_DAYS_OUT_OF_STOCK
        return confirmed, "sku_listing_date", listing_date.strftime("%Y-%m-%d")

    return bool(sale and sale.get("units_shipped")), "sales_report", ""


def _aware(dt: datetime) -> datetime:
    """
    SQLite (via SQLAlchemy's plain DateTime column, same type every other
    model in this file uses) hands datetimes back with tzinfo stripped --
    ActivityLog.scheduler_overview already works around exactly this. Any
    comparison against a freshly-created datetime.now(timezone.utc) needs
    this guard or it raises "can't subtract offset-naive and
    offset-aware datetimes" the moment a row survives one real DB
    round-trip.
    """
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class InventoryCleanupService:

    @staticmethod
    def detect_out_of_stock_candidates(marketplace: str = DEFAULT_MARKETPLACE) -> dict:
        """
        One full sweep: pulls current stock for every FBA SKU and the
        confirmed-sales report, then reconciles against what's already
        tracked in out_of_stock_listings. Returns a summary dict for
        ActivityLog, same convention as every other Atlas sweep.

        Safe to call with no SP-API configured -- get_sp_api_client()
        returns None (never raises), and this just becomes a documented
        no-op, same convention as
        eu_a2a_freshness_service.recheck_pending_eu_a2a.
        """
        sp_client = get_sp_api_client()
        if not sp_client:
            return {
                "checked": 0, "flagged": 0, "graduated": 0, "restocked_cleared": 0,
                "message": "SP-API not configured -- nothing checked. Set SP_API_CLIENT_ID/"
                           "SECRET/REFRESH_TOKEN (and SP_API_SELLER_ID, for deleting later) in .env.",
            }

        inventory = sp_client.get_inventory_summaries(marketplace)
        if inventory is None:
            return {
                "checked": 0, "flagged": 0, "graduated": 0, "restocked_cleared": 0,
                "message": "getInventorySummaries call failed -- nothing checked this sweep "
                           "(check server logs for the real HTTP error).",
            }

        # Soft-fail, not a hard bail (2026-09-02) -- unlike inventory
        # above, this is now only the FALLBACK sold-evidence signal (see
        # _is_confirmed_sold): most real SKUs carry Tamara's own
        # embedded listing date and never need this report at all. A
        # failed report just means non-matching SKUs can't be judged
        # this sweep (they're excluded below, same safe default as
        # always) -- it shouldn't also block every SKU the date-suffix
        # signal COULD have judged just fine on its own.
        sales = sp_client.get_fba_fulfilled_shipments_units(marketplace, SALES_LOOKBACK_DAYS)
        if sales is None:
            print(
                "Fulfilled shipments report failed this sweep -- proceeding with the "
                "SKU-listing-date signal only (non-matching SKUs won't be judged this sweep)."
            )
            sales = {}

        db = SessionLocal()
        now = datetime.now(timezone.utc)
        flagged = 0
        graduated = 0
        restocked_cleared = 0
        revived = 0

        try:
            existing = {row.sku: row for row in db.query(OutOfStockListing).all()}
            zero_stock_skus = set()

            for sku, inv in inventory.items():
                if not _is_zero_stock(inv):
                    continue

                sale = sales.get(sku)
                confirmed, evidence, listing_date_str = _is_confirmed_sold(sku, sale, now)
                if not confirmed:
                    # Zero stock but no confirmed evidence of ever
                    # selling -- exactly the "not yet reached inventory"
                    # case to exclude. Never tracked here at all.
                    continue

                row = existing.get(sku)

                if row is not None and row.status == "dismissed":
                    if row.snoozed_until and _aware(row.snoozed_until) > now:
                        # Still snoozed -- leave it alone entirely (don't
                        # add to zero_stock_skus either, so the restock-
                        # clearing loop below doesn't touch it, same as
                        # any other dismissed/history row).
                        continue
                    # Snooze window has passed and it's STILL zero stock
                    # with confirmed sold evidence -- revive it. Note
                    # first_out_of_stock_at is deliberately left
                    # untouched (see DISMISS_SNOOZE_DAYS' own comment):
                    # this is the SAME stockout continuing, not a new
                    # one, so days_out_of_stock reflects its true full
                    # age immediately, possibly straight past
                    # MIN_DAYS_OUT_OF_STOCK on this very sweep.
                    row.status = "monitoring"
                    row.snoozed_until = None
                    revived += 1

                zero_stock_skus.add(sku)

                if row is None:
                    row = OutOfStockListing(sku=sku, first_out_of_stock_at=now)
                    db.add(row)
                    existing[sku] = row
                    flagged += 1

                row.asin = inv["asin"]
                row.title = inv["title"]
                row.marketplace = marketplace
                row.fulfillable_quantity = inv["fulfillable"]
                row.inbound_working_quantity = inv["inbound_working"]
                row.inbound_shipped_quantity = inv["inbound_shipped"]
                row.inbound_receiving_quantity = inv["inbound_receiving"]
                row.reserved_quantity = inv["reserved"]
                # Populated whenever available regardless of which
                # signal actually decided `confirmed` -- extra context
                # for the review page is never wrong to show, even when
                # sold_evidence is "sku_listing_date" and this is 0/"".
                row.units_shipped_lookback = sale["units_shipped"] if sale else 0
                row.lookback_days = SALES_LOOKBACK_DAYS
                row.last_shipment_date = sale["last_shipment_date"] if sale else ""
                row.sold_evidence = evidence
                row.sku_listing_date = listing_date_str
                row.last_checked_at = now

                if row.status == "monitoring":
                    days_out = (now - _aware(row.first_out_of_stock_at)).days
                    if days_out >= MIN_DAYS_OUT_OF_STOCK:
                        row.status = "pending_review"
                        graduated += 1

            # Anything previously tracked (monitoring or pending_review)
            # that no longer shows as zero-stock has been restocked --
            # clear it entirely rather than leave a stale flag. Rows a
            # human already resolved (deleted/dismissed) are untouched
            # either way, they're just history now.
            for sku, row in list(existing.items()):
                if row.status in ("monitoring", "pending_review") and sku not in zero_stock_skus:
                    db.delete(row)
                    restocked_cleared += 1

            db.commit()
        finally:
            db.close()

        summary = {
            "checked": len(inventory), "flagged": flagged, "graduated": graduated,
            "restocked_cleared": restocked_cleared, "revived": revived,
        }
        ActivityLog.record(
            "inventory_cleanup",
            f"{len(inventory)} SKUs checked, {flagged} newly flagged, "
            f"{graduated} graduated to review, {restocked_cleared} restocked/cleared, "
            f"{revived} revived after snooze expired",
        )
        return summary

    @staticmethod
    def list_pending_review():
        db = SessionLocal()
        try:
            return (
                db.query(OutOfStockListing)
                .filter(OutOfStockListing.status == "pending_review")
                .order_by(OutOfStockListing.first_out_of_stock_at.asc())
                .all()
            )
        finally:
            db.close()

    @staticmethod
    def list_monitoring():
        db = SessionLocal()
        try:
            return (
                db.query(OutOfStockListing)
                .filter(OutOfStockListing.status == "monitoring")
                .order_by(OutOfStockListing.first_out_of_stock_at.asc())
                .all()
            )
        finally:
            db.close()

    @staticmethod
    def list_history(limit: int = 100):
        db = SessionLocal()
        try:
            return (
                db.query(OutOfStockListing)
                .filter(OutOfStockListing.status.in_(("deleted", "dismissed")))
                .order_by(OutOfStockListing.reviewed_at.desc())
                .limit(limit)
                .all()
            )
        finally:
            db.close()

    @staticmethod
    def dismiss(candidate_ids: list[int]) -> int:
        """
        Snoozes the listing for DISMISS_SNOOZE_DAYS -- keeps it on
        Amazon, stops flagging it for now, but a later sweep
        re-evaluates it completely fresh (same zero-stock + confirmed-
        sold check as a brand new candidate, see detect_out_of_stock_
        candidates' revival handling) rather than staying silent
        forever. (2026-09-02, Tamara: "the use case for dismiss is that
        I saw... an item that is quite old but hasn't yet been
        delivered so I don't want to delete it but may want Atlas to
        flag it again... maybe dismiss stops flagging it for a couple
        of weeks?") -- e.g. an inbound shipment Amazon's Inventory API
        hasn't reflected yet, so it looks like a genuine stockout even
        though it isn't really one.

        Works from either "monitoring" or "pending_review" -- dismissing
        never touches Amazon either way, so there's no extra risk in
        allowing it from either stage.
        """
        db = SessionLocal()
        count = 0
        try:
            for candidate_id in candidate_ids:
                row = db.get(OutOfStockListing, candidate_id)
                if row and row.status in ("monitoring", "pending_review"):
                    row.status = "dismissed"
                    row.reviewed_at = datetime.now(timezone.utc)
                    row.snoozed_until = datetime.now(timezone.utc) + timedelta(days=DISMISS_SNOOZE_DAYS)
                    count += 1
            db.commit()
        finally:
            db.close()
        return count

    @staticmethod
    def approve_and_delete(candidate_ids: list[int]) -> dict:
        """
        Deletes each approved row's listing via the real Listings Items
        API -- re-checking live stock immediately before each call, so a
        restock that landed between the review page loading and this
        click is never deleted out from under Tamara (see
        OutOfStockListing's own docstring for the full safety chain).

        Deliberately one getInventorySummaries call per SKU here (via
        seller_skus=[row.sku]) rather than one bulk call reused across
        every id in the batch -- slower, but keeps the "check, then
        immediately act on THAT check" guarantee airtight per SKU instead
        of trusting a single snapshot taken before the loop for whichever
        SKUs happen to be processed later.

        Works from either "monitoring" or "pending_review" (2026-09-02,
        Tamara's own choice -- she wants to act immediately rather than
        wait out the 21-day-since-Atlas-started-watching window, which
        undersells the real evidence for a SKU whose listing date alone
        already proves it's genuinely old, not a fresh stockout). The
        live re-check right below is what actually keeps this safe
        either way -- it re-confirms zero stock immediately before
        deleting regardless of which status the row started at, so
        skipping the monitoring wait does NOT skip the restock guard.
        """
        sp_client = get_sp_api_client()
        if not sp_client:
            return {"deleted": 0, "failed": 0, "restocked_skipped": 0, "message": "SP-API not configured."}

        db = SessionLocal()
        deleted = 0
        failed = 0
        restocked_skipped = 0

        try:
            for candidate_id in candidate_ids:
                row = db.get(OutOfStockListing, candidate_id)
                if not row or row.status not in ("monitoring", "pending_review"):
                    continue

                fresh = sp_client.get_inventory_summaries(row.marketplace, seller_skus=[row.sku])
                if fresh is None:
                    row.delete_error = "Could not re-confirm live stock before deleting -- left pending, try again."
                    failed += 1
                    continue

                fresh_inv = fresh.get(row.sku)
                if fresh_inv and not _is_zero_stock(fresh_inv):
                    # Restocked since the last sweep -- do NOT delete.
                    db.delete(row)
                    restocked_skipped += 1
                    continue

                result = sp_client.delete_listing_item(row.sku, row.marketplace)

                if result["success"]:
                    row.status = "deleted"
                    row.reviewed_at = datetime.now(timezone.utc)
                    row.delete_error = ""
                    deleted += 1
                else:
                    row.delete_error = result["error"]
                    failed += 1

            db.commit()
        finally:
            db.close()

        ActivityLog.record(
            "inventory_cleanup_delete",
            f"{deleted} deleted, {failed} failed, {restocked_skipped} skipped (restocked)",
        )
        return {"deleted": deleted, "failed": failed, "restocked_skipped": restocked_skipped}
