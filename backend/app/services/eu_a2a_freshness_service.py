"""
Daily sweep that re-checks unreviewed EU A2A Review Queue leads against
their SOURCE marketplace's live SP-API price, and removes any whose
deal has genuinely died since Atlas first found it.

Tamara's own instruction (2026-08-29): "if it is taking me a while to
review then I am finding either the price has changed or Amazon are
now out of stock... let's remove dead leads, and yes I only care if
the SOURCE store is out of stock or has increased in price" -- i.e.
this deliberately does NOT touch/remove a lead for a UK (sell-side)
stock change, which Atlas already treats as often a GOOD sign via the
existing "Amazon OOS" -> Watchlist flow (see lead-queue-oos-and-
enrichment notes), not a reason to drop a lead.

WHY SP-API (free, no Keepa tokens): app/sp_api/client.py's
MARKETPLACE_IDS already covers DE/FR/ES/IT, and its module comment
confirms this account's SP-API Pricing access was validated LIVE
(2026-08-21) to return genuine third-party competitive offer data on
all 5 marketplaces, not just UK -- see BrandScanService.
_sp_api_find_source_market for the earlier, similar use of this same
call. That same validation found SP-API can under-report (show no
offer when one genuinely exists) but never over-report -- so this
sweep only ever removes a lead on a CONFIRMED signal (a successful
call that explicitly reports no live offer, or a successful call with
a real price that no longer clears profit), never on a failed/
inconclusive call. See _check_one_record's own comments for exactly
where that line is drawn.

SCOPE (deliberate, 2026-08-29): only ProductRecord rows. Confirmed via
review_queue_service.py that BOTH the "scan" and "competitor" tabs of
/review-queue read best_source_marketplace/best_source_cost_gbp off
the SAME ProductRecord table (the "competitor" branch's merged lead
dict is built from a ProductRecord too, just paired with a
SellerNewListing for detection metadata) -- so one sweep over
ProductRecord covers both tabs. Lead rows (VA Sheet/manual/shortlist)
are deliberately NOT touched here: many carry va_cost_price as
CONFIRMED ground truth (see Lead's own model docstring -- "Keepa is
used for everything else but must never recompute/override these"),
and Lead.source_marketplace already drives a related existing check
(VerdictService.check_source_marketplace) -- folding Lead rows into
this sweep needs its own look at that existing mechanism first, not a
blind extension of this one.

REMOVAL MECHANISM: sets ProductRecord.review to "stale_auto" (with a
human-readable review_reason) rather than deleting the row -- reuses
the exact same pattern already proven for the "oos" review value (see
lead-queue-oos-and-enrichment notes): ProductRepository.list_latest's
every review_filter already excludes ANY truthy review value from the
pending list, so this clears the lead from every tab with zero new
filtering logic, while keeping the row (and why it was removed)
inspectable in the database rather than gone for good.
"""
from datetime import datetime, timezone

from app.database.database import SessionLocal
from app.database.models import ProductRecord
from app.models.product import Product
from app.services.fee_engine import FeeEngine
from app.services.currency_service import CurrencyService
from app.services.product_mapper import MARKETPLACE_CURRENCY
from app.sp_api.client import get_sp_api_client

EU_MARKETPLACES = ("DE", "FR", "ES", "IT")

# Comfortably covers a realistic pending-EU-A2A backlog in one sweep
# (same pragmatic-cap convention as review_queue_service.MAX_LEADS)
# without an unbounded query/SP-API-call count if the queue ever grows
# very large.
MAX_RECORDS_PER_SWEEP = 200


def _pending_eu_a2a_records(db, limit: int) -> list:
    """
    Latest ProductRecord per ASIN, unreviewed, with a real EU source --
    same "latest scan per ASIN" de-dupe ProductRepository.list_latest
    already uses, done directly here since that method doesn't expose
    a best_source_marketplace filter of its own.
    """
    all_records = (
        db.query(ProductRecord)
        .filter(ProductRecord.review.is_(None))
        .filter(ProductRecord.best_source_marketplace.in_(EU_MARKETPLACES))
        .order_by(ProductRecord.scanned_at.desc())
        .all()
    )

    seen = set()
    latest = []
    for record in all_records:
        if record.asin in seen:
            continue
        seen.add(record.asin)
        latest.append(record)
        if len(latest) >= limit:
            break

    return latest


def _check_one_record(sp_client, record: ProductRecord) -> tuple[str, str | None, str | None]:
    """
    Returns (status, review_reason, review_reason_category) where
    status is one of:
      "removed"      -- a CONFIRMED dead source; review_reason explains why.
      "still_viable" -- SP-API gave a real, reachable answer and the
                         deal still clears profit; nothing to do.
      "inconclusive"  -- the check itself didn't produce a usable
                         answer (failed call, or a successful-but-not-
                         "Success" response) -- distinct from
                         "still_viable" so the sweep's own summary
                         doesn't quietly claim more certainty than it
                         has. Never removes a lead on this alone; it's
                         simply checked again on the next sweep.

    review_reason_category (atlas-review-queue-backend-v1.md section 5)
    is only ever NO_BUYABLE_OFFER (confirmed no live offer) or
    NO_LONGER_PROFITABLE (a real offer exists but at a price that no
    longer clears profit) -- None for "still_viable"/"inconclusive".
    Deliberately NOT "PRICE_CHANGED": a price rise alone isn't
    necessarily disqualifying, this sweep only fires once it's already
    established the rise breaks profitability, so NO_LONGER_PROFITABLE
    is the more accurate structured reason for what actually happened.

    ALSO mutates record.last_offer_checked_at/last_offer_price_gbp/
    last_offer_buyable in place on any DEFINITIVE (non-inconclusive)
    answer -- see ProductRecord.last_offer_checked_at's own comment for
    why this used to be computed and thrown away instead of persisted.
    Left untouched on "inconclusive" so a failed/ambiguous call can
    never make a record look more recently/confidently checked than it
    actually was. Caller owns the db session/commit, same convention as
    apply_lead_decision.
    """
    offer = sp_client.get_item_offers(record.asin, record.best_source_marketplace)

    if offer is None:
        # The call itself failed (network, auth, invalid ASIN for that
        # marketplace) -- unknown, not "confirmed dead".
        return "inconclusive", None, None

    if offer.get("status") != "Success":
        return "inconclusive", None, None

    price = offer.get("price")
    offer_count = offer.get("offer_count")
    checked_at = datetime.now(timezone.utc)

    if not price or not offer_count:
        # Reachable AND explicitly reports no live offer right now --
        # a real, trustworthy "out of stock at the source", per
        # get_item_offers' own docstring ("that IS real information,
        # unlike a failed call").
        record.last_offer_checked_at = checked_at
        record.last_offer_price_gbp = None
        record.last_offer_buyable = False
        return "removed", (
            f"Auto-removed {checked_at.date()}: "
            f"{record.best_source_marketplace} source confirmed out of stock via SP-API "
            f"(was £{record.best_source_cost_gbp:.2f})."
        ), "NO_BUYABLE_OFFER"

    currency = MARKETPLACE_CURRENCY.get(record.best_source_marketplace, "EUR")
    new_cost_gbp = CurrencyService.to_gbp(price, currency)

    # Recompute profit with the FRESH source cost, same fee math
    # (VAT/referral/prep-fee handling) every other profit figure in
    # Atlas already goes through -- everything else about the lead
    # (UK price, category) stays exactly as recorded, per Tamara's own
    # scope ("I only care if the source store...").
    product = Product(
        asin=record.asin, title=record.title, brand=record.brand, category=record.category,
        buy_box_now=record.buy_box_now, buy_box_90d=record.buy_box_90d,
        best_source_marketplace=record.best_source_marketplace, best_source_cost_gbp=new_cost_gbp,
        fba_fee=record.fba_fee,
    )
    result = FeeEngine.calculate(product, record.category_name)

    # A real, live offer exists either way from here -- buyable is True
    # even when it's about to be marked "removed" below for no longer
    # being PROFITABLE, since that's a different fact (see
    # review_reason_category's own comment: NO_BUYABLE_OFFER vs
    # NO_LONGER_PROFITABLE must stay distinguishable downstream).
    record.last_offer_checked_at = checked_at
    record.last_offer_price_gbp = new_cost_gbp
    record.last_offer_buyable = True

    if result.profit <= 0:
        return "removed", (
            f"Auto-removed {checked_at.date()}: "
            f"{record.best_source_marketplace} source price rose to £{new_cost_gbp:.2f} "
            f"(was £{record.best_source_cost_gbp:.2f}) -- no longer profitable "
            f"(recomputed profit £{result.profit:.2f})."
        ), "NO_LONGER_PROFITABLE"

    return "still_viable", None, None


def recheck_pending_eu_a2a(limit: int = MAX_RECORDS_PER_SWEEP) -> dict:
    """
    Entry point for both the daily scheduler (app/main.py) and a
    manual "Recheck now" trigger, if one gets added to the Review
    Queue page later. Returns a summary dict for ActivityLog.

    Safe to call with no SP-API configured -- get_sp_api_client()
    returns None (never raises), same convention as everywhere else
    SP-API is used in Atlas, and this just becomes a documented no-op.
    """
    sp_client = get_sp_api_client()
    if not sp_client:
        return {
            "checked": 0, "removed": 0, "still_viable": 0, "inconclusive": 0,
            "message": "SP-API not configured -- nothing checked.",
        }

    db = SessionLocal()
    try:
        records = _pending_eu_a2a_records(db, limit)

        checked = 0
        removed = 0
        still_viable = 0
        inconclusive = 0

        for record in records:
            checked += 1
            status, reason, reason_category = _check_one_record(sp_client, record)

            if status == "removed":
                record.review = "stale_auto"
                record.review_reason = reason
                record.review_reason_category = reason_category
                db.add(record)
                removed += 1
            elif status == "still_viable":
                # last_offer_checked_at/price_gbp/buyable were already
                # set on `record` inside _check_one_record -- `record`
                # is already attached to this session (loaded via
                # db.query() above), so the mutation is picked up by
                # the commit below with no further action needed here.
                still_viable += 1
            else:
                inconclusive += 1

        db.commit()
        return {"checked": checked, "removed": removed, "still_viable": still_viable, "inconclusive": inconclusive}
    finally:
        db.close()
